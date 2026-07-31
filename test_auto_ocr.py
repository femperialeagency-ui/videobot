#!/usr/bin/env python3
"""
test_auto_ocr.py — Mode AUTO : fallback Vision CIBLÉ sur les captions incertaines.

Prouve :
  • aucun appel Vision quand le local est fiable (aucune caption incertaine) ;
  • Vision UNIQUEMENT pour les captions incertaines (les fiables ne sont pas
    envoyées et ne sont pas modifiées) ;
  • un SEUL appel Vision par vidéo B (pas un par caption, ni par vidéo A) ;
  • aucun fallback en mode Local (jamais d'appel Vision) ;
  • Vision direct en mode Vision ;
  • timings/position/géométrie inchangés (Vision ne fait que retranscrire) ;
  • le recadrage réel d'une caption contient bien du texte ;
  • (si ANTHROPIC_API_KEY présent) les 9 textes attendus sur vid1/vid2/vid3.

Usage : DATA_DIR=<persistant> python3 test_auto_ocr.py
"""
import os, sys, json, tempfile, types, warnings
warnings.filterwarnings("ignore")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

FAILS = []
def check(name, cond, extra=""):
    print(("✅" if cond else "❌") + f" {name}" + (f"  {extra}" if extra else ""))
    if not cond: FAILS.append(name)

import app as A

# ── Faux client Anthropic : compte les appels, renvoie un JSON contrôlé ──
CALLS = {"n": 0, "images": 0}
def install_fake_anthropic(mapping):
    """mapping: {image_index: text}. Enregistre un module 'anthropic' factice."""
    CALLS["n"] = 0; CALLS["images"] = 0
    class _Msg:
        def __init__(self, txt): self.content = [types.SimpleNamespace(text=txt)]
    class _Messages:
        def create(self, model, max_tokens, messages):
            CALLS["n"] += 1
            CALLS["images"] += sum(1 for c in messages[0]["content"] if c.get("type") == "image")
            data = [{"i": i, "text": t} for i, t in mapping.items()]
            return _Msg(json.dumps(data))
    class Anthropic:
        def __init__(self, api_key=None): self.messages = _Messages()
    sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=Anthropic)

def line(text, uncertain, cy=0.66, st=0.0, en=2.0):
    return {"text": text, "start_time": st, "end_time": en, "cx_pct": 0.5, "cy_pct": cy,
            "width_pct": 0.75, "fontsize_pct": 0.03, "align": "center", "bold": True,
            "color": "white", "_conf": 80.0, "_uncertain": uncertain}

VID = "/sessions/eager-youthful-thompson/mnt/uploads/vid1.mov"
HAVE_VID = os.path.exists(VID)

# ── 1) _auto_refine_uncertain : sans clé → aucun appel, lignes inchangées ──
os.environ.pop("ANTHROPIC_API_KEY", None)
lines = [line("bon", False), line("douteux", True)]
out, n = A._auto_refine_uncertain(VID if HAVE_VID else "x.mp4", lines)
check("sans clé API : 0 appel Vision", n == 0)
check("sans clé API : lignes inchangées", out == lines)

# ── 2) avec clé + 1 seule caption incertaine → 1 appel, SEULE l'incertaine change ──
if HAVE_VID:
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    install_fake_anthropic({0: "TEXTE VISION CORRIGE"})
    lines = [line("caption fiable", False, cy=0.66),
             line("caption douteuse", True, cy=0.66, st=2.0, en=4.0)]
    out, n = A._auto_refine_uncertain(VID, lines)
    check("Vision appelé une seule fois", n == 1 and CALLS["n"] == 1)
    check("1 seule image envoyée (la caption incertaine)", CALLS["images"] == 1)
    check("caption fiable INCHANGÉE", out[0]["text"] == "caption fiable")
    check("caption incertaine REMPLACÉE par Vision", out[1]["text"] == "TEXTE VISION CORRIGE")
    check("timings/géométrie conservés",
          out[1]["start_time"] == 2.0 and out[1]["end_time"] == 4.0 and out[1]["cy_pct"] == 0.66)

    # ── 3) aucune caption incertaine → aucun appel ──
    install_fake_anthropic({0: "NE DEVRAIT PAS APPARAITRE"})
    lines = [line("a", False), line("b", False)]
    out, n = A._auto_refine_uncertain(VID, lines)
    check("local fiable : 0 appel Vision", n == 0 and CALLS["n"] == 0)
    check("local fiable : textes inchangés", [l["text"] for l in out] == ["a", "b"])

    # ── 4) recadrage réel : contient du texte (proxy de ce que Vision verrait) ──
    #     (la 1re caption de vid1 est vers cy≈0.66 — on recadre sans re-OCR complet)
    import ocr_local as O
    with tempfile.TemporaryDirectory() as td:
        cp = os.path.join(td, "c.png")
        probe = {"start_time": 1.5, "end_time": 2.5, "cy_pct": 0.66, "fontsize_pct": 0.03}
        ok = A._crop_caption_frame(VID, probe, cp) and os.path.exists(cp)
        has_text = False
        if ok:
            ls, _w, _h = O._ocr_frame(cp)
            has_text = any(O._has_usable_text(l["text"]) for l in ls)
        check("recadrage caption réel contient du texte", ok and has_text)
else:
    print("… vid1.mov absent — tests 2-4 ignorés")

# ── 5) _strip_ocr_meta : aucune clé interne ne fuit ──
s = A._strip_ocr_meta([line("x", True)])
check("strip meta : aucune clé _ interne", all(not k.startswith("_") for k in s[0]))

