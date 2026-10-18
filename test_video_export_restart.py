#!/usr/bin/env python3
"""
test_video_export_restart.py — Redémarrage RÉEL pendant un encodage (Gunicorn).

Lance Gunicorn avec les options du Dockerfile, démarre un batch, puis :
  1. tue Gunicorn (kill -9 master + worker) en plein encodage en laissant
     FFmpeg vivant (process orphelin) puis redémarre : jamais 2 FFmpeg
     simultanés, reprise automatique SANS qu'aucun client ne poll ;
  2. tue TOUT (Gunicorn + FFmpeg, comme l'OOM Render) puis redémarre :
     la vidéo interrompue est détectée, reprise, aucune vidéo encodée deux
     fois, statut + ZIP OK après redémarrage (« Aucun fichier valide ») ;
  3. 2 workers Gunicorn + 2 utilisateurs en parallèle : 1 seul FFmpeg ;
  4. kill -9 PENDANT la création d'un ZIP : reprise, aucun ZIP partiel servi.

Usage : python3 test_video_export_restart.py   (gunicorn + requests requis)
"""
import os, sys, json, time, signal, socket, subprocess, tempfile, threading, zipfile, io, re, shutil

REPO = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="vexport_restart_")
EXPORT = os.path.join(DATA, "videobot_video_exports")
ADMIN = ("admin-restart@local.test", "admin-restart-password")
USER2 = ("user2-restart@local.test", "user2-restart-password")
ENV = dict(os.environ, DATA_DIR=DATA, ADMIN_EMAIL=ADMIN[0], ADMIN_PASSWORD=ADMIN[1],
           SECRET_KEY="test-restart-secret", VEXPORT_FFMPEG_THREADS="2", PYTHONUNBUFFERED="1")

try:
    import requests, gunicorn  # noqa: F401
except ImportError as e:
    print(f"⚠️  test NON exécuté : dépendance manquante ({e})"); sys.exit(2)

PASS = 0; FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"✅ {name}")
    else:    FAIL += 1; print(f"❌ {name}   {extra}")

def ps():
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="], capture_output=True, text=True).stdout
    rows = []
    for l in out.splitlines():
        parts = l.strip().split(None, 2)
        if len(parts) == 3:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows

def ffmpeg_pids():
    return [p for p, _, cmd in ps() if cmd.startswith("ffmpeg ") and EXPORT in cmd and "-progress" in cmd]

class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True); self.max = 0; self.stop_ev = threading.Event()
    def run(self):
        while not self.stop_ev.is_set():
            self.max = max(self.max, len(ffmpeg_pids())); time.sleep(0.05)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

LOGS = []
def start_server(workers=1):
    port = free_port()
    log = open(os.path.join(DATA, f"gunicorn_{len(LOGS)}.log"), "w")
    LOGS.append(log.name)
    proc = subprocess.Popen([sys.executable, "-m", "gunicorn", "app:app", "--bind", f"127.0.0.1:{port}",
                             "--workers", str(workers), "--threads", "4", "--timeout", "0",
                             "--keep-alive", "65", "--log-level", "warning"],
                            cwd=REPO, env=ENV, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            if requests.get(base + "/login", timeout=1).status_code == 200:
                return proc, base
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("Gunicorn ne démarre pas : " + open(log.name).read()[-2000:])

def login(base, cred):
    s = requests.Session()
    s.post(base + "/login", data={"email": cred[0], "password": cred[1]}, timeout=10)
    return s

def gunicorn_pids(proc):
    return [proc.pid] + [p for p, pp, _ in ps() if pp == proc.pid]

def mkvid(path, w, h, dur, noise=True):
    src = f"testsrc2=s={w}x{h}:d={dur}:r=30" + (",noise=alls=25:allf=t+u" if noise else "")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", src, "-f", "lavfi", "-i", f"sine=duration={dur}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    path, "-loglevel", "error"], check=True)

def state(bid):
    try:
        return json.load(open(os.path.join(EXPORT, bid, "state.json")))
    except Exception:
        return None

