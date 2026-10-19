#!/usr/bin/env python3
"""
test_video_export.py — Tests du BATCH VIDEO EXPORT (in-process, client Flask,
FFmpeg réel).

Couvre :
  A. upload d'une vidéo            B. upload de plusieurs vidéos
  C. vrai réencodage FFmpeg (H.264/yuv420p/MP4, dimensions, rotation, ratio,
     FPS, durée, audio, paramètres x264 réellement appliqués, pas d'upscale)
  D. progression réelle (par vidéo + globale, jamais « terminée » avant
     validation du fichier)
  E. téléchargement individuel     F. téléchargement ZIP (sur disque, supprimé)
  G. 500 vidéos réelles acceptées et traitées   H. 501e refusée
  I. échecs FFmpeg (source tronquée, source corrompue) sans bloquer le batch
  K. récupération des vidéos interrompues (reprise, erreur après 2
     interruptions, sortie déjà valide, pause anti boucle de crash, relance)
  L. isolation entre utilisateurs  M. nettoyage (sources, temporaires, vieux
     batchs, batchs en file protégés)
  + limite globale : 1 seul FFmpeg à la fois, tous batchs/utilisateurs
    confondus, et tourniquet entre batchs.
  (J. redémarrage réel de Gunicorn : test_video_export_restart.py)

Usage :
    DATA_DIR=/un/dossier python3 test_video_export.py
    VEXPORT_TEST_SKIP_500=1 → saute le test réel de 500 vidéos (~2 min)
"""
import os, io, json, time, subprocess, tempfile, zipfile, sys, shutil, threading, warnings
warnings.filterwarnings("ignore")

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vexport_test_"))
os.environ.setdefault("ADMIN_EMAIL", "admin-test@local.test")       # crée l'utilisateur id 1
os.environ.setdefault("ADMIN_PASSWORD", "admin-test-password")
os.environ.setdefault("VEXPORT_FFMPEG_THREADS", "2")
import app as A

PASS = 0; FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"✅ {name}")
    else:    FAIL += 1; print(f"❌ {name}   {extra}")

def mkvid(path, w, h, dur=1.0, audio=True, fps=30, noise=False, pix="yuv420p", extra=None):
    src = f"testsrc2=s={w}x{h}:d={dur}:r={fps}" + (",noise=alls=25:allf=t+u" if noise else "")
    inp = ["-f", "lavfi", "-i", src]
    if audio:
        inp += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}"]
    cmd = ["ffmpeg", "-y"] + inp + ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", pix]
    if audio: cmd += ["-c:a", "aac", "-shortest"]
    cmd += (extra or []) + [str(path), "-loglevel", "error"]
    subprocess.run(cmd, check=True)

def ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_streams", "-show_format", str(path)],
                       capture_output=True, text=True, timeout=30)
    return json.loads(r.stdout or "{}")

def vstream(meta):
    return next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), None)
def astream(meta):
    return next((s for s in meta.get("streams", []) if s.get("codec_type") == "audio"), None)
def fdur(meta):
    return float(meta.get("format", {}).get("duration") or 0)

def upload(cl, bid, path, name):
    with open(path, "rb") as fh:
        return cl.post("/vexport_upload", data={"batch_id": bid, "file": (io.BytesIO(fh.read()), name)},
                       content_type="multipart/form-data")

def wait_finished(cl, bid, timeout=240, step=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = cl.get(f"/vexport_status/{bid}").get_json()
        if st and st.get("progress", {}).get("finished"):
            return st
        time.sleep(step)
    return None

def wait_settled(cl, bid, timeout=240, step=0.3):
    """Attend que toutes les vidéos ET toutes les archives ZIP soient finales."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = cl.get(f"/vexport_status/{bid}").get_json()
        if st and st.get("progress", {}).get("finished") and st.get("archives_settled"):
            return st
        time.sleep(step)
    return None

def ssim(dist, ref):
    r = subprocess.run(["ffmpeg", "-nostdin", "-i", str(dist), "-i", str(ref), "-lavfi",
                        "[0:v]setpts=PTS-STARTPTS[d];[1:v]setpts=PTS-STARTPTS[r];[d][r]ssim", "-f", "null", "-"],
                       capture_output=True, text=True)
    import re
    m = re.findall(r"All:([0-9.]+)", r.stderr)
    return float(m[-1]) if m else 0.0


class FfmpegSampler:
    """Compte les process FFmpeg d'export vivants (ps toutes les 50 ms)."""
    def __init__(self, marker):
        self.marker = marker; self.max = 0; self._stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
    def _run(self):
        while not self._stop.is_set():
            out = subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True).stdout
            n = sum(1 for l in out.splitlines()
                    if l.startswith("ffmpeg ") and self.marker in l and "-progress" in l)
            self.max = max(self.max, n)
            time.sleep(0.05)
    def start(self): self.t.start(); return self
    def stop(self): self._stop.set(); self.t.join(timeout=2)


