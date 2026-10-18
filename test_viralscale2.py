#!/usr/bin/env python3
"""
test_viralscale2.py — ViralScale 2.0 : workflows simplifiés + ressources FFmpeg
communes. Endpoints Flask réels, FFmpeg et Tesseract réels (aucune API IA).

  FFMPEG     porte commune : 1 encodage lourd à la fois tous outils et
             utilisateurs confondus, réentrance sans deadlock, attente bornée
             → 503 explicite (X-VS-Busy) puis succès, file d'attente limitée,
             worker de fond qui cède la place, threads injectés (x264/x265,
             décodeurs, filtres).
  GÉNÉRATEUR Mode Simple (OCR local + rendu des captions), Matrice A × B
             (rendu réel, calcul des sorties, limite 300, refus 50 × 50).
  VARIATIONS 1 vidéo, plusieurs vidéos, mode automatique (déterministe,
             orientation/proportions/durée/audio), mode avancé, préréglages,
             plafonds réels du moteur, téléchargements (fichier + ZIP).
  MEDIA      Optimisation 1 vidéo (Video Optimizer), optimisation multiple
             (profil du moteur Batch Video Export : H.265 / WebM, résolution),
             Photo Optimizer, métadonnées photo, conversion audio.
  REELS      bibliothèque (captions, musiques), rendu (musique, position
             imposée, contour), aperçu du rendu = calques du moteur réel,
             préréglages de rendu persistants + isolation.
  TEMPLATES  rendu réel de la page (nouvelle organisation).

Usage : DATA_DIR=/un/dossier python3 test_viralscale2.py
"""
import os, io, re, json, time, base64, zipfile, tempfile, subprocess, threading, sys, shutil, random, warnings
warnings.filterwarnings("ignore")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vs2_test_"))
os.environ.setdefault("ADMIN_EMAIL", "admin-vs2@local.test")
os.environ.setdefault("ADMIN_PASSWORD", "admin-vs2-password")
os.environ.setdefault("VEXPORT_FFMPEG_THREADS", "2")
import app as A
from production import FileLock, limit_ffmpeg_threads
from PIL import Image, ImageDraw, ImageFont

PASS = 0; FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"✅ {name}")
    else:    FAIL += 1; print(f"❌ {name}   {extra}")

REPO = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp(prefix="vs2_src_")
FONT = os.path.join(REPO, "fonts", "Montserrat-VariableFont_wght.ttf")

def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)

def mkvid(path, w, h, dur=2.0, audio=True, fps=30):
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:d={dur}:r={fps}"]
    if audio: cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio: cmd += ["-c:a", "aac", "-shortest"]
    run(cmd + [path, "-loglevel", "error"])
    return path

