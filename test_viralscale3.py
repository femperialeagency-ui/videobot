#!/usr/bin/env python3
"""
test_viralscale3.py — ViralScale 3.0 : interface unifiée. Flask réel, Pillow
et FFmpeg réels, aucune API IA.

  ROUTES      une URL stable par module et sous-onglet (rendu serveur du bon
              onglet), anciennes URL (/ , /history), sous-onglet inconnu,
              accès non connecté.
  DROITS      Paramètres / Gestion équipe / Consommation : admin seulement ;
              isolation des productions entre utilisateurs.
  PRODUCTIONS historique réel (usage_logs) + exports vidéo (en cours,
              terminé avec ZIP, nettoyé, dossier supprimé → journal),
              filtres, recherche, suppression d'une ligne (JSON).
  STOCKAGE    /api/storage_summary, /vexport_estimate (= calcul du lancement).
  LIMITE      300 vidéos par NOUVEAU batch ; un batch plus ancien garde 500.
  PHOTOS      compression (qualité réelle), redimensionnement, conversion,
              nettoyage (GPS / tout l'EXIF), métadonnées (moteur inchangé).
  AUDIO       conversion par défaut inchangée, débit choisi, mono.
  DONNÉES     aucune donnée fictive dans les pages (coûts libellés
              « Estimation théorique — non facturée », plus de rentabilité,
              plus de durée/coût inventés).

Usage : python3 test_viralscale3.py
"""
import os, io, json, re, sys, time, tempfile, subprocess, uuid, warnings
warnings.filterwarnings("ignore")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="vs3_test_"))
os.environ.setdefault("ADMIN_EMAIL", "admin-vs3@local.test")
os.environ.setdefault("ADMIN_PASSWORD", "admin-vs3-password")
os.environ.setdefault("VEXPORT_WORKER", "0")          # états d'export construits à la main : pas d'ordonnanceur
import app as A
from PIL import Image

PASS = 0; FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"✅ {name}")
    else:    FAIL += 1; print(f"❌ {name}   {extra}")

tmp = tempfile.mkdtemp(prefix="vs3_src_")

def login(email, password):
    c = A.app.test_client()
    r = c.post("/login", data={"email": email, "password": password})
    assert r.status_code == 302, r.status_code
    return c

admin = login(os.environ["ADMIN_EMAIL"], os.environ["ADMIN_PASSWORD"])
A.create_user("membre-vs3@local.test", "membre-vs3-password", "member")
A.create_user("autre-vs3@local.test", "autre-vs3-password", "member")
member = login("membre-vs3@local.test", "membre-vs3-password")
other = login("autre-vs3@local.test", "autre-vs3-password")
U_ADMIN = A.get_user_by_email(os.environ["ADMIN_EMAIL"])
U_MEM = A.get_user_by_email("membre-vs3@local.test")
U_OTH = A.get_user_by_email("autre-vs3@local.test")

# ══ ROUTES ════════════════════════════════════════════════════════════
PAGES = {
    "/": "studioTab", "/studio": "studioTab", "/generator": "simpleTab", "/generator/batch": "batchTab",
    "/reels": "reelsTab", "/reels/captions": "reelsTab", "/reels/music": "reelsTab", "/variations": "variationMultiTab",
    "/media/videos": "optimizerTab", "/media/videos/optimize": "optimizerTab", "/media/photos": "photoTab",
    "/media/photos/compression": "photoTab", "/media/photos/resize": "photoTab", "/media/photos/convert": "photoTab",
    "/media/photos/clean": "photoTab", "/media/photos/metadata": "photoTab", "/media/audio": "audioTab",
    "/media/audio/optimize": "audioTab", "/productions": "productionsTab", "/productions/active": "productionsTab",
    "/productions/done": "productionsTab", "/productions/failed": "productionsTab",
}
bad = []
for url, tab in PAGES.items():
    for who, cl in (("admin", admin), ("membre", member)):
        r = cl.get(url)
        h = r.get_data(as_text=True)
        actives = re.findall(r'<div id="(\w+)" class="tab-content active"', h)
        if r.status_code != 200 or actives != [tab]:
            bad.append((who, url, r.status_code, actives))
