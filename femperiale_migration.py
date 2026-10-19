"""
studio/viralscale_export.py — EXPORT des données de l'ancien service ViralScale vers FEMPERIALE.

Le même code sert :
  - à l'envoi AUTOMATIQUE depuis ViralScale (copié dans le dépôt videobot : femperiale_migration.py,
    lancé au démarrage quand le secret partagé est présent) ;
  - au SECOURS manuel : servi par FEMPERIALE (GET /studio-import/export.py, jeton requis) et lancé
    dans le Shell Render de ViralScale par la commande affichée dans FEMPERIALE → Production.

Lecture seule du disque ViralScale (/data) — rien n'y est modifié ni supprimé, hormis le fichier
d'état .femperiale_migration.json :
  1. ARCHIVE de métadonnées : instantané COHÉRENT de videobot_users.db (API de sauvegarde SQLite,
     l'application peut continuer de tourner), bibliothèques Reels, états + journal des exports
     vidéo terminés, manifeste (inventaire du disque, fichiers manquants, traitements en cours,
     paramètres non secrets ; d'une clé secrète, seule sa présence) → POST /studio-import/upload ;
  2. FICHIERS PHYSIQUES (exports vidéo, batchs, variations, Reels, archives ZIP) : plan
     (POST /studio-import/files/plan) puis envoi par morceaux reprenables (POST /studio-import/files/upload),
     SHA-256 vérifié par FEMPERIALE, jamais deux fois le même fichier.
Bibliothèque standard Python uniquement.
"""
import hashlib
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "https://femperiale-marketing.onrender.com"
TOKEN_ENVS = ("STUDIO_IMPORT_TOKEN", "IMPORT_TOKEN", "PRODUCTION_API_TOKEN")
CHUNK = 16 * 1024 * 1024
PLAN_BATCH = 500
SETTING_KEYS = ("VEXPORT_CRF", "VEXPORT_PRESET", "VEXPORT_RETENTION_HOURS", "VEXPORT_OUT_RATIO", "FFMPEG_GATE_WAIT",
                "OCR_MODEL_SONNET", "OCR_MODEL_OPUS")
SECRET_KEYS = ("ANTHROPIC_API_KEY",)
CATEGORIES = (("exports", "videobot_video_exports"), ("batches", "videobot_batches"),
              ("variations", "videobot_variations"), ("reels_mix", "videobot_reels_mix"))
STATE_FILE = ".femperiale_migration.json"
ACTIVE_HOURS = 24


def _log(msg):
    print(f"[MIGRATION] {msg}", flush=True)


class Refused(Exception):
    """FEMPERIALE refuse (jeton absent ou invalide) : inutile d'insister avant longtemps."""


class NoSpace(Exception):
    """FEMPERIALE manque d'espace disque : nouvel essai bien plus tard (disque à agrandir)."""


def token_from_env(env=None):
    env = os.environ if env is None else env
    for name in TOKEN_ENVS:
        tok = (env.get(name) or "").strip()
        if len(tok) >= 32:
            return tok
    return None


class Client:
    def __init__(self, url, token, timeout=600):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def _req(self, method, path, params=None, data=None, json_body=None):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        headers = {"X-Import-Token": self.token}
        if json_body is not None:
            data, headers["Content-Type"] = json.dumps(json_body).encode(), "application/json"
        elif data is not None:
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(self.url + path + q, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (401, 404):
                raise Refused(f"HTTP {e.code}")
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}

    def post(self, path, params=None, data=None, json_body=None):
        return self._req("POST", path, params, data, json_body)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _skip(rel):
    parts = rel.split("/")
    return any(p.startswith(".") for p in parts) or rel.endswith((".lock", ".tmp", ".part"))


def _export_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else None
    except (OSError, ValueError):
        return None


def _export_active(st, mtime):
    """Batch d'export encore actif (encodage / envoi en cours) : migré plus tard, signalé."""
    if not st or st.get("cancelled") or st.get("cleaned") or st.get("cleaning"):
        return False
    items = st.get("items") or []
    finished = bool(st.get("started")) and items and all((it or {}).get("status") in ("done", "error") for it in items)
    return not finished and time.time() - mtime < ACTIVE_HOURS * 3600