tmp = tempfile.mkdtemp(prefix="vexport_src_")


def exported(bid, name):
    """Vidéo exportée : MP4 encore sur disque, sinon lue dans son archive
    (stockage tournant : le MP4 intermédiaire est supprimé une fois archivé)."""
    p = A.EXPORT_DIR / bid / "output" / name
    if p.exists():
        return p
    for zp in sorted((A.EXPORT_DIR / bid / "zips").glob("Batch_*.zip")):
        with zipfile.ZipFile(zp) as z:
            if name in z.namelist():
                dst = os.path.join(tmp, f"x_{bid[:6]}_{name}")
                open(dst, "wb").write(z.read(name))
                return dst
    return p


def exported_names(bid):
    names = {p.name for p in (A.EXPORT_DIR / bid / "output").glob("*.mp4")}
    for zp in (A.EXPORT_DIR / bid / "zips").glob("Batch_*.zip"):
        with zipfile.ZipFile(zp) as z:
            names |= set(z.namelist())
    return names
c = A.app.test_client()
with c.session_transaction() as s: s["user_id"] = 1
A.create_user("user2-test@local.test", "user2-test-password")
uid2 = A.get_user_by_email("user2-test@local.test")["id"]
c2 = A.app.test_client()
with c2.session_transaction() as s: s["user_id"] = uid2
sampler = FfmpegSampler(str(A.EXPORT_DIR)).start()
print(f"config : threads={A.VEXPORT_FFMPEG_THREADS} preset={A.VEXPORT_PRESET} crf={A.VEXPORT_CRF}")

# ══ A. Upload d'UNE vidéo ═════════════════════════════════════════════
pA = os.path.join(tmp, "single.mp4"); mkvid(pA, 1080, 1920, dur=2.0)
bidA = c.post("/vexport_create").get_json()["batch_id"]
rA = upload(c, bidA, pA, "single.mp4").get_json()
check("A. upload 1 vidéo accepté", rA.get("ok") is True and rA.get("count") == 1, str(rA))
check("A. start ok", c.post("/vexport_start", data={"batch_id": bidA}).get_json().get("ok") is True)
fA = wait_finished(c, bidA)
check("A. vidéo unique terminée", fA and fA["progress"]["done"] == 1 and fA["progress"]["percent"] == 100, str(fA and fA["progress"]))

# ══ B + C. Plusieurs vidéos + vrai réencodage ═════════════════════════
files = []
def add(name, w, h, audio=True, **kw):
    p = os.path.join(tmp, "src_" + str(len(files)) + os.path.splitext(name)[1]); mkvid(p, w, h, audio=audio, **kw)
    files.append((name, p))
add("vertical.mp4",        1080, 1920, True)   # 9:16
add("horizontal.mp4",      1920, 1080, True)   # 16:9 (≤1080 short → pas de scale)
add("carree.mp4",          1080, 1080, True)   # 1:1
add("4k_vertical.mp4",     2160, 3840, True)   # doit descendre à 1080x1920
add("sans audio.mp4",       720, 1280, False)  # espace + pas d'audio
add("accents_éàç#1.mp4",    540,  960, True)   # caractères spéciaux + petit (pas d'upscale)
add("meme_nom.mp4",         720, 1280, True)   # nom identique
add("meme_nom.mp4",         720, 1280, True)   # collision de nom → doit être dédupliqué
add("cine_24fps.mov",      1280,  720, True, fps=24, dur=2.0)   # FPS 24 conservé
add("impair.mp4",          1079, 1919, False, pix="yuv444p")    # dimensions impaires
# 4K de téléphone stockée en paysage + rotation 90° (cas iPhone) → portrait 1080x1920
raw4k = os.path.join(tmp, "raw4k.mp4"); mkvid(raw4k, 3840, 2160, audio=True)
rot = os.path.join(tmp, "rot4k.mov")
subprocess.run(["ffmpeg", "-y", "-display_rotation", "90", "-i", raw4k, "-c", "copy", rot, "-loglevel", "error"], check=True)
files.append(("rotation_iphone.mov", rot))
bad = os.path.join(tmp, "corrompu.mp4")
open(bad, "wb").write(b"\x00PASNIMPORTEQUOI" * 32)
files.append(("corrompu.mp4", bad))
txt = os.path.join(tmp, "notes.txt"); open(txt, "w").write("hello")

