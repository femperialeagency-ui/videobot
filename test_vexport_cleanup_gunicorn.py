#!/usr/bin/env python3
"""
test_vexport_cleanup_gunicorn.py — Nettoyage automatique avec Gunicorn RÉEL.

  10. 2 workers Gunicorn : 8 lancements simultanés « Supprimer et continuer »
      → un seul nettoyage, un seul nouveau batch, aucune erreur ;
   5. téléchargement HTTP réellement en cours (client lent, autre worker)
      → aucune suppression ; reprise normale une fois le ZIP reçu ;
  11. redémarrage pendant un nettoyage → terminé automatiquement au
      démarrage, sans qu'aucun client ne se connecte.

Usage : python3 test_vexport_cleanup_gunicorn.py   (gunicorn + requests requis)
"""
import os, sys, json, time, socket, subprocess, tempfile, threading, zipfile, signal, shutil

REPO = os.path.dirname(os.path.abspath(__file__))
DATA = tempfile.mkdtemp(prefix="vexport_clean_guni_")
EXPORT = os.path.join(DATA, "videobot_video_exports")
ADMIN = ("admin-cleang@local.test", "admin-cleang-password")
ENV = dict(os.environ, DATA_DIR=DATA, ADMIN_EMAIL=ADMIN[0], ADMIN_PASSWORD=ADMIN[1],
           SECRET_KEY="test-clean-secret", VEXPORT_FFMPEG_THREADS="2", PYTHONUNBUFFERED="1")
try:
    import requests, gunicorn  # noqa: F401
except ImportError as e:
    print(f"⚠️  test NON exécuté : dépendance manquante ({e})"); sys.exit(2)

PASS = 0; FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"✅ {name}")
    else:    FAIL += 1; print(f"❌ {name}   {extra}")

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def start_server(workers):
    port = free_port()
    log = open(os.path.join(DATA, f"gunicorn_{port}.log"), "w")
    proc = subprocess.Popen([sys.executable, "-m", "gunicorn", "app:app", "--bind", f"127.0.0.1:{port}",
                             "--workers", str(workers), "--threads", "4", "--timeout", "0",
                             "--keep-alive", "65", "--log-level", "warning"],
                            cwd=REPO, env=ENV, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(300):
        try:
            if requests.get(base + "/login", timeout=1).status_code == 200:
                return proc, base, port
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("Gunicorn ne démarre pas : " + open(log.name).read()[-2000:])

def stop_server(proc):
    try: os.killpg(proc.pid, signal.SIGKILL)
    except Exception: pass
    proc.wait()

def login(base):
    s = requests.Session()
    s.post(base + "/login", data={"email": ADMIN[0], "password": ADMIN[1]}, timeout=10)
    return s

def mkvid(path, w, h, dur):
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:d={dur}:r=30,noise=alls=40:allf=t+u",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", path, "-loglevel", "error"], check=True)

def state(bid):
    try:
        return json.load(open(os.path.join(EXPORT, bid, "state.json")))
    except Exception:
        return None

def files(bid):
    root = os.path.join(EXPORT, bid)
    out = []
    for r, _d, fs in os.walk(root):
        out += [os.path.relpath(os.path.join(r, f), root) for f in fs]
    return sorted(out)

def tomb_only(bid):
    return set(files(bid)) <= {"state.json", ".state.lock"} and (state(bid) or {}).get("cleaned") is True

def create(base, cookies, confirm=()):
    data = {"clean_previous": "1"}
    if confirm: data["confirm_ids"] = ",".join(confirm)
    r = requests.post(base + "/vexport_create", data=data, cookies=cookies, timeout=60)
    try: return r.status_code, r.json()
    except Exception: return r.status_code, {"raw": r.text[:200]}

def fill_start(s, base, bid, paths):
    for i, p in enumerate(paths):
        with open(p, "rb") as fh:
            r = s.post(base + "/vexport_upload", data={"batch_id": bid}, files={"file": (f"v{i}.mp4", fh)}).json()
            assert r.get("ok"), r
    assert s.post(base + "/vexport_start", data={"batch_id": bid}).json().get("ok")