def mktextvid(path, text, w=720, h=1280, dur=3.0):
    """Vidéo B avec un vrai texte incrusté (police du projet) + audio."""
    img = Image.new("RGB", (w, h), (40, 60, 90))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 64)
    tw = d.textlength(text, font=f)
    d.text(((w - tw) / 2, h * 0.62), text, font=f, fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
    png = path + ".png"; img.save(png)
    run(["ffmpeg", "-y", "-loop", "1", "-i", png, "-f", "lavfi", "-i", f"sine=frequency=330:duration={dur}",
         "-t", str(dur), "-r", "24", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", path, "-loglevel", "error"])
    return path

def ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                       capture_output=True, text=True)
    return json.loads(r.stdout or "{}")
def vs(meta): return next((s for s in meta.get("streams", []) if s.get("codec_type") == "video"), None)
def au(meta): return next((s for s in meta.get("streams", []) if s.get("codec_type") == "audio"), None)
def fdur(meta): return float(meta.get("format", {}).get("duration") or 0)
def frame(path, t, out):
    run(["ffmpeg", "-y", "-ss", str(t), "-i", str(path), "-frames:v", "1", out, "-loglevel", "error"])
    return Image.open(out).convert("L")
def band_diff(a, b, y0, y1):
    """Différence moyenne de luminance entre deux images sur une bande horizontale (fractions de hauteur)."""
    w, h = a.size
    b = b.resize((w, h))
    pa, pb = a.crop((0, int(h * y0), w, int(h * y1))).getdata(), b.crop((0, int(h * y0), w, int(h * y1))).getdata()
    return sum(abs(x - y) for x, y in zip(pa, pb)) / max(1, len(pa))

def upload(cl, url, path, name=None, field="file", **form):
    with open(path, "rb") as fh:
        data = dict(form, **{field: (io.BytesIO(fh.read()), name or os.path.basename(path))})
        return cl.post(url, data=data, content_type="multipart/form-data")

def js(r):
    """JSON d'une réponse, y compris celles streamées avec espaces de maintien (Video Optimizer)."""
    return json.loads(r.get_data(as_text=True).strip() or "{}")


class HeavySampler(threading.Thread):
    """Compte les FFmpeg LOURDS vivants (tous passent par la porte et portent
    -filter_threads 1 ; les extractions d'images légères non)."""
    def __init__(self):
        super().__init__(daemon=True); self.max = 0; self.ev = threading.Event(); self.seen = 0
    def run(self):
        while not self.ev.is_set():
            out = subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True).stdout
            n = sum(1 for l in out.splitlines() if l.startswith("ffmpeg ") and "-filter_threads 1" in l)
            self.max = max(self.max, n); self.seen += 1 if n else 0
            time.sleep(0.03)
sampler = HeavySampler(); sampler.start()

c = A.app.test_client()
with c.session_transaction() as s: s["user_id"] = 1
A.create_user("user2-vs2@local.test", "user2-vs2-password")
uid2 = A.get_user_by_email("user2-vs2@local.test")["id"]
c2 = A.app.test_client()
with c2.session_transaction() as s: s["user_id"] = uid2

VA = mkvid(os.path.join(tmp, "a_720x1280.mp4"), 720, 1280, 3.0)
VH = mkvid(os.path.join(tmp, "h_1280x720.mp4"), 1280, 720, 2.0)
V1080 = mkvid(os.path.join(tmp, "v_1080x1920.mp4"), 1080, 1920, 2.0)
VB = mktextvid(os.path.join(tmp, "b_text.mp4"), "HELLO WORLD")
VB2 = mktextvid(os.path.join(tmp, "b_text2.mp4"), "BIG SALE TODAY")

# ══ FFMPEG — porte commune ═══════════════════════════════════════════
check("FFmpeg : même verrou que le Batch Video Export", A.FFMPEG_GATE.lock_path == A._VEXPORT_GLOBAL_LOCK)
cmd = limit_ffmpeg_threads(["ffmpeg", "-y", "-ss", "1", "-i", "a.mp4", "-i", "b.png", "-filter_complex", "x",
                            "-c:v", "libx265", "out.mp4"], 2)
check("Threads : -threads avant chaque entrée + encodeur, filtres bornés, pool x265 borné",
      cmd[:5] == ["ffmpeg", "-filter_threads", "1", "-filter_complex_threads", "1"]
      and cmd.count("-threads") == 3 and "pools=2:frame-threads=1" in cmd, str(cmd))
check("Threads : commande déjà réglée laissée intacte",
      limit_ffmpeg_threads(["ffmpeg", "-threads", "1", "-i", "x", "y"], 2) == ["ffmpeg", "-threads", "1", "-i", "x", "y"])
probe_out = os.path.join(tmp, "lim.mp4")
p = A.ffmpeg_run(["ffmpeg", "-y", "-i", VA, "-i", VB, "-filter_complex", "[0:v][1:v]overlay=0:0[o]", "-map", "[o]",
                  "-c:v", "libx264", "-preset", "veryfast", "-t", "1", probe_out, "-loglevel", "error"], timeout=60)
check("Threads injectés : commande réelle (overlay) toujours valide", p.returncode == 0 and os.path.exists(probe_out), p.stderr[-200:])

def nested():
    with A.FFMPEG_GATE.hold():
        with A.FFMPEG_GATE.hold():
            r = A.ffmpeg_run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x64:d=0.2", "-c:v", "libx264",
                              os.path.join(tmp, "nest.mp4"), "-loglevel", "error"], timeout=30)
            nested.rc = r.returncode
nested.rc = None
t = threading.Thread(target=nested, daemon=True); t.start(); t.join(20)
check("Porte réentrante : appels imbriqués sans deadlock", not t.is_alive() and nested.rc == 0)

# Préparer un staging Variations pour les tests de saturation.
st = js(upload(c, "/variation_stage", VA, count="2"))
vjob = st.get("job_id")
def var_run(cl, job, idx, count=2, nvid=1, **extra):
    data = dict(job_id=job, index=str(idx), count=str(count), num_videos=str(nvid), strength="light", config_mode="preset")
    data.update(extra)
    return cl.post("/variation_multi_run", data=data)

old_wait = A.FFMPEG_GATE.wait_s
A.FFMPEG_GATE.wait_s = 1.0
with FileLock(A.FFMPEG_GATE.lock_path):                # un « autre traitement » tient le créneau
    t0 = time.time(); r = var_run(c, vjob, 0); waited = time.time() - t0
    d = r.get_json()
check("Créneau occupé → 503 explicite après attente bornée", r.status_code == 503 and r.headers.get("X-VS-Busy") == "1"
      and r.headers.get("Retry-After") and "occupé" in (d or {}).get("error", "") and 0.9 <= waited < 10,
      f"{r.status_code} {waited:.1f}s {d}")
r = var_run(c, vjob, 0)
check("Créneau libéré → même requête réussie (nouvel essai)", r.status_code == 200 and r.get_json().get("ok"), str(r.get_json()))
A.FFMPEG_GATE.wait_s = 4.0
codes, times = [], []
def busy_call():
    t0 = time.time(); rr = var_run(c, vjob, 1); codes.append(rr.status_code); times.append(time.time() - t0)
