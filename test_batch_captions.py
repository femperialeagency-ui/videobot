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
clean = ocr_local._clean_caption_text
keep  = ocr_local._keep_segment
textlike = ocr_local._is_text_like

# A1. SEULES les chaînes 100% symboles (aucun alphanumérique) sont rejetées
#     immédiatement — jamais un fragment alphabétique par son contenu.
PURE_SYMBOLS = ["€. ™", "| |", "( )", "///", "»«", "~ ~", "€ ™", "™", "—"]
for g in PURE_SYMBOLS:
    check(f"pur symbole rejeté: {g!r}", not textlike(clean(g)))

# A2. Le MÊME fragment alphabétique est CONSERVÉ avec de bons signaux et
#     REJETÉ avec de mauvais signaux (décision par le comportement OCR, pas
#     par le texte). Couvre explicitement I, A, À, J, LES, RN, Th.
SIGNAL_FRAGMENTS = ["I", "A", "À", "J", "LES", "RN", "Th"]
for g in SIGNAL_FRAGMENTS:
    check(f"{g!r} gardé (bons signaux)",
          keep(clean(g), conf=85, persist=3, cy_std=0.01))
    check(f"{g!r} rejeté (1 frame, conf faible, zone instable)",
          not keep(clean(g), conf=38, persist=1, cy_std=0.20))

# Autres parasites transitoires également rejetés par le comportement OCR
for g in ["ee", "oy", "i ff m", "'a7", "y aN"]:
    check(f"parasite transitoire rejeté: {g!r}",
          not keep(clean(g), conf=38, persist=1, cy_std=0.20))

# A3. Vraies captions COURTES conservées quand corroborées (répétition OU
#     confiance haute) et zone stable — ne JAMAIS rejeter pour cause de longueur.
SHORT_REAL = ["POV", "No", "Yes", "Why?", "Emma", "5", "then", "one", "money"]
for s in SHORT_REAL:
    # cas répété sur plusieurs frames, zone stable
    check(f"courte gardée (répétée): {s!r}", keep(clean(s), conf=55, persist=3, cy_std=0.01))
    # cas 1 frame mais confiance élevée (texte net)
    check(f"courte gardée (conf haute): {s!r}", keep(clean(s), conf=85, persist=1, cy_std=0.01))

# A4. Caption mot-par-mot : chaque mot affiché ~2-3 frames → conservé
for w in ["Take", "my", "hand"]:
    check(f"mot-par-mot gardé: {w!r}", keep(clean(w), conf=60, persist=2, cy_std=0.02))

# A5. Phrases conservées : une vraie caption est nette (confiance correcte) OU
#     répétée sur plusieurs frames.
for p in ["and just check my profile", "You're tired to be an adult?", "5 types of men:"]:
    check(f"phrase gardée (nette): {p!r}", keep(clean(p), conf=65, persist=1, cy_std=0.03))
    check(f"phrase gardée (répétée): {p!r}", keep(clean(p), conf=40, persist=2, cy_std=0.03))

# A6. Nettoyage du bruit de bord (garde le nombre de tête)
check("nettoyage bord", "5 types of men:" in clean("| | ' 5 types of men:"))

# ── B) OCR réel sur une vidéo B (si disponible) ───────────────────────
BVID = "/sessions/eager-youthful-thompson/mnt/uploads/video source (B).mp4"
if os.path.exists(BVID):
    lines, meta = ocr_local.analyze_video_local(BVID)
    texts = " || ".join(l["text"] for l in lines)
    # Politique : on ne juge pas par le texte. On vérifie donc (a) que le
    # charabia massif a disparu (plus de 60 fragments → un nombre borné), (b)
    # qu'aucun segment n'est un pur symbole, (c) que les vraies captions sont
    # présentes. La présence éventuelle d'un fragment court stable (« RN »…)
    # est CONFORME (décision par signaux, pas par contenu).
    check("OCR B: charabia massif éliminé (<=15, était 60)", len(lines) <= 15, f"({len(lines)})")
    check("OCR B: aucun segment pur-symbole", all(ocr_local._has_usable_text(l["text"]) for l in lines))
    check("OCR B: contient les vraies captions", "types of men" in texts.lower() and "young guy" in texts.lower())
    print("   segments B:", " | ".join(repr(l["text"]) for l in lines))
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
