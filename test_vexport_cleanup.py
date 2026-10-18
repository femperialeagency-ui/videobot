#!/usr/bin/env python3
"""
test_vexport_cleanup.py — Nettoyage automatique du stockage (Batch Video Export).

Vrai moteur (ordonnanceur + créateur de ZIP + FFmpeg réels), client Flask :
  1. terminé + « J'ai sauvegardé mes ZIP » → nouveau batch → nettoyage auto
  2. terminé, ZIP non sauvegardés → confirmation obligatoire
  3. refus de confirmation → aucun fichier supprimé
  4. batch actif → aucune suppression ; annulation propre (aucun FFmpeg orphelin)
  5. téléchargement en cours → aucune suppression
  6/7. actualisation pendant / après traitement → reprise, ZIP accessibles
  8. batch nettoyé → aucun état obsolète
  9. deux utilisateurs → isolation totale
  11. process tué PENDANT le nettoyage → reprise sûre
  12. nettoyage répété → idempotent
  13. disque presque plein → message clair, rien supprimé / nettoyage suffisant
  15. 500 vidéos puis nouveau batch de 400 après nettoyage
  + envoi abandonné, état incertain (jamais automatique), action admin ciblée.

Usage : python3 test_vexport_cleanup.py
"""
import os, io, json, time, signal, subprocess, sys, tempfile, zipfile, shutil, uuid, warnings
warnings.filterwarnings("ignore")

REPO = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vexport_clean_test_"))
os.environ.setdefault("ADMIN_EMAIL", "admin-clean@local.test")
os.environ.setdefault("ADMIN_PASSWORD", "admin-clean-password")
os.environ.setdefault("VEXPORT_FFMPEG_THREADS", "2")
sys.path.insert(0, REPO)
import app as A  # noqa: E402

PASS = 0; FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"✅ {name}")
    else:    FAIL += 1; print(f"❌ {name}   {extra}")

tmp = tempfile.mkdtemp(prefix="vexport_clean_src_")
TINY = os.path.join(tmp, "tiny.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x64:d=0.4:r=10", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", TINY, "-loglevel", "error"], check=True)
MID = os.path.join(tmp, "mid.mp4")                    # assez long pour observer un encodage en cours
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:d=4:r=30,noise=alls=25:allf=t+u",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", MID, "-loglevel", "error"], check=True)
TINY_B, MID_B = open(TINY, "rb").read(), open(MID, "rb").read()

c = A.app.test_client()
with c.session_transaction() as s: s["user_id"] = 1
A.create_user("user2-clean@local.test", "user2-clean-password")
uid2 = A.get_user_by_email("user2-clean@local.test")["id"]
c2 = A.app.test_client()
with c2.session_transaction() as s: s["user_id"] = uid2
EXP = A.EXPORT_DIR


def create(cl, confirm=(), expected=None, count=None):
    data = {"clean_previous": "1"}
    if confirm: data["confirm_ids"] = ",".join(confirm)
    if expected is not None: data["expected_bytes"] = str(expected)
    if count is not None: data["expected_count"] = str(count)
    r = cl.post("/vexport_create", data=data)
    return r.status_code, r.get_json()

def fill(cl, bid, n, data=TINY_B, prefix="v"):
    for i in range(n):
        r = cl.post("/vexport_upload", data={"batch_id": bid, "file": (io.BytesIO(data), f"{prefix}_{i + 1:03d}.mp4")},
                    content_type="multipart/form-data").get_json()
        assert r.get("ok"), r

def start(cl, bid):
    assert cl.post("/vexport_start", data={"batch_id": bid}).get_json().get("ok")

def status(cl, bid):
    return cl.get(f"/vexport_status/{bid}").get_json()

def wait(cl, bid, cond, timeout=600, step=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = status(cl, bid)
        if cond(d):
            return d
        time.sleep(step)
    return status(cl, bid)

def settled(d):
    return d.get("progress", {}).get("finished") and d.get("archives_settled")

def files(bid):
    root = EXP / bid
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []

def tomb_only(bid):
    return set(files(bid)) <= {"state.json", ".state.lock"} and A._vexport_load_state(bid).get("cleaned") is True

def mark_saved(cl, bid):
    return cl.post("/vexport_mark_saved", data={"batch_id": bid})

def current(cl):
    return cl.get("/vexport_current").get_json().get("batch_id")

def export_ffmpeg(bid=None):
    out = subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if l.startswith("ffmpeg ") and str(EXP / (bid or "")) in l]