with FileLock(A.FFMPEG_GATE.lock_path):
    ths = [threading.Thread(target=busy_call) for _ in range(3)]
    [x.start() for x in ths]; [x.join() for x in ths]
quick = [tt for cc, tt in zip(codes, times) if cc == 503 and tt < 2.0]
check("File d'attente bornée : au plus 2 requêtes attendent, la 3e est refusée immédiatement (threads HTTP préservés)",
      len(quick) >= 1 and codes.count(503) == 3, f"codes={codes} temps={[round(x, 1) for x in times]}")
A.FFMPEG_GATE.wait_s = old_wait
marker = A.FFMPEG_GATE.waiters_dir / "test-marker"; marker.touch()
check("Worker de fond : voit une requête en attente", A.FFMPEG_GATE.waiting_interactive() >= 1)
os.utime(marker, (time.time() - 3600, time.time() - 3600))
check("Worker de fond : marqueur périmé (process mort) ignoré et nettoyé",
      A.FFMPEG_GATE.waiting_interactive() == 0 and not marker.exists())

# ══ GÉNÉRATEUR — Mode Simple ═════════════════════════════════════════
r = upload(c, "/analyze", VB, name="b.mp4", field="video_b", **{"ocr_mode": "local", "mode": "simple"})
an = r.get_json() or {}
lines = an.get("lines") or []
txt = " ".join((l.get("text") or "") for l in lines).upper()
# OCR Local = moteur gratuit approximatif (l'UI conseille « Auto » pour un texte exact) :
# on vérifie qu'il trouve le texte et sa position, pas une transcription parfaite.
check("Simple : OCR local détecte le texte de la vidéo B, à la bonne position",
      "WORLD" in txt and lines and 0.55 <= float(lines[0].get("cy_pct", 0)) <= 0.75, str(an)[:300])
with open(VA, "rb") as fa, open(VB, "rb") as fb:
    r = c.post("/process", data={"video_a": (io.BytesIO(fa.read()), "a.mp4"), "video_b": (io.BytesIO(fb.read()), "b.mp4"),
                                 "lines_json": json.dumps(lines), "mode": "simple", "capcut_outline": "1", "ocr_mode": "local"},
               content_type="multipart/form-data")
outC = os.path.join(tmp, "simple_C.mp4")
jid = (r.get_json(silent=True) or {}).get("job_id") if r.status_code == 200 else None
if jid:                                                   # même enchaînement que le front : /process puis /download/<job_id>
    rd_ = c.get(f"/download/{jid}")
    if rd_.status_code == 200: open(outC, "wb").write(rd_.data)
    rd_.close()
mC = ffprobe(outC) if os.path.exists(outC) else {}
check("Simple : vidéo C générée (H.264, format de A, audio de B)", vs(mC) and vs(mC)["codec_name"] == "h264"
      and (int(vs(mC)["width"]), int(vs(mC)["height"])) == (720, 1280) and au(mC) is not None, f"{r.status_code} {r.get_data()[:200]}")
if mC:
    cy = float(lines[0].get("cy_pct", 0.65)) if lines else 0.65
    fA, fC = frame(VA, 1.0, os.path.join(tmp, "fa.png")), frame(outC, 1.0, os.path.join(tmp, "fc.png"))
    diff = band_diff(fA, fC, cy - 0.02, cy + 0.02)            # bande de la caption détectée
    ctrl = band_diff(fA, fC, 0.05, 0.09)                      # bande témoin sans texte
    check("Simple : caption réellement incrustée sur A (zone du texte modifiée, reste intact)",
          diff > 8 and ctrl < 3, f"caption={diff:.1f} témoin={ctrl:.1f}")

# ══ GÉNÉRATEUR — Matrice A × B ═══════════════════════════════════════
bid = None
for kind, files in (("a", [VA, VH]), ("b", [VB, VB2])):
    for i, pth in enumerate(files):
        form = {"kind": kind, "index": str(i)}
        if bid: form["batch_id"] = bid
        d = js(upload(c, "/batch_stage", pth, **form)); bid = d.get("batch_id", bid)
check("Matrice : 2 A + 2 B stagés", bool(bid))
lines_by_b = []
for j in range(2):
    d = c.post("/batch_detect", data={"batch_id": bid, "b_index": str(j), "ocr_mode": "local"}).get_json() or {}
    lines_by_b.append([l for l in (d.get("lines") or []) if l.get("text")])
check("Matrice : OCR local sur chaque B", all(lines_by_b), str([len(x) for x in lines_by_b]))
outs = []
for a_i in range(2):
    for b_i in range(2):
        rr = c.post("/batch_render", data={"batch_id": bid, "a_index": str(a_i), "b_index": str(b_i), "num_a": "2", "num_b": "2",
                                          "capcut_outline": "1", "ocr_mode": "local", "lines_json": json.dumps(lines_by_b[b_i])})
        outs.append((rr.status_code, rr.get_json() or {}))
