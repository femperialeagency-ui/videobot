#!/usr/bin/env python3
"""
test_batch_captions.py — Non-régression captions Batch (reconstruction OCR par
PISTE DOMINANTE).

Vérifie :
  A) helpers de texte (pur symbole rejeté ; tokens alphabétiques préservés,
     aucun jugement par le contenu) ;
  B) PIPELINE bout-en-bout sur une vidéo SYNTHÉTIQUE (reproductible) :
       - sélection de la piste dominante (bruit de bords exclu),
       - exactement 3 captions séquentielles,
       - captions courtes conservées quand elles sont DANS la piste (POV, No),
       - géométrie cohérente (même position/taille),
       - aucune superposition (une seule caption active à la fois) ;
  C) VRAIES vidéos vid1/vid2/vid3 (si présentes) : exactement 3 captions
     chacune, géométrie cohérente, non-chevauchement ;
  D) ISOLATION Batch en rendu réel (/batch_render n'utilise que son lines_json).

Usage : DATA_DIR=<persistant> python3 test_batch_captions.py
"""
import os, sys, json, subprocess, tempfile, warnings
warnings.filterwarnings("ignore")

FAILS = []
def check(name, cond, extra=""):
    print(("✅" if cond else "❌") + f" {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)

import ocr_local
clean = ocr_local._clean_caption_text
usable = ocr_local._has_usable_text

def _adjacent_dupes(lines):
    """Renvoie les paires de captions CONSÉCUTIVES quasi-identiques (même
    caption sur-segmentée = parasite). Deux captions sont « dupliquées » si
    forte similarité de séquence OU partage d'un mot « fort » (≥5 lettres).
    L'invariant de sortie : AUCUNE paire de ce type ne doit subsister."""
    import re as _re
    def strong(t):
        return set(x for x in (_re.sub(r"[^a-z]", "", y) for y in ocr_local._norm(t).split()) if len(x) >= 5)
    bad = []
    for i in range(len(lines) - 1):
        a, b = lines[i]["text"], lines[i + 1]["text"]
        if ocr_local._similar(ocr_local._norm(a), ocr_local._norm(b)) >= 0.5 or (strong(a) & strong(b)):
            bad.append((a.replace("\n", " "), b.replace("\n", " ")))
    return bad

# ── A) Helpers : contenu ne sert JAMAIS de blacklist ──────────────────
for g in ["€. ™", "| |", "( )", "///", "»«", "™", "—"]:
    check(f"pur symbole rejeté: {g!r}", not usable(clean(g)))
for g in ["I", "A", "À", "J", "LES", "RN", "Th", "No", "5", "POV"]:
    check(f"token alphanumérique préservé: {g!r}", usable(clean(g)))
check("nettoyage bord garde le nombre", "5 types of men:" in clean("| | ' 5 types of men:"))
check("nettoyage garde RN (pas de jugement contenu)", clean("| RN |") == "RN")

# ── B) Pipeline sur vidéo SYNTHÉTIQUE (piste dominante) ────────────────
FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "Montserrat-VariableFont_wght.ttf")
WORK = tempfile.mkdtemp()
SYN = os.path.join(WORK, "syn.mp4")
draw = (
  f"drawtext=fontfile={FONT}:text='POV':fontcolor=white:fontsize=80:x=(w-tw)/2:y=h*0.63:enable='between(t,0,2)',"
  f"drawtext=fontfile={FONT}:text='and just check':fontcolor=white:fontsize=80:x=(w-tw)/2:y=h*0.63:enable='between(t,2.3,4.5)',"
  f"drawtext=fontfile={FONT}:text='No':fontcolor=white:fontsize=80:x=(w-tw)/2:y=h*0.63:enable='between(t,4.8,7)',"
  f"drawtext=fontfile={FONT}:text='LES':fontcolor=white:fontsize=44:x=40:y=h*0.08,"          # bruit HAUT
  f"drawtext=fontfile={FONT}:text='x':fontcolor=white:fontsize=44:x=w-70:y=h*0.95"           # bruit BAS
)
subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=black:s=720x1280:d=7:r=24",
                "-vf",draw,"-c:v","libx264","-pix_fmt","yuv420p",SYN,"-loglevel","error"], check=True)