cd = c.post("/vexport_create").get_json()
bid = cd["batch_id"]
check("B. create : batch_id retourné", bool(bid))
check("B. create : max_files = 300 (nouveaux batchs)", cd.get("max_files") == 300 and A._vexport_load_state(bid).get("max_files") == 300)
uploaded = rejected = 0
for name, p in files:
    d = upload(c, bid, p, name).json
    if d.get("ok"): uploaded += 1
    else: rejected += 1
r_txt = upload(c, bid, txt, "notes.txt").get_json()
check("B. extension non vidéo refusée", "error" in r_txt, str(r_txt))
check("B. fichier corrompu rejeté (validation réelle)", rejected == 1, f"rejetés={rejected}")
check("B. 11 vidéos valides acceptées", uploaded == 11, f"acceptés={uploaded}")
st_disk = A._vexport_load_state(bid)
check("B. sources écrites sur le disque persistant (DATA_DIR)",
      all((A.EXPORT_DIR / bid / "input" / it["in_name"]).exists() for it in st_disk["items"])
      and str(A.EXPORT_DIR).startswith(os.environ["DATA_DIR"]))
check("B. durée source mémorisée à l'upload", all(it.get("src_duration", 0) > 0 for it in st_disk["items"]))

sd = c.post("/vexport_start", data={"batch_id": bid}).get_json()
check("B. start : ok", sd.get("ok") is True)
final = wait_finished(c, bid)
check("B. traitement terminé (poll)", final is not None)
if not final:
    print(f"\n{PASS} OK / {FAIL} KO"); sys.exit(1)
prog = final["progress"]
check("B. 11 réussies, 0 erreur", prog["done"] == 11 and prog["error"] == 0, str(prog))

out_dir = A.EXPORT_DIR / bid / "output"
src_of = {}
for name, p in files:
    src_of.setdefault(name, p)
expect_dims = {
    "vertical.mp4": (1080, 1920), "horizontal.mp4": (1920, 1080),
    "carree.mp4": (1080, 1080), "4k_vertical.mp4": (1080, 1920),
    "sans audio.mp4": (720, 1280), "accents_éàç#1.mp4": (540, 960),
    "cine_24fps.mov": (1280, 720), "impair.mp4": (1078, 1918),
    "rotation_iphone.mov": (1080, 1920),
}
wait_settled(c, bid)
outs = sorted(exported_names(bid))
check("C. 11 fichiers de sortie (archivés ou sur disque)", len(outs) == 11, f"n={len(outs)}")
for it in final["items"]:
    if it["status"] != "done": continue
    p = exported(bid, it["out_name"])
    m = ffprobe(p); vs = vstream(m); a = astream(m)
    sm = ffprobe(src_of[it["name"]]); svs = vstream(sm)
    nm = it["name"]
    check(f"C. [{nm}] H.264 / yuv420p / MP4",
          vs and vs.get("codec_name") == "h264" and vs.get("pix_fmt") == "yuv420p"
          and "mp4" in m.get("format", {}).get("format_name", ""))
    ew, eh = expect_dims.get(nm, (int(svs["width"]), int(svs["height"])))
    check(f"C. [{nm}] dimensions {ew}x{eh} (ratio/orientation conservés, pas d'upscale)",
          vs and int(vs["width"]) == ew and int(vs["height"]) == eh,
          f"{vs and vs.get('width')}x{vs and vs.get('height')}")
    check(f"C. [{nm}] pixels carrés (aucun étirement)",
          (vs.get("sample_aspect_ratio") or "1:1") in ("1:1", "0:1"), str(vs.get("sample_aspect_ratio")))
    check(f"C. [{nm}] FPS conservé", vs.get("r_frame_rate") == svs.get("r_frame_rate"),
          f"{vs.get('r_frame_rate')} vs {svs.get('r_frame_rate')}")
    check(f"C. [{nm}] durée conservée (±0,15 s)", abs(fdur(m) - fdur(sm)) <= 0.15, f"{fdur(m)} vs {fdur(sm)}")
    if astream(sm) is None:
        check(f"C. [{nm}] aucune piste audio (source sans audio)", a is None)
    else:
        check(f"C. [{nm}] audio AAC présent", a is not None and a.get("codec_name") == "aac",
              str(a and a.get("codec_name")))
    blob = open(p, "rb").read()
    check(f"C. [{nm}] vrai réencodage x264 (crf={A.VEXPORT_CRF}, preset appliqué)",
          f"crf={A.VEXPORT_CRF}.0".encode() in blob and b"x264" in blob)
