"""
serve.py — commande de démarrage du conteneur (Dockerfile CMD).

  - PRODUCTION_WORKER_ENABLED absent ou ≠ 1 (défaut) : ce processus est REMPLACÉ par Gunicorn
    avec exactement les options d'avant (os.execvp) → comportement strictement identique.
  - PRODUCTION_WORKER_ENABLED=1 : Gunicorn (site ViralScale, inchangé) ET le worker de production
    (production_worker.py) tournent comme DEUX processus séparés, sur la même instance et le même
    disque (aucun changement d'offre Render). Le worker est relancé s'il s'arrête (délai croissant,
    pas de relance si sa configuration est incomplète) ; SIGTERM / SIGINT (redéploiement Render)
    sont transmis aux deux ; le conteneur s'arrête avec Gunicorn. Les processus orphelins
    (FFmpeg d'un worker arrêté brutalement) sont récupérés par ce processus (PID 1).
"""
import os
import signal
import sys
import time

GUNICORN = ["gunicorn", "app:app",
            "--bind", "0.0.0.0:10000",
            "--workers", "1",
            "--threads", "4",
            "--timeout", "0",
            "--keep-alive", "65",
            "--log-level", "info"]
WORKER = [sys.executable, "production_worker.py"]
CONFIG_ERROR = 2                     # code de sortie du worker : configuration incomplète
STOP_GRACE = 25                      # secondes laissées aux processus avant SIGKILL


def log(msg):
    print(f"[SERVE] {msg}", file=sys.stderr, flush=True)


def worker_enabled(env=None):
    env = os.environ if env is None else env
    return (env.get("PRODUCTION_WORKER_ENABLED") or "0").strip() == "1"


class Supervisor:
    def __init__(self, web_cmd, worker_cmd, backoff=(5, 10, 30, 60, 120, 300), stable_after=600):
        self.web_cmd, self.worker_cmd = web_cmd, worker_cmd
        self.backoff, self.stable_after = backoff, stable_after
        self.web = self.worker = None
        self.stopping = False
        self.failures = 0
        self.worker_started = 0.0
        self.worker_due = 0.0

    def _spawn(self, cmd, extra_env=None):
        env = dict(os.environ, **(extra_env or {}))
        pid = os.fork()
        if pid == 0:                                    # enfant
            try:
                os.execvpe(cmd[0], cmd, env)
            finally:
                os._exit(127)
        return pid

    def _start_worker(self):
        self.worker = self._spawn(self.worker_cmd, {"VEXPORT_WORKER": "0"})
        self.worker_started = time.monotonic()
        log(f"worker de production démarré (pid {self.worker})")

    def _signal(self, signum, _frame):
        if not self.stopping:
            log(f"signal {signum} : arrêt de Gunicorn et du worker")
        self.stopping = True
        for pid in (self.web, self.worker):
            if pid:
                try:
                    os.kill(pid, signum)
                except ProcessLookupError:
                    pass

    def run(self):
        signal.signal(signal.SIGTERM, self._signal)
        signal.signal(signal.SIGINT, self._signal)
        self.web = self._spawn(self.web_cmd)
        log(f"Gunicorn démarré (pid {self.web})")
        self._start_worker()
        web_code, deadline = None, None
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                pid, status = 0, 0
            if pid:
                code = os.waitstatus_to_exitcode(status)
                if pid == self.web:
                    self.web, web_code = None, code
                    log(f"Gunicorn arrêté (code {code})")
                    if not self.stopping:
                        self._signal(signal.SIGTERM, None)
                elif pid == self.worker:
                    self.worker = None
                    self._worker_exited(code)
                continue                                # autre processus orphelin : simplement récupéré
            if self.stopping:
                deadline = deadline or time.monotonic() + STOP_GRACE
                if self.web is None and self.worker is None:
                    return web_code if web_code is not None else 0
                if time.monotonic() > deadline:
                    for p in (self.web, self.worker):
                        if p:
                            try:
                                os.kill(p, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
            elif self.worker is None and self.worker_due and time.monotonic() >= self.worker_due:
                self.worker_due = 0.0
                self._start_worker()
            time.sleep(0.5)

    def _worker_exited(self, code):
        if self.stopping:
            return
        if code == CONFIG_ERROR:
            log("worker arrêté : configuration incomplète — pas de relance (corriger les variables Render)")
            return
        if time.monotonic() - self.worker_started > self.stable_after:
            self.failures = 0
        delay = self.backoff[min(self.failures, len(self.backoff) - 1)]
        self.failures += 1
        self.worker_due = time.monotonic() + delay
        log(f"worker arrêté (code {code}) : relance dans {delay} s")


def main():
    if not worker_enabled():
        os.execvp(GUNICORN[0], GUNICORN)                # identique à l'ancienne commande Docker
    return Supervisor(GUNICORN, WORKER).run()


if __name__ == "__main__":
    sys.exit(main())
