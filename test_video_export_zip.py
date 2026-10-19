#!/usr/bin/env python3
"""
test_video_export_zip.py — Batch Video Export V2 : ZIP progressifs de 100.

Endpoints Flask réels, FFmpeg réel, archives réelles sur disque. Couvre :
  1–7. batchs de 1, 2, 99, 100, 101, 235, 500 vidéos → bon découpage en
       groupes, 1 ZIP par groupe, contenu exact, CRC, aucune double création,
       aucun double encodage ;
  8.   refus de la 501e vidéo ;
  9–11. 1er ZIP créé automatiquement AVANT la fin du batch, encodages qui
       continuent, téléchargement d'un ZIP pendant le traitement ;
  12.  « Tout télécharger » : désactivé pendant le traitement, activé quand
       toutes les archives sont prêtes (logique UI : test_video_export_ui.mjs) ;
  13–14. vidéo en erreur → ZIP des seules réussites, relance ciblée →
       archive reconstruite, téléchargement en cours jamais coupé ;
  15.  ZIP temporaire (.part) jamais téléchargeable, chemins arbitraires refusés ;
  16.  ZIP corrompu détecté (tronqué / répertoire central abîmé) → reconstruit ;
  19–20. plusieurs utilisateurs simultanés + isolation des téléchargements ;
  21–22. disque presque plein (ZIP, encodage, ENOSPC en pleine écriture) puis
       reprise automatique après libération d'espace ;
  23.  nettoyage des vieux batchs (un batch avec archive à créer est protégé) ;
  26–27. téléchargements individuels + progression globale (non-régression).
  (17–18. redémarrages réels : test_video_export_restart.py)

Usage : DATA_DIR=/un/dossier python3 test_video_export_zip.py
"""
import os, io, json, time, errno, subprocess, tempfile, zipfile, sys, shutil, threading, warnings
warnings.filterwarnings("ignore")

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vexport_zip_test_"))
os.environ.setdefault("ADMIN_EMAIL", "admin-zip@local.test")
os.environ.setdefault("ADMIN_PASSWORD", "admin-zip-password")
os.environ.setdefault("VEXPORT_FFMPEG_THREADS", "2")
import app as A

PASS = 0; FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"✅ {name}")
    else:    FAIL += 1; print(f"❌ {name}   {extra}")

tmp = tempfile.mkdtemp(prefix="vexport_zip_src_")
TINY = os.path.join(tmp, "tiny.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x64:d=0.4:r=10", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", TINY, "-loglevel", "error"], check=True)
TINY_BYTES = open(TINY, "rb").read()

c = A.app.test_client()
with c.session_transaction() as s: s["user_id"] = 1
A.create_user("user2-zip@local.test", "user2-zip-password")
uid2 = A.get_user_by_email("user2-zip@local.test")["id"]
c2 = A.app.test_client()
with c2.session_transaction() as s: s["user_id"] = uid2


class FfmpegSampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True); self.max = 0; self.ev = threading.Event()
    def run(self):
        while not self.ev.is_set():
            out = subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True).stdout
            self.max = max(self.max, sum(1 for l in out.splitlines()
                                         if l.startswith("ffmpeg ") and str(A.EXPORT_DIR) in l and "-progress" in l))
            time.sleep(0.05)
sampler = FfmpegSampler(); sampler.start()


def create_batch(cl, n, prefix="clip", bad=()):
    bid = cl.post("/vexport_create").get_json()["batch_id"]
    if n > A.VEXPORT_MAX_FILES:                         # batch créé avant la limite 300 (sans « max_files ») : limite 500
        with A._vexport_state_lock(bid):
            st0 = A._vexport_load_state(bid); st0.pop("max_files", None); A._vexport_save_state(st0)
    for i in range(n):
        r = cl.post("/vexport_upload", data={"batch_id": bid, "file": (io.BytesIO(TINY_BYTES), f"{prefix}_{i + 1:03d}.mp4")},
                    content_type="multipart/form-data").get_json()
        assert r.get("ok"), r
    st = A._vexport_load_state(bid)
    for i in bad:                                       # source lisible à l'upload, mais FFmpeg échouera
        (A.EXPORT_DIR / bid / "input" / st["items"][i]["in_name"]).write_bytes(b"\x00garbage" * 2048)
    return bid

