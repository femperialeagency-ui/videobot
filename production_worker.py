"""
production_worker.py — Moteur de production FEMPERIALE Marketing (processus SÉPARÉ).

    FEMPERIALE (tâche) → ce worker → vidéo source Google Drive → variantes → Google Drive
    (dossier PRODUCTIONS / <tâche>) → FEMPERIALE (historique). Aucun navigateur.

Lancé et relancé par serve.py (commande Docker) uniquement si PRODUCTION_WORKER_ENABLED=1 ;
un processus à part entière (pas un thread du serveur web) : un plantage ici n'arrête pas
ViralScale, un redémarrage du serveur web n'interrompt pas le worker.

Moteur INCHANGÉ : on réutilise les fonctions du Studio Variations d'app.py (normalisation de la
source en 1080p, paramètres aléatoires seedés par (tâche, variante), commande FFmpeg, tag
encodeur) et la porte FFmpeg commune (production.py) : un seul encodage lourd à la fois pour
toute l'instance, threads FFmpeg limités, priorité aux requêtes des utilisateurs.

Sûreté :
  - FEMPERIALE reste la source de vérité (réservation exclusive avec bail, reprise après
    redémarrage par le même identifiant de moteur, tentatives limitées côté FEMPERIALE) ;
  - Google Drive : lecture de la source (jamais modifiée), création de fichiers NEUFS dans le
    dossier de la tâche uniquement (envoi reprenable) ; aucune mise à jour, aucune suppression,
    aucun partage. Chaque variante porte sa marque (femp_vuid) : retrouvée avant tout envoi,
    donc jamais envoyée deux fois, même après une interruption ;
  - le jeton Google est de courte durée, fourni par FEMPERIALE pour la tâche réservée ; aucun
    secret n'est écrit dans les journaux ;
  - espace disque contrôlé avant le téléchargement et avant chaque encodage ; fichiers locaux
    supprimés après confirmation du transfert (taille + MD5 contrôlés par Google).
"""
import hashlib
import json
import os
import random
import shutil
import signal
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"

TOKEN_MIN_LENGTH = 32
HTTP_TIMEOUT = (10, 60)               # connexion, lecture
UPLOAD_CHUNK = 8 * 1024 * 1024        # multiple de 256 Kio (exigence Google)
DOWNLOAD_CHUNK = 1024 * 1024
VARIANT_TIMEOUT = 600                 # une variante (source déjà ramenée en 1080p)
HEARTBEAT_SECONDS = 60
DISK_WAIT_SECONDS = 600               # attente d'espace disque avant d'abandonner la tâche
TRANSIENT_PAUSE = 120                 # pause après une panne passagère (la tâche est ensuite reprise)
STALE_HOURS = 24
APP_SOURCE = "capcut"                 # même marquage automatique que /variation_run


def log(msg):
    print(f"[PRODUCTION] {msg}", file=sys.stderr, flush=True)