# ── 6) Routage /batch_detect (OCR local MOCKÉ pour la vitesse) ──
#     Local = jamais de Vision ; Auto = fallback Vision ciblé + cache par B.
import subprocess
BID = "autotest_" + os.urandom(3).hex()
bdir = A.BATCH_DIR / BID
(bdir / "B").mkdir(parents=True, exist_ok=True)
subprocess.run(["ffmpeg","-y","-f","lavfi","-i","color=c=black:s=360x640:d=1","-c:v","libx264",
                "-pix_fmt","yuv420p", str(bdir/"B"/"00.mp4"),"-loglevel","error"], check=True)
client = A.app.test_client()
with client.session_transaction() as s2: s2["user_id"] = 1

CANNED = [line("caption fiable", False), line("caption douteuse", True)]
A._analyze_local_engine = lambda p: [dict(x) for x in CANNED]
A._analyze_local_engine_meta = lambda p: ([dict(x) for x in CANNED], {"needs_vision": True})
REF = {"n": 0}
def spy(video, lines, model=A.OCR_MODEL_SONNET):
    REF["n"] += 1
    out = [dict(x) for x in lines]
    for l in out:
        if l.get("_uncertain"): l["text"] = "VISION_" + l["text"]
    return out, 1
A._auto_refine_uncertain = spy
os.environ["ANTHROPIC_API_KEY"] = "test-key"

# Local explicite → refine JAMAIS appelé, aucune clé _ dans la réponse
REF["n"] = 0
r = client.post("/batch_detect", data={"batch_id": BID, "b_index": "0", "ocr_mode": "local", "ignore_cache": "1"})
d = r.get_json()
check("mode Local : /batch_detect ok", r.status_code == 200)
check("mode Local : AUCUN fallback Vision", REF["n"] == 0)
check("mode Local : réponse ne fuit aucune clé _", all(not k.startswith("_") for l in d.get("lines", []) for k in l))

# Auto → refine appelé une fois, seule la caption incertaine corrigée
REF["n"] = 0
r = client.post("/batch_detect", data={"batch_id": BID, "b_index": "0", "ocr_mode": "auto", "ignore_cache": "1"})
d = r.get_json(); txts = [l["text"] for l in d.get("lines", [])]
check("mode Auto : /batch_detect ok", r.status_code == 200)
check("mode Auto : fallback Vision ciblé déclenché (1×)", REF["n"] == 1)
check("mode Auto : caption fiable inchangée + incertaine corrigée",
      "caption fiable" in txts and "VISION_caption douteuse" in txts)
check("mode Auto : réponse ne fuit aucune clé _", all(not k.startswith("_") for l in d.get("lines", []) for k in l))

# Cache : 2e appel Auto même B → servi par cache (un seul OCR/Vision par B)
REF["n"] = 0
r2 = client.post("/batch_detect", data={"batch_id": BID, "b_index": "0", "ocr_mode": "auto"})
check("mode Auto : 2e appel même B servi par cache", r2.get_json().get("source") == "cache")
check("mode Auto : cache → pas de nouvel appel Vision", REF["n"] == 0)

# Vision explicite → Vision DIRECT (pas de local, pas de refine ciblé)
REF["n"] = 0
A.analyze_with_claude_vision_timed = lambda p, model=A.OCR_MODEL_SONNET: ([line("VISION DIRECT", False)], 0)
r3 = client.post("/batch_detect", data={"batch_id": BID, "b_index": "0", "ocr_mode": "sonnet", "ignore_cache": "1"})
d3 = r3.get_json()
check("mode Vision : source=vision (direct)", d3.get("source") == "vision")
check("mode Vision : pas de refine ciblé (Auto)", REF["n"] == 0)
check("mode Vision : texte Vision utilisé", any("VISION DIRECT" in l["text"] for l in d3.get("lines", [])))

import shutil; shutil.rmtree(bdir, ignore_errors=True)

# ── 7) 9 textes attendus sur les vrais fichiers (seulement si vraie clé Vision) ──
REAL_KEY = os.environ.get("REAL_VISION_KEY")
if REAL_KEY:
    os.environ["ANTHROPIC_API_KEY"] = REAL_KEY
    if "anthropic" in sys.modules and not hasattr(sys.modules["anthropic"], "__file__"):
        del sys.modules["anthropic"]  # retire le faux module
    EXP = {
      "vid1": ["You're tired to be an adult?", "Be my bby then", "and just check my profil"],
      "vid2": ["I have some memory problems", "What does a dihh look like again?", "I'm a visual learner btw"],
      "vid3": ["teach me bby", "What does a dih looks like bby?", "Check my profil if you like me bby"],
    }
    import ocr_local as O
    for v, exp in EXP.items():
        p = f"/sessions/eager-youthful-thompson/mnt/uploads/{v}.mov"
        loc = O.analyze_video_local(p)
        ref, _ = A._auto_refine_uncertain(p, loc)
        got = [l["text"] for l in ref]
        for e in exp:
            check(f"{v}: contient «{e}»", any(e.lower() in g.lower() for g in got), " | ".join(got))
else:
    print("… (test 9-textes Vision ignoré : définir REAL_VISION_KEY=sk-... pour l'exécuter)")

print("\n" + ("✅ TOUS LES TESTS PASSENT" if not FAILS else f"❌ ÉCHECS: {FAILS}"))
sys.exit(1 if FAILS else 0)