def wait_disk(bid, cond, timeout=300):
    """Attend une condition en lisant UNIQUEMENT le disque (aucun poll HTTP)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = state(bid)
        if st and cond(st):
            return st
        time.sleep(0.2)
    return state(bid)

def new_batch(sess, base, srcs):
    bid = sess.post(base + "/vexport_create").json()["batch_id"]
    for i, p in enumerate(srcs):
        with open(p, "rb") as fh:
            r = sess.post(base + "/vexport_upload", data={"batch_id": bid}, files={"file": (f"v{i}.mp4", fh)}).json()
            assert r.get("ok"), r
    assert sess.post(base + "/vexport_start", data={"batch_id": bid}).json().get("ok")
    return bid

def processing_item(st, min_pct_file=None):
    return next((it for it in st["items"] if it["status"] == "processing"), None)

def live_pct():
    try:
        return json.load(open(os.path.join(EXPORT, ".current.json"))).get("pct", 0)
    except Exception:
        return 0

# Second utilisateur (même base SQLite que le serveur), sans lancer d'ordonnanceur ici.
os.environ.update(DATA_DIR=DATA, ADMIN_EMAIL=ADMIN[0], ADMIN_PASSWORD=ADMIN[1], VEXPORT_WORKER="0")
sys.path.insert(0, REPO)
import app as A  # noqa: E402
A.create_user(*USER2)

src_dir = tempfile.mkdtemp(prefix="vexport_restart_src_")
long_v = os.path.join(src_dir, "long.mp4"); mkvid(long_v, 1080, 1920, 8.0)
short_v = os.path.join(src_dir, "short.mp4"); mkvid(short_v, 1080, 1920, 2.0)
sampler = Sampler(); sampler.start()

# ══ 1. kill -9 de Gunicorn seul (FFmpeg orphelin) ═════════════════════
proc, base = start_server(1)
s1 = login(base, ADMIN)
check("login + page principale", s1.get(base + "/").status_code == 200)
bid = new_batch(s1, base, [long_v] * 4)
st = wait_disk(bid, lambda st: processing_item(st) and live_pct() >= 30, 120)
it1 = processing_item(st)
check("1. une vidéo est en cours d'encodage (≥ 30 %)", it1 is not None)
for p in gunicorn_pids(proc):
    try: os.kill(p, signal.SIGKILL)
    except ProcessLookupError: pass
time.sleep(0.3)
orphans = ffmpeg_pids()
print(f"   ℹ️  FFmpeg encore vivant après kill -9 de Gunicorn : {bool(orphans)}")
st_dead = state(bid)
check("1. après le crash : la vidéo reste « processing » sur le disque",
      next(x for x in st_dead["items"] if x["idx"] == it1["idx"])["status"] == "processing")
proc2, base2 = start_server(1)                       # redémarrage immédiat
st = wait_disk(bid, lambda st: next(x for x in st["items"] if x["idx"] == it1["idx"])["status"] == "done", 180)
x1 = next(x for x in st["items"] if x["idx"] == it1["idx"])
check("1. vidéo interrompue reprise automatiquement (aucun poll client)", x1["status"] == "done", str(x1))
check("1. jamais 2 FFmpeg simultanés malgré l'orphelin", sampler.max == 1, f"max={sampler.max}")

# ══ 2. kill -9 de TOUT (Gunicorn + FFmpeg) = OOM Render ═══════════════
st = wait_disk(bid, lambda st: processing_item(st) is not None
               and processing_item(st)["idx"] != it1["idx"] and live_pct() >= 30, 120)
it2 = processing_item(st)
check("2. une autre vidéo est en cours d'encodage", it2 is not None)
os.killpg(proc2.pid, signal.SIGKILL)
time.sleep(0.5)
check("2. OOM simulé : plus aucun process FFmpeg", ffmpeg_pids() == [], str(ffmpeg_pids()))
check("2. état disque : vidéo bloquée « processing »",
      next(x for x in state(bid)["items"] if x["idx"] == it2["idx"])["status"] == "processing")
proc3, base3 = start_server(1)
st = wait_disk(bid, lambda st: all(x["status"] in ("done", "error") for x in st["items"]), 300)
x2 = next(x for x in st["items"] if x["idx"] == it2["idx"])
check("2. batch terminé après redémarrage, sans aucun poll client",
      all(x["status"] == "done" for x in st["items"]), str([(x["idx"], x["status"], x.get("error")) for x in st["items"]]))
check("2. vidéo interrompue : 2 tentatives, 1 interruption, Terminée",
      x2["status"] == "done" and x2.get("attempts") == 2 and x2.get("interrupted") == 1, str(x2))
check("2. interruption comptée au niveau du batch, pas de pause", st.get("interruptions", 0) >= 1 and not st.get("paused"))
outs = sorted(os.listdir(os.path.join(EXPORT, bid, "output")))
check("2. aucun doublon ni fichier partiel en sortie (MP4 archivés supprimés au fil de l'eau)",
      set(outs) <= {x["out_name"] for x in st["items"]} and not any(o.endswith(".part") for o in outs), str(outs))
check("2. sources supprimées après succès", os.listdir(os.path.join(EXPORT, bid, "input")) == [])
logs = "".join(open(l).read() for l in LOGS)
dones = re.findall(rf"batch={bid[:8]} idx=(\d+) done", logs)
check("2. aucune vidéo encodée deux fois avec succès", len(dones) == len(set(dones)) and len(set(dones)) <= 4, str(dones))
# Le client (même cookie : SECRET_KEY stable) retrouve son batch après redémarrage.
d = s1.get(base3 + f"/vexport_status/{bid}").json()
check("2. statut après redémarrage : terminé, 100 %", d["progress"]["finished"] and d["progress"]["percent"] == 100, str(d["progress"]))
st = wait_disk(bid, lambda st: (st.get("zips") or {}).get("0", {}).get("status") == "ready", 60)
check("2. ZIP du groupe créé automatiquement après redémarrage (1 seule création)",
      st["zips"]["0"]["status"] == "ready" and st["zips"]["0"]["builds"] == 1, str(st.get("zips")))
rz = s1.get(base3 + f"/vexport_group_zip/{bid}/1")
names = zipfile.ZipFile(io.BytesIO(rz.content)).namelist() if rz.status_code == 200 else []
check("2. ZIP après redémarrage : 4 vidéos (pas de « Aucun fichier valide »)", len(names) == 4, f"{rz.status_code} {rz.text[:120] if rz.status_code != 200 else ''}")
s1.close()                                           # keep-alive ouvert = 30 s de grâce Gunicorn
os.killpg(proc3.pid, signal.SIGTERM); proc3.wait(timeout=45)

# ══ 3. 2 workers Gunicorn + 2 utilisateurs simultanés ═════════════════
proc4, base4 = start_server(2)
u1 = login(base4, ADMIN); u2 = login(base4, USER2)
check("3. 2 workers Gunicorn actifs", len(gunicorn_pids(proc4)) == 3, str(gunicorn_pids(proc4)))
res = {}
def run(name, sess):
    res[name] = new_batch(sess, base4, [short_v] * 3)
ts = [threading.Thread(target=run, args=("u1", u1)), threading.Thread(target=run, args=("u2", u2))]
[t.start() for t in ts]; [t.join() for t in ts]
fin = {}
for name, sess in (("u1", u1), ("u2", u2)):
    deadline = time.time() + 180
    while time.time() < deadline:
        d = sess.get(base4 + f"/vexport_status/{res[name]}").json()
        if d["progress"]["finished"]:
            fin[name] = d; break
        time.sleep(0.5)
check("3. les 2 batchs (2 utilisateurs) terminés", all(fin.get(n, {}).get("progress", {}).get("done") == 3 for n in ("u1", "u2")))
check("3. isolation inter-workers : u2 ne voit pas le batch de u1",
      u2.get(base4 + f"/vexport_status/{res['u1']}").status_code == 403)
u1.close(); u2.close()
time.sleep(1.0)
check("3. ZIP déjà valide retrouvé après redémarrage, jamais recréé", state(bid)["zips"]["0"]["builds"] == 1
      and state(bid)["zips"]["0"]["status"] == "ready", str(state(bid).get("zips")))
os.killpg(proc4.pid, signal.SIGTERM); proc4.wait(timeout=45)

# ══ 4. kill -9 PENDANT la création d'un ZIP (archive de ~1,2 Go) ══════
big = A.uuid.uuid4().hex
jd = os.path.join(EXPORT, big)
os.makedirs(os.path.join(jd, "input")); os.makedirs(os.path.join(jd, "output"))
items = []
for i in range(2):
    with open(os.path.join(jd, "output", f"gros{i}.mp4"), "wb") as f:
        for _ in range(600):
            f.write(os.urandom(1 << 20))
    items.append({"idx": i, "orig_name": f"gros{i}.mp4", "in_name": f"{i:04d}.mp4", "out_name": f"gros{i}.mp4",
                  "status": "done", "error": "", "in_size": 0, "out_size": 600 << 20, "attempts": 1})
json.dump({"batch_id": big, "user_id": 1, "created": time.time(), "updated": time.time(), "started": True,
           "items": items}, open(os.path.join(jd, "state.json"), "w"))
part = os.path.join(jd, "zips", "Batch_001.zip.part")
proc5, base5 = start_server(1)
deadline = time.time() + 60
while time.time() < deadline and not (os.path.exists(part) and os.path.getsize(part) > (150 << 20)):
    time.sleep(0.02)
caught = os.path.exists(part) and os.path.getsize(part) > (150 << 20)
os.killpg(proc5.pid, signal.SIGKILL)
time.sleep(0.5)
check("4. kill -9 pendant l'écriture du ZIP (.part partiel présent)", caught and os.path.exists(part),
      f"part={os.path.exists(part) and os.path.getsize(part)}")
z_crash = state(big)["zips"]["0"]
check("4. après le crash : archivage marqué en cours, aucun ZIP final exposé",
      (z_crash.get("busy") or z_crash.get("status") == "building") and not os.path.exists(part[:-5]), str(z_crash))
proc6, base6 = start_server(1)
wpid = [p for p in gunicorn_pids(proc6) if p != proc6.pid][0]
rss_peak = 0
deadline = time.time() + 240
while time.time() < deadline and (state(big).get("zips") or {}).get("0", {}).get("status") != "ready":
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(wpid)], capture_output=True, text=True).stdout.strip()
    rss_peak = max(rss_peak, int(out or 0))
    time.sleep(0.05)
z = state(big)["zips"]["0"]
check("4. création ZIP interrompue reprise automatiquement (aucun poll client)", z["status"] == "ready", str(z))
check("4. ajout interrompu annulé puis archive finalisée une seule fois", z.get("builds") == 1, str(z.get("builds")))
check("4. .part interrompu supprimé, ZIP final unique", not os.path.exists(part)
      and os.listdir(os.path.join(jd, "zips")) == ["Batch_001.zip"])
with zipfile.ZipFile(part[:-5]) as zf:
    check("4. ZIP reconstruit intègre (CRC des 1,2 Go) et complet", zf.testzip() is None
          and [(i.filename, i.file_size) for i in zf.infolist()] == [("gros0.mp4", 600 << 20), ("gros1.mp4", 600 << 20)])
print(f"   ℹ️  RSS max du worker Gunicorn pendant la création d'un ZIP de 1,2 Go : {rss_peak / 1024:.0f} Mo")
check("4. mémoire bornée pendant la création du ZIP (worker < 300 Mo)", 0 < rss_peak < 300 * 1024, f"{rss_peak} Ko")
s6 = login(base6, ADMIN)
h = s6.head(base6 + f"/vexport_group_zip/{big}/1")
check("4. ZIP téléchargeable après reprise", h.status_code == 200 and int(h.headers.get("Content-Length", 0)) == z["size"])
check("4. autre utilisateur : 403 sur ce ZIP", login(base6, USER2).head(base6 + f"/vexport_group_zip/{big}/1").status_code == 403)
s6.close()
os.killpg(proc6.pid, signal.SIGTERM); proc6.wait(timeout=45)

sampler.stop_ev.set(); sampler.join(timeout=2)
time.sleep(0.5)
check("Limite globale : max 1 FFmpeg simultané sur tout le test (crashs + 2 workers)", sampler.max == 1, f"max={sampler.max}")
check("Aucun FFmpeg orphelin à la fin", ffmpeg_pids() == [], str(ffmpeg_pids()))
shutil.rmtree(src_dir, ignore_errors=True); shutil.rmtree(DATA, ignore_errors=True)
print(f"\n{'='*40}\n{PASS} tests OK / {FAIL} KO")
sys.exit(0 if FAIL == 0 else 1)