def log_lines():
    try:
        return [json.loads(l) for l in open(A._VEXPORT_CLEAN_LOG, encoding="utf-8")]
    except Exception:
        return []


# ══ 1. Terminé + sauvegarde validée → nouveau batch → nettoyage auto ══════
code, d = create(c)
check("1. premier batch créé (rien à nettoyer)", code == 200 and d["cleaned"]["batches"] == 0, str(d))
A1 = d["batch_id"]
fill(c, A1, 3); start(c, A1)
dA = wait(c, A1, settled)
check("1. batch terminé, ZIP prêt", settled(dA) and dA["groups"][0]["zip"]["status"] == "ready", str(dA.get("progress")))
check("1. pas encore de validation de sauvegarde", dA["saved_confirmed"] is False)
rh = c.head(f"/vexport_group_zip/{A1}/1"); rh.close()
rz = c.get(f"/vexport_group_zip/{A1}/1"); zok = rz.status_code == 200 and zipfile.ZipFile(io.BytesIO(rz.data)).testzip() is None; rz.close()
dA = status(c, A1)
check("4. téléchargement ZIP enregistré côté serveur (HEAD non compté)",
      zok and dA["downloads"] == {"0": 1} and dA["groups"][0]["zip"]["downloads"] == 1, str(dA["downloads"]))
r = mark_saved(c, A1)
check("1. « J'ai sauvegardé mes ZIP » persisté", r.status_code == 200 and status(c, A1)["saved_confirmed"] is True
      and A._vexport_load_state(A1)["saved_confirmed"] is True)
beforeA = files(A1)
check("1. fichiers présents avant nettoyage (vidéos + ZIP)",
      sum(f.startswith("output/") and f.endswith(".mp4") for f in beforeA) == 3 and "zips/Batch_001.zip" in beforeA, str(beforeA))
code, d = create(c)
check("1. nouveau batch : ancien nettoyé SANS confirmation", code == 200 and d["cleaned"]["batches"] == 1
      and d["cleaned"]["freed"] > 0, f"{code} {d}")
B1 = d["batch_id"]
check("1. ancien batch : uniquement les métadonnées minimales", tomb_only(A1), str(files(A1)))
stA = A._vexport_load_state(A1)
check("1. métadonnées de traçabilité conservées", stA["summary"]["done"] == 3 and stA["clean_auth"] == "saved"
      and stA["freed_bytes"] > 0 and stA["items"] == [], str(stA.get("summary")))
check("1. journal de nettoyage", any(e["batch_id"] == A1 and e["auth"] == "saved" for e in log_lines()))
check("8. statut d'un batch nettoyé : aucun fichier réaffiché",
      status(c, A1).get("cleaned") is True and "items" not in status(c, A1) and "groups" not in status(c, A1))
check("8. ZIP / vidéo d'un batch nettoyé → 410",
      c.get(f"/vexport_group_zip/{A1}/1").status_code == 410
      and c.get(f"/vexport_file/{A1}/{dA['items'][0]['out_name']}").status_code == 410)
check("8. upload / relance / validation sur batch nettoyé refusés",
      c.post("/vexport_retry", data={"batch_id": A1}).status_code == 410
      and mark_saved(c, A1).status_code == 410)
check("8. reprise : aucun batch courant (nouveau batch pas encore lancé)", current(c) is None)

# ══ 2–3. ZIP non sauvegardés → confirmation ; refus → rien supprimé ══════
fill(c, B1, 3); start(c, B1)
dB = wait(c, B1, settled)
snapB = files(B1)
code, d = create(c)
check("2. ZIP non sauvegardés → confirmation obligatoire (409)", code == 409 and d.get("code") == "confirm_cleanup", f"{code} {d}")
check("2. message clair", "Avez-vous sauvegardé vos ZIP" in (d.get("error") or ""), d.get("error"))
bl = (d.get("batches") or [{}])[0]
check("2. détail : batch, vidéos, ZIP prêts / téléchargés, taille",
      bl.get("batch_id") == B1 and bl.get("done") == 3 and bl.get("zips_ready") == 1
      and bl.get("zips_downloaded") == 0 and bl.get("bytes", 0) > 0, str(bl))
