#!/usr/bin/env node
/**
 * test_viralscale3_browser.mjs — ViralScale 3.0 dans un VRAI navigateur
 * (Chromium via Playwright), vrai serveur Gunicorn (options du Dockerfile),
 * FFmpeg / Pillow / Tesseract réels. Aucune maquette, aucune donnée simulée.
 *
 *  - Navigation par la sidebar sans rechargement, URL stables, actualisation
 *    sur chaque module et sous-onglet, bouton Précédent / Suivant.
 *  - Anciennes URL (/ → /studio, /history → /productions).
 *  - Générateur Mode Simple : A + B → C réellement générée (OCR local).
 *  - Media Tools : Photos (compression + aperçu mesuré, métadonnées),
 *    Audio (optimisation 64 kb/s), Vidéos (estimation, export réel, reprise
 *    après actualisation pendant le traitement).
 *  - Studio : production en cours avec progression réelle, dernières
 *    productions, stockage mesuré.
 *  - Mes productions : téléchargement du ZIP, suppression confirmée →
 *    « Fichiers supprimés — historique conservé. ».
 *  - Reels Studio : sous-onglets Bibliothèque / Captions / Musiques.
 *  - Droits : membre sans Administration ni Paramètres.
 *  - Mobile 390 px : tiroir de navigation, aucun débordement horizontal.
 *  - Aucune erreur JavaScript.
 *
 * Usage :
 *   PYTHON=/chemin/python PLAYWRIGHT_MODULE=/chemin/playwright/index.mjs node test_viralscale3_browser.mjs
 */
import { spawn, execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const REPO = path.dirname(fileURLToPath(import.meta.url));
const PY = process.env.PYTHON || "python3";
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "vs3_browser_"));
const SRC = fs.mkdtempSync(path.join(os.tmpdir(), "vs3_browser_src_"));
const ADMIN = { email: "admin-vs3b@local.test", password: "admin-vs3b-password" };
const MEMBER = { email: "membre-vs3b@local.test", password: "membre-vs3b-password" };
const PORT = 50000 + Math.floor(Math.random() * 2000);
const BASE = `http://127.0.0.1:${PORT}`;