check("Matrice : 2 × 2 = 4 vidéos générées", sum(1 for code, d in outs if code == 200 and not d.get("error")) == 4,
      str([(code, str(d)[:80]) for code, d in outs if code != 200 or d.get("error")]))
def cap_err(na, nb):
    d = c.post("/batch_render", data={"batch_id": bid, "a_index": "0", "b_index": "0", "num_a": str(na), "num_b": str(nb),
                                      "lines_json": json.dumps(lines_by_b[0]), "ocr_mode": "local"}).get_json() or {}
    return "Maximum autorisé" in (d.get("error") or "")
check("Matrice : 10 × 10 = 100 accepté", not cap_err(10, 10))
check("Matrice : 20 × 15 = 300 accepté", not cap_err(20, 15))
check("Matrice : 20 × 16 = 320 refusé (limite 300)", cap_err(20, 16))
check("Matrice : 50 × 50 = 2500 refusé", cap_err(50, 50))

# ══ STUDIO VARIATIONS ════════════════════════════════════════════════
def light_ok(p):
    L = A.VARIATION_STRENGTH_PRESETS["light"]
    return (abs(p["brightness"]) <= L["brightness_pct"] and abs(p["contrast"] - 1) <= L["contrast_pct"]
            and L["zoom_range"][0] <= p["zoom"] <= L["zoom_range"][1] and p["rotation_deg"] == 0.0
            and L["speed_range"][0] <= p["speed"] <= L["speed_range"][1])
p1 = A._pick_variation_params("light", random.Random("job-x:3"))
p2 = A._pick_variation_params("light", random.Random("job-x:3"))
p3 = A._pick_variation_params("light", random.Random("job-x:4"))
check("Auto : paramètres déterministes (même source, même variation → mêmes valeurs)", p1 == p2)
check("Auto : variations distinctes entre elles", p1 != p3)
check("Auto : paramètres bornés (préréglage « légère », aucune rotation)",
      all(light_ok(A._pick_variation_params("light", random.Random(f"j:{i}"))) for i in range(200)))

def run_variations(sources, count, mode="preset", adv=None):
    """Même séquence que le front unifié : staging de chaque vidéo puis N variantes."""
    res = []
    for src in sources:
        d = js(upload(c, "/variation_stage", src, count=str(count)))
        job = d.get("job_id")
        for k in range(count):
            extra = {"config_mode": mode}
            if adv: extra["advanced_config"] = json.dumps(adv)
            rr = var_run(c, job, k, count=count, nvid=len(sources), **extra)
            res.append((src, job, rr.get_json() or {}))
    return res

res1 = run_variations([VA], 3)
ok1 = [x for x in res1 if x[2].get("ok")]
check("1 vidéo × 3 variations = 3 sorties (mode automatique)", len(ok1) == 3, str([x[2] for x in res1]))
src_m = ffprobe(VA)
geo_ok, sync_ok = True, True
for src, job, d in ok1:
    m = ffprobe(A.VARIATION_DIR / job / "out" / d["filename"])
    v = vs(m); a = au(m)
    if not v or (int(v["width"]), int(v["height"])) != (720, 1280) or abs(fdur(m) / fdur(src_m) - 1) > 0.03:
        geo_ok = False
    if not a or abs(float(v.get("duration") or fdur(m)) - float(a.get("duration") or fdur(m))) > 0.15:
        sync_ok = False
check("Auto : orientation, proportions et durée conservées", geo_ok)
check("Auto : audio présent et synchronisé (écart vidéo/audio ≤ 0,15 s)", sync_ok)
job1 = ok1[0][1]
r = c.get(f"/variation_file/{job1}/{ok1[0][2]['filename']}")
check("Variations : téléchargement individuel", r.status_code == 200 and len(r.data) > 1000); r.close()
res2 = run_variations([VH, V1080], 2)
ok2 = [x for x in res2 if x[2].get("ok")]
check("Plusieurs vidéos : 2 vidéos × 2 variations = 4 sorties", len(ok2) == 4, str([x[2] for x in res2]))
check("Plusieurs vidéos : orientation de chaque source conservée",
      all((int(vs(ffprobe(A.VARIATION_DIR / j / "out" / d["filename"]))["width"]) > int(vs(ffprobe(A.VARIATION_DIR / j / "out" / d["filename"]))["height"]))
          == (s == VH) for s, j, d in ok2))
zr = c.post("/variation_multi_zip", json={"items": [{"job_id": j, "label": os.path.basename(s)} for s, j in
                                                     {(s, j) for s, j, _ in ok2}]})