def _env_int(name, default, lo, hi):
    try:
        return max(lo, min(hi, int(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.enabled = (env.get("PRODUCTION_WORKER_ENABLED") or "0").strip() == "1"
        self.url = (env.get("FEMPERIALE_URL") or "").strip().rstrip("/")
        self.token = (env.get("PRODUCTION_API_TOKEN") or "").strip()
        self.poll = _env_int("PRODUCTION_POLL_SECONDS", 30, 5, 600)
        self.min_free = _env_int("PRODUCTION_MIN_FREE_MB", 1024, 100, 100_000) * 1024 * 1024

    def errors(self):
        out = []
        p = urlparse(self.url)
        local = p.hostname in ("127.0.0.1", "localhost")
        if not p.hostname or (p.scheme != "https" and not (local and p.scheme == "http")):
            out.append("FEMPERIALE_URL (https://… du service FEMPERIALE Marketing)")
        if len(self.token) < TOKEN_MIN_LENGTH:
            out.append(f"PRODUCTION_API_TOKEN ({TOKEN_MIN_LENGTH} caractères minimum, même valeur que FEMPERIALE)")
        return out


class Stop(Exception):
    """La tâche doit s'arrêter : annulée, réservation perdue ou arrêt du worker."""


class Fatal(Exception):
    """La tâche ne peut pas aboutir (source introuvable, dossier refusé, disque plein…)."""


class Transient(Exception):
    """Panne passagère (réseau, service indisponible) : la tâche sera reprise."""


class ApiConflict(Exception):
    def __init__(self, code, message=""):
        super().__init__(code)
        self.code, self.message = code, message


# ─── API FEMPERIALE ──────────────────────────────────────────────────────────

class FemperialeApi:
    def __init__(self, base_url, token, http=None):
        self.base = base_url.rstrip("/") + "/production/api"
        self._token = token
        self.http = http or requests.Session()

    def call(self, path, payload):
        try:
            r = self.http.post(self.base + path, json=payload, timeout=HTTP_TIMEOUT, allow_redirects=False,
                               headers={"X-Production-Token": self._token})
        except requests.RequestException as exc:
            raise Transient(f"FEMPERIALE injoignable ({type(exc).__name__})")
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 200:
            return data
        if r.status_code in (404, 409):
            raise ApiConflict(data.get("error") or str(r.status_code), data.get("message") or "")
        if r.status_code in (401, 403):
            raise Transient(f"FEMPERIALE refuse le worker (HTTP {r.status_code} : secret différent ?)")
        raise Transient(f"FEMPERIALE HTTP {r.status_code}")


# ─── Google Drive (lecture de la source, création de fichiers NEUFS) ─────────

class DriveError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Drive:
    def __init__(self, token_provider, http=None):
        self._provider = token_provider
        self._token, self._exp = None, 0.0
        self.http = http or requests.Session()
        self.calls = []                                  # méthodes HTTP utilisées (contrôle des tests)

    def _auth(self, force=False):
        if force or not self._token or time.time() > self._exp - 120:
            tok = self._provider()
            self._token = tok["access_token"]
            self._exp = time.time() + int(tok.get("expires_in") or 3600)
        return {"Authorization": "Bearer " + self._token}

    def _req(self, method, url, retry_auth=True, **kw):
        self.calls.append(method)
        kw.setdefault("timeout", HTTP_TIMEOUT)
        headers = dict(kw.pop("headers", {}) or {})
        headers.update(self._auth())
        try:
            r = self.http.request(method, url, headers=headers, allow_redirects=False, **kw)
        except requests.RequestException as exc:
            raise DriveError(f"Google Drive injoignable ({type(exc).__name__})")
        if r.status_code == 401 and retry_auth:
            self._auth(force=True)
            return self._req(method, url, retry_auth=False, headers={k: v for k, v in headers.items()
                                                                   if k != "Authorization"}, **kw)
        return r

    @staticmethod
    def _error(r, what):
        try:
            msg = (r.json().get("error") or {}).get("message") or ""
        except ValueError:
            msg = ""
        return DriveError(f"{what} : HTTP {r.status_code} {msg[:160]}".strip(), r.status_code)

    def meta(self, file_id):
        r = self._req("GET", f"{DRIVE_API}/files/{quote(file_id)}",
                      params={"fields": "id,name,size,md5Checksum,mimeType,trashed,parents", "supportsAllDrives": "true"})
        if r.status_code != 200:
            raise self._error(r, "Lecture de la vidéo source")
        return r.json()

    def download(self, file_id, dest):
        """Téléchargement en flux vers dest (fichier .part puis renommé) — jamais en mémoire."""
        part = Path(str(dest) + ".part")
        r = self._req("GET", f"{DRIVE_API}/files/{quote(file_id)}", params={"alt": "media", "supportsAllDrives": "true"},
                      stream=True)
        if r.status_code != 200:
            raise self._error(r, "Téléchargement de la vidéo source")
        md5, size = hashlib.md5(), 0
        try:
            with open(part, "wb") as f:
                for chunk in r.iter_content(DOWNLOAD_CHUNK):
                    if chunk:
                        f.write(chunk)
                        md5.update(chunk)
                        size += len(chunk)
                f.flush()
                os.fsync(f.fileno())
        except requests.RequestException as exc:
            part.unlink(missing_ok=True)
            raise DriveError(f"Téléchargement interrompu ({type(exc).__name__})")
        finally:
            r.close()
        os.replace(part, dest)
        return size, md5.hexdigest()

    def find_variant(self, folder_id, vuid):
        """Variante déjà envoyée (marque femp_vuid) dans le dossier de la tâche, ou None."""
        q = (f"'{folder_id}' in parents and trashed = false "
             f"and appProperties has {{ key='femp_vuid' and value='{vuid}' }}")
        r = self._req("GET", f"{DRIVE_API}/files", params={"q": q, "pageSize": 10, "supportsAllDrives": "true",
                                                           "includeItemsFromAllDrives": "true",
                                                           "fields": "files(id,name,size,md5Checksum)"})
        if r.status_code != 200:
            raise self._error(r, "Recherche d'une variante déjà envoyée")
        files = [f for f in (r.json().get("files") or []) if int(f.get("size") or 0) > 0]
        return files[0] if files else None

    def upload(self, path, folder_id, name, app_properties):
        """Envoi REPRENABLE (protocole « resumable » de Google) d'un fichier NEUF : après une
        coupure, l'état réel est demandé à Google et l'envoi reprend à l'octet confirmé."""
        size = os.path.getsize(path)
        session = self._start_session(folder_id, name, app_properties, size)
        offset, errors = 0, 0
        with open(path, "rb") as f:
            while True:
                if offset < size:
                    f.seek(offset)
                    r = self._put(session, (offset, f.read(UPLOAD_CHUNK)), size)
                else:
                    r = self._put(session, None, size)            # tout envoyé : état final
                if r is not None:
                    if r.status_code in (200, 201):
                        return r.json()
                    if r.status_code in (404, 410):               # session expirée : aucun fichier créé
                        raise DriveError("Session d'envoi expirée", r.status_code)
                    if r.status_code == 308:
                        new = self._range_end(r)
                        if new > offset:
                            offset, errors = new, 0
                            continue
                    elif r.status_code < 500 and r.status_code != 429:
                        raise self._error(r, "Envoi vers Google Drive")
                errors += 1                                       # coupure, 5xx, 429 ou aucun progrès
                if errors > 6:
                    raise DriveError("Envoi vers Google Drive interrompu trop de fois")
                time.sleep(min(30, 2 ** errors))
                st = self._put(session, None, size)               # où en est réellement Google ?
                if st is not None:
                    if st.status_code in (200, 201):
                        return st.json()
                    if st.status_code in (404, 410):
                        raise DriveError("Session d'envoi expirée", st.status_code)
                    if st.status_code == 308:
                        offset = self._range_end(st)

    def _start_session(self, folder_id, name, app_properties, size):
        body = {"name": name, "parents": [folder_id], "mimeType": "video/mp4", "appProperties": app_properties}
        r = self._req("POST", DRIVE_UPLOAD, params={"uploadType": "resumable", "supportsAllDrives": "true",
                                                    "fields": "id,name,size,md5Checksum,parents"},
                      json=body, headers={"X-Upload-Content-Type": "video/mp4",
                                          "X-Upload-Content-Length": str(size)})
        if r.status_code != 200 or not r.headers.get("Location"):
            raise self._error(r, "Ouverture de l'envoi vers Google Drive")
        return r.headers["Location"]

    def _put(self, session, part, size):
        """Un morceau (part = (offset, octets)) ou, sans morceau, demande de l'état de l'envoi."""
        if part is None:
            headers = {"Content-Length": "0", "Content-Range": f"bytes */{size}"}
            data = b""
        else:
            offset, data = part
            headers = {"Content-Length": str(len(data)), "Content-Range": f"bytes {offset}-{offset + len(data) - 1}/{size}"}
        try:
            return self._req("PUT", session, data=data, headers=headers, timeout=(10, 120))
        except DriveError:
            return None

    @staticmethod
    def _range_end(r):
        rng = r.headers.get("Range") or ""
        try:
            return int(rng.split("-")[-1]) + 1 if rng else 0
        except ValueError:
            return 0


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(DOWNLOAD_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


# ─── Worker ──────────────────────────────────────────────────────────────────

class Worker:
    def __init__(self, engine, api, cfg, work_dir, worker_id, stop=None, drive_factory=None):
        self.A = engine                                   # module app (moteur existant)
        self.api = api
        self.cfg = cfg
        self.dir = Path(work_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.worker_id = worker_id
        self.stop = stop or threading.Event()
        self.drive_factory = drive_factory or (lambda provider: Drive(provider))
        self.current = None

    # -- boucle ---------------------------------------------------------------
    def run_forever(self):
        log(f"worker {self.worker_id} démarré (attente des tâches FEMPERIALE toutes les {self.cfg.poll} s)")
        self.sweep()
        backoff = self.cfg.poll
        while not self.stop.is_set():
            try:
                did = self.run_once()
                backoff = self.cfg.poll
            except Transient as exc:
                log(f"pause : {exc}")
                did, backoff = False, min(600, backoff * 2)
            except Exception as exc:                          # jamais d'arrêt sur une erreur imprévue
                log(f"pause après une erreur inattendue ({type(exc).__name__}: {str(exc)[:200]})")
                did, backoff = False, min(600, backoff * 2)
            if not did:
                self.stop.wait(backoff)
        log("worker arrêté")

    def run_once(self):
        """Une tâche au plus (retourne False s'il n'y avait rien à faire)."""
        res = self.api.call("/claim", {"worker_id": self.worker_id, "engine": "viralscale"})
        job = res.get("job")
        if not job:
            return False
        self.process(job)
        return True

    # -- une tâche --------------------------------------------------------------
    def process(self, job):
        jdir = self.dir / job["uid"]
        ctx = {"cancel": False, "lost": False, "status": "processing", "done": threading.Event()}
        self.current = job["uid"]
        hb = threading.Thread(target=self._heartbeat, args=(job, ctx), name="production-heartbeat", daemon=True)
        log(f"tâche #{job['id']} réservée (tentative {job.get('attempt')}, {len(job['variants'])} variante(s))")
        final = False
        try:
            self._beat(job, ctx, "processing")
            hb.start()
            final = self._produce(job, ctx, jdir)
        except Stop as exc:
            log(f"tâche #{job['id']} arrêtée : {exc}")
            if ctx["cancel"] and not ctx["lost"]:
                final = self._finish(job, "cancelled")
        except Fatal as exc:
            log(f"tâche #{job['id']} en échec : {exc}")
            final = self._finish(job, "failed", str(exc))
        except ApiConflict as exc:
            ctx["lost"] = True
            log(f"tâche #{job['id']} abandonnée : réservation refusée par FEMPERIALE ({exc.code})")
        except (Transient, DriveError) as exc:
            log(f"tâche #{job['id']} interrompue ({exc}) — reprise au prochain passage")
            self.stop.wait(TRANSIENT_PAUSE)
        except Exception as exc:                              # filet de sécurité : le worker continue
            log(f"tâche #{job['id']} interrompue par une erreur inattendue ({type(exc).__name__}: {str(exc)[:200]})")
            self.stop.wait(TRANSIENT_PAUSE)
        finally:
            ctx["done"].set()
            self.current = None
            if final or ctx["lost"]:
                shutil.rmtree(jdir, ignore_errors=True)       # fichiers temporaires de la tâche

    def _beat(self, job, ctx, status=None):
        try:
            res = self.api.call(f"/jobs/{job['id']}/heartbeat", {"reservation_id": job["reservation_id"],
                                                                 "status": status or ctx["status"]})
            ctx["cancel"] = ctx["cancel"] or bool(res.get("cancel"))
        except ApiConflict:
            ctx["lost"] = True
        except Transient:
            pass

    def _heartbeat(self, job, ctx):
        while not ctx["done"].wait(HEARTBEAT_SECONDS):
            self._beat(job, ctx)

    def _check(self, ctx):
        if ctx["lost"]:
            raise Stop("réservation perdue (reprise par un autre passage)")
        if ctx["cancel"]:
            raise Stop("annulée depuis FEMPERIALE")
        if self.stop.is_set():
            raise Transient("arrêt du worker")

    def _report(self, job, ctx, idx, status, **kw):
        try:
            res = self.api.call(f"/jobs/{job['id']}/variants/{idx}",
                                {"reservation_id": job["reservation_id"], "status": status, **kw})
        except ApiConflict as exc:
            if exc.code in ("reservation_lost", "not_active", "not_found"):
                ctx["lost"] = True
                raise Stop(f"réservation perdue ({exc.code})")
            raise
        ctx["cancel"] = ctx["cancel"] or bool(res.get("cancel"))
        return res

    def _finish(self, job, status, error=None):
        try:
            res = self.api.call(f"/jobs/{job['id']}/finish", {"reservation_id": job["reservation_id"],
                                                              "status": status, "error": (error or "")[:400] or None})
            log(f"tâche #{job['id']} : {res.get('status')}")
            return True
        except (ApiConflict, Transient) as exc:
            log(f"fin de la tâche #{job['id']} non confirmée ({exc})")
            return False

    def _wait_for_disk(self, ctx, need, what):
        deadline = time.time() + DISK_WAIT_SECONDS
        while True:
            free = self.A._disk_free_bytes(self.dir)
            if free is None or free >= need:
                return
            if time.time() > deadline:
                raise Fatal(f"Espace disque insuffisant sur le moteur pour {what} "
                            f"({free / 1024 ** 2:.0f} Mo libres, {need / 1024 ** 2:.0f} Mo nécessaires)")
            self._check(ctx)
            self.stop.wait(15)

    def _produce(self, job, ctx, jdir):
        A = self.A
        self._check(ctx)
        folder = job.get("folder_id")
        if not folder:
            raise Fatal("Dossier Google Drive de la tâche absent")
        drive = self.drive_factory(lambda: self.api.call(f"/jobs/{job['id']}/drive-token",
                                                         {"reservation_id": job["reservation_id"]}))
        try:
            meta = drive.meta(job["source"]["file_id"])
        except DriveError as exc:
            if exc.status in (403, 404):
                raise Fatal(f"Vidéo source inaccessible ({exc})")
            raise
        if meta.get("trashed"):
            raise Fatal("La vidéo source est dans la corbeille Google Drive")
        if folder in (meta.get("parents") or []):
            raise Fatal("Refus : le dossier de sortie est celui de la vidéo source")
        src_size = int(meta.get("size") or job["source"].get("size") or 0)
        pending = [v for v in job["variants"] if v["status"] != "completed"]
        if not pending:
            return self._finish(job, "completed")
        (jdir / "in").mkdir(parents=True, exist_ok=True)
        (jdir / "out").mkdir(parents=True, exist_ok=True)
        raw, src = jdir / "in" / "source_raw", jdir / "in" / "source.mp4"
        if not src.exists():
            if not (raw.exists() and raw.stat().st_size == src_size):
                self._wait_for_disk(ctx, src_size * 3 + self.cfg.min_free, "la vidéo source")
                size, md5 = drive.download(job["source"]["file_id"], raw)
                if src_size and size != src_size or (meta.get("md5Checksum") and md5 != meta["md5Checksum"]):
                    raw.unlink(missing_ok=True)
                    raise Transient("vidéo source téléchargée incomplète")
            self._check(ctx)
            self._gate(lambda: A._normalize_variation_source(str(raw), str(src), max_side=1920))
            if not (src.exists() and src.stat().st_size > 0):
                shutil.move(str(raw), str(src))               # même repli que /variation_stage
            raw.unlink(missing_ok=True)
        width, height = A.get_video_dims(str(src))
        for v in sorted(pending, key=lambda x: x["idx"]):
            self._check(ctx)
            self._variant(job, ctx, drive, folder, src, width, height, v)
        return self._finish(job, "completed")

    def _gate(self, fn):
        """Encodage lourd : la porte FFmpeg commune (attente sans limite pour ce worker de fond,
        après le passage des requêtes des utilisateurs en attente)."""
        self.A.FFMPEG_GATE.yield_to_interactive()
        with self.A.FFMPEG_GATE.hold(blocking=True):
            return fn()

    def _variant(self, job, ctx, drive, folder, src, width, height, v):
        A, idx = self.A, v["idx"]
        props = {"femp_role": "production_variant", "femp_pjob": job["uid"], "femp_vuid": v["uid"],
                 "femp_vidx": str(idx)}
        found = drive.find_variant(folder, v["uid"])          # déjà envoyée (interruption avant confirmation)
        if found:
            self._report(job, ctx, idx, "completed", drive_file_id=found["id"], size=int(found["size"]),
                         md5=found.get("md5Checksum"))
            log(f"tâche #{job['id']} v{idx:02d} déjà dans Drive : confirmée sans nouvel envoi")
            return
        try:
            self._report(job, ctx, idx, "processing")
        except ApiConflict as exc:                            # tentatives épuisées : variante en échec
            log(f"tâche #{job['id']} v{idx:02d} ignorée ({exc.code})")
            return
        self._check(ctx)                                      # annulée entre-temps : rien n'est encodé
        out = Path(src).parent.parent / "out" / f"{idx:03d}.mp4"
        try:
            self._wait_for_disk(ctx, max(Path(src).stat().st_size * 2, 50 * 1024 ** 2) + self.cfg.min_free // 2,
                                "une variante")
            rng = random.Random(f"{job['uid']}:{idx}")       # même graine pour une reprise
            params = A._pick_variation_params(job.get("strength") or "light", rng)
            profile = A._pick_metadata_profile(rng)
            cmd = A._build_variation_ffmpeg_cmd(str(src), str(out), params, profile, width, height,
                                                app_source=APP_SOURCE, origin="")
            try:
                proc = self._gate(lambda: A.ffmpeg_run(cmd, timeout=VARIANT_TIMEOUT))
            except Exception as exc:
                proc, err = None, f"FFmpeg : {type(exc).__name__}"
            else:
                err = (proc.stderr or "FFmpeg a échoué")[-300:] if proc.returncode != 0 else ""
            if proc is None or proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
                raise RuntimeError(err or "aucun fichier produit")
            A._apply_encoder_tag(out, profile, APP_SOURCE)
            if not (A._get_video_duration_seconds(str(out)) or 0) > 0:
                raise RuntimeError("vidéo produite illisible")
            self._check(ctx)
            ctx["status"] = "uploading"
            self._report(job, ctx, idx, "uploading")
            local_md5, local_size = md5_of(out), out.stat().st_size
            try:
                sent = drive.upload(out, folder, v["file_name"], props)
            except DriveError:
                found = drive.find_variant(folder, v["uid"])  # réponse perdue mais fichier créé ?
                if not found:
                    raise
                sent = found
            if int(sent.get("size") or 0) != local_size or (sent.get("md5Checksum") and sent["md5Checksum"] != local_md5):
                raise RuntimeError("contrôle du transfert : taille ou MD5 différents dans Google Drive")
            self._report(job, ctx, idx, "completed", drive_file_id=sent["id"], size=local_size, md5=local_md5)
            log(f"tâche #{job['id']} v{idx:02d} envoyée dans Drive ({local_size} octets)")
        except (Stop, Transient):
            raise
        except DriveError as exc:
            if exc.status is None or exc.status >= 500 or exc.status == 429:
                raise Transient(str(exc))
            if exc.status == 404:
                raise Fatal("Dossier de la tâche introuvable dans Google Drive (PRODUCTIONS)")
            if exc.status == 403:
                raise Fatal(f"Google Drive refuse l'envoi ({exc})")
            self._report(job, ctx, idx, "failed", error=str(exc)[:300])
            log(f"tâche #{job['id']} v{idx:02d} en échec : {str(exc)[:200]}")
        except Exception as exc:
            self._report(job, ctx, idx, "failed", error=str(exc)[:300])
            log(f"tâche #{job['id']} v{idx:02d} en échec : {str(exc)[:200]}")
        finally:
            ctx["status"] = "processing"
            out.unlink(missing_ok=True)                       # jamais gardé : confirmé ou à refaire

    def sweep(self):
        """Dossiers de travail abandonnés (plus de STALE_HOURS)."""
        cutoff = time.time() - STALE_HOURS * 3600
        for d in self.dir.iterdir():
            try:
                if d.is_dir() and d.name != self.current and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def worker_identity(work_dir):
    """Identifiant STABLE du moteur (sur le disque persistant) : après un redémarrage, le même
    moteur reprend tout de suite sa tâche, sans attendre l'expiration du bail."""
    p = Path(work_dir) / "worker_id"
    try:
        wid = p.read_text().strip()
        if 8 <= len(wid) <= 80:
            return wid
    except OSError:
        pass
    wid = "viralscale-" + uuid.uuid4().hex[:20]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(wid)
    os.replace(tmp, p)
    return wid


def main(env=None):
    cfg = Config(env)
    if not cfg.enabled:
        log("désactivé (PRODUCTION_WORKER_ENABLED ≠ 1)")
        return 0
    missing = cfg.errors()
    if missing:
        log("configuration incomplète, worker non démarré : " + " ; ".join(missing))
        return 2
    os.environ["VEXPORT_WORKER"] = "0"                    # l'ordonnanceur d'export reste dans le serveur web
    import app as engine                                  # moteur existant (aucune route servie ici)
    import fcntl
    work_dir = engine.DATA_DIR / "videobot_production"
    work_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    lock = os.open(str(work_dir / ".worker.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    while not stop.is_set():                              # un seul worker par instance
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            stop.wait(10)
    if stop.is_set():
        return 0
    worker = Worker(engine, FemperialeApi(cfg.url, cfg.token), cfg, work_dir, worker_identity(work_dir), stop)
    worker.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