lines, meta = ocr_local.analyze_video_local(SYN)
texts = [l["text"].lower() for l in lines]
check("synthétique: exactement 3 captions", len(lines) == 3, f"({len(lines)})")
check("synthétique: caption courte 'POV' conservée (dans la piste)", any("pov" in t for t in texts))
check("synthétique: 'and just check' conservée", any("check" in t for t in texts))
check("synthétique: caption courte 'No' conservée", any(t.strip() == "no" for t in texts))
check("synthétique: bruit de bords 'LES'/'x' exclu",
      all(t.strip() not in ("les", "x") for t in texts))
check("synthétique: géométrie cohérente (1 seule position/taille)",
      len(set(l["cy_pct"] for l in lines)) == 1 and len(set(l["fontsize_pct"] for l in lines)) == 1)
check("synthétique: aucune superposition (séquentiel)",
      all(lines[i]["end_time"] <= lines[i+1]["start_time"] + 0.01 for i in range(len(lines)-1)))
check("synthétique: aucune caption dupliquée adjacente", not _adjacent_dupes(lines))
import shutil; shutil.rmtree(WORK, ignore_errors=True)

# ── C) VRAIES vidéos (si fournies) ────────────────────────────────────
REAL = {
  "vid1": ["tired", "my", "profil"],
  "vid2": ["memory", "look", "learner"],
  "vid3": ["teach", "looks", "profil"],
}
updir = "/sessions/eager-youthful-thompson/mnt/uploads"
if os.environ.get("SKIP_REAL"):
    REAL = {}
    print("… (partie C vidéos réelles ignorée : SKIP_REAL=1)")
for name, kws in REAL.items():
    path = os.path.join(updir, name + ".mov")
    if not os.path.exists(path):
        print(f"… {name}.mov absent — ignoré")
        continue
    lines, meta = ocr_local.analyze_video_local(path)
    joined = " ".join(l["text"].lower() for l in lines)
    check(f"{name}: exactement 3 captions", len(lines) == 3, f"({len(lines)})")
    check(f"{name}: géométrie cohérente", len(set(l["cy_pct"] for l in lines)) == 1 and len(set(l["fontsize_pct"] for l in lines)) == 1)
    check(f"{name}: non-chevauchement", all(lines[i]["end_time"] <= lines[i+1]["start_time"] + 0.01 for i in range(len(lines)-1)))
    check(f"{name}: aucune caption dupliquée adjacente", not _adjacent_dupes(lines), str(_adjacent_dupes(lines))[:100])
    check(f"{name}: contient les vraies captions", all(k in joined for k in kws), joined[:80])

# ── C2) RÉGRESSION compression : une vidéo B ré-encodée en 720p (basse qualité,
# comme un upload dégradé) ne doit PAS sur-segmenter une caption en deux à cause
# d'une frame-transition parasite. vid2 = cas connu (« I have some memory
# problems » scindé en deux avant le correctif). ──
_v2 = os.path.join(updir, "vid2.mov")
if not os.environ.get("SKIP_REAL") and os.path.exists(_v2):
    _tmp = tempfile.mkdtemp(); _v2c = os.path.join(_tmp, "vid2_720.mp4")
    subprocess.run(["ffmpeg", "-y", "-i", _v2, "-vf", "scale=720:-2", "-c:v", "libx264",
                    "-crf", "30", "-preset", "veryfast", "-c:a", "aac", "-b:a", "96k",
                    _v2c, "-loglevel", "error"], check=True)
    lc, _ = ocr_local.analyze_video_local(_v2c)
    check("vid2 720p (compressé): exactement 3 captions", len(lc) == 3, f"({len(lc)})")
    check("vid2 720p (compressé): aucune caption dupliquée adjacente", not _adjacent_dupes(lc),
          str(_adjacent_dupes(lc))[:100])
    import shutil as _sh; _sh.rmtree(_tmp, ignore_errors=True)

# ── D) Isolation Batch (rendu réel) ───────────────────────────────────
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
import app as A
BATCH_ID = "testcap_" + os.urandom(4).hex()
bdir = A.BATCH_DIR / BATCH_ID
for sub in ("A", "B", "out"):
    (bdir / sub).mkdir(parents=True, exist_ok=True)
def mkvid(path, seconds=2):
    subprocess.run(["ffmpeg","-y","-f","lavfi","-i",f"color=c=navy:s=576x1024:d={seconds}:r=24",
                    "-f","lavfi","-i",f"sine=frequency=330:duration={seconds}",
                    "-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac","-shortest",str(path),"-loglevel","error"], check=True)
