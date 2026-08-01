#!/usr/bin/env python3
"""
test_auto_ocr.py — Mode AUTO : fallback Vision CIBLÉ + SÉCURITÉ fail-safe.

Prouve :
  • aucun appel Vision quand le local est fiable ;
  • Vision UNIQUEMENT pour les captions incertaines, 1 appel/vidéo B ;
  • timings/position/géométrie inchangés (Vision ne fait que retranscrire) ;
  • SÉCURITÉ : si Vision ne peut pas corriger l'incertain (clé absente,
    erreur/timeout, réponse vide/invalide, décompte incohérent) → ok=False,
    l'API renvoie une ERREUR, aucun rendu, AUCUN cache ;
  • Auto + Vision réussi → fonctionnement normal, résultat mis en cache ;
  • Local sans clé → fonctionnement local normal (aucune erreur) ;
  • Vision direct en mode Vision ;
  • (si REAL_VISION_KEY) les 9 textes attendus sur vid1/vid2/vid3.

Usage : DATA_DIR=<persistant> python3 test_auto_ocr.py
"""
import os, sys, json, tempfile, types, warnings, subprocess
warnings.filterwarnings("ignore")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

FAILS = []
def check(name, cond, extra=""):
    print(("✅" if cond else "❌") + f" {name}" + (f"  {extra}" if extra else ""))
    if not cond: FAILS.append(name)

import app as A

CALLS = {"n": 0, "images": 0}
def fake_anthropic(*, mapping=None, raw=None, raise_exc=False):
    """Installe un module 'anthropic' factice au comportement contrôlé."""
    CALLS["n"] = 0; CALLS["images"] = 0
    class _Msg:
        def __init__(self, txt): self.content = [types.SimpleNamespace(text=txt)]
    class _Messages:
        def create(self, model, max_tokens, messages):
            CALLS["n"] += 1
            CALLS["images"] += sum(1 for c in messages[0]["content"] if c.get("type") == "image")
            if raise_exc: raise RuntimeError("timeout Vision simulé")
            if raw is not None: return _Msg(raw)
            return _Msg(json.dumps([{"i": i, "text": t} for i, t in mapping.items()]))
    class Anthropic:
        def __init__(self, api_key=None): self.messages = _Messages()
    sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=Anthropic)

def line(text, uncertain, cy=0.66, st=0.0, en=2.0):
    return {"text": text, "start_time": st, "end_time": en, "cx_pct": 0.5, "cy_pct": cy,
            "width_pct": 0.75, "fontsize_pct": 0.03, "align": "center", "bold": True,
            "color": "white", "_conf": 80.0, "_uncertain": uncertain}

VID = "/sessions/eager-youthful-thompson/mnt/uploads/vid1.mov"
HAVE_VID = os.path.exists(VID)
VP = VID if HAVE_VID else "x.mp4"

# ── A) _auto_refine_uncertain : succès & tous les échecs ──────────────
os.environ.pop("ANTHROPIC_API_KEY", None)

# aucune caption incertaine → succès, rien à faire
out, ok, why = A._auto_refine_uncertain(VP, [line("a", False), line("b", False)])
check("aucun incertain → ok, inchangé", ok and why is None and [l["text"] for l in out] == ["a", "b"])

# incertain + clé absente → ÉCHEC no_key (jamais continuer en silence)
out, ok, why = A._auto_refine_uncertain(VP, [line("a", False), line("douteux", True)])
check("incertain + pas de clé → ok=False (no_key)", (ok is False) and why == "no_key")
check("incertain + pas de clé → texte local NON modifié", out[1]["text"] == "douteux")