rot_out = exported(bid, next(it["out_name"] for it in final["items"] if it["name"] == "rotation_iphone.mov"))
rvs = vstream(ffprobe(rot_out))
check("C. rotation iPhone appliquée une seule fois (pas de métadonnée résiduelle)",
      not any("rotation" in json.dumps(sd) for sd in (rvs.get("side_data_list") or [])), str(rvs.get("side_data_list")))
vert_out = exported(bid, next(it["out_name"] for it in final["items"] if it["name"] == "vertical.mp4"))
s_v = ssim(vert_out, src_of["vertical.mp4"])
check("C. aucune modification visuelle volontaire (SSIM ≥ 0,97 vs source)", s_v >= 0.97, f"SSIM={s_v}")
same = [it["out_name"] for it in final["items"] if it["name"] == "meme_nom.mp4"]
check("B. collision de noms gérée (out_name distincts)", len(same) == 2 and len(set(same)) == 2, str(same))

# ══ D. Progression réelle ═════════════════════════════════════════════
pD = os.path.join(tmp, "long.mp4"); mkvid(pD, 1080, 1920, dur=10.0, noise=True)
bidD = c.post("/vexport_create").get_json()["batch_id"]
upload(c, bidD, pD, "long.mp4"); upload(c, bidD, pA, "court.mp4")
c.post("/vexport_start", data={"batch_id": bidD})
seen_pct, glob, statuses, early_done = [], [], [], False
out_dirD = A.EXPORT_DIR / bidD / "output"
deadline = time.time() + 180
while time.time() < deadline:
    d = c.get(f"/vexport_status/{bidD}").get_json()
    it0 = d["items"][0]
    statuses.append(it0["status"])
    if it0["status"] == "processing":
        seen_pct.append(it0["pct"])
        fpath = out_dirD / it0["out_name"]
        if fpath.exists() and not A._vexport_validate_output(str(fpath), 10.0)[0]:
            early_done = True          # sous son nom final, le fichier doit TOUJOURS être complet et validé
    glob.append(d["progress"]["percent"])
    if d["progress"]["finished"]:
        break
    time.sleep(0.15)
inter = [p for p in seen_pct if 0 < p < 100]
check("D. progression intermédiaire réelle observée (0 < % < 100)", len(set(inter)) >= 2, str(sorted(set(seen_pct))))
check("D. % global jamais décroissant", all(b >= a for a, b in zip(glob, glob[1:])), str(glob[:40]))
check("D. % global = 100 à la fin", glob and glob[-1] == 100, str(glob[-3:]))
order = [s for i, s in enumerate(statuses) if i == 0 or statuses[i - 1] != s]
check("D. statuts En attente → En cours → Terminée", order[-2:] == ["processing", "done"] and "error" not in order, str(order))
check("D. jamais de fichier final partiel (écrit en .part, validé, puis renommé)", not early_done)

# ══ E. Téléchargement individuel ══════════════════════════════════════
one = next(it for it in final["items"] if it["status"] == "done")
r = c.get(f"/vexport_file/{bid}/{one['out_name']}")
check("E. téléchargement individuel : 200 + octets identiques au disque",
      r.status_code == 200 and r.data == open(exported(bid, one["out_name"]), "rb").read(), f"status={r.status_code}")
r.close()                                              # téléchargement terminé (sinon : « en cours » pour le serveur)
r_trav =c.get(f"/vexport_file/{bid}/..%2Fstate.json")
check("E. traversée de chemin refusée", r_trav.status_code in (400, 404), f"status={r_trav.status_code}")
check("E. fichier non terminé / inconnu refusé (404)", c.get(f"/vexport_file/{bid}/inconnu.mp4").status_code == 404)

# ══ F. ZIP (archive de groupe : 11 vidéos → 1 seul ZIP) ════════════════
sF = wait_settled(c, bid)
check("F. archive du groupe créée automatiquement", sF and sF["groups"][0]["zip"]["status"] == "ready",
      str(sF and sF["groups"]))