code2, d2 = create(c, confirm=[uuid.uuid4().hex])
check("3. confirmation d'un AUTRE identifiant → toujours refusé", code2 == 409 and d2.get("code") == "confirm_cleanup")
check("3. refus : aucun fichier supprimé", files(B1) == snapB)
rz = c.get(f"/vexport_group_zip/{B1}/1"); check("3. refus : ZIP toujours téléchargeable", rz.status_code == 200); rz.close()
check("3. refus : aucun nouveau batch créé", len([p for p in EXP.iterdir() if p.is_dir() and A._vexport_safe_id(p.name)]) == 2)
code, d = create(c, confirm=[B1])
check("2. « Supprimer et continuer » → nettoyé puis nouveau batch", code == 200 and d["cleaned"]["batches"] == 1
      and tomb_only(B1), f"{code} {d}")
C1 = d["batch_id"]

# ══ 4. Batch actif → aucune suppression ; annulation propre ══════════════
fill(c, C1, 4, MID_B, prefix="mid"); start(c, C1)
dC = wait(c, C1, lambda x: x["progress"]["processing"] >= 1, timeout=60, step=0.05)
snapC = files(C1)
code, d = create(c)
check("4. export actif → 409 (patienter / annuler)", code == 409 and d.get("code") == "batch_active"
      and d["batch"]["batch_id"] == C1 and d["batch"]["busy"] in ("encoding", "queued"), f"{code} {d}")
code, d = create(c, confirm=[C1])
check("4. une confirmation ne permet PAS de supprimer un export actif", code == 409 and d.get("code") == "batch_active")
def lost_files(bid, snap):
    """Fichiers disparus, hors effets normaux de l'encodage (source supprimée
    après succès, .part / log FFmpeg temporaires)."""
    done_in = {it["in_name"] for it in A._vexport_load_state(bid)["items"] if it["status"] == "done"}
    return [f for f in snap if not (EXP / bid / f).exists() and not f.endswith((".part", ".log"))
            and not (f.startswith("input/") and f[6:] in done_in)]
check("4. aucun fichier de l'export actif supprimé", lost_files(C1, snapC) == [], str(lost_files(C1, snapC)))
check("9. un autre utilisateur ne peut pas annuler", c2.post("/vexport_cancel", data={"batch_id": C1}).status_code == 403)
r = c.post("/vexport_cancel", data={"batch_id": C1}).get_json()
check("4. annulation acceptée (rien supprimé à ce stade)", r.get("cancelled") is True and r.get("cleaned") is False, str(r))
dC = wait(c, C1, lambda x: x["progress"]["processing"] == 0 and x["progress"]["finished"] and x["archives_settled"], timeout=60)
check("4. annulation : encodage arrêté proprement", dC["progress"]["processing"] == 0 and dC["cancelled"] is True, str(dC["progress"]))
check("4. annulation : vidéos restantes « Annulé »", any(it["status"] == "error" and "Annulé" in it["error"] for it in dC["items"]))
time.sleep(1.0)
check("4. aucun FFmpeg orphelin après annulation", export_ffmpeg(C1) == [], str(export_ffmpeg(C1)))
check("4. batch annulé : plus actif, relance refusée", not A._vexport_is_active(A._vexport_load_state(C1))
      and c.post("/vexport_retry", data={"batch_id": C1}).status_code == 410)
code, d = create(c)
if dC["progress"]["done"]:
    check("4. après annulation : vidéos exportées → confirmation", code == 409 and d.get("code") == "confirm_cleanup")
    code, d = create(c, confirm=[C1])
check("4. après annulation + confirmation : nettoyé, nouveau batch", code == 200 and tomb_only(C1), f"{code} {d}")
D1 = d["batch_id"]

# ══ 5. Téléchargement en cours → aucune suppression ══════════════════════
fill(c, D1, 3); start(c, D1)
wait(c, D1, settled)
mark_saved(c, D1)
snapD = files(D1)
rs = c.get(f"/vexport_group_zip/{D1}/1", buffered=False)
check("5. téléchargement en cours enregistré (marqueur actif)", rs.status_code == 200 and A._vexport_active_downloads(D1) == 1)
code, d = create(c)
check("5. téléchargement actif → aucune suppression (409)", code == 409 and d["batch"]["busy"] == "downloading", f"{code} {d}")
check("5. fichiers intacts pendant le téléchargement", set(snapD) <= set(files(D1)))
data = b"".join(rs.response); rs.close()
check("5. téléchargement complet et valide", zipfile.ZipFile(io.BytesIO(data)).testzip() is None)
check("5. fin du téléchargement → marqueur retiré", A._vexport_active_downloads(D1) == 0)
code, d = create(c)
check("5. après téléchargement : nettoyage auto", code == 200 and tomb_only(D1), f"{code} {d}")
E1 = d["batch_id"]

