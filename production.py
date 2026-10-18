"""
production.py — Briques communes de production ViralScale (sans Flask).

Composants réutilisables par tous les moteurs (Batch Video Export, Générateur,
Studio Variations, Reels Studio, Video Optimizer) et par la future production
automatique (sources → production → stockage → distribution) :

  • FileLock          : verrou exclusif flock() inter-threads ET inter-process,
                        libéré par le noyau si le process meurt.
  • atomic_write_json : écriture JSON atomique (tmp + rename).
  • FFmpegGate        : porte COMMUNE pour les traitements FFmpeg lourds de toute
                        l'instance (2 Go de RAM sur Render) : un seul encodage
                        lourd à la fois, attente contrôlée et bornée pour les
                        requêtes HTTP, réentrante par thread, verrou hérité par
                        FFmpeg (un FFmpeg orphelin continue de bloquer tout
                        second encodage). Les workers de fond cèdent la place aux
                        requêtes interactives en attente.
  • limit_ffmpeg_threads : threads FFmpeg explicites (décodeurs, filtres,
                        encodeur, pool x265) — FFmpeg dimensionne sinon ses
                        threads sur les cœurs de la MACHINE HÔTE.
  • OutputFile / export_batch_outputs : inventaire normalisé des fichiers
                        produits (base des futurs exports Google Drive / suivi).

Aucune dépendance à Flask : testable seul, utilisable par un futur worker.
"""
import os
import json
import math
import time
import fcntl
import threading
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from pathlib import Path


class FileLock:
    """Verrou exclusif flock() (bloquant). Exclusion entre threads (descripteurs
    distincts) et entre process ; libéré automatiquement à la mort du process."""

    def __init__(self, path):
        self.path = str(path)
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        except Exception:
            os.close(self.fd); self.fd = None
            raise
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd); self.fd = None
        return False


def atomic_write_json(path, data) -> None:
    """Écrit `data` en JSON de façon atomique : jamais de fichier tronqué."""
    p = str(path)
    tmp = f"{p}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, p)
    except Exception:
        try: os.path.exists(tmp) and os.remove(tmp)
        except Exception: pass
        raise


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ── Ressources CPU / threads FFmpeg ───────────────────────────────

def ffmpeg_cpu_quota() -> float:
    """CPU réellement alloués au conteneur (quota cgroup v2/v1), pas les cœurs
    de l'hôte (os.cpu_count)."""
    try:
        q, p = open("/sys/fs/cgroup/cpu.max").read().split()[:2]
        if q != "max" and int(p) > 0:
            return int(q) / int(p)
    except Exception:
        pass
    try:
        q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        p = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if q > 0 and p > 0:
            return q / p
    except Exception:
        pass
    try:
        return float(len(os.sched_getaffinity(0)))
    except Exception:
        return float(os.cpu_count() or 1)


def ffmpeg_mem_limit():
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(p).read().strip()
            if v and v != "max" and int(v) < (1 << 50):
                return int(v)
        except Exception:
            pass
    return None


def ffmpeg_default_threads(env_var: str = "VEXPORT_FFMPEG_THREADS") -> int:
    """2 à 4 threads selon le quota CPU réel (surcharge : variable d'env)."""
    raw = os.environ.get(env_var)
    if raw:
        try:
            return max(1, min(8, int(raw)))
        except Exception:
            return 2
    return max(2, min(4, math.ceil(ffmpeg_cpu_quota())))


def limit_ffmpeg_threads(cmd, threads: int) -> list:
    """Ajoute des threads EXPLICITES à une commande FFmpeg existante sans en
    changer le sens : `-threads N` avant chaque entrée (décodeurs) et après la
    dernière entrée (encodeur de la 1re sortie), filtres limités, pool x265
    borné. Ne touche pas une commande qui fixe déjà `-threads`."""
    cmd = list(cmd)
    if not cmd or os.path.basename(str(cmd[0])) != "ffmpeg" or "-threads" in cmd:
        return cmd
    th = str(int(threads))
    out = [cmd[0]]
    if "-filter_threads" not in cmd:
        out += ["-filter_threads", "1"]
    if "-filter_complex_threads" not in cmd:
        out += ["-filter_complex_threads", "1"]
    inputs = [i for i, a in enumerate(cmd) if a == "-i" and i + 1 < len(cmd)]
    last_input = inputs[-1] if inputs else None
    i = 1
    while i < len(cmd):
        a = cmd[i]
        if i in inputs:
            out += ["-threads", th, "-i", cmd[i + 1]]
            if i == last_input:
                out += ["-threads", th]
            i += 2
            continue
        out.append(a)
        if a == "libx265" and "-x265-params" not in cmd:
            out += ["-x265-params", f"pools={th}:frame-threads=1"]
        i += 1
    return out


# ── Porte FFmpeg commune ──────────────────────────────────────────