def status(cl, bid):
    return cl.get(f"/vexport_status/{bid}").get_json()

def wait_settled(cl, bid, timeout=600, step=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = status(cl, bid)
        if d.get("progress", {}).get("finished") and d.get("archives_settled"):
            return d
        time.sleep(step)
    return status(cl, bid)

def zip_names(cl, bid, n):
    r = cl.get(f"/vexport_group_zip/{bid}/{n}")
    if r.status_code != 200:
        r.close(); return None, r.status_code
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        ok = z.testzip() is None
        names = z.namelist()
    r.close()
    return (names if ok else None), 200

def zstate(bid, g):
    return (A._vexport_load_state(bid).get("zips") or {}).get(str(g)) or {}

def expected_sizes(n):
    return [min(100, n - 100 * g) for g in range((n + 99) // 100)]


# ══ 1–8. Découpage en groupes : 1, 2, 99, 100, 101, 235, 500 (+ 501 refusé) ══
progressive = {}
for n in (1, 2, 99, 100, 101, 235, 500):
    t0 = time.time()
    bid = create_batch(c, n, prefix=f"b{n}")
    if n == 500:
        r501 = c.post("/vexport_upload", data={"batch_id": bid, "file": (io.BytesIO(TINY_BYTES), "extra.mp4")},
                      content_type="multipart/form-data").get_json()
        check("8. ancien batch (avant la limite 300) : 501e vidéo refusée avec message clair", "error" in r501 and "500" in r501["error"], str(r501))
        bnew = create_batch(c, 300, prefix="n300")
        r301 = c.post("/vexport_upload", data={"batch_id": bnew, "file": (io.BytesIO(TINY_BYTES), "extra.mp4")},
                      content_type="multipart/form-data").get_json()
        check("8. nouveau batch : 301e vidéo refusée (limite 300)", "error" in r301 and "300" in r301["error"], str(r301))
        c.post("/vexport_cancel", data={"batch_id": bnew, "delete": "1"})
    c.post("/vexport_start", data={"batch_id": bid})
    if n in (235, 500):
        # 9–11 : le 1er ZIP doit être prêt et téléchargeable PENDANT le traitement.
        seen, glob = None, []
        deadline = time.time() + 300
        while time.time() < deadline:
            d = status(c, bid)
            glob.append(d["progress"]["percent"])
            check_all = d.get("all_zips_ready")
            if d["groups"][0]["zip"]["status"] == "ready" and not d["progress"]["finished"]:
                seen = d; break
            if d["progress"]["finished"]:
                break
            time.sleep(0.1)
        progressive[n] = seen
        check(f"9. [{n}] ZIP 1 créé automatiquement avant la fin du batch", seen is not None)
        if seen:
            check(f"12. [{n}] « Tout télécharger » désactivé pendant le traitement", seen["all_zips_ready"] is False)
            done_before = seen["progress"]["done"]
            names1, code1 = zip_names(c, bid, 1)
            check(f"11. [{n}] ZIP 1 téléchargé pendant le traitement (100 vidéos, CRC OK)",
                  code1 == 200 and names1 and len(names1) == 100, f"{code1} {names1 and len(names1)}")
            check(f"9. [{n}] ZIP 2 pas encore proposé", seen["groups"][1]["zip"]["url"] == "")
            time.sleep(1.0)
            d2 = status(c, bid)
            check(f"10. [{n}] encodages poursuivis après le 1er ZIP",
                  d2["progress"]["done"] > done_before, f"{done_before} → {d2['progress']['done']}")
            check(f"27. [{n}] % global jamais décroissant", all(b >= a for a, b in zip(glob, glob[1:])))
    d = wait_settled(c, bid)
    st = A._vexport_load_state(bid)
    sizes = [g["total"] for g in d["groups"]]
    check(f"{n} vidéos : groupes {expected_sizes(n)}", sizes == expected_sizes(n), str(sizes))
    check(f"{n} vidéos : toutes terminées, un seul batch logique", d["progress"]["done"] == n
          and d["batch_id"] == bid and len([p for p in A.EXPORT_DIR.iterdir() if p.is_dir() and p.name == bid]) == 1)
    all_ok = True; names_all = []
    for g in d["groups"]:
        names, code = zip_names(c, bid, g["n"])
        exp = [it["out_name"] for it in sorted(st["items"], key=lambda x: x["idx"])][(g["n"] - 1) * 100: g["n"] * 100]
        if names != exp:
            all_ok = False
        names_all += names or []
    check(f"{n} vidéos : chaque ZIP contient exactement ses vidéos, dans l'ordre (CRC OK)", all_ok)
    check(f"{n} vidéos : noms uniques sur l'ensemble des ZIP", len(names_all) == n == len(set(names_all)))
    check(f"25. {n} vidéos : aucune double création de ZIP",
          all(zstate(bid, g)["builds"] == 1 for g in range(len(sizes))), str([zstate(bid, g).get("builds") for g in range(len(sizes))]))
    check(f"24. {n} vidéos : aucun double encodage", all(it.get("attempts") == 1 for it in st["items"]))
    check(f"12. {n} vidéos : « Tout télécharger » activé quand tout est prêt", d["all_zips_ready"] is True)
    check(f"{n} vidéos : noms Batch_00N.zip sur disque", sorted(p.name for p in (A.EXPORT_DIR / bid / "zips").iterdir())
          == [f"Batch_{g:03d}.zip" for g in range(1, len(sizes) + 1)])
    print(f"   ⏱️  {n} vidéos : {time.time() - t0:.1f} s (upload + encodage + ZIP)")
    if n == 235:
        b235 = bid
        it0 = st["items"][150]
        r = c.get(f"/vexport_file/{bid}/{it0['out_name']}")
        with zipfile.ZipFile(A._vexport_zip_path(bid, 1)) as zz:          # MP4 archivé dans le ZIP 2
            ref = zz.read(it0["out_name"])
        check("26. téléchargement individuel toujours fonctionnel (vidéo lue dans son archive)",
              r.status_code == 200 and r.data == ref)
        r.close()
    if n == 2:
        b2 = bid
    if n not in (2, 235):
        c.post("/vexport_cleanup", json={"batch_id": bid})

# ══ 13–14. Erreur dans un groupe + relance ciblée + reconstruction ═══════
bE = create_batch(c, 101, prefix="err", bad=(5,))
c.post("/vexport_start", data={"batch_id": bE})
dE = wait_settled(c, bE)
g1 = dE["groups"][0]
check("13. groupe 1 : 99 exportées — 1 erreur", g1["done"] == 99 and g1["error"] == 1, str(g1))
names, _ = zip_names(c, bE, 1)
check("14. ZIP 1 contient uniquement les 99 réussites", names is not None and len(names) == 99
      and "err_006.mp4" not in names, str(names and len(names)))
check("13. les vidéos suivantes ne sont pas bloquées (ZIP 2 prêt)", dE["groups"][1]["zip"]["status"] == "ready")
check("13. vidéo en erreur relançable", dE["retryable"] == 1)
# Téléchargement du ZIP 1 commencé AVANT la relance, terminé APRÈS la reconstruction.
stream = c.get(f"/vexport_group_zip/{bE}/1", buffered=False)
chunks = iter(stream.response)
first = next(chunks)
stE = A._vexport_load_state(bE)
(A.EXPORT_DIR / bE / "input" / stE["items"][5]["in_name"]).write_bytes(TINY_BYTES)   # source réparée
check("13. relance ciblée acceptée", c.post("/vexport_retry", data={"batch_id": bE}).get_json().get("requeued") == 1)
deadline = time.time() + 120
while time.time() < deadline and not (zstate(bE, 0).get("builds") == 2 and zstate(bE, 0).get("status") == "ready"):
    time.sleep(0.2)
old_bytes = first + b"".join(chunks); stream.close()
with zipfile.ZipFile(io.BytesIO(old_bytes)) as zo:
    check("14. téléchargement en cours jamais coupé par la reconstruction (archive complète, CRC OK)",
          zo.testzip() is None and len(zo.namelist()) == 99)
dE2 = wait_settled(c, bE)
names2, _ = zip_names(c, bE, 1)
check("14. relance réussie → ZIP 1 reconstruit avec 100 vidéos", names2 is not None and len(names2) == 100
      and "err_006.mp4" in names2, str(names2 and len(names2)))
check("14. reconstruction unique (2 créations au total)", zstate(bE, 0)["builds"] == 2, str(zstate(bE, 0).get("builds")))
check("14. ZIP 2 non recréé inutilement", zstate(bE, 1)["builds"] == 1)

# ══ 15. ZIP temporaire jamais téléchargeable + chemins arbitraires ═══════
with A._vexport_flock(A._VEXPORT_ZIP_LOCK):           # créateur de ZIP à l'arrêt
    with A._vexport_lock(b2):
        st = A._vexport_load_state(b2); saved = dict(st["zips"]["0"])
        st["zips"]["0"].update(status="building"); A._vexport_save_state(st)
    part = A.EXPORT_DIR / b2 / "zips" / "Batch_001.zip.part"; part.write_bytes(b"PK\x03\x04partiel")
    check("15. ZIP en création → 409 (jamais servi)", c.get(f"/vexport_group_zip/{b2}/1").status_code == 409)
    check("15. aucune URL pour un ZIP en création", status(c, b2)["groups"][0]["zip"]["url"] == "")
    check("15. « .part » non adressable", c.get(f"/vexport_group_zip/{b2}/1.part").status_code in (404, 500)
          and c.get(f"/vexport_file/{b2}/Batch_001.zip.part").status_code == 404)
    check("15. traversée vers zips/ refusée", c.get(f"/vexport_file/{b2}/..%2Fzips%2FBatch_001.zip").status_code in (400, 404))
    check("15. groupe inexistant → 404", c.get(f"/vexport_group_zip/{b2}/0").status_code == 404
          and c.get(f"/vexport_group_zip/{b2}/9").status_code == 404)
    part.unlink()
    with A._vexport_lock(b2):
        st = A._vexport_load_state(b2); st["zips"]["0"] = saved; A._vexport_save_state(st)

# ══ 16. ZIP corrompu détecté ═══════════════════════════════════════════
# Stockage tournant : une fois l'archive finalisée et vérifiée, c'est la SEULE
# copie (MP4 intermédiaires et sources supprimés) → une archive abîmée n'est
# jamais servie et ses vidéos sont signalées en erreur (pas de boucle).
zp = A.EXPORT_DIR / b2 / "zips" / "Batch_001.zip"
builds_before = zstate(b2, 0).get("builds")
data = zp.read_bytes()
zp.write_bytes(data[: len(data) // 2])                # tronqué
r = c.get(f"/vexport_group_zip/{b2}/1")
check("16. ZIP tronqué détecté au téléchargement (409, pas servi)", r.status_code == 409, str(r.status_code))
A._VEXPORT_ZIP_WAKE.set()
d16 = wait_settled(c, b2, timeout=60)
check("16. archive perdue : vidéos signalées en erreur, plus de lien de téléchargement",
      d16["groups"][0]["zip"]["url"] == "" and all(it["status"] == "error" and "Archive endommagée" in it["error"]
                                                   for it in d16["items"]) and not zp.exists(), str(d16["items"][:1]))
check("16. aucune boucle de reconstruction", zstate(b2, 0).get("builds") == builds_before)
zp3 = A.EXPORT_DIR / b235 / "zips" / "Batch_003.zip"
data = zp3.read_bytes()
zp3.write_bytes(data[:-22] + b"\x00" * 22)            # répertoire central abîmé, même taille
A._VEXPORT_ZIP_CHECKED.discard(b235)                  # = contrôle effectué au redémarrage
A._VEXPORT_ZIP_WAKE.set()
deadline = time.time() + 60
while time.time() < deadline and zstate(b235, 2).get("status") == "ready":
    time.sleep(0.2)
d16b = wait_settled(c, b235, timeout=60)
check("16. ZIP abîmé (taille inchangée) détecté au démarrage : groupe 3 en erreur, groupes 1–2 intacts",
      d16b["groups"][2]["zip"]["url"] == "" and d16b["groups"][2]["error"] == 35
      and zip_names(c, b235, 1)[1] == 200 and zip_names(c, b235, 2)[1] == 200, str(d16b["groups"][2]))

# ══ 19–20. Utilisateurs simultanés + isolation ═════════════════════════
res = {}
def run_user(key, cl, n):
    b = create_batch(cl, n, prefix=key)
    cl.post("/vexport_start", data={"batch_id": b})
    res[key] = b
ths = [threading.Thread(target=run_user, args=("u1", c, 120)), threading.Thread(target=run_user, args=("u2", c2, 110))]
[t.start() for t in ths]; [t.join() for t in ths]
d1 = wait_settled(c, res["u1"]); d2 = wait_settled(c2, res["u2"])
check("19. 2 utilisateurs simultanés : 2 + 2 archives prêtes",
      [g["total"] for g in d1["groups"]] == [100, 20] and [g["total"] for g in d2["groups"]] == [100, 10]
      and d1["all_zips_ready"] and d2["all_zips_ready"])
check("19. aucune double création de ZIP (utilisateurs simultanés)",
      all(zstate(res[k], g)["builds"] == 1 for k in ("u1", "u2") for g in (0, 1)))
check("20. ZIP d'un autre utilisateur : 403", c2.get(f"/vexport_group_zip/{res['u1']}/1").status_code == 403
      and c.get(f"/vexport_group_zip/{res['u2']}/1").status_code == 403)
check("20. ancienne URL ZIP d'un autre utilisateur : 403", c2.get(f"/vexport_zip/{res['u1']}").status_code == 403)
anon = A.app.test_client()
check("20. non connecté → redirigé vers /login", anon.get(f"/vexport_group_zip/{res['u1']}/1").status_code == 302)
check("20. batch_id invalide → 400", c.get("/vexport_group_zip/abc!def/1").status_code == 400)
check("ancienne URL /vexport_zip sur un batch multi-groupes → 409 explicite (pas de ZIP géant)",
      c.get(f"/vexport_zip/{res['u1']}").status_code == 409)

# ══ 21–22. Disque presque plein puis reprise ═══════════════════════════
real_free = A._disk_free_bytes
# a) ZIP : espace insuffisant → message, rien de supprimé, aucun ZIP corrompu, reprise auto
with A._vexport_flock(A._VEXPORT_ZIP_LOCK):
    bD = create_batch(c, 3, prefix="disk")
    c.post("/vexport_start", data={"batch_id": bD})
    deadline = time.time() + 60
    while time.time() < deadline and not status(c, bD)["progress"]["finished"]:
        time.sleep(0.2)
    A._disk_free_bytes = lambda path=None: 50_000_000
A._VEXPORT_ZIP_WAKE.set()
deadline = time.time() + 30
while time.time() < deadline and zstate(bD, 0).get("status") != "no_space":
    time.sleep(0.1)
dD = status(c, bD)
check("21. ZIP : espace insuffisant → état explicite", dD["groups"][0]["zip"]["status"] == "no_space"
      and "insuffisant" in dD["groups"][0]["zip"]["message"], str(dD["groups"][0]["zip"]))
check("21. aucun ZIP (ni .part) produit, vidéos conservées",
      not list((A.EXPORT_DIR / bD / "zips").glob("*")) if (A.EXPORT_DIR / bD / "zips").exists() else True)
check("21. vidéos exportées toujours téléchargeables individuellement",
      c.get(f"/vexport_file/{bD}/{dD['items'][0]['out_name']}").status_code == 200)
check("21. batch avec archive en attente protégé du nettoyage", A._vexport_is_active(A._vexport_load_state(bD)))
A._disk_free_bytes = real_free
A._VEXPORT_ZIP_WAKE.set()
dD2 = wait_settled(c, bD, timeout=60)
names, code = zip_names(c, bD, 1)
check("22. espace libéré → ZIP créé automatiquement", code == 200 and names and len(names) == 3)
# b) Encodage : disque presque plein → mise en attente (pas d'échec), reprise auto
bD2 = create_batch(c, 4, prefix="diskenc")
A._disk_free_bytes = lambda path=None: 100_000_000
c.post("/vexport_start", data={"batch_id": bD2})
time.sleep(2.5)
dDe = status(c, bD2)
check("21. encodage : disque presque plein → rien n'échoue, tout reste en attente",
      dDe["progress"]["pending"] == 4 and dDe["progress"]["error"] == 0, str(dDe["progress"]))
check("21. encodage : message disque presque plein exposé", dDe["disk_low"] is True)
A._disk_free_bytes = real_free
A._vexport_wake()
dDe2 = wait_settled(c, bD2, timeout=60)
check("22. espace libéré → encodages repris et ZIP créé", dDe2["progress"]["done"] == 4 and dDe2["all_zips_ready"])
# c) ENOSPC en pleine écriture du ZIP
real_write = A._vexport_part_append
def failing_write(batch_id, g, z_part, src, name):
    A._vexport_part_path(batch_id, g).parent.mkdir(parents=True, exist_ok=True)
    with open(A._vexport_part_path(batch_id, g), "ab") as f:   # ajout partiel puis disque plein
        f.write(b"PK\x03\x04" + b"\x00" * 4096)
    raise OSError(errno.ENOSPC, "No space left on device")
with A._vexport_flock(A._VEXPORT_ZIP_LOCK):
    bD3 = create_batch(c, 2, prefix="enospc")
    c.post("/vexport_start", data={"batch_id": bD3})
    deadline = time.time() + 60
    while time.time() < deadline and not status(c, bD3)["progress"]["finished"]:
        time.sleep(0.2)
    A._vexport_part_append = failing_write
A._VEXPORT_ZIP_WAKE.set()
deadline = time.time() + 30
while time.time() < deadline and zstate(bD3, 0).get("status") != "no_space":
    time.sleep(0.1)
check("21. ENOSPC pendant l'écriture → no_space, .part supprimé, rien servi",
      zstate(bD3, 0).get("status") == "no_space" and not list((A.EXPORT_DIR / bD3 / "zips").glob("*"))
      and c.get(f"/vexport_group_zip/{bD3}/1").status_code == 409, str(zstate(bD3, 0)))
A._vexport_part_append = real_write
A._VEXPORT_ZIP_WAKE.set()
wait_settled(c, bD3, timeout=60)
names, code = zip_names(c, bD3, 1)
check("22. après ENOSPC : ZIP recréé correctement", code == 200 and names and len(names) == 2)

# ══ 23. Nettoyage des vieux batchs ═════════════════════════════════════
with A._vexport_flock(A._VEXPORT_GLOBAL_LOCK), A._vexport_flock(A._VEXPORT_ZIP_LOCK):
    for b, zst in ((b2, "ready"), (bD, "no_space")):
        with A._vexport_lock(b):
            st = A._vexport_load_state(b); st["zips"]["0"]["status"] = zst
            A._vexport_save_state(st)
        st = A._vexport_load_state(b); st["updated"] = time.time() - 30 * 3600
        A._vexport_state_path(b).write_text(json.dumps(st))
    A._cleanup_stale_vexport()
    check("23. vieux batch terminé (ZIP prêts) purgé avec ses archives", not (A.EXPORT_DIR / b2).exists())
    check("23. vieux batch dont l'archive attend de l'espace : jamais purgé", (A.EXPORT_DIR / bD).exists())
    check("23. batch récent conservé", (A.EXPORT_DIR / b235).exists())

for b in (b235, bE, bD, bD2, bD3, res["u1"]):
    c.post("/vexport_cleanup", json={"batch_id": b})
c2.post("/vexport_cleanup", json={"batch_id": res["u2"]})
sampler.ev.set(); sampler.join(timeout=2)
check("Mémoire : jamais plus d'1 FFmpeg d'export simultané (ZIP compris)", sampler.max == 1, f"max={sampler.max}")
leftovers = [p for p in A.EXPORT_DIR.rglob("*") if p.name.endswith((".part", ".tmp", ".log"))]
check("Aucun fichier temporaire résiduel", leftovers == [], str(leftovers))
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'='*40}\n{PASS} tests OK / {FAIL} KO")
sys.exit(0 if FAIL == 0 else 1)