check(f"Routes : {len(PAGES)} URL stables rendues avec le BON onglet actif (admin + membre)", not bad, str(bad[:4]))
r = admin.get("/generator/inconnu")
check("Routes : sous-onglet inconnu → redirection vers la page parente", r.status_code == 302 and r.headers["Location"].endswith("/generator"))
r = admin.get("/media/photos/xyz")
check("Routes : /media/photos/xyz → /media/photos", r.status_code == 302 and r.headers["Location"].endswith("/media/photos"))
for cl in (admin, member):
    r = cl.get("/history")
    check("Ancienne URL : /history → /productions", r.status_code == 302 and r.headers["Location"].endswith("/productions"))
anon = A.app.test_client()
r = anon.get("/media/photos/clean")
check("Non connecté : redirection vers la connexion", r.status_code == 302 and "/login" in r.headers["Location"])
r = anon.get("/api/productions")
check("Non connecté : API productions inaccessible", r.status_code == 302)

# ══ DROITS ADMINISTRATEUR ════════════════════════════════════════════
r = admin.get("/admin/settings"); h = r.get_data(as_text=True)
check("Paramètres : admin 200 + onglet Paramètres actif", r.status_code == 200 and 'id="settingsTab" class="tab-content active"' in h)
check("Paramètres : membre refusé (403)", member.get("/admin/settings").status_code == 403)
check("API Paramètres : membre refusé (403)", member.get("/api/admin/settings").status_code == 403)
for u in ("/admin/users", "/admin/consumption"):
    check(f"{u} : membre refusé, admin 200", member.get(u).status_code == 403 and admin.get(u).status_code == 200)
hm = member.get("/studio").get_data(as_text=True)
check("Membre : aucune section Administration ni onglet Paramètres dans la page",
      "Gestion équipe" not in hm and 'href="/admin/settings"' not in hm and 'id="settingsTab"' not in hm)
ha = admin.get("/studio").get_data(as_text=True)
nav = re.findall(r'class="vs-nav-item[^"]*" href="([^"]+)" data-nav="(\w+)"', ha)
check("Sidebar admin : Accueil → Paramètres dans l'ordre demandé",
      [n for _, n in nav] == ["studio", "generator", "reels", "variations", "videos", "photos", "audio", "productions", "team", "consumption", "settings"], str(nav))
check("Sidebar : groupes STUDIO / CRÉATION / MEDIA TOOLS / PRODUCTIONS / ADMINISTRATION",
      all(f'<div class="vs-nav-label">{g}</div>' in ha for g in ("Studio", "Création", "Media Tools", "Productions", "Administration")))
check("Sidebar : anciennes entrées retirées (Dashboard, Historique, « Bientôt », menu Nouvelle génération)",
      "Bientôt" not in ha and "genDropMenu" not in ha and ">Historique<" not in ha and ">Dashboard<" not in ha)
for u in ("/admin/users", "/admin/consumption"):
    hh = admin.get(u).get_data(as_text=True)
    check(f"{u} : même sidebar partagée + feuille de style 3.0", 'id="vsSidebar"' in hh and "css/vs3.css?v=" in hh)
check("/consumption (membre) : sidebar partagée", 'id="vsSidebar"' in member.get("/consumption").get_data(as_text=True))

# ══ AUCUNE DONNÉE FICTIVE ════════════════════════════════════════════
check("Pages : plus de « Coût estimé » présenté comme réel, ni de durée inventée",
      "Coût estimé" not in ha and "fmtEta(out * 8)" not in ha and "VM_COST_PER_SECOND_PER_VARIANT * totalDur" not in ha
      and "Temps estimé" not in ha.split('id="variationTab"')[0] and "Aucune notification" not in ha)
check("Pages : pas de pourcentage animé inventé (Vidéo C : barre d'activité)", "if (c < 88)" not in ha and "vs-indet" in ha)
check("Pages : aucun score inventé (unicité / sécurité)", 'id="vmEstUniqueness"' not in ha and 'id="vmEstSafety"' not in ha)
check("Crédits : pas de jauge de consommation (le code ne décompte pas les crédits)", 'class="credit-fill"' not in ha and "solde configuré" in ha)
for who, cl, u in (("admin", admin, "/admin/consumption"), ("membre", member, "/consumption")):
    A.log_usage_event(U_ADMIN if who == "admin" else U_MEM, "simple", source_seconds=12, output_seconds=12,
                      source_count=1, output_count=1, success=True)
    hh = cl.get(u).get_data(as_text=True)
    check(f"Consommation ({who}) : coûts libellés « Estimation théorique — non facturée », sans rentabilité",
          "Estimation théorique — non facturée" in hh and not re.search(r"Bénéfice|Rentabilité|Marge|[Pp]rofit\b", hh))

