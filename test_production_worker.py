#!/usr/bin/env python3
"""
test_production_worker.py — Worker de production FEMPERIALE (production_worker.py + serve.py).

FFmpeg RÉEL (moteur du Studio Variations inchangé), faux FEMPERIALE (même contrat d'API) et faux
Google Drive HTTP (protocole d'envoi reprenable), aucun accès réseau extérieur :

  CONFIG     désactivé par défaut ; configuration incomplète → non démarré, aucun secret affiché
  SERVE      sans production : exactement l'ancienne commande Gunicorn ; superviseur : relance du
             worker, pas de relance sur configuration incomplète, arrêt avec Gunicorn
  PRODUCTION tâche complète : source Drive téléchargée, variantes FFmpeg réelles (différentes,
             lisibles), envoyées dans le dossier de la tâche avec leur marque, MD5 confirmé,
             FEMPERIALE informé, fichiers temporaires supprimés, source intacte, aucune
             modification / suppression Drive, porte FFmpeg tenue pendant chaque encodage
  REPRISE    coupure pendant l'envoi → reprise à l'octet confirmé ; interruption après l'envoi et
             avant la confirmation → aucun doublon Drive
  ARRÊTS     annulation, réservation perdue, disque plein, dossier de sortie = dossier source
  SECRETS    jetons jamais écrits dans les journaux

Usage : python3 test_production_worker.py
"""
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vs_prod_test_"))
os.environ.setdefault("ADMIN_EMAIL", "admin-prod@local.test")
os.environ.setdefault("ADMIN_PASSWORD", "admin-prod-password")
os.environ["VEXPORT_WORKER"] = "0"
import app as A                      # noqa: E402
import production_worker as W        # noqa: E402
import serve                         # noqa: E402

PASS = 0; FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"✅ {name}")
    else:    FAIL += 1; print(f"❌ {name}   {extra}")

API_TOKEN = "prod-api-" + "k" * 40
W.TRANSIENT_PAUSE = 0
W.DISK_WAIT_SECONDS = 0


def make_source():
    path = Path(tempfile.mkdtemp(prefix="vs_prod_src_")) / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path.read_bytes()


SOURCE_BYTES = make_source()


# ─── Faux Google Drive (HTTP) ────────────────────────────────────────────────

class FakeDrive:
    def __init__(self):
        self.files, self.sessions, self.log = {}, {}, []
        self.tokens = set()
        self.fail_put = {}                  # numéro d'appel PUT → code HTTP renvoyé APRÈS avoir stocké le morceau
        self.puts = 0
        self.lock = threading.Lock()

    def add(self, name, data, parents, mime="video/mp4", props=None):
        fid = "f" + uuid.uuid4().hex[:24]
        self.files[fid] = {"id": fid, "name": name, "data": data, "parents": parents, "mimeType": mime,
                           "appProperties": props or {}, "trashed": False}
        return fid

    def view(self, f):
        return {"id": f["id"], "name": f["name"], "size": str(len(f["data"])), "parents": f["parents"],
                "mimeType": f["mimeType"], "trashed": f["trashed"], "appProperties": f["appProperties"],
                "md5Checksum": hashlib.md5(f["data"]).hexdigest()}