def wait_settled(s, base, bid, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = s.get(f"{base}/vexport_status/{bid}").json()
        if d.get("progress", {}).get("finished") and d.get("archives_settled"):
            return d
        time.sleep(0.3)
    return s.get(f"{base}/vexport_status/{bid}").json()

def cleanup_log():
    try:
        return [json.loads(l) for l in open(os.path.join(EXPORT, ".cleanup_log.jsonl"))]
    except Exception:
        return []


src = tempfile.mkdtemp(prefix="vexport_clean_guni_src_")
tiny = os.path.join(src, "tiny.mp4"); mkvid(tiny, 64, 64, 0.4)
big = os.path.join(src, "big.mp4"); mkvid(big, 1080, 1920, 3.0)

proc, base, port = start_server(2)
s = login(base)
ck = s.cookies.get_dict()
workers = [l for l in subprocess.run(["ps", "-A", "-o", "ppid="], capture_output=True, text=True).stdout.split()
           if l.strip() == str(proc.pid)]
check("Gunicorn : 2 workers démarrés", len(workers) == 2, str(workers))

# ══ 10. Lancements simultanés sur 2 workers ═════════════════════════════
code, d = create(base, ck)
A1 = d["batch_id"]
fill_start(s, base, A1, [tiny] * 3)
wait_settled(s, base, A1)
results = []
def go():
    results.append(create(base, ck, confirm=[A1]))
ths = [threading.Thread(target=go) for _ in range(8)]
[t.start() for t in ths]; [t.join() for t in ths]
codes = sorted(c for c, _ in results)
check("10. 8 lancements simultanés : un seul nouveau batch", codes.count(200) == 1, str(codes))
check("10. les autres : refus propre (export en cours), aucune erreur 500",
      all(c == 409 and d.get("code") == "batch_active" and d["batch"]["busy"] == "uploading" for c, d in results if c != 200),
      str([d for c, d in results if c != 200][:2]))
check("10. ancien batch nettoyé une seule fois", tomb_only(A1)
      and sum(1 for e in cleanup_log() if e["batch_id"] == A1) == 1)
alive = [b for b in os.listdir(EXPORT) if os.path.isdir(os.path.join(EXPORT, b)) and len(b) == 32
         and not (state(b) or {}).get("cleaned")]
N1 = next(d["batch_id"] for c, d in results if c == 200)
check("10. un seul batch actif pour l'utilisateur", alive == [N1], str(alive))

# ══ 5. Téléchargement HTTP réellement en cours ══════════════════════════
fill_start(s, base, N1, [big] * 3)
dN = wait_settled(s, base, N1, timeout=900)
s.post(base + "/vexport_mark_saved", data={"batch_id": N1})
zsize = dN["groups"][0]["zip"]["size"]
snap = files(N1)
cookie = "; ".join(f"{k}={v}" for k, v in ck.items())
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
sock.connect(("127.0.0.1", port))
sock.sendall(f"GET /vexport_group_zip/{N1}/1 HTTP/1.1\r\nHost: 127.0.0.1\r\nCookie: {cookie}\r\nConnection: close\r\n\r\n".encode())
head = b""
while b"\r\n\r\n" not in head:
    head += sock.recv(4096)
hdr, body = head.split(b"\r\n\r\n", 1)
while len(body) < 65536:                                 # début reçu, puis le client « traîne »
    body += sock.recv(4096)
time.sleep(1.0)
blocked = [create(base, ck) for _ in range(6)]           # répartis sur les 2 workers
check("5. ZIP volumineux (dépasse les tampons réseau)", zsize > 20_000_000, str(zsize))
check("5. téléchargement en cours (autre worker) → aucune suppression",
      all(c == 409 and d["batch"]["busy"] == "downloading" for c, d in blocked), str([c for c, _ in blocked]))
check("5. fichiers intacts pendant le téléchargement", set(snap) <= set(files(N1)))
with open(os.path.join(src, "dl.zip"), "wb") as fh:
    fh.write(body)
    while True:
        b = sock.recv(1 << 20)
        if not b:
            break
        fh.write(b)
sock.close()
ok = os.path.getsize(os.path.join(src, "dl.zip")) == zsize
try:
    ok = ok and zipfile.ZipFile(os.path.join(src, "dl.zip")).testzip() is None
except Exception:
    ok = False
check("5. ZIP reçu complet et valide malgré la tentative de nettoyage", b" 200 " in hdr.split(b"\r\n")[0] and ok)
deadline = time.time() + 15
code, d = create(base, ck)
while code != 200 and time.time() < deadline:
    time.sleep(0.3); code, d = create(base, ck)
check("5. téléchargement terminé → nettoyage automatique", code == 200 and tomb_only(N1), f"{code} {d}")
N2 = d["batch_id"]

# ══ 12. « Annuler et supprimer » : 2 workers, double clic simultané ═════
X = s.post(base + "/vexport_create").json()["batch_id"]
fill_start(s, base, X, [big] * 3)
deadline = time.time() + 120
while time.time() < deadline and not any(it["status"] == "processing" for it in (state(X) or {}).get("items", [])):
    time.sleep(0.05)
dels = []
def cdel():
    r = requests.post(base + "/vexport_cancel", data={"batch_id": X, "delete": "1"}, cookies=ck, timeout=90)
    dels.append((r.status_code, r.json()))
ths = [threading.Thread(target=cdel) for _ in range(2)]
[t.start() for t in ths]; [t.join() for t in ths]
check("12. double clic sur 2 workers : 2 réponses OK, batch supprimé", [c for c, _ in dels] == [200, 200]
      and all(d.get("cleaned") for _, d in dels) and tomb_only(X), str(dels))
time.sleep(1.0)
left = [l for l in subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True).stdout.splitlines()
        if l.startswith("ffmpeg ") and os.path.join(EXPORT, X) in l]