def inventory(data):
    """Fichiers du disque à transférer (par catégorie), batchs actifs, références manquantes."""
    files, active, missing = [], [], {"export_zips": 0}
    for cat, top in CATEGORIES:
        root = os.path.join(data, top)
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            path = os.path.join(root, entry)
            if not os.path.isdir(path) or entry.startswith("."):
                continue
            if cat == "exports":
                st_path = os.path.join(path, "state.json")
                st = _export_state(st_path)
                if st is not None and _export_active(st, os.path.getmtime(st_path)):
                    active.append(entry)
                    continue
                for g, z in ((st or {}).get("zips") or {}).items():         # archive prête mais absente du disque
                    if str(g).isdigit() and (z or {}).get("status") == "ready" \
                            and not os.path.isfile(os.path.join(path, "zips", f"Batch_{int(g) + 1:03d}.zip")):
                        missing["export_zips"] += 1
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for name in sorted(filenames):
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, data).replace(os.sep, "/")
                    if _skip(rel) or os.path.islink(full) or not os.path.isfile(full):
                        continue
                    files.append({"path": rel, "size": os.path.getsize(full), "category": cat})
    for name in sorted(os.listdir(data)) if os.path.isdir(data) else ():
        full = os.path.join(data, name)
        if os.path.isfile(full) and not os.path.islink(full) and name.endswith((".zip", ".json")) \
                and name.startswith(("batch_", "reels_", "zipbuild_")):
            files.append({"path": name, "size": os.path.getsize(full), "category": "zips"})
    return files, active, missing