def drive_server(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def _json(self, code, obj, headers=None):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _auth(self):
            tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
            if tok not in fake.tokens:
                self._json(401, {"error": {"message": "invalid token"}})
                return False
            return True

        def do_GET(self):
            u = urlparse(self.path); q = parse_qs(u.query)
            fake.log.append(("GET", u.path, q.get("alt", [""])[0]))
            if not self._auth(): return
            m = re.fullmatch(r"/drive/v3/files/([\w-]+)", u.path)
            if m:
                f = fake.files.get(m.group(1))
                if not f: return self._json(404, {"error": {"message": "notFound"}})
                if q.get("alt") == ["media"]:
                    self.send_response(200); self.send_header("Content-Length", str(len(f["data"]))); self.end_headers()
                    self.wfile.write(f["data"]); return
                return self._json(200, fake.view(f))
            if u.path == "/drive/v3/files":
                qq = q.get("q", [""])[0]
                folder = re.search(r"'([\w-]+)' in parents", qq).group(1)
                vuid = re.search(r"value='([\w-]+)'", qq).group(1)
                out = [fake.view(f) for f in fake.files.values() if folder in f["parents"] and not f["trashed"]
                       and f["appProperties"].get("femp_vuid") == vuid]
                return self._json(200, {"files": out})
            self._json(404, {})

        def do_POST(self):
            u = urlparse(self.path)
            fake.log.append(("POST", u.path, ""))
            if not self._auth(): return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            sid = uuid.uuid4().hex
            fake.sessions[sid] = {"meta": body, "size": int(self.headers.get("X-Upload-Content-Length")), "data": b"",
                                  "done": None}
            self._json(200, {}, {"Location": f"http://127.0.0.1:{self.server.server_port}/upload/session/{sid}"})

        def do_PUT(self):
            u = urlparse(self.path)
            with fake.lock:
                fake.puts += 1; n = fake.puts
            fake.log.append(("PUT", u.path, self.headers.get("Content-Range")))
            if not self._auth(): return
            s = fake.sessions.get(u.path.rsplit("/", 1)[-1])
            data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if not s: return self._json(404, {})
            if s["done"]:
                return self._json(200, fake.view(fake.files[s["done"]]))
            rng = self.headers.get("Content-Range") or ""
            m = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", rng)
            if m and int(m.group(1)) == len(s["data"]):
                s["data"] += data
            if len(s["data"]) == s["size"]:
                meta = s["meta"]
                s["done"] = fake.add(meta["name"], s["data"], meta["parents"], meta.get("mimeType"), meta.get("appProperties"))
            code = fake.fail_put.pop(n, None)
            if code:
                return self._json(code, {"error": {"message": "backendError"}})
            if s["done"]:
                return self._json(200, fake.view(fake.files[s["done"]]))
            hdr = {"Range": f"bytes=0-{len(s['data']) - 1}"} if s["data"] else {}
            self._json(308, {}, hdr)

        def do_PATCH(self):
            fake.log.append(("PATCH", self.path, "")); self._json(403, {})

        def do_DELETE(self):
            fake.log.append(("DELETE", self.path, "")); self._json(403, {})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ─── Faux FEMPERIALE (même contrat que routes/production.py) ─────────────────

class FakeFemperiale:
    def __init__(self, drive, n_variants=3, folder=None, source=None):
        self.drive, self.calls, self.finished = drive, [], []
        self.cancel_after = None            # annulation demandée après N variantes terminées
        self.fail_once = {}                 # (chemin, statut) → code HTTP renvoyé une fois
        self.lost_on = None                 # (idx, statut) → réservation perdue
        self.job = {"id": 7, "uid": uuid.uuid4().hex, "kind": "variations", "reservation_id": "r1", "status": "pending",
                    "strength": "light", "attempt": 0, "max_variant_attempts": 2, "worker": None,
                    "source": {"file_id": source, "name": "clip.mp4", "size": len(SOURCE_BYTES), "mime": "video/mp4"},
                    "folder_id": folder,
                    "variants": [{"idx": i, "uid": uuid.uuid4().hex, "file_name": f"clip_P7_v{i:02d}.mp4",
                                  "status": "pending", "attempts": 0, "drive_file_id": None, "md5": None, "size": None}
                                 for i in range(1, n_variants + 1)]}

    def handle(self, path, body, token):
        self.calls.append((path, body.get("status")))
        if token != API_TOKEN:
            return 401, {"ok": False, "error": "unauthorized"}
        key = (path, body.get("status"))
        if key in self.fail_once:
            return self.fail_once.pop(key), {"ok": False}
        j = self.job
        if path == "/claim":
            if j["status"] in ("completed", "failed", "cancelled"):
                return 200, {"ok": True, "job": None}
            if j["status"] == "pending" or j["worker"] == body["worker_id"]:
                j.update(status="reserved", worker=body["worker_id"], reservation_id=uuid.uuid4().hex,
                         attempt=j["attempt"] + 1)
                for v in j["variants"]:
                    if v["status"] in ("processing", "uploading"):
                        v["status"] = "pending"
                return 200, {"ok": True, "job": json.loads(json.dumps(j))}
            return 200, {"ok": True, "job": None}
        if body.get("reservation_id") != j["reservation_id"]:
            return 409, {"ok": False, "error": "reservation_lost"}
        cancel = self.cancel_after is not None and \
            sum(v["status"] == "completed" for v in j["variants"]) >= self.cancel_after
        m = re.fullmatch(r"/jobs/7/variants/(\d+)", path)
        if m:
            v = j["variants"][int(m.group(1)) - 1]
            if self.lost_on == (v["idx"], body["status"]):
                j["reservation_id"] = "taken-by-other"
                return 409, {"ok": False, "error": "reservation_lost"}
            if body["status"] == "processing":
                v["attempts"] += 1
            if body["status"] == "completed":
                if v["status"] == "completed" and v["drive_file_id"] != body["drive_file_id"]:
                    return 409, {"ok": False, "error": "drive_conflict"}
                v.update(drive_file_id=body["drive_file_id"], md5=body.get("md5"), size=body.get("size"))
            v["status"] = body["status"]
            return 200, {"ok": True, "cancel": cancel}
        if path == "/jobs/7/heartbeat":
            if body.get("status"): j["status"] = body["status"]
            return 200, {"ok": True, "cancel": cancel}
        if path == "/jobs/7/drive-token":
            tok = "fake-access-" + uuid.uuid4().hex
            self.drive.tokens.add(tok)
            return 200, {"ok": True, "access_token": tok, "expires_in": 3599}
        if path == "/jobs/7/finish":
            st = "cancelled" if (cancel or body["status"] == "cancelled") else body["status"]
            if st == "completed" and any(v["status"] not in ("completed", "failed") for v in j["variants"]):
                return 409, {"ok": False, "error": "variants_pending"}
            j["status"] = st; self.finished.append((st, body.get("error")))
            return 200, {"ok": True, "status": st}
        return 404, {"ok": False}


def femperiale_server(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            code, out = fake.handle(self.path.replace("/production/api", ""), body, self.headers.get("X-Production-Token"))
            data = json.dumps(out).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ─── Montage d'un scénario ───────────────────────────────────────────────────

def scenario(n_variants=3, folder_in_source=False):
    drive = FakeDrive()
    dsrv = drive_server(drive)
    W.DRIVE_API = f"http://127.0.0.1:{dsrv.server_port}/drive/v3"
    W.DRIVE_UPLOAD = f"http://127.0.0.1:{dsrv.server_port}/upload/drive/v3/files"
    space = "space" + uuid.uuid4().hex[:8]
    src_folder = "srcfolder" + uuid.uuid4().hex[:8]
    job_folder = src_folder if folder_in_source else "jobfolder" + uuid.uuid4().hex[:8]
    source = drive.add("clip.mp4", SOURCE_BYTES, [src_folder])
    fem = FakeFemperiale(drive, n_variants, folder=job_folder, source=source)
    fsrv = femperiale_server(fem)
    cfg = W.Config({"PRODUCTION_WORKER_ENABLED": "1", "FEMPERIALE_URL": f"http://127.0.0.1:{fsrv.server_port}",
                    "PRODUCTION_API_TOKEN": API_TOKEN, "PRODUCTION_MIN_FREE_MB": "100"})
    work = Path(tempfile.mkdtemp(prefix="vs_prod_work_"))
    worker = W.Worker(A, W.FemperialeApi(cfg.url, cfg.token), cfg, work, "viralscale-test-0001")
    return drive, fem, worker, work, source, job_folder, space


def produced(drive, folder):
    return [f for f in drive.files.values() if folder in f["parents"]]


def playable(data):
    p = Path(tempfile.mkdtemp(prefix="vs_prod_chk_")) / "v.mp4"
    p.write_bytes(data)
    return (A._get_video_duration_seconds(str(p)) or 0) > 0


# ═════════════════════════════════════════════════════════════════════════════
print("\n— CONFIG")
err = io.StringIO()
with contextlib.redirect_stderr(err):
    code0 = W.main({})
    code = W.main({"PRODUCTION_WORKER_ENABLED": "1", "PRODUCTION_API_TOKEN": API_TOKEN,
                   "FEMPERIALE_URL": "http://femperiale.example.com"})
    code2 = W.main({"PRODUCTION_WORKER_ENABLED": "1", "FEMPERIALE_URL": "https://x.onrender.com",
                    "PRODUCTION_API_TOKEN": "court"})
check("désactivé par défaut (aucune variable) : rien ne démarre", code0 == 0)
check("URL non https → non démarré (code 2)", code == 2)
check("secret trop court → non démarré (code 2)", code2 == 2)
check("le secret n'est jamais affiché", API_TOKEN not in err.getvalue())
check("message explicite sur la configuration", "FEMPERIALE_URL" in err.getvalue() and "PRODUCTION_API_TOKEN" in err.getvalue())

print("\n— SERVE")
called = {}
class Replaced(Exception):
    """os.execvp ne rend jamais la main : le processus est remplacé par Gunicorn."""
def fake_exec(f, args):
    called["args"] = list(args)
    raise Replaced()
orig_exec = os.execvp
os.execvp = fake_exec
os.environ.pop("PRODUCTION_WORKER_ENABLED", None)
try:
    serve.main()
except Replaced:
    pass
finally:
    os.execvp = orig_exec
docker_old = ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--workers", "1", "--threads", "4",
              "--timeout", "0", "--keep-alive", "65", "--log-level", "info"]
check("sans production : exactement l'ancienne commande Gunicorn du Dockerfile", called.get("args") == docker_old,
      str(called))
check("le Dockerfile lance serve.py", 'CMD ["python", "serve.py"]' in Path("Dockerfile").read_text())

flag = Path(tempfile.mkdtemp(prefix="vs_serve_")) / "starts"
worker_cmd = [sys.executable, "-c", f"open({str(flag)!r}, 'a').write('x'); import sys; sys.exit(1)"]
web_cmd = [sys.executable, "-c", "import time; time.sleep(3)"]
sup = serve.Supervisor(web_cmd, worker_cmd, backoff=(0.2,), stable_after=60)
t0 = time.time()
rc = sup.run()
starts = len(flag.read_text()) if flag.exists() else 0
check("superviseur : worker relancé après un arrêt", starts >= 3, f"{starts} démarrages")
check("superviseur : s'arrête avec Gunicorn (code de Gunicorn)", rc == 0 and time.time() - t0 < 15)
flag2 = Path(tempfile.mkdtemp(prefix="vs_serve_")) / "starts"
sup2 = serve.Supervisor(web_cmd, [sys.executable, "-c", f"open({str(flag2)!r}, 'a').write('x'); import sys; sys.exit(2)"],
                        backoff=(0.2,), stable_after=60)
sup2.run()
check("superviseur : configuration incomplète (code 2) → pas de relance", len(flag2.read_text()) == 1)
long_web = [sys.executable, "-c", "import time; time.sleep(60)"]
long_worker = [sys.executable, "-c", "import time; time.sleep(60)"]
sup3 = serve.Supervisor(long_web, long_worker)
threading.Timer(1.0, lambda: os.kill(os.getpid(), __import__("signal").SIGTERM)).start()
t0 = time.time()
sup3.run()
check("superviseur : SIGTERM transmis, Gunicorn et worker arrêtés", time.time() - t0 < 10 and sup3.web is None and sup3.worker is None)
__import__("signal").signal(__import__("signal").SIGTERM, __import__("signal").SIG_DFL)

print("\n— PRODUCTION")
drive, fem, worker, work, source, folder, _ = scenario(3)
gate_held = []
orig_run = A.ffmpeg_run
def spy(cmd, timeout=None, **kw):
    gate_held.append(A.FFMPEG_GATE.held_by_current_thread())
    return orig_run(cmd, timeout=timeout, **kw)
A.ffmpeg_run = spy
logbuf = io.StringIO()
try:
    with contextlib.redirect_stderr(logbuf):
        did = worker.run_once()
finally:
    A.ffmpeg_run = orig_run
files = produced(drive, folder)
vs = fem.job["variants"]
check("une tâche traitée", did is True)
check("tâche terminée côté FEMPERIALE", fem.finished == [("completed", None)], str(fem.finished))
check("3 variantes confirmées avec leur identifiant Drive", all(v["status"] == "completed" and v["drive_file_id"] for v in vs))
check("3 fichiers dans le dossier de la tâche, aucun ailleurs", len(files) == 3 and len(drive.files) == 4)
check("noms et marques des variantes", sorted(f["name"] for f in files) == [v["file_name"] for v in vs]
      and all(f["appProperties"].get("femp_pjob") == fem.job["uid"] for f in files)
      and {f["appProperties"].get("femp_vuid") for f in files} == {v["uid"] for v in vs})
check("MD5 confirmé = MD5 du fichier Drive", all(hashlib.md5(drive.files[v["drive_file_id"]]["data"]).hexdigest() == v["md5"] for v in vs))
check("variantes toutes différentes", len({hashlib.md5(f["data"]).hexdigest() for f in files}) == 3)
check("variantes lisibles (FFmpeg réel)", all(playable(f["data"]) for f in files))
check("vidéo source intacte", drive.files[source]["data"] == SOURCE_BYTES and drive.files[source]["parents"])
check("aucune modification / suppression Drive", not [l for l in drive.log if l[0] in ("PATCH", "DELETE")])
check("la source n'est que lue", not [l for l in drive.log if l[0] != "GET" and source in l[1]])
check("porte FFmpeg tenue pendant chaque encodage lourd", gate_held and all(gate_held), str(gate_held))
check("fichiers temporaires supprimés", not any(work.iterdir()) or not any(p.is_file() for p in work.rglob("*")),
      str(list(work.rglob('*'))))
check("jetons jamais journalisés", API_TOKEN not in logbuf.getvalue()
      and not any(t in logbuf.getvalue() for t in drive.tokens))
check("aucune autre tâche : retour à l'attente", worker.run_once() is False)

print("\n— REPRISE")
drive, fem, worker, work, source, folder, _ = scenario(2)
W.UPLOAD_CHUNK = 256 * 1024
drive.fail_put[2] = 503                              # 2e morceau reçu puis réponse 503 (coupure)
with contextlib.redirect_stderr(io.StringIO()):
    worker.run_once()
files = produced(drive, folder)
check("coupure pendant l'envoi : reprise à l'octet confirmé, fichier complet",
      fem.finished == [("completed", None)] and len(files) == 2
      and all(hashlib.md5(f["data"]).hexdigest() == v["md5"] for f, v in
              zip(sorted(files, key=lambda f: f["name"]), fem.job["variants"])))
check("état réel demandé à Google après la coupure", any(l[0] == "PUT" and (l[2] or "").startswith("bytes */") for l in drive.log))
W.UPLOAD_CHUNK = 8 * 1024 * 1024

drive, fem, worker, work, source, folder, _ = scenario(3)
fem.fail_once[("/jobs/7/variants/2", "completed")] = 503   # envoyée, confirmation perdue (panne)
with contextlib.redirect_stderr(io.StringIO()):
    worker.run_once()                                # interrompue après l'envoi de v02
    uploads_before = sum(1 for l in drive.log if l[0] == "POST")
    worker.run_once()                                # même moteur : reprise
files = produced(drive, folder)
check("interruption après l'envoi : tâche reprise et terminée", fem.finished == [("completed", None)], str(fem.finished))
check("aucun doublon Drive après reprise", len(files) == 3, f"{len(files)} fichiers")
check("variante déjà envoyée retrouvée par sa marque (pas de nouvel envoi)",
      sum(1 for l in drive.log if l[0] == "POST") == 3 and uploads_before == 2)

print("\n— ARRÊTS")
drive, fem, worker, work, source, folder, _ = scenario(3)
fem.cancel_after = 1
with contextlib.redirect_stderr(io.StringIO()):
    worker.run_once()
check("annulation : arrêt après l'étape en cours, tâche annulée", fem.finished and fem.finished[-1][0] == "cancelled")
check("annulation : pas d'envoi supplémentaire", len(produced(drive, folder)) == 1)
check("annulation : fichiers temporaires supprimés", not any(p.is_file() for p in work.rglob("*")))

drive, fem, worker, work, source, folder, _ = scenario(3)
fem.lost_on = (2, "processing")
with contextlib.redirect_stderr(io.StringIO()):
    worker.run_once()
check("réservation perdue : arrêt sans fin de tâche", fem.finished == [] and len(produced(drive, folder)) == 1)
check("réservation perdue : fichiers temporaires supprimés", not any(p.is_file() for p in work.rglob("*")))

drive, fem, worker, work, source, folder, _ = scenario(2)
orig_free = A._disk_free_bytes
A._disk_free_bytes = lambda path=None: 10 * 1024 * 1024
try:
    with contextlib.redirect_stderr(io.StringIO()):
        worker.run_once()
finally:
    A._disk_free_bytes = orig_free
check("disque plein : tâche en échec avec un message explicite",
      fem.finished and fem.finished[-1][0] == "failed" and "Espace disque insuffisant" in (fem.finished[-1][1] or ""))
check("disque plein : la source n'est pas téléchargée", not [l for l in drive.log if l[2] == "media"])

drive, fem, worker, work, source, folder, _ = scenario(2, folder_in_source=True)
with contextlib.redirect_stderr(io.StringIO()):
    worker.run_once()
check("dossier de sortie = dossier de la source : refusé", fem.finished and fem.finished[-1][0] == "failed"
      and not [l for l in drive.log if l[0] in ("POST", "PUT")])

print(f"\n{PASS} réussis, {FAIL} échecs")
sys.exit(0 if FAIL == 0 else 1)