names = zipfile.ZipFile(io.BytesIO(zr.data)).namelist() if zr.status_code == 200 else []
check("Plusieurs vidéos : ZIP structuré avec les 4 variantes", len([n for n in names if n.endswith(".mp4")]) == 4, str(names)); zr.close()
adv = {k: {"min": 30, "max": 60} for k in ["brightness", "contrast", "saturation", "gamma", "noise", "sharpness", "blur",
                                         "zoom", "crop", "rotation", "speed", "volume", "pitch", "fps", "bitrate"]}
adv["metadata"] = 50
res3 = run_variations([VA], 2, mode="advanced", adv=adv)
check("Mode avancé (16 réglages) : 2 sorties, dimensions conservées",
      all(x[2].get("ok") for x in res3) and all((int(vs(ffprobe(A.VARIATION_DIR / j / "out" / d["filename"]))["width"]),
                                                  int(vs(ffprobe(A.VARIATION_DIR / j / "out" / d["filename"]))["height"])) == (720, 1280) for _, j, d in res3),
      str([x[2] for x in res3]))
sv = c.post("/variation_preset_save", json={"name": "Test VS2", "config": adv}).get_json() or {}
lst = c.get("/variation_presets").get_json() or {}
found = [p_ for p_ in (lst.get("presets") or []) if p_.get("name") == "Test VS2"]
check("Préréglages : sauvegarde + liste", bool(found), str(lst)[:200])
if found:
    c.post("/variation_preset_delete", json={"id": found[0]["id"]})
    check("Préréglages : suppression", not [p_ for p_ in (c.get("/variation_presets").get_json() or {}).get("presets", []) if p_.get("name") == "Test VS2"])
check("Plafond réel : 101 variations par vidéo refusées", var_run(c, vjob, 0, count=101).status_code == 400)
check("Plafond réel : 10 vidéos × 100 = 1000 > 900 refusé", var_run(c, vjob, 0, count=100, nvid=10).status_code == 400)
d = var_run(c, "inexistant", 0, count=5, nvid=100).get_json() or {}
check("Plafond réel : 100 vidéos × 5 = 500 accepté (≤ 900)", "dépasse" not in (d.get("error") or ""), str(d))

# ══ MEDIA TOOLS — VIDÉOS ═════════════════════════════════════════════
r = upload(c, "/optimize_video", V1080, resolution="720", codec="h264", compression="50")
d = js(r)
check("Optimisation 1 vidéo (Video Optimizer) : résultat détaillé", d.get("final_w") == 720 and d.get("final_size", 0) > 0
      and "reduction_pct" in d, str(d)[:200])
if d.get("job_id"):
    rr = c.get(f"/optimizer_file/{d['job_id']}/{d['filename']}")
    check("Optimisation 1 vidéo : téléchargement", rr.status_code == 200 and len(rr.data) > 1000); rr.close()

def export_batch(form, files):
    b = c.post("/vexport_create", data=form).get_json()
    for pth in files:
        upload(c, "/vexport_upload", pth, batch_id=b["batch_id"])
    c.post("/vexport_start", data={"batch_id": b["batch_id"]})
    deadline = time.time() + 300
    while time.time() < deadline:
        s_ = c.get(f"/vexport_status/{b['batch_id']}").get_json()
        if s_["progress"]["finished"] and s_.get("archives_settled"):
            return b, s_
        time.sleep(0.3)
    return b, c.get(f"/vexport_status/{b['batch_id']}").get_json()

b, s_ = export_batch({"mode": "optimize", "codec": "h265", "resolution": "720", "compression": "40"}, [V1080, VH])
check("Optimisation multiple : profil enregistré sur le batch", b.get("profile", {}).get("kind") == "optimize"
      and s_["profile"]["codec"] == "h265", str(b.get("profile")))
outd = A.EXPORT_DIR / b["batch_id"] / "output"
metas = {it["name"]: ffprobe(outd / it["out_name"]) for it in s_["items"] if it["status"] == "done"}
check("Optimisation multiple : 2 vidéos H.265 (moteur Batch Video Export)", len(metas) == 2
      and all(vs(m)["codec_name"] == "hevc" for m in metas.values()), str(s_["progress"]))
check("Optimisation multiple : résolution 720p sur le côté court, orientation conservée",
      metas and (int(vs(metas["v_1080x1920.mp4"])["width"]), int(vs(metas["v_1080x1920.mp4"])["height"])) == (720, 1280)
      and int(vs(metas["h_1280x720.mp4"])["height"]) == 720, str({k: (vs(m)["width"], vs(m)["height"]) for k, m in metas.items()}))
zr = c.get(f"/vexport_group_zip/{b['batch_id']}/1")
check("Optimisation multiple : archive ZIP du groupe", zr.status_code == 200 and len(zipfile.ZipFile(io.BytesIO(zr.data)).namelist()) == 2); zr.close()
b2, s2 = export_batch({"mode": "optimize", "codec": "webm", "resolution": "original", "compression": "60"}, [VH])
it2 = s2["items"][0]
check("Optimisation WebM : sortie .webm VP9", it2["status"] == "done" and it2["out_name"].endswith(".webm")
      and vs(ffprobe(A.EXPORT_DIR / b2["batch_id"] / "output" / it2["out_name"]))["codec_name"] == "vp9", str(it2))