if HAVE_VID:
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    # succès Vision → dès qu'UNE caption est incertaine, TOUTES sont
    # retranscrites dans le MÊME appel unique (corrige aussi une lecture
    # locale « sûre » mais fausse). 1 appel, N images, timings gardés.
    fake_anthropic(mapping={0: "FIABLE VISION", 1: "CORRIGE PAR VISION"})
    lines = [line("fiable", False), line("douteuse", True, st=2.0, en=4.0)]
    out, ok, why = A._auto_refine_uncertain(VID, lines)
    check("succès Vision → ok=True", ok and why is None)
    check("succès : 1 appel, TOUTES les captions envoyées", CALLS["n"] == 1 and CALLS["images"] == 2)
    check("succès : toutes les captions corrigées par Vision",
          out[0]["text"] == "FIABLE VISION" and out[1]["text"] == "CORRIGE PAR VISION")
    check("succès : timings/géométrie conservés",
          out[1]["start_time"] == 2.0 and out[1]["end_time"] == 4.0 and out[1]["cy_pct"] == 0.66)

    # erreur / timeout Vision → ÉCHEC vision_error
    fake_anthropic(raise_exc=True)
    out, ok, why = A._auto_refine_uncertain(VID, [line("x", True)])
    check("timeout/erreur Vision → ok=False (vision_error)", ok is False and why == "vision_error")

    # réponse vide → ÉCHEC vision_empty
    fake_anthropic(raw="")
    out, ok, why = A._auto_refine_uncertain(VID, [line("x", True)])
    check("réponse Vision vide → ok=False", ok is False and why in ("vision_empty", "vision_unusable"))

    # réponse invalide (pas du JSON) → ÉCHEC vision_unusable
    fake_anthropic(raw="désolé je ne peux pas")
    out, ok, why = A._auto_refine_uncertain(VID, [line("x", True)])
    check("réponse Vision invalide → ok=False (vision_unusable)", ok is False and why == "vision_unusable")

    # décompte incohérent (2 envoyées, 1 renvoyée) → ÉCHEC vision_incomplete
    fake_anthropic(mapping={0: "seulement la première"})
    out, ok, why = A._auto_refine_uncertain(VID, [line("u1", True), line("u2", True)])
    check("décompte incohérent → ok=False (vision_incomplete)", ok is False and why == "vision_incomplete")
    check("échec → aucune ligne modifiée", [l["text"] for l in out] == ["u1", "u2"])

    # ── B) recadrage réel contient du texte ──
    import ocr_local as O
    with tempfile.TemporaryDirectory() as td:
        cp = os.path.join(td, "c.png")
        probe = {"start_time": 1.5, "end_time": 2.5, "cy_pct": 0.66, "fontsize_pct": 0.03}
        okc = A._crop_caption_frame(VID, probe, cp) and os.path.exists(cp)
        has_text = okc and any(O._has_usable_text(l["text"]) for l in O._ocr_frame(cp)[0])
        check("recadrage caption réel contient du texte", bool(has_text))
else:
    print("… vid1.mov absent — tests Vision unitaires ignorés")

# ── C) strip meta ──
s = A._strip_ocr_meta([line("x", True)])
check("strip meta : aucune clé _ interne", all(not k.startswith("_") for k in s[0]))

# ── D) Routage /batch_detect (OCR local MOCKÉ pour la vitesse) ─────────
BID = "autotest_" + os.urandom(3).hex()
bdir = A.BATCH_DIR / BID
(bdir / "B").mkdir(parents=True, exist_ok=True)
# couleur ALÉATOIRE → hash de contenu unique par run (pas de collision avec le
# cache OCR persistant d'une exécution précédente).
_rndcol = "0x%06x" % (int.from_bytes(os.urandom(3), "big"))
subprocess.run(["ffmpeg","-y","-f","lavfi","-i",f"color=c={_rndcol}:s=360x640:d=1","-c:v","libx264",
                "-pix_fmt","yuv420p", str(bdir/"B"/"00.mp4"),"-loglevel","error"], check=True)
client = A.app.test_client()
with client.session_transaction() as s2: s2["user_id"] = 1
CANNED = [line("fiable", False), line("douteuse", True)]
A._analyze_local_engine = lambda p: [dict(x) for x in CANNED]
A._analyze_local_engine_meta = lambda p: ([dict(x) for x in CANNED], {"needs_vision": True})

def cache_rows():
    with A._users_db() as conn:
        return conn.execute("SELECT COUNT(*) FROM ocr_cache").fetchone()[0]

# Local sans clé → normal (aucune erreur), aucun Vision
os.environ.pop("ANTHROPIC_API_KEY", None)
r = client.post("/batch_detect", data={"batch_id": BID, "b_index": "0", "ocr_mode": "local", "ignore_cache": "1"})
check("Local sans clé : 200 (fonctionnement local normal)", r.status_code == 200 and "error" not in r.get_json())

# Auto + incertain + clé ABSENTE → erreur 502, aucun cache
before = cache_rows()
r = client.post("/batch_detect", data={"batch_id": BID, "b_index": "0", "ocr_mode": "auto", "ignore_cache": "1"})
d = r.get_json()
check("Auto + pas de clé : erreur visible", r.status_code == 502 and A._AUTO_VISION_FAIL_MSG in d.get("error", ""))
check("Auto échec : AUCUN cache écrit", cache_rows() == before)