# ══ 6–7. Actualisation pendant / après traitement ════════════════════════
fill(c, E1, 3, MID_B, prefix="ref"); start(c, E1)
wait(c, E1, lambda x: x["progress"]["processing"] >= 1, timeout=60, step=0.05)
c_new = A.app.test_client()                            # nouvel onglet / appareil (pas de localStorage)
with c_new.session_transaction() as s: s["user_id"] = 1
check("6. actualisation pendant traitement : batch retrouvé", current(c) == E1 and current(c_new) == E1)
dE = status(c_new, E1)
check("6. progression reprise normalement", dE["started"] and not dE["progress"]["finished"]
      and dE["progress"]["total"] == 3, str(dE["progress"]))
dE = wait(c_new, E1, settled)
check("7. actualisation après traitement : batch terminé retrouvé", current(c_new) == E1 and settled(dE))
rz = c_new.get(dE["groups"][0]["zip"]["url"])
check("7. ZIP toujours téléchargeable après actualisation", rz.status_code == 200 and len(zipfile.ZipFile(io.BytesIO(rz.data)).namelist()) == 3)
rz.close()
mark_saved(c_new, E1)
code, d = create(c)
check("8. batch nettoyé → plus jamais proposé à la reprise", code == 200 and tomb_only(E1) and current(c) is None
      and status(c, E1).get("cleaned") is True)
F1 = d["batch_id"]

# ══ 9. Deux utilisateurs ═════════════════════════════════════════════════
code, d = create(c2)
U2 = d["batch_id"]
fill(c2, U2, 2); start(c2, U2); wait(c2, U2, settled)
fill(c, F1, 2); start(c, F1); wait(c, F1, settled)
snapU2, snapF1 = files(U2), files(F1)
code, d = create(c2)
check("9. utilisateur 2 : seule SA confirmation demandée", code == 409 and [b["batch_id"] for b in d["batches"]] == [U2])
code, d = create(c2, confirm=[F1])
check("9. confirmer l'ID d'un autre utilisateur : sans effet", code == 409 and files(F1) == snapF1)
code, d = create(c, confirm=[U2])
check("9. utilisateur 1 : ne voit que son batch", code == 409 and [b["batch_id"] for b in d["batches"]] == [F1])
check("9. aucun fichier de l'autre utilisateur touché", files(U2) == snapU2 and files(F1) == snapF1)
check("9. accès croisés refusés (statut, validation, annulation)",
      c2.get(f"/vexport_status/{F1}").status_code == 403 and mark_saved(c2, F1).status_code == 403
      and c2.post("/vexport_cancel", data={"batch_id": F1}).status_code == 403 and current(c2) == U2)
with A._vexport_flock(A._VEXPORT_CLEAN_LOCK):
    r = A._vexport_clean_batch(U2, user_id=1, confirmed=True)
check("9. fonction centrale : propriétaire vérifié", r.get("forbidden") is True and files(U2) == snapU2)
inv = c.get("/vexport_storage")
check("9. inventaire stockage réservé à l'admin", inv.status_code == 200 and c2.get("/vexport_storage").status_code == 403
      and {U2, F1} <= {b["batch_id"] for b in inv.get_json()["batches"]})

# ══ 11. Process tué PENDANT le nettoyage → reprise sûre ═════════════════
mark_saved(c, F1)
(EXP / F1 / "output" / "extra").mkdir()
for i in range(6):
    (EXP / F1 / "output" / "extra" / f"junk_{i}.bin").write_bytes(b"x" * 1000)