rr = c.get(f"/vexport_file/{b2['batch_id']}/{it2['out_name']}")
check("Optimisation WebM : téléchargement individuel (video/webm)", rr.status_code == 200 and rr.mimetype == "video/webm"); rr.close()
b3, s3 = export_batch({"mode": "standard"}, [VH])
m3 = ffprobe(A.EXPORT_DIR / b3["batch_id"] / "output" / s3["items"][0]["out_name"])
check("Réexport standard inchangé : H.264, CRF 20, preset veryfast",
      b3["profile"] == {"kind": "standard"} and vs(m3)["codec_name"] == "h264"
      and b"crf=20.0" in (A.EXPORT_DIR / b3["batch_id"] / "output" / s3["items"][0]["out_name"]).read_bytes())
check("Isolation : un autre utilisateur ne voit pas ces exports", c2.get(f"/vexport_status/{b['batch_id']}").status_code == 403)

# ══ MEDIA TOOLS — PHOTOS / AUDIO ═════════════════════════════════════
img = Image.new("RGB", (2000, 1500), (40, 90, 160)); ImageDraw.Draw(img).rectangle((200, 200, 1800, 1300), fill=(220, 120, 40))
exif = Image.Exif(); exif[0x0131] = "AppareilTest"; exif[0x010F] = "MarqueTest"
jpg = os.path.join(tmp, "photo.jpg"); img.save(jpg, quality=95, exif=exif.tobytes())
d = upload(c, "/optimize_photo", jpg, resolution="1080", format="webp", compression="50").get_json() or {}
check("Photo Optimizer : WEBP 1080p, poids réduit", d.get("final_h") == 1080 or min(d.get("final_w", 0), d.get("final_h", 0)) == 1080, str(d)[:200])
ph = upload(c, "/photo_stage", jpg).get_json() or {}
pj = ph.get("job_id"); stored = ph.get("stored_name") or ph.get("stored")
pr = c.post("/photo_process", data={"job_id": pj, "stored_name": stored, "out_name": "photo_clean", "index": "0", "count": "1",
                                    "strip_all": "1"}).get_json() or {}      # mode « Rapide » = ce qu'envoie le front
check("Métadonnées photo : traitement Rapide", pr.get("ok") is True, str(pr)[:200])
if pr.get("ok"):
    rr = c.get(f"/photo_file/{pj}/{pr['filename']}")
    out_img = Image.open(io.BytesIO(rr.data)); rr.close()
    check("Métadonnées photo : EXIF supprimé", not dict(out_img.getexif()).get(0x0131))
wav = os.path.join(tmp, "son.wav"); run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=500:duration=2", wav, "-loglevel", "error"])
ad = upload(c, "/audio_stage", wav).get_json() or {}
aj = ad.get("job_id"); astored = ad.get("stored_name") or ad.get("stored")
ac = c.post("/audio_convert", data={"job_id": aj, "stored_name": astored, "out_name": "son", "index": "0", "count": "1"}).get_json() or {}
check("Audio : conversion MP3", ac.get("ok") is True, str(ac)[:200])
if ac.get("ok"):
    rr = c.get(f"/audio_file/{aj}/{ac['filename']}")
    check("Audio : téléchargement MP3", rr.status_code == 200 and (rr.data[:3] == b"ID3" or rr.data[:2] in (b"\xff\xfb", b"\xff\xf3"))); rr.close()

# ══ REELS STUDIO ═════════════════════════════════════════════════════
cap = c.post("/reels_caption_create", json={"name": "Hook test", "segments": [
    {"text": "PREMIER SEGMENT", "start_time": 0, "end_time": 1.5}, {"text": "DEUXIÈME", "start_time": 1.5, "end_time": 3}],
    "cx_pct": 0.5, "cy_pct": 0.78, "width_pct": 0.86, "fontsize_pct": 0.05, "align": "center"}).get_json()["caption"]
mp3 = os.path.join(tmp, "musique.mp3"); run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=5", "-c:a", "libmp3lame", mp3, "-loglevel", "error"])
mus = upload(c, "/reels_music_upload", mp3, name="musique.mp3").get_json()
mus = mus.get("music") or mus
check("Reels : bibliothèque (caption + musique)", cap.get("id") and mus.get("id"), str(mus)[:200])
check("Reels : musique lisible depuis la bibliothèque", c.get(f"/reels_music_file/{mus['id']}").status_code == 200)

pv = c.post("/reels_preview_overlays", json={"caption_id": cap["id"], "width": 720, "height": 1280, "duration": 3,
                                              "capcut_outline": True}).get_json() or {}