# ══ MES PRODUCTIONS ══════════════════════════════════════════════════
with A._users_db() as conn:                               # repart d'un historique propre
    conn.execute("DELETE FROM usage_logs"); conn.commit()
A.log_usage_event(U_ADMIN, "simple", source_count=1, output_count=1, success=True)
A.log_usage_event(U_MEM, "batch", source_count=2, output_count=0, success=False)
A.log_usage_event(U_MEM, "variation_multi", source_count=3, output_count=15, success=True)
A.log_usage_event(U_OTH, "audio_converter", source_count=4, output_count=4, success=True)

def craft(uid, **st):
    bid = uuid.uuid4().hex
    d = A._vexport_dir(bid); (d / "input").mkdir(parents=True); (d / "output").mkdir()
    base = {"batch_id": bid, "user_id": uid, "created": time.time(), "items": [], "profile": {"kind": "standard"}}
    base.update(st)
    A._vexport_save_state(base)
    return bid

def items(statuses, packed=False):
    return [{"idx": i, "orig_name": f"clip_{i}.mp4", "in_name": f"{i:04d}.mp4", "out_name": f"clip_{i}.mp4",
             "status": s, "error": "", "in_size": 1000, "out_size": 900 if s == "done" else 0, "packed": packed and s == "done"}
            for i, s in enumerate(statuses)]

b_active = craft(U_MEM["id"], started=True, items=items(["done", "done", "pending", "pending"]), created=time.time() - 50)
b_done = craft(U_MEM["id"], started=True, items=items(["done", "done"], packed=True), created=time.time() - 40,
               zips={"0": {"status": "ready", "size": 4321, "count": 2}}, profile={"kind": "optimize"})
b_clean = craft(U_MEM["id"], started=True, cleaned=True, cleaned_at=time.time(), created=time.time() - 30,
                summary={"total": 5, "done": 5, "error": 0})
b_other = craft(U_OTH["id"], started=True, items=items(["pending"]), created=time.time() - 20)
gone = uuid.uuid4().hex
with open(A._VEXPORT_CLEAN_LOG, "a", encoding="utf-8") as f:
    f.write(json.dumps({"t": time.time() - 3600, "batch_id": gone, "user_id": U_MEM["id"], "reason": "retention",
                        "summary": {"total": 120, "done": 118, "error": 2}, "created": time.time() - 7200,
                        "started": True, "cancelled": False, "kind": "standard"}) + "\n")

d = member.get("/api/productions").get_json()
ids = [x["id"] for x in d["items"]]
check("Productions (membre) : uniquement SES données (2 générations + 4 exports)", d["total"] == 6 and len(ids) == 6
      and not any(x["owner"] for x in d["items"]) and f"vx:{b_other}" not in ids, str(ids))
d2 = member.get("/api/productions?scope=all").get_json()
check("Productions (membre) : « toute l'équipe » ignoré (isolation)", d2["scope"] == "mine" and d2["total"] == 6)
by = {x["id"]: x for x in d["items"]}
ra = by[f"vx:{b_active}"]
check("Export en cours : statut En cours + progression RÉELLE (2 / 4, 50 %)", ra["status"] == "active"
      and ra["progress"] == {"done": 2, "total": 4, "percent": 50.0}, str(ra))
check("Export en cours : actions Voir / Annuler / Supprimer, aucun téléchargement fictif",
      [a["type"] for a in ra["actions"]] == ["view", "cancel", "delete"] and ra["downloads"] == [])
rd = by[f"vx:{b_done}"]
check("Export terminé : Terminé, taille = ZIP réel, téléchargement du ZIP prêt", rd["status"] == "done" and rd["size"] == 4321
      and rd["downloads"] == [{"label": "Batch_001.zip", "url": f"/vexport_group_zip/{b_done}/1", "size": 4321}]
      and rd["module_url"] == "/media/videos/optimize", str(rd))
