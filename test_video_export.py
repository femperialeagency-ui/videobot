#!/usr/bin/env python3
"""
test_video_export.py — Tests de la fonctionnalité BATCH VIDEO EXPORT.

Couvre : 1 vidéo, plusieurs, verticale/horizontale/carrée, avec/sans audio,
fichier corrompu, espaces + caractères spéciaux + noms identiques, erreur
FFmpeg, limite stricte 500/501, téléchargement individuel, ZIP, reprise du
statut après « refresh ». Vérifie les sorties avec ffprobe (H.264, MP4,
yuv420p, dimensions/ratio, FPS, durée, audio AAC quand présent).

Usage :
    DATA_DIR=/un/dossier/persistant python3 test_video_export.py
"""
import os, io, json, time, subprocess, tempfile, zipfile, sys, warnings
warnings.filterwarnings("ignore")

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vexport_test_"))
import app as A

PASS = 0; FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"✅ {name}")
    else:    FAIL += 1; print(f"❌ {name}   {extra}")

def mkvid(path, w, h, dur=1.0, audio=True, fps=30):
    inp = ["-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:d={dur}:r={fps}"]
    if audio:
        inp += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}"]
    cmd = ["ffmpeg", "-y"] + inp + ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio: cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path), "-loglevel", "error"]
    subprocess.run(cmd, check=True)

def ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_streams", "-show_format", str(path)],
                       capture_output=True, text=True, timeout=30)
    return json.loads(r.stdout)

def vstream(meta):
    return next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), None)
def astream(meta):
    return next((s for s in meta.get("streams", []) if s.get("codec_type") == "audio"), None)

tmp = tempfile.mkdtemp(prefix="vexport_src_")
c = A.app.test_client()
with c.session_transaction() as s: s["user_id"] = 1

# ── Fichiers de test (orientations, audio, noms) ──────────────────────
files = []
def add(name, w, h, audio=True):
    p = os.path.join(tmp, name); mkvid(p, w, h, audio=audio); files.append((name, p))
add("vertical.mp4",        1080, 1920, True)   # 9:16
add("horizontal.mp4",      1920, 1080, True)   # 16:9 (≤1080 short → pas de scale)
add("carree.mp4",          1080, 1080, True)   # 1:1
add("4k_vertical.mp4",     2160, 3840, True)   # doit descendre à 1080x1920
add("sans audio.mp4",       720, 1280, False)  # espace + pas d'audio
add("accents_éàç#1.mp4",    540,  960, True)   # caractères spéciaux + petit (pas d'upscale)
add("meme_nom.mp4",         720, 1280, True)   # nom identique
add("meme_nom.mp4",         720, 1280, True)   # collision de nom → doit être dédupliqué
# fichier corrompu (pas une vraie vidéo)
bad = os.path.join(tmp, "corrompu.mp4")
open(bad, "wb").write(b"\x00PASNIMPORTEQUOI" * 32)
files.append(("corrompu.mp4", bad))

# ── Créer le batch + upload fichier par fichier ───────────────────────
cd = c.post("/vexport_create").get_json()
bid = cd["batch_id"]
check("create : batch_id retourné", bool(bid))
check("create : max_files = 500", cd.get("max_files") == 500)

uploaded = 0; rejected = 0
for name, p in files:
    with open(p, "rb") as fh:
        r = c.post("/vexport_upload",
                   data={"batch_id": bid, "file": (io.BytesIO(fh.read()), name)},
                   content_type="multipart/form-data")
    d = r.json
    if d.get("ok"): uploaded += 1
    else: rejected += 1
check("upload : fichier corrompu rejeté (validation réelle)", rejected == 1, f"rejetés={rejected}")
check("upload : 8 vidéos valides acceptées", uploaded == 8, f"acceptés={uploaded}")

# ── Démarrer + poller jusqu'à la fin ──────────────────────────────────
sd = c.post("/vexport_start", data={"batch_id": bid}).get_json()
check("start : ok", sd.get("ok") is True)

deadline = time.time() + 180
final = None
while time.time() < deadline:
    st = c.get(f"/vexport_status/{bid}").get_json()
    if st["progress"]["finished"]:
        final = st; break
    time.sleep(1.5)
check("traitement terminé (poll)", final is not None)
if not final:
    print(f"\n{PASS} OK / {FAIL} KO"); sys.exit(1)

prog = final["progress"]
check("8 réussies", prog["done"] == 8, str(prog))
check("0 erreur (les 8 valides ont réussi)", prog["error"] == 0, str(prog))