let PASS = 0, FAIL = 0;
const check = (name, cond, extra = "") => { if (cond) { PASS++; console.log(`✅ ${name}`); } else { FAIL++; console.log(`❌ ${name}   ${extra}`); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ── Médias de test réels ──
const ff = args => execFileSync("ffmpeg", ["-y", "-loglevel", "error", ...args]);
const mkvid = (name, w, h, dur = 2) => { const p = path.join(SRC, name);
  ff(["-f", "lavfi", "-i", `testsrc2=s=${w}x${h}:d=${dur}:r=30`, "-f", "lavfi", "-i", `sine=frequency=440:duration=${dur}`,
      "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", p]); return p; };
const V1 = mkvid("clip_a.mp4", 720, 1280, 3), V2 = mkvid("clip_b.mp4", 1280, 720, 2);
const VT = path.join(SRC, "texte_b.mp4");
execFileSync(PY, ["-c", `
from PIL import Image, ImageDraw, ImageFont
import subprocess
img = Image.new("RGB", (720, 1280), (40, 60, 90)); d = ImageDraw.Draw(img)
f = ImageFont.truetype(${JSON.stringify(path.join(REPO, "fonts", "Montserrat-VariableFont_wght.ttf"))}, 64)
t = "HELLO WORLD"; d.text(((720 - d.textlength(t, font=f)) / 2, 1280 * 0.62), t, font=f, fill=(255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0))
img.save(${JSON.stringify(VT + ".png")})
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", ${JSON.stringify(VT + ".png")}, "-f", "lavfi", "-i", "sine=duration=3",
                "-t", "3", "-r", "24", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", ${JSON.stringify(VT)}], check=True)
`]);
const JPG = path.join(SRC, "photo.jpg"), JPG2 = path.join(SRC, "photo2.jpg");
execFileSync(PY, ["-c", `
from PIL import Image
import random
r = random.Random(3)
for p, w, h in ((${JSON.stringify(JPG)}, 800, 600), (${JSON.stringify(JPG2)}, 640, 480)):
    img = Image.new("RGB", (w, h)); px = img.load()
    for y in range(h):
        for x in range(w): px[x, y] = ((x * 3 + r.randint(0, 40)) % 256, (y * 2 + r.randint(0, 40)) % 256, (x + y) % 256)
    ex = img.getexif(); ex[0x013B] = "Auteur"; gps = ex.get_ifd(0x8825); gps[1] = "N"; gps[2] = (48.0, 51.0, 24.0)
    img.save(p, "JPEG", quality=95, exif=ex.tobytes())
`]);
const WAV = path.join(SRC, "son.wav");
ff(["-f", "lavfi", "-i", "sine=frequency=500:duration=4", "-ac", "2", WAV]);

// ── Serveur réel (options du Dockerfile) ──
const server = spawn(PY, ["-m", "gunicorn", "app:app", "--bind", `127.0.0.1:${PORT}`, "--workers", "1", "--threads", "4",
  "--timeout", "0", "--keep-alive", "65", "--log-level", "warning"], {
  cwd: REPO, detached: true, stdio: ["ignore", fs.openSync(path.join(DATA, "gunicorn.log"), "w"), fs.openSync(path.join(DATA, "gunicorn.err"), "w")],
  env: { ...process.env, DATA_DIR: DATA, ADMIN_EMAIL: ADMIN.email, ADMIN_PASSWORD: ADMIN.password, SECRET_KEY: "vs3-browser-test",
         VEXPORT_FFMPEG_THREADS: "2", FFMPEG_GATE_WAIT: "2" } });
const stopServer = () => { try { process.kill(-server.pid, "SIGKILL"); } catch (e) {} };
process.on("exit", stopServer);
for (let i = 0; i < 150; i++) { try { if ((await fetch(BASE + "/login")).ok) break; } catch (e) {} await sleep(100); }
execFileSync(PY, ["-c", `import app as A; A.create_user(${JSON.stringify(MEMBER.email)}, ${JSON.stringify(MEMBER.password)}, "member")`],
  { cwd: REPO, env: { ...process.env, DATA_DIR: DATA, VEXPORT_WORKER: "0", ADMIN_EMAIL: ADMIN.email, ADMIN_PASSWORD: ADMIN.password }, stdio: "ignore" });

const browser = await chromium.launch();
const jsErrors = [];
async function newPage(who = ADMIN, opts = {}) {
  const ctx = await browser.newContext({ acceptDownloads: true, viewport: { width: 1440, height: 900 }, ...opts });
  const page = await ctx.newPage();
  page.on("pageerror", e => jsErrors.push(e.message));
  page.on("dialog", d => d.accept());
  await page.goto(BASE + "/login");
  await page.fill('input[name="email"]', who.email); await page.fill('input[name="password"]', who.password);
  await Promise.all([page.waitForNavigation(), page.click('button[type="submit"], input[type="submit"]')]);
  return page;
}
const txt = (page, sel) => page.locator(sel).innerText();
const state = page => page.evaluate(() => ({ path: location.pathname, tab: document.querySelector(".tab-content.active")?.id,
  nav: document.querySelector("#vsSidebar .vs-nav-item.active")?.dataset.nav || null,
  sub: document.querySelector("#subnav .subnav-pill.active")?.dataset.sub || null, h1: document.querySelector(".app-header h1")?.textContent }));
const holdEncoder = () => {                              // occupe le créneau d'encodage : un export reste « en cours »
  const h = spawn(PY, ["-c", `import fcntl, os, time, sys
fd = os.open(${JSON.stringify(path.join(DATA, "videobot_video_exports", ".encode.lock"))}, os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX); print("held", flush=True); sys.stdin.read()`], { stdio: ["pipe", "pipe", "inherit"] });
  return { ready: new Promise(r => h.stdout.once("data", r)), release: () => { try { h.stdin.end(); } catch (e) {} } };
};

try {
  const page = await newPage();

  // ══ ANCIENNES URL ═══════════════════════════════════════════════════
  check("« / » ouvre le Studio et l'URL devient /studio", (await state(page)).path === "/studio" && (await state(page)).tab === "studioTab");
  await page.goto(BASE + "/history");
  check("Ancienne URL /history → Mes productions", (await state(page)).path === "/productions" && (await state(page)).tab === "productionsTab");

  // ══ NAVIGATION SANS RECHARGEMENT ═════════════════════════════════════
  await page.goto(BASE + "/studio");
  await page.evaluate(() => { window.__vsMarker = 42; });
  const NAV = [["generator", "/generator", "simpleTab"], ["reels", "/reels", "reelsTab"], ["variations", "/variations", "variationMultiTab"],
               ["videos", "/media/videos", "optimizerTab"], ["photos", "/media/photos", "photoTab"], ["audio", "/media/audio", "audioTab"],
               ["productions", "/productions", "productionsTab"], ["settings", "/admin/settings", "settingsTab"], ["studio", "/studio", "studioTab"]];
  const navBad = [];
  for (const [key, url, tabId] of NAV) {
    await page.click(`#vsSidebar [data-nav="${key}"]`); await sleep(150);
    const s = await state(page);
    if (s.path !== url || s.tab !== tabId || s.nav !== key) navBad.push([key, s]);
  }
  check("Sidebar : chaque entrée ouvre son module, URL et entrée active à jour", navBad.length === 0, JSON.stringify(navBad));
  check("Sidebar : navigation sans rechargement de la page", await page.evaluate(() => window.__vsMarker === 42));
  await page.click('#vsSidebar [data-nav="team"]'); await page.waitForURL(BASE + "/admin/users");
  check("Gestion équipe : page serveur avec la même sidebar (entrée active)", await page.locator('#vsSidebar [data-nav="team"].active').count() === 1);
  await page.click('#vsSidebar [data-nav="photos"]'); await page.waitForURL(BASE + "/media/photos");
  check("Depuis une page serveur : retour vers un module par la sidebar", (await state(page)).tab === "photoTab");

  // ══ ACTUALISATION SUR CHAQUE MODULE / SOUS-ONGLET ════════════════════
  const RELOAD = [["/generator", "simpleTab", null], ["/generator/batch", "batchTab", null], ["/reels", "reelsTab", "library"],
    ["/reels/captions", "reelsTab", "captions"], ["/reels/music", "reelsTab", "music"], ["/variations", "variationMultiTab", null],
    ["/media/videos", "optimizerTab", "standard"], ["/media/videos/optimize", "optimizerTab", "optimize"], ["/media/photos", "photoTab", "compression"],
    ["/media/photos/resize", "photoTab", "resize"], ["/media/photos/convert", "photoTab", "convert"], ["/media/photos/clean", "photoTab", "clean"],
    ["/media/photos/metadata", "photoTab", "metadata"], ["/media/audio", "audioTab", "convert"], ["/media/audio/optimize", "audioTab", "optimize"],
    ["/productions", "productionsTab", "all"], ["/productions/done", "productionsTab", "done"], ["/admin/settings", "settingsTab", null], ["/studio", "studioTab", null]];
  const reloadBad = [];
  for (const [url, tabId, sub] of RELOAD) {
    await page.goto(BASE + url); await page.reload(); await sleep(150);
    const s = await state(page);
    if (s.path !== url || s.tab !== tabId || (sub && s.sub !== sub)) reloadBad.push([url, s]);
  }
  check(`Actualisation : ${RELOAD.length} modules / sous-onglets restent affichés (jamais de retour à l'accueil)`, reloadBad.length === 0, JSON.stringify(reloadBad));
  await page.goto(BASE + "/media/photos/metadata");
  check("Photos / Métadonnées : seul le panneau Métadonnées est visible", await page.locator('[data-vsub][data-sub=\"metadata\"]').isVisible()
        && await page.locator('[data-vsub][data-sub=\"compression\"]').isHidden() && await page.locator('[data-vsub][data-sub=\"clean\"]').isHidden());
  await page.goto(BASE + "/media/videos/optimize");
  check("Vidéos / Optimisation : mode optimisation actif (réglages visibles)", await page.locator("#vtOptParams").isVisible()
        && await page.evaluate(() => vtMode()) === "optimize");
  await page.goto(BASE + "/generator/batch");
  check("Générateur : sous-onglets Mode Simple / Mode Batch", JSON.stringify(await page.locator("#subnav .subnav-pill").allInnerTexts()) === JSON.stringify(["Mode Simple", "Mode Batch"]));

  // ══ BOUTON PRÉCÉDENT / SUIVANT ═══════════════════════════════════════
  await page.goto(BASE + "/studio");
  await page.click('#vsSidebar [data-nav="photos"]'); await sleep(100);
  await page.click('#subnav a[data-sub="resize"]'); await sleep(100);
  await page.click('#subnav a[data-sub="clean"]'); await sleep(100);
  await page.goBack(); await sleep(150); const b1 = await state(page);
  await page.goBack(); await sleep(150); const b2 = await state(page);
  await page.goBack(); await sleep(150); const b3 = await state(page);
  await page.goForward(); await sleep(150); const f1 = await state(page);
  check("Précédent : Nettoyage → Redimensionnement → Compression → Studio", b1.sub === "resize" && b1.path === "/media/photos/resize"
        && b2.sub === "compression" && b3.tab === "studioTab", JSON.stringify([b1, b2, b3]));
  check("Suivant : retour à Photos / Compression", f1.tab === "photoTab" && f1.sub === "compression");
  await page.click('#subnav a[data-sub="convert"]'); await sleep(100);
  await page.click('#vsSidebar [data-nav="audio"]'); await sleep(100);
  await page.click('#vsSidebar [data-nav="photos"]'); await sleep(100);
  check("Sous-onglet persistant : retour sur Photos = dernier sous-onglet utilisé", (await state(page)).sub === "convert");

  // ══ GÉNÉRATEUR · MODE SIMPLE (A + B → C réel) ════════════════════════
  await page.goto(BASE + "/generator");
  const simpleTxt = await txt(page, "#simpleTab");
  check("Mode Simple : 5 étapes (A, B, Paramètres, Aperçu, Génération)", ["1 Vidéo A", "2 Vidéo B", "3 Paramètres", "4 Aperçu du texte de la Vidéo C", "5 Génération"]
        .every(t => simpleTxt.replace(/\s+/g, " ").includes(t)) && simpleTxt.includes("Vidéo A + Vidéo B → Vidéo C"), simpleTxt.slice(0, 300));
  check("Mode Simple : paramètres avancés repliés", await page.locator("#simpleTab details.adv-params").first().evaluate(d => !d.open));
  await page.locator("#simpleTab details.adv-params summary").first().click();
  await page.locator('label:has(input[name="ocrModeS"][value="local"])').click();
  check("Mode Simple : résumé des paramètres à jour", (await txt(page, "#sParamSummary")).includes("Local"));
  await page.setInputFiles("#inputA", V1); await page.setInputFiles("#inputB", VT);
  await page.waitForFunction(() => !document.getElementById("submitBtn").disabled, null, { timeout: 120000 });
  check("Mode Simple : texte de la Vidéo B détecté (aperçu)", /HELLO|WORLD/i.test(await page.evaluate(() => JSON.stringify(linesData))));
  await page.click("#submitBtn");
  check("Mode Simple : barre d'activité sans pourcentage inventé", await page.locator("#progressCard .pbar.vs-indet").count() === 1);
  await page.waitForSelector("#resultCard", { state: "visible", timeout: 240000 });
  const cHref = await page.getAttribute("#downloadBtn", "href");
  const cRes = await page.request.get(BASE + cHref);
  check("Mode Simple : Vidéo C réellement générée et téléchargeable", cRes.ok() && (await cRes.body()).length > 10000, `${cRes.status()}`);

  // ══ MEDIA TOOLS · PHOTOS ════════════════════════════════════════════
  await page.goto(BASE + "/media/photos");
  await page.setInputFiles("#phInput", [JPG, JPG2]);
  await page.waitForFunction(() => document.getElementById("phPrevOrigM").textContent.includes("800 × 600"), null, { timeout: 10000 });
  check("Photos : import (2 photos), miniatures, taille totale et aperçu de l'original",
        (await txt(page, "#phEstCount")) === "2" && await page.locator("#phThumbs img").count() === 2 && (await txt(page, "#phEstSize")) !== "—");
  check("Photos : « Qualité de sortie : 80 % » par défaut", (await txt(page, "#phqQualityV")) === "80 %");
  await page.locator("#phqQuality").fill("40");
  await page.click('[data-vsub][data-sub=\"compression\"] .vs-ph-run');
  await page.waitForFunction(() => /Compression terminée/.test(document.getElementById("phProgressTitle").innerText), null, { timeout: 60000 });
  await page.waitForFunction(() => /\(−\d+ %\)/.test(document.getElementById("phPrevOutM").textContent), null, { timeout: 10000 });
  check("Photos : compression réelle, aperçu du résultat MESURÉ (poids et réduction)", await page.locator("#phPrevOut").isVisible()
        && /→/.test(await txt(page, "#phResultsList")) && /total .* → /.test(await txt(page, "#phGlobalStatus")), await txt(page, "#phPrevOutM"));
  const [zipDl] = await Promise.all([page.waitForEvent("download", { timeout: 30000 }), page.click("#phZipBtn")]);
  check("Photos : ZIP des photos compressées", zipDl.suggestedFilename() === "photos_compressees.zip");
  await page.click('#subnav a[data-sub="resize"]');
  check("Redimensionnement : bouton inactif tant qu'aucune dimension n'est choisie", await page.locator('[data-vsub][data-sub=\"resize\"] .vs-ph-run').isDisabled());
  await page.locator('label:has(input[name="phrMode"][value="custom"])').click(); await page.fill("#phrW", "200");
  check("Redimensionnement : actif avec une largeur", await page.locator('[data-vsub][data-sub=\"resize\"] .vs-ph-run').isEnabled());
  await page.click('#subnav a[data-sub="metadata"]');
  await page.locator('label:has(input[name="phMode"][value="standard"])').click();
  await page.click("#phLaunchBtn");
  await page.waitForFunction(() => /Traitement terminé/.test(document.getElementById("phProgressTitle").innerText), null, { timeout: 60000 });
  check("Photos : métadonnées (mode Standard, moteur existant) traitées", ((await txt(page, "#phResultsList")).match(/✅/g) || []).length === 2);

  // ══ MEDIA TOOLS · AUDIO ═════════════════════════════════════════════
  await page.goto(BASE + "/media/audio/optimize");
  await page.setInputFiles("#auInput", WAV);
  await page.selectOption("#auBitrate", "64");
  await page.waitForFunction(() => /Taille estimée/.test(document.getElementById("auEstOpt").textContent), null, { timeout: 10000 });
  check("Audio Optimisation : taille estimée = débit × durée réelle", /≈/.test(await txt(page, "#auEstOpt")) && (await txt(page, "#auEstOut")) === "MP3 · 64 kb/s");
  check("Audio Optimisation : bouton « Optimiser les fichiers audio »", (await txt(page, "#auLaunchLbl")) === "Optimiser les fichiers audio");
  await page.click("#auLaunchBtn");
  await page.waitForFunction(() => /✅/.test(document.getElementById("auResultsList").innerText), null, { timeout: 60000 });
  check("Audio : conversion réelle, poids avant → après affiché", /→/.test(await txt(page, "#auResultsList")));

  // ══ MEDIA TOOLS · VIDÉOS + STUDIO + MES PRODUCTIONS ═════════════════
  await page.goto(BASE + "/media/videos");
  check("Vidéos : limite affichée 300", (await txt(page, "#veLbl")).includes("300") && (await txt(page, "#optimizerTab")).includes("Maximum : 300 vidéos par batch"));
  await page.setInputFiles("#veInput", [V1, V2, path.join(SRC, "texte_b.mp4.png")]);
  await page.waitForFunction(() => document.getElementById("veInfoNeed").textContent !== "—", null, { timeout: 10000 });
  check("Vidéos : nombre, taille des sources, espace disponible et espace nécessaire (estimés par le serveur)",
        (await txt(page, "#veInfoN")) === "2" && /[KMG]o/.test(await txt(page, "#veInfoSize")) && /Go/.test(await txt(page, "#veInfoFree")) && /[KMG]o/.test(await txt(page, "#veInfoNeed")));
  check("Vidéos : compatibilité (fichier ignoré signalé)", /ignoré/.test(await txt(page, "#veCompat")), await txt(page, "#veCompat"));
  const hold = holdEncoder(); await hold.ready;
  await page.click("#veBtn");
  await page.waitForFunction(() => veBatchId && veLastStatus && veLastStatus.started, null, { timeout: 60000 });
  const bid = await page.evaluate(() => veBatchId);
  await page.reload();
  await page.waitForFunction(id => veBatchId === id && document.getElementById("veProgressWrap").style.display === "block", bid, { timeout: 20000 });
  check("Actualisation pendant un export : le traitement serveur est repris (jamais perdu)", (await state(page)).tab === "optimizerTab");
  await page.click('#vsSidebar [data-nav="studio"]');
  await page.waitForFunction(() => /Export vidéo/.test(document.getElementById("vsActiveList").innerText), null, { timeout: 15000 });
  const act = await txt(page, "#vsActiveList");
  check("Studio : production en cours avec progression RÉELLE (0 / 2 vidéos) et bouton Voir", /0 \/ 2 vidéos/.test(act) && /Voir/.test(act)
        && !/Temps restant/.test(act), act);
  check("Sidebar : compteur de productions en cours", (await txt(page, "#vsNavActive")) === "1");
  hold.release();
  await page.click('#vsActiveList a:has-text("Voir")');
  await page.waitForFunction(() => veLastStatus && veLastStatus.archives_settled && veLastStatus.all_zips_ready, null, { timeout: 180000 });
  check("« Voir » ouvre l'export dans Media Tools · Vidéos, terminé avec son ZIP", (await state(page)).tab === "optimizerTab"
        && (await txt(page, "#veArchList")).includes("2 vidéos exportées"));
  await page.click('#vsSidebar [data-nav="studio"]');
  await page.waitForFunction(() => /Export vidéo/.test(document.getElementById("vsRecentList").innerText), null, { timeout: 15000 });
  check("Studio : dernière production avec téléchargement réel", await page.locator('#vsRecentList a:has-text("Télécharger")').count() >= 1);
  check("Studio : stockage mesuré (total / utilisé / disponible)", /Go/.test(await txt(page, "#vsStoTotal")) && /Go/.test(await txt(page, "#vsStoFree")));
  await page.click("#vsFreeBtn");
  await page.waitForSelector("#veModal", { state: "visible", timeout: 15000 });
  check("Studio (admin) : « Libérer de l'espace » ouvre l'inventaire réel (fonction de nettoyage existante)",
        (await txt(page, "#veModalTitle")) === "Libérer de l'espace" && /Espace libre/.test(await txt(page, "#veModalDetails")));
  await page.locator("#veModalActions button").first().click();
  await page.waitForSelector("#veModal", { state: "hidden" });
  await page.click('#vsSidebar [data-nav="productions"]');
  await page.waitForFunction(id => !!document.querySelector(`#vsProdBody tr[data-batch="${id}"]`), bid, { timeout: 15000 });
  const row = page.locator(`#vsProdBody tr[data-batch="${bid}"]`);
  check("Mes productions : export listé (module, 2 fichiers, taille du ZIP, Terminé)", /Media Tools · Vidéos/.test(await row.innerText())
        && /Terminé/.test(await row.innerText()) && (await row.locator('td[data-l="Fichiers"]').innerText()) === "2"
        && (await row.locator('td[data-l="Taille"]').innerText()) !== "—", await row.innerText());
  const [zdl] = await Promise.all([page.waitForEvent("download", { timeout: 30000 }), row.locator('a:has-text("Télécharger")').click()]);
  check("Mes productions : téléchargement du ZIP", zdl.suggestedFilename() === "Batch_001.zip");
  await page.click('#subnav a[data-sub="failed"]');
  await page.waitForFunction(() => !document.getElementById("vsProdBody").innerText.includes("Chargement"), null, { timeout: 10000 });
  check("Mes productions : onglet Échoués sans l'export terminé", await page.locator(`#vsProdBody tr[data-batch="${bid}"]`).count() === 0);
  await page.click('#subnav a[data-sub="all"]');
  await page.waitForFunction(id => !!document.querySelector(`#vsProdBody tr[data-batch="${id}"]`), bid, { timeout: 15000 });
  await page.locator(`#vsProdBody tr[data-batch="${bid}"] button:has-text("Supprimer")`).click();
  await page.waitForSelector("#veModal", { state: "visible" });
  check("Suppression : confirmation explicite", (await txt(page, "#veModalText")).includes("définitivement")
        && JSON.stringify(await page.locator("#veModalActions button").allInnerTexts()) === JSON.stringify(["Conserver", "Supprimer"]));
  await page.locator('#veModalActions button:has-text("Supprimer")').click();
  await page.waitForFunction(id => { const r = document.querySelector(`#vsProdBody tr[data-batch="${id}"]`); return r && /Fichiers supprimés — historique conservé/.test(r.innerText); }, bid, { timeout: 30000 });
  check("Après suppression : « Fichiers supprimés — historique conservé. », plus de téléchargement",
        await page.locator(`#vsProdBody tr[data-batch="${bid}"] a:has-text("Télécharger")`).count() === 0);
  const gens = await txt(page, "#vsProdBody");
  check("Mes productions : générations réelles du test présentes (Simple, Photos, Audio)",
        /Générateur vidéo · Simple/.test(gens) && /Media Tools · Photos/.test(gens) && /Media Tools · Audio/.test(gens), gens.slice(0, 400));

  // ══ REELS STUDIO : sous-onglets ═════════════════════════════════════
  await page.goto(BASE + "/reels");
  check("Reels / Bibliothèque : reels + captions visibles", await page.locator("#rZoneReels").isVisible() && await page.locator("#rCapPane").isVisible());
  await page.click('#subnav a[data-sub="captions"]');
  check("Reels / Captions : liste des captions en pleine largeur (bibliothèque des reels masquée)",
        await page.locator("#rCapPane").isVisible() && await page.locator("#rZoneReels").isHidden() && await page.locator("#rGenBtn").isVisible());
  await page.click('#subnav a[data-sub="music"]');
  check("Reels / Musiques : bibliothèque audio", await page.locator("#rMusPane").isVisible() && await page.locator("#rCapPane").isHidden());
  const genbar = await txt(page, "#reelsTab .ws-genbar .ws-infobar");
  check("Reels / Production : compteurs reels, sélectionnés, captions, musiques, à générer",
        ["reels", "sélectionnés", "captions", "musiques", "à générer"].every(t => genbar.toLowerCase().includes(t)), genbar);

  // ══ AUCUNE DONNÉE FICTIVE ═══════════════════════════════════════════
  const fake = [];
  for (const u of ["/studio", "/generator/batch", "/variations", "/productions", "/admin/settings"]) {
    await page.goto(BASE + u); await sleep(400);
    const t = await page.locator("main").innerText();
    if (/Temps restant|Coût estimé|temps estimé|\$\d/.test(t)) fake.push(u);
  }
  check("Aucune durée, aucun coût inventé affichés (Studio, Batch, Variations, Productions, Paramètres)", fake.length === 0, JSON.stringify(fake));
  check("Paramètres : valeurs réelles du serveur", /300/.test(await txt(page, "#vsSetExport")) && /CRF 20/.test(await txt(page, "#vsSetExport")));
  await page.waitForFunction(() => /Batch Video Export/.test(document.getElementById("vsSetUsage").innerText), null, { timeout: 15000 });
  check("Paramètres : répartition du stockage par module mesurée", /Générateur vidéo/.test(await txt(page, "#vsSetUsage")));

  // ══ DROITS : MEMBRE ═════════════════════════════════════════════════
  const mp = await newPage(MEMBER);
  check("Membre : pas de section Administration", await mp.locator('#vsSidebar [data-nav="settings"], #vsSidebar [data-nav="team"]').count() === 0);
  await mp.goto(BASE + "/admin/settings");
  check("Membre : /admin/settings refusé", (await mp.locator("body").innerText()).includes("réservé aux administrateurs"));
  await mp.goto(BASE + "/studio");
  check("Membre : « Libérer de l'espace » mène à ses productions terminées", await mp.locator('#vsStoragePanel a[href="/productions/done"]').count() === 1);
  await mp.goto(BASE + "/productions");
  await mp.waitForFunction(() => !document.getElementById("vsProdBody").innerText.includes("Chargement"), null, { timeout: 10000 });
  check("Membre : ne voit pas les productions de l'administrateur", (await mp.locator("#vsProdBody tr").count()) === 0
        && (await txt(mp, "#vsProdEmpty")).includes("Aucune production"));
  await mp.context().close();

  // ══ MOBILE ══════════════════════════════════════════════════════════
  const m = await newPage(ADMIN, { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const over = [];
  for (const u of ["/studio", "/generator", "/generator/batch", "/reels", "/variations", "/media/videos", "/media/photos", "/media/audio", "/productions", "/admin/settings"]) {
    await m.goto(BASE + u); await sleep(300);
    const w = await m.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    if (w[0] > w[1] + 1) over.push([u, w]);
  }
  check("Mobile 390 px : aucun débordement horizontal (10 pages)", over.length === 0, JSON.stringify(over));
  check("Mobile : sidebar repliée par défaut", await m.evaluate(() => document.getElementById("vsSidebar").getBoundingClientRect().right <= 0));
  await m.click("#vsBurger"); await sleep(350);
  check("Mobile : le bouton menu ouvre la navigation", await m.evaluate(() => document.getElementById("vsSidebar").getBoundingClientRect().left >= 0));
  await m.click('#vsSidebar [data-nav="audio"]'); await sleep(350);
  check("Mobile : choisir une page ferme le menu et ouvre le module", (await state(m)).tab === "audioTab"
        && await m.evaluate(() => !document.body.classList.contains("vs-side-open")));
  await m.context().close();
} catch (e) {
  FAIL++; console.log("❌ Exception : " + (e && e.stack || e));
} finally {
  check("Aucune erreur JavaScript de page", jsErrors.length === 0, JSON.stringify(jsErrors.slice(0, 5)));
  await browser.close();
  stopServer();
  fs.rmSync(SRC, { recursive: true, force: true });
  console.log(`\n${"=".repeat(40)}\n${PASS} tests OK / ${FAIL} KO`);
  process.exit(FAIL ? 1 : 0);
}