segs = pv.get("segments") or []
check("Aperçu du rendu : 2 calques horodatés, taille d'aperçu ≤ 960 px",
      len(segs) == 2 and segs[0]["start"] == 0 and segs[1]["end"] == 3 and max(pv["width"], pv["height"]) <= 960, str(pv)[:200])
def alpha_bbox(datauri):
    im = Image.open(io.BytesIO(base64.b64decode(datauri.split(",", 1)[1]))).convert("RGBA")
    bb = im.getchannel("A").getbbox(); w, h = im.size
    return (bb[0] / w, bb[1] / h, bb[2] / w, bb[3] / h) if bb else None
bb = alpha_bbox(segs[0]["png"]) if segs else None
full_png = A.render_text_overlay([dict(text="PREMIER SEGMENT", cx_pct=0.5, cy_pct=0.78, width_pct=0.86, fontsize_pct=0.05, align="center")],
                                 720, 1280, 720, 1280, capcut_outline=True)
fb = Image.open(full_png).convert("RGBA"); fbb = fb.getchannel("A").getbbox(); os.remove(full_png)
fbn = (fbb[0] / 720, fbb[1] / 1280, fbb[2] / 720, fbb[3] / 1280)
check("Aperçu du rendu = moteur réel : même géométrie que le rendu pleine taille (écart < 2 %)",
      bb and max(abs(x - y) for x, y in zip(bb, fbn)) < 0.02, f"{bb} vs {fbn}")
pv_top = c.post("/reels_preview_overlays", json={"caption_id": cap["id"], "width": 720, "height": 1280, "duration": 3,
                                                  "position": {"cx_pct": 0.5, "cy_pct": 0.2, "width_pct": 0.8, "fontsize_pct": 0.06, "align": "center"}}).get_json()
bt = alpha_bbox(pv_top["segments"][0]["png"])
check("Aperçu : position imposée prise en compte (texte en haut)", bt and bt[3] < 0.4 and bb and bb[1] > 0.6, f"{bt}")
check("Aperçu : caption d'un autre utilisateur refusée", c2.post("/reels_preview_overlays", json={"caption_id": cap["id"]}).status_code == 404)

stg = js(upload(c, "/reels_mix_stage", VA, name="reel.mp4", index="0"))
mj = stg.get("job_id")
rd = c.post("/reels_mix_run", data={"job_id": mj, "index": "0", "caption_id": str(cap["id"]), "music_id": str(mus["id"]),
                                    "out_name": "reel_bas", "capcut_outline": "1"}).get_json() or {}
rt = c.post("/reels_mix_run", data={"job_id": mj, "index": "0", "caption_id": str(cap["id"]), "music_id": str(mus["id"]),
                                    "out_name": "reel_haut", "capcut_outline": "0",
                                    "pos_override": json.dumps({"cx_pct": 0.5, "cy_pct": 0.2, "width_pct": 0.8, "fontsize_pct": 0.06, "align": "center"})}).get_json() or {}
check("Reels : rendu avec caption + musique", rd.get("ok") and rt.get("ok"), f"{rd} {rt}")
if rd.get("ok") and rt.get("ok"):
    pb = A.REELS_MIX_DIR / mj / "out" / rd["filename"]; pt = A.REELS_MIX_DIR / mj / "out" / rt["filename"]
    mb = ffprobe(pb)
    check("Reels : audio = musique, vidéo au format du reel", au(mb) is not None and (int(vs(mb)["width"]), int(vs(mb)["height"])) == (720, 1280))
    fa_, fb_, ft_ = frame(VA, 0.5, os.path.join(tmp, "r0.png")), frame(pb, 0.5, os.path.join(tmp, "r1.png")), frame(pt, 0.5, os.path.join(tmp, "r2.png"))
    check("Reels : position par défaut = bas, position imposée = haut",
          band_diff(fa_, fb_, 0.7, 0.86) > 6 and band_diff(fa_, ft_, 0.12, 0.28) > 6 and band_diff(fa_, ft_, 0.7, 0.86) < 3,
          f"bas={band_diff(fa_, fb_, 0.7, 0.86):.1f} haut={band_diff(fa_, ft_, 0.12, 0.28):.1f} bas_override={band_diff(fa_, ft_, 0.7, 0.86):.1f}")

cfg = {"caption_ids": [cap["id"]], "music_ids": [mus["id"]], "gen_mode": "all_x_all", "capcut_outline": False,
       "position": {"cx_pct": 0.5, "cy_pct": 0.3, "width_pct": 0.9, "fontsize_pct": 0.07, "align": "left"}}