n0 = len(files(F1))
killer = f"""
import os, sys, time
sys.path.insert(0, {REPO!r})
import app as A
real = os.unlink
def slow(p, *a, **k):
    time.sleep(0.5); return real(p, *a, **k)
os.unlink = slow
with A._vexport_flock(A._VEXPORT_CLEAN_LOCK):
    A._vexport_clean_batch({F1!r}, system=True, reason="test_kill")
"""
env = dict(os.environ, VEXPORT_WORKER="0")
pk = subprocess.Popen([sys.executable, "-c", killer], cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
deadline = time.time() + 60
while time.time() < deadline:
    st = A._vexport_load_state(F1) or {}
    if st.get("cleaning") and len(files(F1)) < n0 - 1:
        break
    time.sleep(0.05)
pk.send_signal(signal.SIGKILL); pk.wait()
stF = A._vexport_load_state(F1)
left = files(F1)
check("11. nettoyage interrompu en plein milieu (kill -9)", stF.get("cleaning") and not stF.get("cleaned")
      and 2 < len(left) < n0, f"{len(left)}/{n0}")
check("11. batch en cours de nettoyage jamais réaffiché ni servi",
      status(c, F1).get("cleaned") is True and c.get(f"/vexport_group_zip/{F1}/1").status_code == 410)
A._cleanup_stale_vexport()
check("11. reprise automatique : nettoyage terminé", tomb_only(F1), str(files(F1)))
check("11. traçabilité conservée après reprise", A._vexport_load_state(F1)["clean_reason"] == "test_kill"
      and A._vexport_load_state(F1)["freed_bytes"] > 0)
# ══ 12. Idempotence ══════════════════════════════════════════════════════
with A._vexport_flock(A._VEXPORT_CLEAN_LOCK):
    r1 = A._vexport_clean_batch(F1, user_id=1)
    r2 = A._vexport_clean_batch(F1, system=True)
A._cleanup_stale_vexport(); A._cleanup_stale_vexport()
check("12. nettoyage répété : aucune erreur", r1.get("ok") and r1.get("already") and r2.get("ok") and tomb_only(F1))
r1 = c.post("/vexport_cleanup", json={"batch_id": F1}); r2 = c.post("/vexport_cleanup", json={"batch_id": F1})
check("12. suppression explicite répétée : 200 puis 404 (jamais 500)", r1.status_code == 200 and r2.status_code == 404
      and not (EXP / F1).exists())

# ══ Envoi abandonné / interrompu ═════════════════════════════════════════
code, d = create(c)
G1 = d["batch_id"]
fill(c, G1, 2)
code, d = create(c)
check("Envoi en cours (non lancé, récent) → protégé (409)", code == 409 and d["batch"]["busy"] == "uploading")
r = c.post("/vexport_cancel", data={"batch_id": G1, "only_unstarted": "1"}).get_json()
check("Envoi interrompu par actualisation → abandonné et nettoyé", r.get("cleaned") is True and tomb_only(G1))
check("Upload vers un envoi annulé refusé",
      c.post("/vexport_upload", data={"batch_id": G1, "file": (io.BytesIO(TINY_B), "x.mp4")},
             content_type="multipart/form-data").status_code == 410)
code, d = create(c)
H1 = d["batch_id"]
fill(c, H1, 1)
r = c.post("/vexport_cancel", data={"batch_id": H1, "only_unstarted": "1"}).get_json()
check("only_unstarted sur envoi non lancé → nettoyé", r.get("cleaned") is True and tomb_only(H1))
code, d = create(c)
H2 = d["batch_id"]
fill(c, H2, 1)
st = A._vexport_load_state(H2); st["updated"] = time.time() - 1000
A._vexport_state_path(H2).write_text(json.dumps(st))
code, d = create(c)
check("Envoi abandonné depuis > 15 min → nettoyé automatiquement", code == 200 and d["cleaned"]["batches"] == 1
      and tomb_only(H2), f"{code} {d}")
I1 = d["batch_id"]
fill(c, I1, 1); start(c, I1)
r = c.post("/vexport_cancel", data={"batch_id": I1, "only_unstarted": "1"}).get_json()
check("only_unstarted sur batch lancé → aucun effet", r.get("cancelled") is False
      and not A._vexport_load_state(I1).get("cancelled"))
wait(c, I1, settled)

# ══ 13. Disque presque plein ═════════════════════════════════════════════
real_free = A._disk_free_bytes
snapI = files(I1)
sizeI = A._vexport_dir_size(I1)
A._disk_free_bytes = lambda path=None: 100_000_000
code, d = create(c, expected=1_000_000_000, count=500)
check("13. disque insuffisant même après nettoyage → 507 + message clair",
      code == 507 and d.get("code") == "disk_full" and "insuffisant" in d["error"] and "Go" in d["error"], f"{code} {d}")
code, d = create(c, confirm=[I1], expected=1_000_000_000, count=500)
check("13. traitement impossible : rien supprimé (même confirmé)", code == 507 and files(I1) == snapI)
need = A.VEXPORT_MIN_FREE_CREATE + 2 * 1000
base = need - sizeI // 2
A._disk_free_bytes = lambda path=None: base + (sizeI if (A._vexport_load_state(I1) or {}).get("cleaned") else 0)
code, d = create(c, expected=1000, count=1)
check("13. nettoyage suffisant → confirmation demandée d'abord", code == 409 and d.get("code") == "confirm_cleanup")
code, d = create(c, confirm=[I1], expected=1000, count=1)
check("13. nettoyage → espace recalculé → batch créé", code == 200 and d["cleaned"]["batches"] == 1 and tomb_only(I1), f"{code} {d}")
J1 = d["batch_id"]
A._disk_free_bytes = real_free

# ══ État incertain : jamais automatique, action admin ciblée confirmée ═══
unk = uuid.uuid4().hex
(EXP / unk / "output").mkdir(parents=True)
(EXP / unk / "output" / "old.mp4").write_bytes(b"0" * 5000)
old = time.time() - 30 * 3600
for p in (EXP / unk / "output" / "old.mp4", EXP / unk / "output", EXP / unk):
    os.utime(p, (old, old))
A._cleanup_stale_vexport(max_age_hours=1)
check("8. ancien dossier à l'état incertain : jamais supprimé automatiquement", (EXP / unk / "output" / "old.mp4").exists())
row = next((b for b in c.get("/vexport_storage").get_json()["batches"] if b["batch_id"] == unk), {})
check("8. inventaire admin : signalé « incertain »", row.get("state") == "incertain" and row.get("bytes") == 5000, str(row))
check("8. action ciblée : confirmation obligatoire, admin uniquement",
      c.post("/vexport_storage_clean", json={"batch_id": unk}).status_code == 400
      and c2.post("/vexport_storage_clean", json={"batch_id": unk, "confirm": 1}).status_code == 403
      and c.post("/vexport_storage_clean", json={"batch_id": "../" + unk, "confirm": 1}).status_code == 404)
r = c.post("/vexport_storage_clean", json={"batch_id": unk, "confirm": 1})
check("8. action ciblée confirmée → supprimé, rien d'autre", r.status_code == 200 and not (EXP / unk).exists()
      and (EXP / J1).exists() and (EXP / U2).exists())

# ══ 15. 500 vidéos → nettoyage → nouveau batch de 400 ════════════════════
t0 = time.time()
fill(c, J1, 500, prefix="j")
r501 = c.post("/vexport_upload", data={"batch_id": J1, "file": (io.BytesIO(TINY_B), "x.mp4")},
              content_type="multipart/form-data").get_json()
check("15. limite 500 conservée", "500" in (r501.get("error") or ""))
start(c, J1)
dJ = wait(c, J1, settled, timeout=1800, step=1.0)
check("15. 500 vidéos exportées, 5 ZIP progressifs", dJ["progress"]["done"] == 500
      and [g["zip"]["status"] for g in dJ["groups"]] == ["ready"] * 5, str(dJ["progress"]))
print(f"   ⏱️  500 vidéos : {time.time() - t0:.0f} s")
for g in range(1, 6):
    rz = c.get(f"/vexport_group_zip/{J1}/{g}"); rz.close()
mark_saved(c, J1)
sizeJ = A._vexport_dir_size(J1)
code, d = create(c, expected=400 * len(TINY_B), count=400)
check("15. nouveau batch de 400 : ancien batch de 500 nettoyé automatiquement",
      code == 200 and d["cleaned"]["batches"] == 1 and d["cleaned"]["freed"] >= sizeJ * 0.9 and tomb_only(J1), f"{code} {d}")
K1 = d["batch_id"]
fill(c, K1, 400, prefix="k"); start(c, K1)
dK = wait(c, K1, lambda x: x["progress"]["done"] >= 5, timeout=120)
check("15. batch de 400 démarré correctement après nettoyage", dK["progress"]["total"] == 400 and dK["progress"]["done"] >= 5)
c.post("/vexport_cancel", data={"batch_id": K1})
wait(c, K1, lambda x: x["progress"]["finished"] and x["archives_settled"], timeout=120)
code, d = create(c, confirm=[K1])
check("15. annulation puis nettoyage confirmé", code == 200 and tomb_only(K1))

# ══ Bilan ════════════════════════════════════════════════════════════════
time.sleep(0.5)
check("Aucun FFmpeg d'export résiduel", export_ffmpeg() == [], str(export_ffmpeg()))
leftovers = [p for p in EXP.rglob("*") if p.name.endswith((".part", ".tmp"))]
check("Aucun fichier temporaire résiduel", leftovers == [], str(leftovers))
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'=' * 40}\n{PASS} tests OK / {FAIL} KO")
sys.exit(1 if FAIL else 0)
