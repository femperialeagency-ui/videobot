#!/usr/bin/env python3
"""
test_vexport_storage.py — Stockage tournant du Batch Video Export.

Vrai moteur (ordonnanceur + archivage + FFmpeg réels), fichiers de test légers :
  1/4/5/6/7. 100 vidéos : 1 ZIP intègre, MP4 intermédiaires supprimés, sources supprimées
  2. 311 vidéos : ZIP progressifs (100/100/100/11), au plus quelques MP4 sur disque à la fois
  3. 500 petites vidéos : 5 ZIP, aucun MP4 restant
  8. téléchargement individuel d'une vidéo archivée (archive en cours ET finalisée)
  9. reprise d'un ajout interrompu (état de crash) : archive réparée, complète, intègre
  11. disque presque plein : estimation détaillée (311 vidéos / 8,7 Go), archivage en attente sans perte
  12. isolation entre utilisateurs
  14. téléchargement actif : MP4 jamais supprimé pendant l'envoi
  15. mesure de l'espace par module + nettoyage des seuls temporaires abandonnés

Usage : python3 test_vexport_storage.py
"""
import os, io, json, time, subprocess, sys, tempfile, zipfile, shutil, warnings
warnings.filterwarnings("ignore")

REPO = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vexport_storage_test_"))
os.environ.setdefault("ADMIN_EMAIL", "admin-storage@local.test")
os.environ.setdefault("ADMIN_PASSWORD", "admin-storage-password")
os.environ.setdefault("VEXPORT_FFMPEG_THREADS", "2")
sys.path.insert(0, REPO)
import app as A  # noqa: E402

PASS = 0; FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"✅ {name}")
    else:    FAIL += 1; print(f"❌ {name}   {extra}")