rc = by[f"vx:{b_clean}"]
check("Export nettoyé : « Fichiers supprimés — historique conservé. », aucun téléchargement",
      rc["note"] == "Fichiers supprimés — historique conservé." and rc["downloads"] == [] and rc["actions"] == [] and rc["files"] == 5)
rg = by.get(f"vx:{gone}")
check("Export dont le dossier a été supprimé : retrouvé via le journal de nettoyage", rg is not None and rg["files"] == 120
      and rg["status"] == "done" and rg["note"] == "Fichiers supprimés — historique conservé.", str(rg))
logs = [x for x in d["items"] if x["kind"] == "log"]
check("Générations : statut réel (Terminé / Échoué), fichiers = sorties enregistrées, pas de téléchargement inventé",
      sorted((x["module"], x["status"], x["files"]) for x in logs) == [("Générateur vidéo · Batch", "failed", 0), ("Studio Variations", "done", 15)]
      and all(x["downloads"] == [] and x["note"] == "Fichiers temporaires — historique conservé." for x in logs), str(logs))
check("Productions : tri du plus récent au plus ancien", [x["date"] for x in d["items"]] == sorted([x["date"] for x in d["items"]], reverse=True))
act = member.get("/api/productions?status=active").get_json()
check("Filtre En cours : seulement l'export actif", [x["id"] for x in act["items"]] == [f"vx:{b_active}"] and act["active_count"] == 1)
done = member.get("/api/productions?status=done").get_json()
check("Filtre Terminés", {x["id"] for x in done["items"]} == {f"vx:{b_done}", f"vx:{b_clean}", f"vx:{gone}"} | {x["id"] for x in logs if x["status"] == "done"})
fail = member.get("/api/productions?status=failed").get_json()
check("Filtre Échoués", [x["module"] for x in fail["items"]] == ["Générateur vidéo · Batch"])
q = member.get("/api/productions?q=variations").get_json()
check("Recherche par module", [x["module"] for x in q["items"]] == ["Studio Variations"], str(q["items"]))
pg = member.get("/api/productions?limit=2&offset=2").get_json()
check("Pagination", len(pg["items"]) == 2 and pg["total"] == 6 and [x["id"] for x in pg["items"]] == ids[2:4])
da = admin.get("/api/productions?scope=all").get_json()
check("Admin « toute l'équipe » : tout le monde, propriétaire affiché, aucune action sur les exports des autres",
      da["total"] == 9 and any(x["owner"] == "autre-vs3@local.test" for x in da["items"])
      and all(x["actions"] == [] and x["downloads"] == [] for x in da["items"] if x["kind"] == "vexport" and not x["mine"]), str(da["total"]))
dm = admin.get("/api/productions").get_json()
check("Admin par défaut : ses productions seulement", dm["total"] == 1 and dm["scope"] == "mine")
lid_admin = [x for x in da["items"] if x["kind"] == "log" and x["owner"] == os.environ["ADMIN_EMAIL"]][0]["log_id"]
lid_mem = [x for x in logs if x["status"] == "failed"][0]["log_id"]
r = member.post(f"/history/delete/{lid_admin}", headers={"X-VS-Ajax": "1"})
check("Suppression d'une ligne d'un autre utilisateur : refusée (403 JSON)", r.status_code == 403 and r.get_json().get("error"))
r = member.post(f"/history/delete/{lid_mem}", headers={"X-VS-Ajax": "1"})
check("Suppression de sa ligne d'historique : JSON ok", r.status_code == 200 and r.get_json() == {"ok": True}
      and member.get("/api/productions").get_json()["total"] == 5)
r = member.post(f"/history/delete/{lid_mem}", headers={"X-VS-Ajax": "1"})
check("Ligne déjà supprimée : 404 JSON", r.status_code == 404)
check("Export d'un autre utilisateur : statut inaccessible", member.get(f"/vexport_status/{b_other}").status_code == 403)