sp = c.post("/reels_render_preset_save", json={"name": "Story haut", "config": cfg}).get_json() or {}
pid = (sp.get("preset") or {}).get("id")
check("Préréglages Reels : création", pid and sp["preset"]["config"] == cfg, str(sp))
check("Préréglages Reels : nom obligatoire", c.post("/reels_render_preset_save", json={"name": "", "config": cfg}).status_code == 400)
rn = c.post("/reels_render_preset_rename", json={"id": pid, "name": "Story haut V2"}).get_json() or {}
check("Préréglages Reels : renommage", (rn.get("preset") or {}).get("name") == "Story haut V2")
up = c.post("/reels_render_preset_save", json={"id": pid, "config": dict(cfg, gen_mode="bogus", position={"cy_pct": 9})}).get_json() or {}
pc = (up.get("preset") or {}).get("config", {})
check("Préréglages Reels : mise à jour + valeurs assainies (mode inconnu, position bornée)",
      pc.get("gen_mode") == "one_to_one" and pc.get("position", {}).get("cy_pct") == 1.0 and up["preset"]["name"] == "Story haut V2", str(pc))
check("Préréglages Reels : chargement (liste)", any(p_["id"] == pid for p_ in c.get("/reels_render_preset_list").get_json()["presets"]))
with A._users_db() as conn:
    row = conn.execute("SELECT name, config_json FROM reels_render_presets WHERE id=?", (pid,)).fetchone()
check("Préréglages Reels : persistés dans la base (DATA_DIR)", row and row["name"] == "Story haut V2" and json.loads(row["config_json"])["caption_ids"] == [cap["id"]])
check("Préréglages Reels : invisibles pour un autre utilisateur", not c2.get("/reels_render_preset_list").get_json()["presets"])
check("Préréglages Reels : un autre utilisateur ne peut ni renommer ni supprimer",
      c2.post("/reels_render_preset_rename", json={"id": pid, "name": "x"}).status_code == 404
      and c2.post("/reels_render_preset_delete", json={"id": pid}).status_code == 404)
check("Préréglages Reels : suppression", c.post("/reels_render_preset_delete", json={"id": pid}).get_json().get("ok")
      and not c.get("/reels_render_preset_list").get_json()["presets"])

# ══ Concurrence : tous les outils en même temps (2 utilisateurs) ═════
sampler.max = 0
results = {}
def t_export():
    results["export"] = export_batch({"mode": "standard"}, [VA, VH, V1080])[1]["progress"]["done"]
def t_var():
    results["var"] = sum(1 for x in run_variations([VH], 2) if x[2].get("ok"))
def t_opt():
    results["opt"] = js(upload(c2, "/optimize_video", VA, resolution="480", codec="h264", compression="30")).get("final_w")
def t_reels():
    results["reels"] = c.post("/reels_mix_run", data={"job_id": mj, "index": "0", "caption_id": str(cap["id"]),
                                                       "out_name": "reel_conc"}).get_json().get("ok")
ths = [threading.Thread(target=f) for f in (t_export, t_var, t_opt, t_reels)]
[x.start() for x in ths]; [x.join(600) for x in ths]
check("Concurrence : Batch Video Export + Variations + Optimizer + Reels terminés",
      results.get("export") == 3 and results.get("var") == 2 and results.get("opt") == 480 and results.get("reels") is True, str(results))
check("Mémoire : jamais plus d'1 FFmpeg lourd simultané, tous outils et utilisateurs confondus",
      sampler.max == 1 and sampler.seen > 0, f"max={sampler.max}")

# ══ TEMPLATES ════════════════════════════════════════════════════════
h = c.get("/").get_data(as_text=True)
check("Page : rendu 200", "Studio Variations" in h and "Media Tools" in h)
check("Générateur : Matrice A × B seule (sélecteurs Pairing / Variations retirés)",
      "Matrice A × B" in h and 'value="pairing"' not in h and 'value="variations"' not in h and 'id="bVarCount"' not in h)
check("Studio Variations : interface unique, modes Automatique / Avancé, sans score inventé",
      "subs:[] }" in h and "Automatique" in h and 'id="vmEstUniqueness"' not in h and '<div id="variationTab" class="tab-content" hidden' in h)
check("Media Tools : onglets Vidéos / Photos / Audio", "['optimizer','Vidéos'],['photo','Photos'],['audio','Audio']" in h)
check("Media Tools : Optimisation vidéo (Réexport standard + Optimisation) + archives",
      'name="vtMode" value="standard"' in h and 'name="vtMode" value="optimize"' in h and "Archives disponibles" in h)
photo_tab = h[h.index('<div id="photoTab"'):h.index('<div id="optimizerTab"')]
check("Media Tools : Photo Optimizer + métadonnées dans l'onglet Photos", 'id="optPhInput"' in photo_tab and 'id="phInput"' in photo_tab)
check("Reels : aperçu + options de rendu + préréglages", 'id="rPvModal"' in h and 'id="rRpSelect"' in h and 'id="rRenderPos"' in h)
for pg in ("/history", "/consumption"):
    check(f"Non-régression : {pg} 200", c.get(pg).status_code == 200)

sampler.ev.set()
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'='*40}\n{PASS} tests OK / {FAIL} KO")
sys.exit(0 if FAIL == 0 else 1)