# Auto + Vision qui échoue (spy ok=False) → erreur 502, aucun cache
os.environ["ANTHROPIC_API_KEY"] = "test-key"
A._auto_refine_uncertain = lambda video, lines, model=A.OCR_MODEL_SONNET: (lines, False, "vision_error")
before = cache_rows()
r = client.post("/batch_detect", data={"batch_id": BID, "b_index": "0", "ocr_mode": "auto", "ignore_cache": "1"})
check("Auto + Vision en échec : erreur 502", r.status_code == 502)
check("Auto en échec : toujours aucun cache", cache_rows() == before)
# 2e tentative → toujours erreur (pas de faux succès mis en cache)
r = client.post("/batch_detect", data={"batch_id": BID, "b_index": "0", "ocr_mode": "auto"})
check("Auto échec : 2e appel encore en erreur (pas de cache de succès)", r.status_code == 502)

# Auto + Vision réussi (spy) → 200 hybride, corrigé, mis en cache
def spy_ok(video, lines, model=A.OCR_MODEL_SONNET):
    out = [dict(x) for x in lines]
    for l in out:
        if l.get("_uncertain"): l["text"] = "VISION_" + l["text"]
    return out, True, None
A._auto_refine_uncertain = spy_ok
r = client.post("/batch_detect", data={"batch_id": BID, "b_index": "0", "ocr_mode": "auto", "ignore_cache": "1"})
d = r.get_json(); txts = [l["text"] for l in d.get("lines", [])]
check("Auto succès : 200", r.status_code == 200 and "error" not in d)
check("Auto succès : fiable inchangée + incertaine corrigée",
      "fiable" in txts and "VISION_douteuse" in txts)
check("Auto succès : aucune clé _ ne fuit", all(not k.startswith("_") for l in d["lines"] for k in l))
# cache réutilisé (un seul OCR/Vision par B)
r2 = client.post("/batch_detect", data={"batch_id": BID, "b_index": "0", "ocr_mode": "auto"})
check("Auto succès : 2e appel même B servi par cache", r2.get_json().get("source") == "cache")

# Vision explicite → Vision direct
A.analyze_with_claude_vision_timed = lambda p, model=A.OCR_MODEL_SONNET: ([line("VISION DIRECT", False)], 0)
r3 = client.post("/batch_detect", data={"batch_id": BID, "b_index": "0", "ocr_mode": "sonnet", "ignore_cache": "1"})
d3 = r3.get_json()
check("Vision : source=vision (direct)", d3.get("source") == "vision")
check("Vision : texte Vision utilisé", any("VISION DIRECT" in l["text"] for l in d3.get("lines", [])))

import shutil; shutil.rmtree(bdir, ignore_errors=True)

# ── E) 9 textes attendus (uniquement avec une VRAIE clé Vision) ───────
REAL_KEY = os.environ.get("REAL_VISION_KEY")
if REAL_KEY and HAVE_VID:
    os.environ["ANTHROPIC_API_KEY"] = REAL_KEY
    if "anthropic" in sys.modules and isinstance(sys.modules["anthropic"], types.SimpleNamespace):
        del sys.modules["anthropic"]
    import importlib, ocr_local as O
    A2 = importlib.reload(A) if False else A
    EXP = {
      "vid1": ["You're tired to be an adult?", "Be my bby then", "and just check my profil"],
      "vid2": ["I have some memory problems", "What does a dihh look like again?", "I'm a visual learner btw"],
      "vid3": ["teach me bby", "What does a dih looks like bby?", "Check my profil if you like me bby"],
    }
    for v, exp in EXP.items():
        p = f"/sessions/eager-youthful-thompson/mnt/uploads/{v}.mov"
        loc = O.analyze_video_local(p)
        ref, ok, why = A._auto_refine_uncertain(p, loc)
        check(f"{v}: Vision OK", ok is True)
        got = [l["text"] for l in ref]
        for e in exp:
            check(f"{v}: contient «{e}»", any(e.lower() in g.lower() for g in got), " | ".join(got))
else:
    print("… (test 9-textes réels ignoré : définir REAL_VISION_KEY=sk-... pour l'exécuter)")

print("\n" + ("✅ TOUS LES TESTS PASSENT" if not FAILS else f"❌ ÉCHECS: {FAILS}"))
sys.exit(1 if FAILS else 0)