class FFmpegBusy(RuntimeError):
    """Aucun créneau FFmpeg obtenu dans le délai d'attente."""


class FFmpegGate:
    """Un seul traitement FFmpeg LOURD à la fois pour toute l'instance.

    - hold()               : requêtes HTTP — attente bornée (`wait_s`), au plus
                             `max_waiters` threads en attente par process (les
                             autres threads restent libres pour les pages et le
                             suivi de progression) ; FFmpegBusy sinon.
    - hold(blocking=True)  : workers de fond — attente illimitée.
    - réentrant par thread : un appel imbriqué ne se bloque jamais lui-même.
    - le descripteur du verrou est fourni pour être hérité par FFmpeg
      (pass_fds) : un FFmpeg orphelin garde le verrou jusqu'à sa fin.
    - waiting_interactive() : les workers de fond l'interrogent pour céder la
      place aux requêtes en attente entre deux encodages.
    """

    def __init__(self, lock_path, wait_s: float = 150.0, max_waiters: int = 2, poll_s: float = 0.2):
        self.lock_path = Path(lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.waiters_dir = self.lock_path.parent / ".gate_waiters"
        self.waiters_dir.mkdir(parents=True, exist_ok=True)
        self.wait_s = wait_s
        self.poll_s = poll_s
        self._tls = threading.local()
        self._waiters = threading.BoundedSemaphore(max(1, int(max_waiters)))

    def held_by_current_thread(self) -> bool:
        return getattr(self._tls, "depth", 0) > 0

    @contextmanager
    def hold(self, blocking: bool = False, wait_s: float = None):
        if self.held_by_current_thread():
            self._tls.depth += 1
            try:
                yield self._tls.fd
            finally:
                self._tls.depth -= 1
            return
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        acquired = False
        try:
            if blocking:
                fcntl.flock(fd, fcntl.LOCK_EX)
                acquired = True
            else:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    acquired = self._wait(fd, self.wait_s if wait_s is None else wait_s)
                if not acquired:
                    raise FFmpegBusy("Serveur occupé : un autre traitement vidéo est en cours. "
                                     "Réessaie dans quelques instants.")
            self._tls.depth = 1
            self._tls.fd = fd
            try:
                yield fd
            finally:
                self._tls.depth = 0
                self._tls.fd = None
        finally:
            if acquired:
                try: fcntl.flock(fd, fcntl.LOCK_UN)
                except Exception: pass
            os.close(fd)

    def _wait(self, fd: int, wait_s: float) -> bool:
        if not self._waiters.acquire(blocking=False):
            return False                                   # trop de requêtes déjà en attente
        marker = self.waiters_dir / f"{os.getpid()}-{threading.get_ident()}"
        try:
            deadline = time.time() + max(0.0, wait_s)
            last_touch = 0.0
            while time.time() < deadline:
                now = time.time()
                if now - last_touch > 5:
                    try: marker.touch()
                    except Exception: pass
                    last_touch = now
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    time.sleep(self.poll_s)
            return False
        finally:
            try: marker.unlink()
            except Exception: pass
            self._waiters.release()

    def waiting_interactive(self, fresh_s: float = 15.0) -> int:
        """Nombre de requêtes (tous process confondus) en attente du créneau."""
        n, now = 0, time.time()
        try:
            for m in self.waiters_dir.iterdir():
                try:
                    if now - m.stat().st_mtime < fresh_s:
                        n += 1
                    else:
                        m.unlink()                         # marqueur d'un process mort
                except Exception:
                    pass
        except Exception:
            pass
        return n

    def yield_to_interactive(self, max_s: float = 60.0) -> float:
        """Pour un worker de fond, entre deux encodages : attend (au plus
        `max_s`) que les requêtes interactives en attente aient pris leur tour."""
        t0 = time.time()
        while self.waiting_interactive() and time.time() - t0 < max_s:
            time.sleep(0.25)
        return time.time() - t0


# ── Fichiers produits (base des futurs exports / suivi de stock) ──

@dataclass
class OutputFile:
    engine: str            # « video_export », …
    job_id: str
    name: str
    path: str
    size: int
    group: int = 0         # groupe / archive d'appartenance (1 = vidéos 1–100)
    status: str = "done"

    def to_dict(self) -> dict:
        return asdict(self)


def export_batch_outputs(export_dir, state: dict, group_size: int = 100) -> list:
    """Fichiers terminés d'un batch Batch Video Export, dans l'ordre d'import."""
    out = []
    if not state:
        return out
    bid = state.get("batch_id", "")
    odir = Path(export_dir) / bid / "output"
    for pos, it in enumerate(sorted(state.get("items", []), key=lambda x: x["idx"])):
        if it.get("status") != "done":
            continue
        p = odir / it["out_name"]
        try:
            size = p.stat().st_size
        except OSError:
            continue
        out.append(OutputFile("video_export", bid, it["out_name"], str(p), size, pos // group_size + 1))
    return out