check("12. FFmpeg arrêté (autre worker possible), aucun orphelin", left == [], str(left))
time.sleep(3)
check("12. aucune reprise automatique", tomb_only(X) and (state(X) or {}).get("cancelled") is True)

# ══ 11. Redémarrage pendant un nettoyage ════════════════════════════════
fill_start(s, base, N2, [tiny] * 2)
wait_settled(s, base, N2)
s.post(base + "/vexport_mark_saved", data={"batch_id": N2})
# Batch annulé dont la suppression était encore en attente (ex. téléchargement
# en cours) au moment du crash.
Y = s.post(base + "/vexport_create").json()["batch_id"]
fill_start(s, base, Y, [tiny] * 2)
wait_settled(s, base, Y)
assert s.post(base + "/vexport_cancel", data={"batch_id": Y}).json().get("cancelled")
stop_server(proc)
stY = state(Y); stY["delete_requested"] = time.time()
json.dump(stY, open(os.path.join(EXPORT, Y, "state.json"), "w"))
open(os.path.join(EXPORT, Y, ".delete"), "w").close()
st = state(N2)
st["cleaning"] = {"at": time.time(), "reason": "test_restart", "auth": "saved", "bytes": 1, "pid": 0}
json.dump(st, open(os.path.join(EXPORT, N2, "state.json"), "w"))
for nm in ("junk_a.bin", "junk_b.bin"):                 # fichiers du batch, dont un déjà supprimé avant le « crash »
    open(os.path.join(EXPORT, N2, "output", nm), "wb").write(b"x" * 1000)
os.remove(os.path.join(EXPORT, N2, "output", "junk_a.bin"))
check("11. état simulé : nettoyage interrompu, fichiers restants", not tomb_only(N2) and len(files(N2)) > 3)
proc, base, port = start_server(1)                      # options du Dockerfile (1 worker)
deadline = time.time() + 60
while time.time() < deadline and not tomb_only(N2):
    time.sleep(0.2)
check("11. redémarrage : nettoyage terminé automatiquement (aucun client)", tomb_only(N2), str(files(N2)))
deadline = time.time() + 30
while time.time() < deadline and not tomb_only(Y):
    time.sleep(0.2)
check("12. redémarrage : « Annuler et supprimer » en attente terminé, batch jamais repris", tomb_only(Y)
      and state(Y).get("cancelled") is True, str(files(Y)))
s = login(base)
d = s.get(f"{base}/vexport_status/{N2}").json()
check("11. statut après reprise : batch nettoyé", d.get("cleaned") is True and "items" not in d)
check("11. reprise : aucun batch courant obsolète", s.get(base + "/vexport_current").json().get("batch_id") is None)
stop_server(proc)
time.sleep(0.5)
left = [l for l in subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True).stdout.splitlines()
        if l.startswith("ffmpeg ") and EXPORT in l]
check("Aucun FFmpeg résiduel", left == [], str(left))
shutil.rmtree(src, ignore_errors=True)
print(f"\n{'=' * 40}\n{PASS} tests OK / {FAIL} KO")
sys.exit(1 if FAIL else 0)
