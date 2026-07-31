#!/usr/bin/env python3
"""
test_batch_captions.py — Non-régression captions Batch.

Vérifie :
  A) le filtre anti-charabia de l'OCR local (fragments parasites rejetés,
     vraies captions conservées) ;
  B) analyze_video_local sur une vraie vidéo B → sortie propre (pas de
     « LES », « RN », « € ™ »… ; contient les vraies captions) ;
  C) l'ISOLATION Batch : /batch_render n'utilise QUE le lines_json fourni →
     C(A,B_j) ne contient que les captions de B_j, jamais celles d'un autre B,
     et deux rendus successifs ne se contaminent pas.

Usage : DATA_DIR=<persistant> python3 test_batch_captions.py
"""
import os, sys, json, subprocess, tempfile, warnings
warnings.filterwarnings("ignore")

FAILS = []
def check(name, cond, extra=""):
    print(("✅" if cond else "❌") + f" {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)

# ── A) Filtre unitaire ────────────────────────────────────────────────
import ocr_local
GARBAGE = ["i ff m", "RN", "~ r | |", "oy?", "€. ™", "À", "| |", "A", "ee",
           "'a7", "LES", "j", "Th", "y aN", "( 5 )"]
REAL = ["5 types of men:", "1. Young guy", "2. Shy one", "3. Older man",
        "You're tired to be an adult?", "and just check my profile", "1 2 3 4 5 6"]
for g in GARBAGE:
    check(f"charabia rejeté: {g!r}", not ocr_local._is_caption_like(ocr_local._clean_caption_text(g)))
for r in REAL:
    check(f"vraie caption gardée: {r!r}", ocr_local._is_caption_like(ocr_local._clean_caption_text(r)))
# nettoyage du bruit de bord
check("nettoyage bord", "5 types of men:" in ocr_local._clean_caption_text("| | ' 5 types of men:"))

# ── B) OCR réel sur une vidéo B (si disponible) ───────────────────────
BVID = "/sessions/eager-youthful-thompson/mnt/uploads/video source (B).mp4"
if os.path.exists(BVID):
    lines, meta = ocr_local.analyze_video_local(BVID)
    texts = " || ".join(l["text"] for l in lines)
    check("OCR B: peu de segments (<=8)", len(lines) <= 8, f"({len(lines)})")
    check("OCR B: pas de parasite 'LES'/'RN'", " LES " not in f" {texts} " and " RN " not in f" {texts} ")
    check("OCR B: contient les vraies captions", "types of men" in texts.lower() and "young guy" in texts.lower())
    check("OCR B: toutes caption-like", all(ocr_local._is_caption_like(l["text"]) for l in lines))
else:
    print("… (vidéo B d'exemple absente — partie B ignorée)")

# ── C) Isolation /batch_render (rendu réel) ───────────────────────────
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
with client.session_transaction() as s:
    s["user_id"] = 1

def caption(text):
    return [{"text": text, "start_time": 0.0, "end_time": 2.0,
             "cx_pct": 0.5, "cy_pct": 0.5, "width_pct": 0.8, "fontsize_pct": 0.08,
             "align": "center", "bold": True, "color": "white"}]

def render(a, b, text):
    r = client.post("/batch_render", data={
        "batch_id": BATCH_ID, "a_index": str(a), "b_index": str(b),
        "num_a": "1", "num_b": "2", "ocr_mode": "local",
        "lines_json": json.dumps(caption(text))})
    return r

CAP_B01 = "ZEBRAONE"; CAP_B02 = "MANGOTWO"
r1 = render(0, 0, CAP_B01)
r2 = render(0, 1, CAP_B02)
check("batch_render B01 ok", r1.status_code == 200, str(r1.status_code))
check("batch_render B02 ok", r2.status_code == 200, str(r2.status_code))

def read_caption(mp4):
    lines, _ = ocr_local.analyze_video_local(str(mp4))
    return " ".join(l["text"] for l in lines).upper()

out1 = bdir/"out"/"A01_B01_output.mp4"; out2 = bdir/"out"/"A01_B02_output.mp4"
if out1.exists() and out2.exists():
    t1 = read_caption(out1); t2 = read_caption(out2)
    # chaque sortie contient SA caption et PAS celle de l'autre B (isolation)
    check("A01_B01 contient CAP_B01", CAP_B01 in t1.replace(" ", ""), t1)
    check("A01_B01 NE contient PAS CAP_B02", CAP_B02 not in t1.replace(" ", ""))
    check("A01_B02 contient CAP_B02", CAP_B02 in t2.replace(" ", ""), t2)
    check("A01_B02 NE contient PAS CAP_B01", CAP_B01 not in t2.replace(" ", ""))
else:
    check("sorties batch générées", False, "fichiers de sortie manquants")

import shutil; shutil.rmtree(bdir, ignore_errors=True)
print("\n" + ("✅ TOUS LES TESTS PASSENT" if not FAILS else f"❌ ÉCHECS: {FAILS}"))
sys.exit(1 if FAILS else 0)