def build_archive(data, tmp, active):
    """Archive de métadonnées + manifeste. Retourne (chemin, empreinte, empreinte du contenu, manifeste)."""
    db = os.path.join(data, "videobot_users.db")
    if not os.path.isfile(db):
        raise SystemExit(f"Base introuvable : {db}")
    snap = os.path.join(tmp, "videobot_users.db")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = sqlite3.connect(snap)
    src.backup(out)
    counts = {t: out.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
              for (t,) in out.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
    missing_music = 0
    lib = os.path.join(data, "videobot_libraries")
    if "reels_music" in counts:
        for (fn,) in out.execute("SELECT filename FROM reels_music"):
            if not fn or not os.path.isfile(os.path.join(lib, "music", fn)):
                missing_music += 1
    out.close()
    src.close()
    exports = os.path.join(data, "videobot_video_exports")
    history = []
    if os.path.isdir(exports):
        for name in sorted(os.listdir(exports)):
            path = os.path.join(exports, name)
            if name.startswith(".cleanup_log.jsonl") and os.path.isfile(path):
                history.append((path, name))
            elif name not in active and os.path.isfile(os.path.join(path, "state.json")):
                history.append((os.path.join(path, "state.json"), f"{name}/state.json"))
    settings = {k: os.environ[k] for k in SETTING_KEYS if os.environ.get(k)}
    settings.update({k: bool(os.environ.get(k)) for k in SECRET_KEYS})
    content = hashlib.sha256()
    content.update(open(snap, "rb").read())
    for path, arc in history:
        content.update(arc.encode())
        content.update(open(path, "rb").read())
    if os.path.isdir(lib):
        for dirpath, _d, filenames in sorted(os.walk(lib)):
            for name in sorted(filenames):
                p = os.path.join(dirpath, name)
                content.update(f"{os.path.relpath(p, lib)}:{os.path.getsize(p)}".encode())
    manifest = {"created_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "tables": counts,
                "settings": settings, "active": {"exports": len(active)},
                "missing": {"music": missing_music}}
    return snap, history, lib, manifest, content.hexdigest()


def push_archive(client, tmp, snap, history, lib, manifest):
    archive = os.path.join(tmp, "viralscale_export.tar.gz")
    man_path = os.path.join(tmp, "manifest.json")
    with open(man_path, "w") as f:
        json.dump(manifest, f)
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(snap, arcname="videobot_users.db")
        tar.add(man_path, arcname="manifest.json")
        if os.path.isdir(lib):
            tar.add(lib, arcname="videobot_libraries")
        for path, arc in history:
            tar.add(path, arcname="videobot_video_exports/" + arc)
    size, digest = os.path.getsize(archive), _sha256(archive)
    _log(f"archive : {size} octets, sha256 {digest[:16]}…")
    offset, tries = 0, 0
    with open(archive, "rb") as f:
        while True:
            f.seek(offset)
            status, res = client.post("/studio-import/upload", {"offset": offset, "total": size, "sha256": digest},
                                      data=f.read(CHUNK))
            if status == 507:
                raise NoSpace(res.get("message") or "espace disque insuffisant sur FEMPERIALE")
            if status == 200 and res.get("done"):
                rep = res.get("report") or {}
                _log("métadonnées importées : " + json.dumps({k: rep.get(k) for k in ("users", "tables", "exports")},
                                                            ensure_ascii=False))
                return rep
            if status in (200, 409) and isinstance(res.get("offset"), int):
                offset, tries = res["offset"], 0
                continue
            tries += 1
            if tries > 8:
                raise RuntimeError(f"échec de l'envoi de l'archive (HTTP {status}) : {res}")
            time.sleep(min(60, 2 ** tries))


def push_files(client, data, files, hashes=None):
    """Plan puis envoi des fichiers nécessaires. Retourne les comptes par résultat. `hashes` : cache
    des empreintes (chemin|taille|date → SHA-256), pour ne pas relire tout le disque à chaque essai."""
    totals = {"sent": 0, "already": 0, "no_space": 0, "conflict": 0, "refused": 0, "errors": 0, "bytes_sent": 0}
    hashes = {} if hashes is None else hashes
    for i in range(0, len(files), PLAN_BATCH):
        batch = files[i:i + PLAN_BATCH]
        for e in batch:
            full = os.path.join(data, e["path"])
            key = f"{e['path']}|{e['size']}|{int(os.path.getmtime(full))}"
            e["sha256"] = hashes.get(key) or _sha256(full)
            hashes[key] = e["sha256"]
        status, plan = client.post("/studio-import/files/plan", json_body={"files": batch})
        if status != 200:
            raise RuntimeError(f"plan refusé (HTTP {status}) : {plan}")
        for k in ("already", "no_space", "conflict", "refused"):
            totals[k] += len(plan.get(k) or [])
        by_path = {e["path"]: e for e in batch}
        for rel in plan.get("needed") or []:
            e = by_path.get(rel)
            if e is None:
                continue
            if _send_file(client, data, e):
                totals["sent"] += 1
                totals["bytes_sent"] += e["size"]
            else:
                totals["errors"] += 1
    return totals


def _send_file(client, data, e):
    path, offset, tries = os.path.join(data, e["path"]), 0, 0
    params = {"path": e["path"], "total": e["size"], "sha256": e["sha256"]}
    with open(path, "rb") as f:
        while True:
            f.seek(offset)
            status, res = client.post("/studio-import/files/upload", dict(params, offset=offset), data=f.read(CHUNK))
            if status == 200 and res.get("done"):
                return True
            if isinstance(res.get("offset"), int) and status in (200, 409) and not res.get("message"):
                offset, tries = res["offset"], (tries + 1 if status == 409 else 0)
                if tries > 3:
                    return False
                continue
            tries += 1
            if tries > 6:
                _log(f"fichier non transféré : {e['path']} (HTTP {status} {res.get('error') or ''})")
                return False
            time.sleep(min(30, 2 ** tries))


def run(data, url, token, force=False):
    """Une migration complète (idempotente). Retourne le résumé écrit dans DATA/.femperiale_migration.json."""
    client = Client(url, token)
    state_path = os.path.join(data, STATE_FILE)
    prev = _export_state(state_path) or {}
    files, active, missing = inventory(data)
    tmp = tempfile.mkdtemp(prefix="vs_export_")
    try:
        snap, history, lib, manifest, content = build_archive(data, tmp, set(active))
        cats = {}
        for e in files:
            c = cats.setdefault(e["category"], {"count": 0, "bytes": 0})
            c["count"] += 1
            c["bytes"] += e["size"]
        manifest["files"] = cats
        manifest["missing"].update(missing)
        _log(f"inventaire : {json.dumps(cats)} ; batchs actifs : {len(active)} ; base : {manifest['tables']}")
        if force or prev.get("content_sha256") != content or prev.get("files") != cats:
            push_archive(client, tmp, snap, history, lib, manifest)
        else:
            _log("base inchangée depuis le dernier envoi")
        hashes = prev.get("hashes") if isinstance(prev.get("hashes"), dict) else {}
        totals = push_files(client, data, files, hashes)
    finally:
        for root, dirs, names in os.walk(tmp, topdown=False):
            for n in names:
                os.unlink(os.path.join(root, n))
            for d in dirs:
                os.rmdir(os.path.join(root, d))
        os.rmdir(tmp)
    live = {f"{e['path']}|{e['size']}|{int(os.path.getmtime(os.path.join(data, e['path'])))}" for e in files
            if os.path.exists(os.path.join(data, e["path"]))}
    summary = {"at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()), "url": url, "content_sha256": content,
               "files": cats, "active_exports": len(active), "result": totals,
               "complete": not active and totals["no_space"] == 0 and totals["errors"] == 0 and totals["conflict"] == 0,
               "hashes": {k: v for k, v in hashes.items() if k in live}}
    try:
        with open(state_path + ".tmp", "w") as f:
            json.dump(summary, f)
        os.replace(state_path + ".tmp", state_path)
    except OSError:
        pass
    _log("terminé : " + json.dumps(totals) + ("" if summary["complete"] else " — migration INCOMPLÈTE"))
    return summary


_BACKGROUND = {}


def start_background(data, env=None):
    """ViralScale : envoi AUTOMATIQUE au démarrage, en tâche de fond, si le secret partagé est posé.
    Un seul processus l'exécute (verrou fichier) ; FEMPERIALE_MIGRATION=0 le désactive. Reprises :
    15 min après un échec, 1 h si FEMPERIALE refuse le jeton, 6 h après une migration complète
    (nouvelles données éventuelles, sans doublon)."""
    env = os.environ if env is None else env
    token = token_from_env(env)
    if not token or (env.get("FEMPERIALE_MIGRATION") or "1").strip() == "0" or _BACKGROUND:
        return None
    import fcntl
    import threading
    try:
        fd = os.open(os.path.join(data, ".femperiale_migration.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None                                   # un autre processus s'en charge
    url = (env.get("FEMPERIALE_URL") or env.get("IMPORT_URL") or DEFAULT_URL).rstrip("/")
    delay = int(env.get("FEMPERIALE_MIGRATION_DELAY") or 90)

    def loop():
        time.sleep(delay)
        while True:
            try:
                summary = run(data, url, token)
                result = summary.get("result") or {}
                pause = 6 * 3600 if summary.get("complete") else (
                    3600 if result.get("no_space") and not result.get("errors") else 15 * 60)
            except NoSpace as exc:
                _log(f"FEMPERIALE manque d'espace disque ({exc}) — agrandir son disque ; nouvel essai dans 1 h")
                pause = 3600
            except Refused as exc:
                _log(f"refusé par FEMPERIALE ({exc}) : poser le même STUDIO_IMPORT_TOKEN des deux côtés — nouvel essai dans 1 h")
                pause = 3600
            except Exception as exc:
                _log(f"échec ({type(exc).__name__}: {str(exc)[:200]}) — nouvel essai dans 15 min")
                pause = 15 * 60
            time.sleep(pause)

    thread = threading.Thread(target=loop, name="femperiale-migration", daemon=True)
    _BACKGROUND.update(fd=fd, thread=thread)
    thread.start()
    _log(f"envoi automatique vers {url} programmé dans {delay} s")
    return thread


def main():
    data = os.environ.get("DATA_DIR", "/data")
    url = (os.environ.get("IMPORT_URL") or os.environ.get("FEMPERIALE_URL") or DEFAULT_URL).rstrip("/")
    token = token_from_env()
    if not token:
        sys.exit("IMPORT_TOKEN manquant (même valeur que STUDIO_IMPORT_TOKEN sur FEMPERIALE).")
    run(data, url, token, force=True)


if __name__ == "__main__" or __name__ == "builtins":
    main()