tmp = tempfile.mkdtemp(prefix="vexport_storage_src_")
TINY = os.path.join(tmp, "tiny.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=s=64x64:d=0.4:r=10", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", TINY, "-loglevel", "error"], check=True)
TINY_B = open(TINY, "rb").read()

c = A.app.test_client()
with c.session_transaction() as s: s["user_id"] = 1
A.create_user("user2-storage@local.test", "user2-storage-password")
uid2 = A.get_user_by_email("user2-storage@local.test")["id"]
c2 = A.app.test_client()
with c2.session_transaction() as s: s["user_id"] = uid2
EXP = A.EXPORT_DIR
real_free = A._disk_free_bytes


def new_batch(n, prefix):
    bid = c.post("/vexport_create").get_json()["batch_id"]
    for i in range(n):
        r = c.post("/vexport_upload", data={"batch_id": bid, "file": (io.BytesIO(TINY_B), f"{prefix}_{i + 1:03d}.mp4")},
                   content_type="multipart/form-data").get_json()
        assert r.get("ok"), r
    return bid

def start(bid):
    assert c.post("/vexport_start", data={"batch_id": bid}).get_json().get("ok")

def status(bid, cl=c):
    return cl.get(f"/vexport_status/{bid}").get_json()

def settled(d):
    return d.get("progress", {}).get("finished") and d.get("archives_settled")

def wait(bid, cond, timeout=900, step=0.3, probe=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe: probe()
        d = status(bid)
        if cond(d):
            return d
        time.sleep(step)
    return status(bid)

def mp4s(bid):
    return sorted(p.name for p in (EXP / bid / "output").glob("*.mp4"))

def zip_check(bid, g):
    r = c.get(f"/vexport_group_zip/{bid}/{g}")
    if r.status_code != 200:
        r.close(); return None
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        ok = z.testzip() is None
        names = z.namelist()
        data = {n: z.read(n) for n in names[:3]}
    r.close()
    return (names, data) if ok else None

def full_check(bid, sizes, label):
    d = status(bid)
    st = A._vexport_load_state(bid)
    groups = [zip_check(bid, g + 1) for g in range(len(sizes))]
    check(f"{label} : {len(sizes)} ZIP progressifs ({'/'.join(map(str, sizes))}), intègres (CRC)",
          all(gz is not None for gz in groups) and [len(gz[0]) for gz in groups] == sizes, str([gz and len(gz[0]) for gz in groups]))
    allnames = [n for gz in groups if gz for n in gz[0]]
    check(f"{label} : noms uniques, toutes les vidéos présentes", len(set(allnames)) == sum(sizes) == d["progress"]["done"])
    check(f"{label} : MP4 intermédiaires supprimés (tout est dans les ZIP)", mp4s(bid) == []
          and all(it.get("packed") for it in st["items"] if it["status"] == "done"), str(mp4s(bid)[:5]))
    check(f"{label} : sources supprimées, aucun .part / journal temporaire",
          not list((EXP / bid / "input").iterdir()) and not list((EXP / bid).rglob("*.part"))
          and not list((EXP / bid / "zips").glob(".*.tail")), str(list((EXP / bid).rglob("*.part"))))
    zbytes = sum((EXP / bid / "zips" / A._vexport_zip_name(g)).stat().st_size for g in range(len(sizes)))
    check(f"{label} : disque du batch ≈ taille des ZIP (pas de copie redondante)",
          A._vexport_dir_size(bid) - zbytes < 400_000, f"{A._vexport_dir_size(bid)} vs {zbytes}")
    return groups


# ══ 1. 100 vidéos ═════════════════════════════════════════════════════
b100 = new_batch(100, "c")
start(b100)
d = wait(b100, settled)
check("1. 100 vidéos traitées", d["progress"]["done"] == 100 and d["all_zips_ready"], str(d["progress"]))
g100 = full_check(b100, [100], "1. 100 vidéos")
st = A._vexport_load_state(b100)
it = st["items"][42]
r = c.get(f"/vexport_file/{b100}/{it['out_name']}")
with zipfile.ZipFile(io.BytesIO(c.get(f"/vexport_group_zip/{b100}/1").data)) as z:
    ref = z.read(it["out_name"])
check("8. téléchargement individuel d'une vidéo archivée (lue dans le ZIP finalisé)",
      r.status_code == 200 and r.data == ref and r.headers.get("Content-Length") == str(len(ref))
      and "attachment" in r.headers.get("Content-Disposition", ""), f"{r.status_code} {len(r.data)} vs {len(ref)}")
r.close()
check("12. isolation : vidéo archivée d'un autre utilisateur refusée (403)",
      c2.get(f"/vexport_file/{b100}/{it['out_name']}").status_code == 403
      and c2.get(f"/vexport_group_zip/{b100}/1").status_code == 403)
check("8. téléchargement individuel : marqueur retiré après l'envoi", A._vexport_active_downloads(b100) == 0)

# ══ 2. 311 vidéos : stockage tournant + ZIP progressifs ═══════════════
b311 = new_batch(311, "s")
start(b311)
peak = {"mp4": 0, "part_dl": None}
def probe():
    peak["mp4"] = max(peak["mp4"], len(mp4s(b311)))
    if peak["part_dl"] is None:
        st = A._vexport_load_state(b311) or {}
        z = (st.get("zips") or {}).get("1") or {}
        if z.get("part") and len(z["part"]["members"]) >= 3:
            name = z["part"]["members"][1][0]
            rr = c.get(f"/vexport_file/{b311}/{name}")
            peak["part_dl"] = (name, rr.status_code, rr.data); rr.close()
seen_progressive = {}
def cond(d):
    if d["groups"][0]["zip"]["status"] == "ready" and not d["progress"]["finished"]:
        seen_progressive["ok"] = True
    return settled(d)
d = wait(b311, cond, probe=probe, step=0.1)
check("2. 311 vidéos traitées", d["progress"]["done"] == 311, str(d["progress"]))
check("4. ZIP 1 prêt pendant que le traitement continue (progressif)", seen_progressive.get("ok") is True)
check("5. stockage tournant : au plus quelques MP4 sur le disque à la fois", 0 < peak["mp4"] <= 4, f"max={peak['mp4']}")
g311 = full_check(b311, [100, 100, 100, 11], "2. 311 vidéos")
pd = peak["part_dl"]
with zipfile.ZipFile(io.BytesIO(c.get(f"/vexport_group_zip/{b311}/2").data)) as z:
    ref2 = z.read(pd[0]) if pd else b""
check("8. téléchargement individuel pendant l'archivage du groupe (archive en cours)", pd and pd[1] == 200 and pd[2] == ref2,
      str(pd and (pd[1], len(pd[2]), len(ref2))))
rz = c.get(f"/vexport_group_zip/{b311}/4", buffered=True)
check("7. téléchargement ZIP 4 (11 vidéos)", rz.status_code == 200 and len(zipfile.ZipFile(io.BytesIO(rz.data)).namelist()) == 11)

# ══ 3. 500 petites vidéos ═════════════════════════════════════════════
t0 = time.time()
b500 = new_batch(500, "f")
start(b500)
d = wait(b500, settled, step=1.0)
check("3. 500 vidéos traitées", d["progress"]["done"] == 500, str(d["progress"]))
full_check(b500, [100] * 5, "3. 500 vidéos")
print(f"   ⏱️  500 vidéos : {time.time() - t0:.0f} s")

# ══ 14. Téléchargement actif : MP4 jamais supprimé pendant l'envoi ═════
with A._vexport_flock(A._VEXPORT_ZIP_LOCK):            # archivage retenu le temps du montage
    bdl = new_batch(2, "dl")
    start(bdl)
    wait(bdl, lambda x: x["progress"]["finished"], timeout=60)
    name = A._vexport_load_state(bdl)["items"][0]["out_name"]
    rs = c.get(f"/vexport_file/{bdl}/{name}", buffered=False)
A._VEXPORT_ZIP_WAKE.set()
wait(bdl, lambda x: all(it.get("packed") for it in A._vexport_load_state(bdl)["items"]), timeout=60)
time.sleep(1.0)
check("14. vidéo archivée mais téléchargement en cours : MP4 conservé", (EXP / bdl / "output" / name).exists())
data = b"".join(rs.response); rs.close()
check("14. téléchargement terminé intact", len(data) == A._vexport_load_state(bdl)["items"][0]["out_size"])
d = wait(bdl, lambda x: settled(x) and not mp4s(bdl), timeout=60, probe=A._VEXPORT_ZIP_WAKE.set)
check("14. fin du téléchargement → MP4 intermédiaire supprimé", mp4s(bdl) == [] and settled(d))

# ══ 9. Reprise d'un ajout interrompu (état de crash) ══════════════════
with A._vexport_flock(A._VEXPORT_ZIP_LOCK):
    brs = new_batch(3, "rs")
    start(brs)
    wait(brs, lambda x: x["progress"]["finished"], timeout=60)
    job = A._vexport_zip_prepare(brs); A._vexport_zip_job(brs, job)          # 1re vidéo archivée
    st = A._vexport_load_state(brs)
    p = st["zips"]["0"]["part"]
    part = A._vexport_part_path(brs, 0)
    with open(part, "r+b") as f:                       # crash en plein ajout : répertoire central écrasé
        f.seek(p["good_end"]); f.write(b"\x00PARTIAL" * 4000); f.truncate()
    with A._vexport_lock(brs):
        st = A._vexport_load_state(brs); st["zips"]["0"]["busy"] = True; A._vexport_save_state(st)
    try:
        zipfile.ZipFile(str(part)); broken = False
    except zipfile.BadZipFile:
        broken = True
check("9. archive en cours endommagée par l'interruption (simulée)", broken)
A._VEXPORT_ZIP_CHECKED.discard(brs)                    # = contrôle au redémarrage
A._VEXPORT_ZIP_WAKE.set()
d = wait(brs, settled, timeout=60)
gz = zip_check(brs, 1)
check("9. reprise : archive réparée puis complétée (3 vidéos, CRC OK)", gz is not None and len(gz[0]) == 3 and mp4s(brs) == [],
      str(gz and gz[0]))

# ══ 11. Disque presque plein ══════════════════════════════════════════
A._disk_free_bytes = lambda path=None: 8_800_000_000
r = c2.post("/vexport_create", data={"clean_previous": "1", "expected_bytes": "8700000000", "expected_count": "311",
                                    "expected_max": "60000000"})
dd = r.get_json()
print("   📐 311 vidéos / 8,7 Go, 8,8 Go libres →", dd.get("error"))
check("11. 311 vidéos / 8,7 Go avec 8,8 Go libres : refus clair et détaillé", r.status_code == 507
      and all(k in dd["error"] for k in ("Sources : 8,7 Go", "ZIP estimés : 8,7 Go", "Pic nécessaire", "Espace disponible : 8,8 Go", "Limite réaliste")),
      dd.get("error"))
check("11. nouvelle estimation ≈ 9,6 Go (au lieu de 18,2 Go), limite réaliste ≈ 280 vidéos",
      9.4e9 < dd["need"] < 9.8e9 and 270 <= dd["max_videos"] <= 290, f"need={dd.get('need')} max={dd.get('max_videos')}")
A._disk_free_bytes = lambda path=None: 10_500_000_000
r = c2.post("/vexport_create", data={"clean_previous": "1", "expected_bytes": "8700000000", "expected_count": "311",
                                    "expected_max": "60000000"})
check("11. même batch avec 10,5 Go libres : accepté", r.status_code == 200, str(r.get_json()))
bok = r.get_json().get("batch_id")
A._disk_free_bytes = real_free
c2.post("/vexport_cancel", data={"batch_id": bok, "delete": "1"})
# Archivage : disque plein → attente, rien perdu, reprise automatique
with A._vexport_flock(A._VEXPORT_ZIP_LOCK):
    bns = new_batch(3, "ns")
    start(bns)
    wait(bns, lambda x: x["progress"]["finished"], timeout=60)
    A._disk_free_bytes = lambda path=None: 50_000_000
A._VEXPORT_ZIP_WAKE.set(); time.sleep(2.5)
d = status(bns)
check("11. archivage sans espace : en attente, MP4 conservés, rien perdu", len(mp4s(bns)) == 3
      and not any(it.get("packed") for it in A._vexport_load_state(bns)["items"])
      and "insuffisant" in (A._vexport_load_state(bns)["zips"]["0"].get("message") or ""), str(A._vexport_load_state(bns)["zips"]))
A._disk_free_bytes = real_free
A._VEXPORT_ZIP_WAKE.set()
d = wait(bns, settled, timeout=60)
check("11. espace revenu : archivage repris automatiquement", settled(d) and mp4s(bns) == [] and zip_check(bns, 1) is not None)

# ══ 15. Mesure de l'espace + temporaires abandonnés ═══════════════════
D = A.DATA_DIR
(A.BATCH_DIR / "jobX").mkdir(parents=True, exist_ok=True); (A.BATCH_DIR / "jobX" / "a.mp4").write_bytes(b"g" * 300_000)
A.VARIATION_DIR.mkdir(parents=True, exist_ok=True); (A.VARIATION_DIR / "v.mp4").write_bytes(b"v" * 200_000)
(A.REELS_MIX_DIR / "r.mp4").write_bytes(b"r" * 100_000)
old = time.time() - 3 * 3600
for pth, data in ((D / "batch_old.zip", b"z" * 50_000), (EXP / ".current.json.999.tmp", b"t"),
                  (EXP / b100 / "state.json.1.2.tmp", b"t"), (D / "notes_inconnues.txt", b"n" * 1000)):
    pth.write_bytes(data); os.utime(pth, (old, old))
(EXP / "recent.tmp").write_bytes(b"t")
inv = c.get("/vexport_storage").get_json()
u = {x["key"]: x for x in inv["usage"]}
check("15. répartition : Batch Video Export / Générateur / Variations / Reels / temporaires / autres",
      {"export", "generator", "variations", "reels", "temp", "other"} <= set(u), str(list(u)))
check("15. tailles mesurées sur les dossiers réels", u["generator"]["bytes"] >= 300_000 and u["variations"]["bytes"] >= 200_000
      and u["reels"]["bytes"] >= 100_000 and abs(u["export"]["bytes"] - A._vx_tree_bytes(EXP)) < 200_000,
      json.dumps({k: v["bytes"] for k, v in u.items()}))
check("15. autres données : base de données identifiée (jamais supprimée)",
      any(x["name"] == "videobot_users.db" for x in u["other"].get("detail", [])) and (D / "videobot_users.db").exists())
check("15. temporaires abandonnés (> 1 h) supprimés ; récents et inconnus conservés",
      not (D / "batch_old.zip").exists() and not (EXP / ".current.json.999.tmp").exists()
      and not (EXP / b100 / "state.json.1.2.tmp").exists() and (EXP / "recent.tmp").exists()
      and (D / "notes_inconnues.txt").exists() and (A.BATCH_DIR / "jobX" / "a.mp4").exists())
check("15. espace disque global exposé", inv["disk"]["free"] > 0 and inv["disk"]["total"] >= inv["disk"]["used"])
check("15. inventaire réservé à l'admin", c2.get("/vexport_storage").status_code == 403)

time.sleep(0.5)
left = [l for l in subprocess.run(["ps", "-A", "-o", "command="], capture_output=True, text=True).stdout.splitlines()
        if l.startswith("ffmpeg ") and str(EXP) in l]
check("Aucun FFmpeg d'export résiduel", left == [], str(left))
shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'=' * 40}\n{PASS} tests OK / {FAIL} KO")
sys.exit(1 if FAIL else 0)