mkvid(bdir/"A"/"00.mp4"); mkvid(bdir/"B"/"00.mp4"); mkvid(bdir/"B"/"01.mp4")
client = A.app.test_client()
with client.session_transaction() as s: s["user_id"] = 1
def caption(text):
    return [{"text": text, "start_time": 0.0, "end_time": 2.0, "cx_pct": 0.5, "cy_pct": 0.5,
             "width_pct": 0.8, "fontsize_pct": 0.08, "align": "center", "bold": True, "color": "white"}]
def render(a, b, text):
    return client.post("/batch_render", data={"batch_id": BATCH_ID, "a_index": str(a), "b_index": str(b),
        "num_a": "1", "num_b": "2", "ocr_mode": "local", "lines_json": json.dumps(caption(text))})
CAP_B01 = "ZEBRAONE"; CAP_B02 = "MANGOTWO"
check("batch_render B01 ok", render(0, 0, CAP_B01).status_code == 200)
check("batch_render B02 ok", render(0, 1, CAP_B02).status_code == 200)
def read_caption(mp4):
    ls, _ = ocr_local.analyze_video_local(str(mp4)); return " ".join(l["text"] for l in ls).upper().replace(" ", "")
o1 = bdir/"out"/"A01_B01_output.mp4"; o2 = bdir/"out"/"A01_B02_output.mp4"
if o1.exists() and o2.exists():
    t1 = read_caption(o1); t2 = read_caption(o2)
    check("A01_B01 = sa caption seule", CAP_B01 in t1 and CAP_B02 not in t1)
    check("A01_B02 = sa caption seule", CAP_B02 in t2 and CAP_B01 not in t2)
else:
    check("sorties batch générées", False)
shutil.rmtree(bdir, ignore_errors=True)

# ── E) Garde-fou d'espace disque : purge des batches/ZIP temporaires ───
import time as _t
# vieux dossier de batch (> 3 h) → doit être purgé
old = A.BATCH_DIR / "old_batch_e"; old.mkdir(parents=True, exist_ok=True)
(old / "f.bin").write_bytes(b"x" * 1024)
os.utime(old, (_t.time() - 4 * 3600, _t.time() - 4 * 3600))
# dossier récent (< 10 min) → conservé (potentiellement en cours)
recent = A.BATCH_DIR / "recent_batch_e"; recent.mkdir(parents=True, exist_ok=True)
(recent / "f.bin").write_bytes(b"x" * 1024)
# lot explicitement conservé : plus vieux que le seuil « en cours » (10 min)
# donc éligible à la purge agressive, mais protégé par keep_batch_id. Reste
# sous les 3 h pour survivre au nettoyage doux, comme le lot courant réel.
keep = A.BATCH_DIR / "keep_batch_e"; keep.mkdir(parents=True, exist_ok=True)
(keep / "f.bin").write_bytes(b"x" * 1024)
os.utime(keep, (_t.time() - 3600, _t.time() - 3600))   # 1 h : gardé via keep_batch_id
# ZIP orphelins → doivent être balayés
zb = A.DATA_DIR / "zipbuild_teste.zip"; zb.write_bytes(b"x" * 1024)
bz = A.DATA_DIR / "batch_teste.zip";    bz.write_bytes(b"x" * 1024)
os.utime(zb, (_t.time() - 3 * 3600, _t.time() - 3 * 3600))
os.utime(bz, (_t.time() - 3 * 3600, _t.time() - 3 * 3600))

# _sweep_orphan_zips seul retire les ZIP > 2 h
A._sweep_orphan_zips()
check("disk: ZIP orphelin zipbuild_* purgé", not zb.exists())
check("disk: ZIP orphelin batch_*.zip purgé", not bz.exists())

# min_free démesuré → force la purge AGRESSIVE de façon déterministe
A._free_disk_space(keep_batch_id="keep_batch_e", min_free=10**18)
check("disk: vieux dossier de batch purgé", not old.exists())
check("disk: lot en cours (keep) conservé", keep.exists())
check("disk: dossier récent conservé", recent.exists())
for d in (recent, keep):
    shutil.rmtree(d, ignore_errors=True)

print("\n" + ("✅ TOUS LES TESTS PASSENT" if not FAILS else f"❌ ÉCHECS: {FAILS}"))
sys.exit(1 if FAILS else 0)