# ══ STOCKAGE / ESTIMATION / PARAMÈTRES ═══════════════════════════════
sm, sa = member.get("/api/storage_summary").get_json(), admin.get("/api/storage_summary").get_json()
import shutil
du = shutil.disk_usage(os.environ["DATA_DIR"])
check("Stockage : total / utilisé / disponible mesurés (disque DATA_DIR)", sm["total"] == du.total
      and abs(sm["free"] - du.free) < 500_000_000 and sm["used"] > 0)
check("Stockage : « Libérer de l'espace » réservé à l'admin", sm["can_clean"] is False and sa["can_clean"] is True)
e = member.get("/vexport_estimate?bytes=3000000000&count=100&max=60000000").get_json()
ref = A._vexport_estimate(3_000_000_000, 100, 60_000_000)
check("Estimation : même calcul que le lancement (pic + marge)", abs(e["need"] - ref["need"]) < 1 and e["sources"] == 3_000_000_000
      and e["max_files"] == 300 and e["count_ok"] is True, str(e))
e = member.get("/vexport_estimate?bytes=3000000&count=301&max=10000").get_json()
check("Estimation : 301 vidéos signalées hors limite", e["count_ok"] is False)
real_free = A._disk_free_bytes
A._disk_free_bytes = lambda path=None: 2_000_000_000
e = member.get("/vexport_estimate?bytes=3000000000&count=100&max=60000000").get_json()
A._disk_free_bytes = real_free
check("Estimation : espace insuffisant → refus annoncé + limite réaliste", e["fits"] is False and 0 < e["max_videos"] < 100, str(e))
s = admin.get("/api/admin/settings").get_json()
check("Paramètres : valeurs RÉELLES du serveur (lecture seule)", s["export"]["max_files"] == 300 and s["export"]["crf"] == A.VEXPORT_CRF
      and s["export"]["preset"] == A.VEXPORT_PRESET and s["limits"]["photo_files"] == A.MAX_PHOTO_FILES
      and s["accounts"]["users"] == 3 and s["ocr"]["local_available"] == bool(shutil.which("tesseract")))

# ══ LIMITE 300 PAR NOUVEAU BATCH ═════════════════════════════════════
r = member.post("/vexport_create", data={"expected_count": "301", "expected_bytes": "1000"})
check("Limite : création annoncée à 301 vidéos refusée (400, message clair)", r.status_code == 400
      and "300" in r.get_json()["error"])
r = member.post("/vexport_create", data={"expected_count": "300", "expected_bytes": "1000"})
nb = r.get_json()["batch_id"]
check("Limite : nouveau batch créé avec max_files = 300", r.status_code == 200 and A._vexport_load_state(nb)["max_files"] == 300)
tiny = os.path.join(tmp, "tiny.mp4")
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=64x64:d=0.4:r=10", "-c:v", "libx264",
                "-preset", "ultrafast", "-pix_fmt", "yuv420p", tiny], check=True)
with A._vexport_state_lock(nb):
    st = A._vexport_load_state(nb); st["items"] = items(["pending"] * 300); A._vexport_save_state(st)
up = lambda b: member.post("/vexport_upload", data={"batch_id": b, "file": (open(tiny, "rb"), "x.mp4")},
                           content_type="multipart/form-data").get_json()
r = up(nb)
check("Limite : 301e vidéo d'un nouveau batch refusée", "error" in r and "300" in r["error"], str(r))
with A._vexport_state_lock(nb):                            # même batch « créé avant » la limite : 500 conservé
    st = A._vexport_load_state(nb); st.pop("max_files"); A._vexport_save_state(st)
r = up(nb)
check("Limite : batch créé avant la limite 300 → reprise inchangée (301e acceptée)", r.get("ok") is True and r.get("count") == 301, str(r))
member.post("/vexport_cancel", data={"batch_id": nb, "delete": "1"})