rz = c.get(f"/vexport_group_zip/{bid}/1")
check("F. ZIP : 200", rz.status_code == 200)
zp = os.path.join(tmp, "out.zip"); open(zp, "wb").write(rz.data); rz.close()
rz_alias = c.get(f"/vexport_zip/{bid}")
check("F. ancienne URL /vexport_zip (1 groupe) → même archive", rz_alias.status_code == 200
      and rz_alias.data == open(zp, "rb").read()); rz_alias.close()
with zipfile.ZipFile(zp) as z:
    names = z.namelist()
    check("F. ZIP intègre (CRC)", z.testzip() is None)
    check("F. ZIP à plat, STORED", all("/" not in n for n in names)
          and all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))
check("F. ZIP : contient les 11 réussies", len(names) == 11, str(len(names)))
check("F. ZIP : noms uniques", len(set(names)) == len(names))
check("F. aucun ZIP temporaire (.part) ni ZIP géant", not list((A.EXPORT_DIR / bid).rglob("*.part"))
      and not list(A.EXPORT_DIR.glob("_zip_*")))     # (le ZIP d'un AUTRE batch peut être en cours de création)
check("F. archive conservée sur disque après téléchargement", (A.EXPORT_DIR / bid / "zips" / "Batch_001.zip").exists())