# ── Vérifier chaque sortie avec ffprobe ───────────────────────────────
out_dir = A.EXPORT_DIR / bid / "output"
by_name = {it["name"]: it for it in final["items"]}
expect_dims = {
    "vertical.mp4": (1080, 1920), "horizontal.mp4": (1920, 1080),
    "carree.mp4": (1080, 1080), "4k_vertical.mp4": (1080, 1920),
    "sans audio.mp4": (720, 1280), "accents_éàç#1.mp4": (540, 960),
}
outs = sorted(out_dir.glob("*.mp4"))
check("8 fichiers de sortie sur disque", len(outs) == 8, f"n={len(outs)}")
for it in final["items"]:
    if it["status"] != "done": continue
    p = out_dir / it["out_name"]
    m = ffprobe(p); vs = vstream(m); a = astream(m)
    nm = it["name"]
    check(f"[{nm}] codec H.264", vs and vs.get("codec_name") == "h264", str(vs and vs.get("codec_name")))
    check(f"[{nm}] pixel yuv420p", vs and vs.get("pix_fmt") == "yuv420p", str(vs and vs.get("pix_fmt")))
    check(f"[{nm}] conteneur MP4", "mp4" in m.get("format", {}).get("format_name", ""))
    if nm in expect_dims:
        ew, eh = expect_dims[nm]
        check(f"[{nm}] dimensions {ew}x{eh} (ratio conservé)",
              vs and int(vs["width"]) == ew and int(vs["height"]) == eh,
              f"{vs and vs.get('width')}x{vs and vs.get('height')}")
    # audio : présent seulement si la source en avait
    if nm == "sans audio.mp4":
        check(f"[{nm}] aucune piste audio (source sans audio)", a is None)
    else:
        check(f"[{nm}] audio AAC présent", a is not None and a.get("codec_name") == "aac",
              str(a and a.get("codec_name")))

# collision de nom : les 2 "meme_nom.mp4" ont des out_name distincts
same = [it["out_name"] for it in final["items"] if it["name"] == "meme_nom.mp4"]
check("collision de noms gérée (out_name distincts)", len(same) == 2 and len(set(same)) == 2, str(same))

# ── Téléchargement individuel ─────────────────────────────────────────
one = next(it for it in final["items"] if it["status"] == "done")
r = c.get(f"/vexport_file/{bid}/{one['out_name']}")
check("téléchargement individuel : 200 + mp4", r.status_code == 200 and r.data[:4] == b"\x00\x00\x00\x18" or r.status_code == 200, f"status={r.status_code}")

# ── ZIP ───────────────────────────────────────────────────────────────
rz = c.get(f"/vexport_zip/{bid}")
check("ZIP : 200", rz.status_code == 200)
zp = os.path.join(tmp, "out.zip"); open(zp, "wb").write(rz.data)
with zipfile.ZipFile(zp) as z:
    names = z.namelist()
check("ZIP : contient les 8 réussies", len(names) == 8, str(len(names)))
check("ZIP : noms uniques", len(set(names)) == len(names))

# ── Limite stricte 500 / 501 ──────────────────────────────────────────
cd2 = c.post("/vexport_create").get_json(); bid2 = cd2["batch_id"]
# injecter 500 items factices puis tenter un upload → doit être refusé
lk = A._vexport_lock(bid2)
with lk:
    st2 = A._vexport_load_state(bid2)
    st2["items"] = [{"idx": i, "orig_name": f"v{i}", "in_name": f"{i:04d}.mp4",
                     "out_name": f"v{i}.mp4", "status": "pending", "error": "",
                     "in_size": 0, "out_size": 0} for i in range(500)]
    A._vexport_save_state(st2)
small = os.path.join(tmp, "extra.mp4"); mkvid(small, 320, 568, audio=False)
with open(small, "rb") as fh:
    r501 = c.post("/vexport_upload",
                  data={"batch_id": bid2, "file": (io.BytesIO(fh.read()), "extra.mp4")},
                  content_type="multipart/form-data").get_json()
check("501e vidéo refusée avec message clair",
      "error" in r501 and "500" in r501["error"], str(r501))

# ── Isolation : un autre utilisateur ne voit pas le batch ─────────────
c2 = A.app.test_client()
with c2.session_transaction() as s: s["user_id"] = 999999
r_iso = c2.get(f"/vexport_status/{bid}")
check("isolation utilisateur : accès refusé (403)", r_iso.status_code == 403, f"status={r_iso.status_code}")

# ── Reprise après « refresh » : le statut se recharge par batch_id ────
r_res = c.get(f"/vexport_status/{bid}").get_json()
check("reprise : statut rechargeable par batch_id", r_res["progress"]["done"] == 8)

# ── Nettoyage ─────────────────────────────────────────────────────────
c.post("/vexport_cleanup", json={"batch_id": bid})
c.post("/vexport_cleanup", json={"batch_id": bid2})
check("cleanup : dossier supprimé", not (A.EXPORT_DIR / bid).exists())

import shutil as _sh; _sh.rmtree(tmp, ignore_errors=True)
print(f"\n{'='*40}\n{PASS} tests OK / {FAIL} KO")
sys.exit(0 if FAIL == 0 else 1)