# ══ PHOTOS ═══════════════════════════════════════════════════════════
def noisy(w, h, mode="RGB"):
    import random
    rnd = random.Random(7)
    img = Image.new(mode, (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            v = ((x * 7 + y * 13) % 256, (x * 3 + rnd.randint(0, 60)) % 256, (y * 5 + rnd.randint(0, 60)) % 256)
            px[x, y] = v + ((200,) if mode == "RGBA" else ())
    return img

jpg = os.path.join(tmp, "photo.jpg")
img = noisy(400, 300)
ex = img.getexif()
ex[0x013B] = "Auteur Test"; ex[0x0131] = "AppTest"
gps = ex.get_ifd(0x8825); gps[1] = "N"; gps[2] = (48.0, 51.0, 24.0); gps[3] = "E"; gps[4] = (2.0, 21.0, 3.0)
img.save(jpg, "JPEG", quality=95, exif=ex.tobytes())
png = os.path.join(tmp, "alpha.png"); noisy(200, 100, "RGBA").save(png)
src_exif = Image.open(jpg).getexif()
check("Photos (préparation) : JPEG source avec auteur + GPS", src_exif.get(0x013B) == "Auteur Test" and bool(src_exif.get_ifd(0x8825)))

def run_photo(cl, path, **form):
    st = cl.post("/photo_stage", data={"file": (open(path, "rb"), os.path.basename(path))}, content_type="multipart/form-data").get_json()
    data = {"job_id": st["job_id"], "stored_name": st["stored_name"], "out_name": "sortie", "index": "0", "count": "1"}
    data.update({k: str(v) for k, v in form.items()})
    res = cl.post("/photo_process", data=data).get_json()
    out = None
    if res.get("ok"):
        rr = cl.get(f"/photo_file/{st['job_id']}/{res['filename']}")
        out = Image.open(io.BytesIO(rr.data)); out.load(); res["_bytes"] = len(rr.data)
    return res, out

r95, o95 = run_photo(member, jpg, op="compress", quality=95, format="same", keep_meta=1)
r30, o30 = run_photo(member, jpg, op="compress", quality=30, format="same", keep_meta=1)
check("Compression : qualité 30 % → fichier plus léger que 95 % (même sens que l'interface)",
      r30["final_size"] < r95["final_size"] and r30["final_size"] == r30["_bytes"], f"{r30.get('final_size')} vs {r95.get('final_size')}")
check("Compression : dimensions inchangées, format d'origine, tailles avant/après mesurées",
      o30.size == (400, 300) and r30["filename"].endswith(".jpg") and r30["orig_size"] == os.path.getsize(jpg)
      and (r30["orig_w"], r30["final_w"]) == (400, 400))
check("Compression : métadonnées conservées par défaut (auteur + GPS)", o30.getexif().get(0x013B) == "Auteur Test" and bool(o30.getexif().get_ifd(0x8825)))
rn, on = run_photo(member, jpg, op="compress", quality=80, format="webp", keep_meta=0)
check("Compression : sortie WEBP, sans métadonnées si décoché", rn["filename"].endswith(".webp") and on.format == "WEBP" and not on.getexif().get(0x013B))
rp, _ = run_photo(member, png, op="compress", quality=80, format="same")
check("Compression : PNG au format d'origine → message clair (sans perte)", "PNG" in (rp.get("error") or ""), str(rp))
rr_, orr = run_photo(member, jpg, op="resize", width=100, keep_ratio=1)
check("Redimensionnement : largeur 100 px, proportions conservées → 100 × 75, EXIF conservé", orr.size == (100, 75)
      and orr.getexif().get(0x013B) == "Auteur Test" and rr_["filename"].endswith(".jpg"))
_, orr2 = run_photo(member, jpg, op="resize", width=120, height=120, keep_ratio=1)
check("Redimensionnement : cadre 120 × 120 avec proportions → 120 × 90", orr2.size == (120, 90))
_, orr3 = run_photo(member, jpg, op="resize", width=120, height=120, keep_ratio=0)
check("Redimensionnement : sans proportions → exactement 120 × 120", orr3.size == (120, 120))
rr4, _ = run_photo(member, jpg, op="resize", keep_ratio=1)
check("Redimensionnement : aucune dimension → erreur claire", "largeur" in (rr4.get("error") or ""), str(rr4))
rc1, oc1 = run_photo(member, png, op="convert", format="webp")
check("Conversion : PNG → WEBP (transparence conservée, dimensions inchangées)", rc1["filename"].endswith(".webp") and oc1.mode == "RGBA" and oc1.size == (200, 100))
rc2, oc2 = run_photo(member, png, op="convert", format="jpg")
check("Conversion : PNG transparent → JPG", rc2["filename"].endswith(".jpg") and oc2.format == "JPEG" and oc2.mode == "RGB")
rc3, oc3 = run_photo(member, jpg, op="convert", format="png")
check("Conversion : JPG → PNG", rc3["filename"].endswith(".png") and oc3.format == "PNG" and oc3.size == (400, 300))
rc4, _ = run_photo(member, jpg, op="convert", format="gif")
check("Conversion : format non supporté refusé", r"error" in (rc4 or {}) or rc4 is None)
rk1, ok1 = run_photo(member, jpg, strip_gps=1)
check("Nettoyage GPS : GPS supprimé, auteur conservé, pixels/dimensions conservés",
      not ok1.getexif().get_ifd(0x8825) and ok1.getexif().get(0x013B) == "Auteur Test" and ok1.size == (400, 300))
rk2, ok2 = run_photo(member, jpg, strip_all=1)
check("Nettoyage complet : plus aucune métadonnée EXIF", len(ok2.getexif()) == 0 and ok2.size == (400, 300))
rm, om = run_photo(member, jpg, strip_gps=1, rewrite_dates=1, rewrite_software=1, rewrite_author=1,
                   software="ViralScale", author="", copyright="", datetime="2026:01:02 03:04:05")
check("Métadonnées Standard (moteur inchangé) : GPS retiré, logiciel = ViralScale, date réécrite",
      not om.getexif().get_ifd(0x8825) and om.getexif().get(0x0131) == "ViralScale" and om.getexif().get(0x0132) == "2026:01:02 03:04:05")
check("Photo : opération inconnue refusée", member.post("/photo_process", data={"job_id": "x" * 32, "stored_name": "a.jpg", "op": "rm"}).status_code in (400, 404))
with A._users_db() as conn:
    modes = [r[0] for r in conn.execute("SELECT mode FROM usage_logs WHERE user_id = ? AND mode LIKE '%photo%'", (U_MEM["id"],))]
check("Photos : historique — outils = media_optimizer_photo, métadonnées/nettoyage = photo_metadata",
      "media_optimizer_photo" in modes and "photo_metadata" in modes, str(set(modes)))

# ══ AUDIO ════════════════════════════════════════════════════════════
wav = os.path.join(tmp, "son.wav")
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-ac", "2", wav], check=True)

def run_audio(**form):
    st = member.post("/audio_stage", data={"file": (open(wav, "rb"), "son.wav")}, content_type="multipart/form-data").get_json()
    data = {"job_id": st["job_id"], "stored_name": st["stored_name"], "out_name": "son", "index": "0", "count": "1"}
    data.update(form)
    res = member.post("/audio_convert", data=data).get_json()
    p = os.path.join(tmp, f"out_{uuid.uuid4().hex}.mp3")
    open(p, "wb").write(member.get(f"/audio_file/{st['job_id']}/{res['filename']}").data)
    pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=bit_rate,channels,codec_name", "-of", "json", p],
                                   capture_output=True, text=True).stdout)["streams"][0]
    return res, pr

r0, p0 = run_audio()
check("Audio Conversion par défaut : MP3 (moteur d'origine, stéréo)", r0["ok"] and p0["codec_name"] == "mp3" and p0["channels"] == 2
      and r0["final_size"] > 0 and r0["orig_size"] == os.path.getsize(wav))
r1, p1 = run_audio(bitrate="64")
check("Audio Optimisation : débit 64 kb/s réellement appliqué", abs(int(p1["bit_rate"]) - 64000) <= 2000, str(p1))
r2, p2 = run_audio(bitrate="96", mono="1")
check("Audio Optimisation : mono (1 canal)", p2["channels"] == 1 and abs(int(p2["bit_rate"]) - 96000) <= 2000, str(p2))
r3, p3 = run_audio(bitrate="999")
check("Audio : débit non proposé ignoré (réglage d'origine)", r3["ok"] and p3["codec_name"] == "mp3")
check("Audio : 64 kb/s plus léger que 192 kb/s", r1["final_size"] < run_audio(bitrate="192")[0]["final_size"])

print(f"\n{'=' * 40}\n{PASS} tests OK / {FAIL} KO")
sys.exit(0 if FAIL == 0 else 1)