# ══ I. Échecs FFmpeg ══════════════════════════════════════════════════
pT = os.path.join(tmp, "tronquee_src.mp4"); mkvid(pT, 1080, 1920, dur=6.0, noise=True, extra=["-movflags", "+faststart"])
data = open(pT, "rb").read(); pT2 = os.path.join(tmp, "tronquee.mp4"); open(pT2, "wb").write(data[: len(data) * 2 // 5])
bidI = c.post("/vexport_create").get_json()["batch_id"]
rT = upload(c, bidI, pT2, "tronquee.mp4").get_json()
check("I. source tronquée acceptée à l'upload (en-tête lisible)", rT.get("ok") is True, str(rT))
upload(c, bidI, pA, "valide.mp4")
rG = upload(c, bidI, pA, "corrompue_apres.mp4").get_json()
stI = A._vexport_load_state(bidI)
gpath = A.EXPORT_DIR / bidI / "input" / stI["items"][rG["idx"]]["in_name"]
gpath.write_bytes(b"\x00\x01garbage" * 4096)           # FFmpeg échouera réellement
c.post("/vexport_start", data={"batch_id": bidI})
fI = wait_finished(c, bidI)
by = {it["name"]: it for it in fI["items"]}
check("I. sortie tronquée détectée → Erreur (pas « Terminée »)",
      by["tronquee.mp4"]["status"] == "error" and by["tronquee.mp4"]["error"], str(by["tronquee.mp4"]))
check("I. échec FFmpeg → Erreur avec message", by["corrompue_apres.mp4"]["status"] == "error"
      and by["corrompue_apres.mp4"]["error"], str(by["corrompue_apres.mp4"]))
check("I. un échec ne bloque pas le batch (vidéo valide terminée)", by["valide.mp4"]["status"] == "done")
wait_settled(c, bidI)
check("I. aucune sortie partielle laissée pour les échecs",
      {p.name for p in (A.EXPORT_DIR / bidI / "output").iterdir()} <= {by["valide.mp4"]["out_name"]}
      and exported_names(bidI) == {by["valide.mp4"]["out_name"]},
      str(list((A.EXPORT_DIR / bidI / "output").iterdir())))
check("I. sources en erreur conservées (relance possible)", fI["retryable"] == 2, str(fI["retryable"]))
rr = c.post("/vexport_retry", data={"batch_id": bidI}).get_json()
check("I. relance manuelle : 2 vidéos remises en file", rr.get("requeued") == 2, str(rr))
fI2 = wait_finished(c, bidI)
check("I. après relance : toujours en erreur (pas de faux succès)", fI2["progress"]["error"] == 2 and fI2["progress"]["done"] == 1)

# ══ K. Récupération des vidéos interrompues (déterministe) ════════════
def craft_batch(statuses, started=True, user_id=1, **extra):
    b = A.uuid.uuid4().hex
    jd = A.EXPORT_DIR / b
    (jd / "input").mkdir(parents=True); (jd / "output").mkdir()
    items = []
    for i, (status, attempts) in enumerate(statuses):
        shutil.copy(pA, jd / "input" / f"{i:04d}.mp4")
        items.append({"idx": i, "orig_name": f"k{i}.mp4", "in_name": f"{i:04d}.mp4", "out_name": f"k{i}.mp4",
                      "status": status, "error": "", "in_size": 1, "out_size": 0,
                      "src_duration": 2.0, "attempts": attempts, "progress": 0})
    st = {"batch_id": b, "user_id": user_id, "created": time.time(), "updated": time.time(),
          "started": started, "items": items}
    st.update(extra)
    A._vexport_save_state(st)
    return b

with A._vexport_flock(A._VEXPORT_GLOBAL_LOCK):     # aucun encodage possible pendant le montage
    bK = craft_batch([("processing", 1), ("processing", 2), ("processing", 1), ("processing", 1), ("pending", 0)],
                     started=False)
    jd = A.EXPORT_DIR / bK
    subprocess.run(["ffmpeg", "-y", "-i", pA, "-c:v", "libx264", "-f", "mp4", str(jd / "output" / "k2.mp4"),
                    "-loglevel", "error"], check=True)                 # sortie finale déjà valide
    subprocess.run(["ffmpeg", "-y", "-i", pA, "-c:v", "libx264", "-f", "mp4", str(jd / "output" / "k3.mp4.part"),
                    "-loglevel", "error"], check=True)                 # .part complet juste avant la coupure
    (jd / "output" / "k0.mp4.part").write_bytes(b"\x00" * 5000)        # .part tronqué
    n = A._vexport_recover_batch(bK)
    stK = A._vexport_load_state(bK); it = stK["items"]
check("K. 4 vidéos interrompues détectées", n == 4, str(n))
check("K. interrompue (1re fois) → remise En attente", it[0]["status"] == "pending" and it[0]["interrupted"] == 1, str(it[0]))
check("K. .part tronqué supprimé", not (jd / "output" / "k0.mp4.part").exists())
check("K. interrompue 2 fois → Erreur explicite « relance nécessaire »",
      it[1]["status"] == "error" and "Relance nécessaire" in it[1]["error"], str(it[1]))
check("K. erreur après interruptions : source conservée", (jd / "input" / "0001.mp4").exists())
check("K. sortie finale déjà valide → Terminée sans réencodage", it[2]["status"] == "done"
      and not (jd / "input" / "0002.mp4").exists())
check("K. .part complet et valide → promu, Terminée", it[3]["status"] == "done" and (jd / "output" / "k3.mp4").exists())
check("K. vidéo en attente non touchée", it[4]["status"] == "pending" and it[4]["attempts"] == 0)
check("K. compteur d'interruptions du batch", stK.get("interruptions") == 1 and not stK.get("paused"))

with A._vexport_flock(A._VEXPORT_GLOBAL_LOCK):
    bP = craft_batch([("processing", 1), ("pending", 0)], started=True, interruptions=2)
    A._vexport_recover_batch(bP)
stP = A._vexport_load_state(bP)
check("K. 3e interruption → batch en pause (anti boucle de crash)", stP.get("paused") is True, str(stP.get("interruptions")))
time.sleep(1.5)
check("K. batch en pause : rien n'est relancé automatiquement",
      all(x["status"] == "pending" for x in A._vexport_load_state(bP)["items"]))
dP = c.get(f"/vexport_status/{bP}").get_json()
check("K. statut expose la pause + raison", dP["paused"] is True and dP["pause_reason"], str(dP.get("pause_reason")))
check("K. « Reprendre » : retry sort de pause", c.post("/vexport_retry", data={"batch_id": bP}).get_json().get("ok") is True)
fP = wait_finished(c, bP)
check("K. après reprise : batch terminé sans double traitement",
      fP and fP["progress"]["done"] == 2 and wait_settled(c, bP) and len(exported_names(bP)) == 2,
      str(fP and fP["progress"]))
check("K. vidéo reprise : 2 tentatives comptées", A._vexport_load_state(bP)["items"][0]["attempts"] == 2,
      str(A._vexport_load_state(bP)["items"][0].get("attempts")))

# ══ L. Isolation entre utilisateurs ═══════════════════════════════════
done_name = one["out_name"]
check("L. statut d'un autre utilisateur : 403", c2.get(f"/vexport_status/{bid}").status_code == 403)
check("L. fichier d'un autre utilisateur : 403", c2.get(f"/vexport_file/{bid}/{done_name}").status_code == 403)
check("L. ZIP d'un autre utilisateur : 403", c2.get(f"/vexport_zip/{bid}").status_code == 403)
check("L. upload dans le batch d'un autre : 403", upload(c2, bidA, pA, "x.mp4").status_code == 403)
check("L. start / retry / cleanup d'un autre : 403",
      c2.post("/vexport_start", data={"batch_id": bid}).status_code == 403
      and c2.post("/vexport_retry", data={"batch_id": bid}).status_code == 403
      and c2.post("/vexport_cleanup", json={"batch_id": bid}).status_code == 403)
anon = A.app.test_client()
check("L. non connecté → redirigé vers /login", anon.get(f"/vexport_status/{bid}").status_code == 302)
check("L. batch_id invalide refusé (400)", c.get("/vexport_status/abc!def").status_code == 400)

# ══ Limite globale + tourniquet (2 utilisateurs, 2 batchs simultanés) ═
pR = os.path.join(tmp, "rr.mp4"); mkvid(pR, 1080, 1920, dur=3.0, noise=True)
bigB = c.post("/vexport_create").get_json()["batch_id"]
for i in range(5): upload(c, bigB, pR, f"big{i}.mp4")
smallB = c2.post("/vexport_create").get_json()["batch_id"]
upload(c2, smallB, pR, "small.mp4")
c.post("/vexport_start", data={"batch_id": bigB})
t_wait = time.time() + 30
while time.time() < t_wait and c.get(f"/vexport_status/{bigB}").get_json()["progress"]["processing"] == 0:
    time.sleep(0.05)
c2.post("/vexport_start", data={"batch_id": smallB})
w = c2.get(f"/vexport_status/{smallB}").get_json()
check("Concurrence : le 2e utilisateur voit « en file d'attente »", w["waiting_other"] is True, str(w["progress"]))
fS = wait_finished(c2, smallB); fB = wait_finished(c, bigB)
stB = A._vexport_load_state(bigB)
done_big_after_small = sum(1 for x in stB["items"] if x.get("t_start", 0) > A._vexport_load_state(smallB)["items"][0]["t_start"])
check("Concurrence : tourniquet — le petit batch passe avant la fin du gros",
      done_big_after_small >= 3, f"vidéos du gros batch traitées après le petit : {done_big_after_small}")
check("Concurrence : les 2 batchs terminés", fS["progress"]["done"] == 1 and fB["progress"]["done"] == 5)

# ══ G / H. Nouveau batch : 300 vidéos RÉELLES acceptées, 301e refusée ══
#    puis batch ANTÉRIEUR à la limite (sans « max_files ») : 500 traitées.
tiny = os.path.join(tmp, "tiny.mp4"); mkvid(tiny, 64, 64, dur=0.4, audio=False, fps=10)
r_many = c.post("/vexport_create", data={"expected_count": "301", "expected_bytes": "1000"})
check("H. création annoncée à 301 vidéos refusée (limite 300)", r_many.status_code == 400
      and r_many.get_json().get("code") == "too_many", str(r_many.get_json()))
if os.environ.get("VEXPORT_TEST_SKIP_500") == "1":
    print("⚠️  G. test réel 500 vidéos SAUTÉ (VEXPORT_TEST_SKIP_500=1)")
    bid500 = c.post("/vexport_create").get_json()["batch_id"]
    with A._vexport_lock(bid500):
        st5 = A._vexport_load_state(bid500)
        st5["items"] = [{"idx": i, "orig_name": f"v{i}", "in_name": f"{i:04d}.mp4", "out_name": f"v{i}.mp4",
                         "status": "pending", "error": "", "in_size": 0, "out_size": 0} for i in range(300)]
        A._vexport_save_state(st5)
else:
    t0 = time.time()
    bid500 = c.post("/vexport_create").get_json()["batch_id"]
    tiny_bytes = open(tiny, "rb").read()
    ok300 = 0
    for i in range(300):
        rr5 = c.post("/vexport_upload", data={"batch_id": bid500, "file": (io.BytesIO(tiny_bytes), f"clip_{i:03d}.mp4")},
                     content_type="multipart/form-data").get_json()
        ok300 += 1 if rr5.get("ok") else 0
    check("G. 300 vidéos réelles acceptées (nouveau batch)", ok300 == 300, str(ok300))
r301 = upload(c, bid500, tiny, "extra.mp4").get_json()
check("H. 301e vidéo refusée avec message clair", "error" in r301 and "300" in r301["error"], str(r301))
if os.environ.get("VEXPORT_TEST_SKIP_500") != "1":
    # Batch créé AVANT la limite 300 (état sans « max_files ») : sa limite reste 500.
    with A._vexport_lock(bid500):
        st5 = A._vexport_load_state(bid500); st5.pop("max_files", None); A._vexport_save_state(st5)
    for i in range(300, 500):
        rr5 = c.post("/vexport_upload", data={"batch_id": bid500, "file": (io.BytesIO(tiny_bytes), f"clip_{i:03d}.mp4")},
                     content_type="multipart/form-data").get_json()
        ok300 += 1 if rr5.get("ok") else 0
    check("G. ancien batch (créé avant la limite 300) : 500 vidéos acceptées", ok300 == 500, str(ok300))
    r501 = upload(c, bid500, tiny, "extra.mp4").get_json()
    check("H. ancien batch : 501e vidéo refusée", "error" in r501 and "500" in r501["error"], str(r501))
    t_up = time.time() - t0
    c.post("/vexport_start", data={"batch_id": bid500})
    f500 = wait_finished(c, bid500, timeout=900, step=2)
    t_all = time.time() - t0
    check("G. 500 vidéos traitées (0 erreur)", f500 and f500["progress"]["done"] == 500, str(f500 and f500["progress"]))
    s500 = wait_settled(c, bid500)
    names5 = []
    for gi in range(1, 6):
        rz5 = c.get(f"/vexport_group_zip/{bid500}/{gi}")
        if rz5.status_code == 200:
            with zipfile.ZipFile(io.BytesIO(rz5.data)) as z5:
                names5 += z5.namelist()
        rz5.close()
    check("G. 5 archives de 100 vidéos (500 noms uniques)", len(names5) == 500 and len(set(names5)) == 500, str(len(names5)))
    print(f"   ⏱️  500 vidéos : upload {t_up:.0f} s, total upload+encodage {t_all:.0f} s")
    c.post("/vexport_cleanup", json={"batch_id": bid500})

# ══ M. Nettoyage ══════════════════════════════════════════════════════
in_left = list((A.EXPORT_DIR / bid / "input").iterdir())
check("M. sources supprimées après encodage réussi", in_left == [], str(in_left))
check("M. résultats conservés après traitement (dans l'archive)", len(exported_names(bid)) == 11
      and not list(out_dir.glob("*.mp4")))
leftovers = [p for p in A.EXPORT_DIR.rglob("*") if p.name.endswith((".part", ".tmp", ".log"))]
check("M. aucun .part / .tmp / log FFmpeg résiduel", leftovers == [], str(leftovers))
with A._vexport_flock(A._VEXPORT_GLOBAL_LOCK), A._vexport_flock(A._VEXPORT_ZIP_LOCK):   # encodeur et créateur de ZIP à l'arrêt
    old_done = craft_batch([("done", 1)], started=True)
    old_queued = craft_batch([("pending", 0)], started=True)
    for b in (old_done, old_queued):
        s_ = A._vexport_load_state(b); s_path = A._vexport_state_path(b)
        s_["updated"] = time.time() - 30 * 3600
        s_path.write_text(json.dumps(s_))
    orphan_zip = A.EXPORT_DIR / "_zip_orphelin.zip"; orphan_zip.write_bytes(b"PK")
    os.utime(orphan_zip, (time.time() - 7200, time.time() - 7200))
    A._cleanup_stale_vexport()
    check("M. vieux batch terminé purgé automatiquement", not (A.EXPORT_DIR / old_done).exists())
    check("M. vieux batch encore EN FILE jamais purgé", (A.EXPORT_DIR / old_queued).exists())
    check("M. ZIP orphelin purgé", not orphan_zip.exists())
    check("M. batch récent conservé", (A.EXPORT_DIR / bid).exists())
    shutil.rmtree(A.EXPORT_DIR / old_queued)
for b in (bidA, bid, bidD, bidI, bK, bP, bigB):
    c.post("/vexport_cleanup", json={"batch_id": b})
c2.post("/vexport_cleanup", json={"batch_id": smallB})
check("M. cleanup : dossiers supprimés", not any((A.EXPORT_DIR / b).exists() for b in (bidA, bid, bidD, bidI)))

sampler.stop()
check("Mémoire : jamais plus d'1 FFmpeg d'export simultané (tous tests confondus)", sampler.max == 1, f"max={sampler.max}")

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'='*40}\n{PASS} tests OK / {FAIL} KO")
sys.exit(0 if FAIL == 0 else 1)
