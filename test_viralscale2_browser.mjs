#!/usr/bin/env node
/**
 * test_viralscale2_browser.mjs — Tests dans un VRAI navigateur (Chromium via
 * Playwright) des interfaces ViralScale 2.0, sur un vrai serveur Gunicorn
 * (options du Dockerfile) avec FFmpeg réel. Aucune maquette.
 *
 *  - Générateur : Matrice A × B seule, calcul affiché, limite 300, rendu 1 × 1 réel.
 *  - Studio Variations : interface unique, Automatique / Avancé, estimation,
 *    génération réelle, nouvel essai automatique quand le serveur répond 503.
 *  - Media Tools : onglets Vidéos / Photos / Audio, réexport standard +
 *    archives + « Tout télécharger », optimisation 1 vidéo et plusieurs.
 *  - Reels Studio : aperçu (vidéo + calques de caption du moteur réel +
 *    musique), préréglages de rendu (créer, persistance après rechargement,
 *    charger, renommer, supprimer), génération + aperçu du rendu final.
 *  - Mobile (390 px) : pas de défilement horizontal sur les écrans modifiés.
 *  - Aucune erreur JavaScript de page.
 *
 * Usage :
 *   PYTHON=/chemin/python PLAYWRIGHT_MODULE=/chemin/playwright/index.mjs node test_viralscale2_browser.mjs
 */
import { spawn, execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const REPO = path.dirname(fileURLToPath(import.meta.url));
const PY = process.env.PYTHON || "python3";
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "vs2_browser_"));
const SRC = fs.mkdtempSync(path.join(os.tmpdir(), "vs2_browser_src_"));
const ADMIN = { email: "admin-browser@local.test", password: "admin-browser-password" };
const PORT = 52000 + Math.floor(Math.random() * 3000);
const BASE = `http://127.0.0.1:${PORT}`;

let PASS = 0, FAIL = 0;
const check = (name, cond, extra = "") => { if (cond) { PASS++; console.log(`✅ ${name}`); } else { FAIL++; console.log(`❌ ${name}   ${extra}`); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ── Médias de test ──
const ff = args => execFileSync("ffmpeg", ["-y", "-loglevel", "error", ...args]);
const mkvid = (name, w, h, dur = 2) => { const p = path.join(SRC, name);
  ff(["-f", "lavfi", "-i", `testsrc2=s=${w}x${h}:d=${dur}:r=30`, "-f", "lavfi", "-i", `sine=frequency=440:duration=${dur}`,
      "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", p]); return p; };
const V1 = mkvid("clip_a.mp4", 720, 1280, 3), V2 = mkvid("clip_b.mp4", 1280, 720, 2), V1080 = mkvid("clip_1080.mp4", 1080, 1920, 2);
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
const MP3 = path.join(SRC, "musique.mp3");
ff(["-f", "lavfi", "-i", "sine=frequency=220:duration=5", "-c:a", "libmp3lame", MP3]);
const PNG = path.join(SRC, "photo.png");
execFileSync(PY, ["-c", `from PIL import Image; Image.new("RGB", (1600, 1200), (60, 120, 200)).save(${JSON.stringify(PNG)})`]);
const WAV = path.join(SRC, "son.wav");
ff(["-f", "lavfi", "-i", "sine=frequency=500:duration=2", WAV]);

// ── Serveur réel ──
const server = spawn(PY, ["-m", "gunicorn", "app:app", "--bind", `127.0.0.1:${PORT}`, "--workers", "1", "--threads", "4",
  "--timeout", "0", "--keep-alive", "65", "--log-level", "warning"], {
  cwd: REPO, detached: true, stdio: ["ignore", fs.openSync(path.join(DATA, "gunicorn.log"), "w"), fs.openSync(path.join(DATA, "gunicorn.err"), "w")],
  env: { ...process.env, DATA_DIR: DATA, ADMIN_EMAIL: ADMIN.email, ADMIN_PASSWORD: ADMIN.password, SECRET_KEY: "browser-test",
         VEXPORT_FFMPEG_THREADS: "2", FFMPEG_GATE_WAIT: "2" } });
const stopServer = () => { try { process.kill(-server.pid, "SIGKILL"); } catch (e) {} };
process.on("exit", stopServer);
for (let i = 0; i < 150; i++) { try { if ((await fetch(BASE + "/login")).ok) break; } catch (e) {} await sleep(100); }

const browser = await chromium.launch();
const jsErrors = [];
async function newPage(opts = {}) {
  const ctx = await browser.newContext({ acceptDownloads: true, viewport: { width: 1366, height: 900 }, ...opts });
  const page = await ctx.newPage();
  page.on("pageerror", e => jsErrors.push(e.message));
  await page.goto(BASE + "/login");
  await page.fill("#email", ADMIN.email); await page.fill("#password", ADMIN.password);
  await Promise.all([page.waitForURL(BASE + "/"), page.click("button[type=submit]")]);
  return page;
}
const tab = (page, t) => page.evaluate(t => switchTab(t), t);
const txt = (page, sel) => page.locator(sel).innerText();

try {
  const page = await newPage();

  // ══ GÉNÉRATEUR ══════════════════════════════════════════════════════
  await tab(page, "batch");
  check("Générateur : sélecteurs Pairing / Variations absents", await page.locator('input[name="bStrategy"][value="pairing"], input[name="bStrategy"][value="variations"], #bVarCount, #bPairMode').count() === 0);
  check("Générateur : mode Matrice A × B affiché", await page.locator("#batchTab .ws-mode-fixed").isVisible()
        && (await txt(page, "#batchTab .ws-mode-fixed")).includes("Matrice A × B"));
  await page.setInputFiles("#bInputA", [V1, V2]); await page.setInputFiles("#bInputB", [VT, VT, VT]);
  check("Générateur : « 2 vidéos A × 3 vidéos B = 6 vidéos générées »",
        (await txt(page, "#bComboPreview .ws-matrix-total")).includes("2 vidéos A × 3 vidéos B = 6 vidéos générées"));
  check("Générateur : lancement possible", await page.locator("#bLaunchBtn").isEnabled());
  await page.setInputFiles("#bInputA", Array(20).fill(V1)); await page.setInputFiles("#bInputB", Array(16).fill(VT));
  const over = await txt(page, "#bComboPreview .ws-matrix-total");
  check("Générateur : 20 × 16 = 320 signalé et bloqué (limite 300)", over.includes("320") && over.includes("trop") && !(await page.locator("#bLaunchBtn").isEnabled()), over);
  await page.setInputFiles("#bInputA", Array(20).fill(V1)); await page.setInputFiles("#bInputB", Array(15).fill(VT));
  check("Générateur : 20 × 15 = 300 accepté", (await txt(page, "#bComboPreview .ws-matrix-total")).includes("= 300 vidéos générées") && await page.locator("#bLaunchBtn").isEnabled());
  await page.setInputFiles("#bInputA", [V1]); await page.setInputFiles("#bInputB", [VT]);
  await page.locator('label:has(input[name="ocrModeB"][value="local"])').click();
  await page.click("#bLaunchBtn");
  await page.waitForFunction(() => /✅/.test(document.getElementById("bResultsList").innerText) || /❌/.test(document.getElementById("bResultsList").innerText), null, { timeout: 180000 });
  check("Générateur : matrice 1 × 1 réellement générée dans le navigateur (OCR local + captions)",
        (await txt(page, "#bResultsList")).includes("✅"), await txt(page, "#bResultsList"));

  // ══ STUDIO VARIATIONS ═══════════════════════════════════════════════
  await tab(page, "variation");
  check("Variations : plus de sous-onglets « 1 vidéo / Plusieurs vidéos »", await page.locator("#subnav").isHidden());
  check("Variations : interface unique affichée", await page.locator("#variationMultiTab").isVisible() && await page.locator("#variationTab").isHidden());
  check("Variations : Automatique (Recommandé) par défaut", await page.locator('input[name="vmConfigMode"][value="preset"]').isChecked()
        && (await txt(page, "#variationMultiTab .v-modeswitch")).includes("Automatique"));
  check("Variations : aucun score d'unicité / de sécurité inventé", !(await txt(page, "#variationMultiTab")).includes("Score d"));
  await page.evaluate(() => setVmCount(2));
  await page.setInputFiles("#vmInput", [V1]);
  await page.waitForFunction(() => document.getElementById("vmEstVideos").textContent === "1");
  check("Variations : 1 vidéo × 2 = 2 sorties estimées", (await txt(page, "#vmEstTotal")) === "2");
  await page.setInputFiles("#vmInput", [V1, V2, V1080]);
  await page.waitForFunction(() => document.getElementById("vmEstVideos").textContent === "3");
  check("Variations : 3 vidéos × 2 = 6 sorties estimées", (await txt(page, "#vmEstTotal")) === "6");
  await page.locator('label:has(input[name="vmConfigMode"][value="advanced"])').click();
  check("Variations : mode Avancé = 16 réglages", await page.locator("#vmAdvancedPanel").isVisible()
        && await page.locator("#vmAdvBody .v-adv-row").count() === 16);
  await page.locator('label:has(input[name="vmConfigMode"][value="preset"])').click();
  check("Variations : retour au mode Automatique", await page.locator("#vmAdvancedPanel").isHidden());
  // Génération réelle pendant qu'un autre traitement occupe le créneau FFmpeg → 503 puis nouvel essai automatique.
  await page.setInputFiles("#vmInput", [V1]);
  await page.waitForFunction(() => document.getElementById("vmEstVideos").textContent === "1");
  const statuses = [];
  page.on("response", r => { if (/\/variation_(stage|multi_run)/.test(r.url())) statuses.push(r.status()); });
  const holder = spawn(PY, ["-c", `import fcntl, os, time
fd = os.open(${JSON.stringify(path.join(DATA, "videobot_video_exports", ".encode.lock"))}, os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX); print("held", flush=True); time.sleep(7)`], { stdio: ["ignore", "pipe", "inherit"] });
  await new Promise(r => holder.stdout.once("data", r));
  await page.click("#vmLaunchBtn");
  await page.waitForFunction(() => { const b = document.getElementById("vmZipBtn"); return b && b.style.display !== "none"; }, null, { timeout: 180000 });
  const rows = await txt(page, "#vmResultsList");
  check("Variations : 2 variantes générées dans le navigateur", (rows.match(/✅/g) || []).length === 2, rows);
  check("Créneau FFmpeg occupé : 503 reçu puis nouvel essai automatique réussi",
        statuses.includes(503) && statuses[statuses.length - 1] === 200 && !rows.includes("❌"), JSON.stringify(statuses));
  check("Variations : liens de téléchargement", await page.locator('#vmResultsList a.dl-link').count() === 2);

  // ══ MEDIA TOOLS ═════════════════════════════════════════════════════
  await tab(page, "optimizer");
  check("Media Tools : onglets Vidéos / Photos / Audio", JSON.stringify(await page.locator("#subnav .subnav-pill").allInnerTexts()) === JSON.stringify(["Vidéos", "Photos", "Audio"]));
  check("Vidéos : deux modes, réglages d'optimisation masqués en réexport", await page.locator("#optimizerTab .vt-mode").count() === 2 && await page.locator("#vtOptParams").isHidden());
  await page.setInputFiles("#veInput", [V1, V2]);
  check("Vidéos : « Lancer le réexport » (2 vidéos)", (await txt(page, "#veBtn")) === "Lancer le réexport" && await page.locator("#veBtn").isEnabled());
  await page.click("#veBtn");
  await page.waitForFunction(() => veLastStatus && veLastStatus.archives_settled && veLastStatus.all_zips_ready, null, { timeout: 180000 });
  const firstBatch = await page.evaluate(() => veBatchId);
  check("Réexport standard : archive ZIP 1 prête (2 vidéos exportées)", (await txt(page, "#veArchList")).includes("2 vidéos exportées"));
  const [dl] = await Promise.all([page.waitForEvent("download", { timeout: 30000 }), page.click("#veZipBtn")]);
  check("« Tout télécharger » : téléchargement Batch_001.zip", dl.suggestedFilename() === "Batch_001.zip");
  await page.click("#veSavedBtn");
  await page.waitForFunction(() => /Sauvegarde confirmée/.test(document.getElementById("veSaveInfo").innerText), null, { timeout: 15000 });
  check("« J'ai sauvegardé mes ZIP » : validation enregistrée", (await txt(page, "#veSaveInfo")).includes("Sauvegarde confirmée"));
  await page.locator('label:has(input[name="vtMode"][value="optimize"])').click();
  check("Optimisation : réglages (résolution, codec, compression) affichés", await page.locator("#vtOptParams").isVisible() && await page.locator("#optVdCodec").isVisible());
  await page.setInputFiles("#veInput", [V1080]);
  await page.selectOption("#optVdRes", "720");
  check("Optimisation 1 vidéo : « Optimiser la vidéo »", (await txt(page, "#veBtn")) === "Optimiser la vidéo");
  await page.click("#veBtn");
  await page.waitForSelector("#optVdResult", { state: "visible", timeout: 180000 });
  check("Optimisation 1 vidéo (Video Optimizer) : 1080×1920 → 720×1280, réduction affichée",
        (await txt(page, "#optVdFinalRes")) === "720×1280" && (await txt(page, "#optVdReduction")).startsWith("−"));
  await page.setInputFiles("#veInput", [V1, V2]);
  check("Optimisation multiple : « Optimiser les 2 vidéos »", (await txt(page, "#veBtn")) === "Optimiser les 2 vidéos");
  await page.click("#veBtn");
  await page.waitForFunction(id => veLastStatus && veLastStatus.batch_id !== id && veLastStatus.archives_settled && veLastStatus.all_zips_ready, firstBatch, { timeout: 180000 });
  check("Nouveau batch : ancien batch nettoyé automatiquement (sans confirmation)",
        await page.locator("#veModal").isHidden() && (await txt(page, "#veCleanNote")).includes("Ancien batch nettoyé"));
  check("Optimisation multiple : profil d'optimisation appliqué au batch", await page.evaluate(() => veLastStatus.profile.kind === "optimize"));
  check("Optimisation multiple : 2 vidéos traitées par le moteur Batch Video Export", (await txt(page, "#veResultsList")).match(/Terminée/g)?.length === 2);
  await tab(page, "photo");
  check("Photos : Photo Optimizer + métadonnées", await page.locator("#optPhZone").isVisible() && await page.locator("#phZone").isVisible());
  await page.setInputFiles("#optPhInput", PNG); await page.selectOption("#optPhFmt", "webp"); await page.click("#optPhBtn");
  await page.waitForSelector("#optPhResult", { state: "visible", timeout: 60000 });
  check("Photos : Photo Optimizer fonctionnel", (await txt(page, "#optPhReduction")).length > 0);
  await tab(page, "audio");
  check("Audio : conversion audio accessible", await page.locator("#auZone").isVisible());
  await page.setInputFiles("#auInput", WAV); await page.click("#auLaunchBtn");
  await page.waitForFunction(() => /✅/.test(document.getElementById("auResultsList").innerText), null, { timeout: 60000 });
  check("Audio : conversion MP3 réussie", (await txt(page, "#auResultsList")).includes("✅"));
  check("Ancien lien « Plusieurs vidéos » → interface unique", await page.evaluate(() => { switchTab("variation_multi"); return document.getElementById("variationMultiTab").classList.contains("active"); }));

  // ══ REELS STUDIO ════════════════════════════════════════════════════
  const capId = await page.evaluate(async () => (await (await fetch("/reels_caption_create", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "Hook navigateur", segments: [{ text: "PREMIER SEGMENT", start_time: 0, end_time: 1.5 }, { text: "DEUXIÈME", start_time: 1.5, end_time: 3 }] }) })).json()).caption.id);
  const mu = await page.request.post(BASE + "/reels_music_upload", { multipart: { file: { name: "musique.mp3", mimeType: "audio/mpeg", buffer: fs.readFileSync(MP3) } } });
  check("Reels : bibliothèque prête (caption + musique)", capId > 0 && mu.ok());
  const openReels = async () => {
    await tab(page, "reels");
    await page.waitForFunction(() => /Hook navigateur/.test(document.getElementById("rCapList").innerText), null, { timeout: 20000 });
    await page.setInputFiles("#rInputReels", [V1]);
    await page.waitForSelector("#rGalleryReels .ws-card");
    await page.evaluate(() => rQuickAssign("both"));
  };
  await openReels();
  await page.locator("#rGalleryReels .ws-card").first().click();
  await page.waitForSelector("#rPvModal", { state: "visible" });
  await page.waitForFunction(() => { const o = document.getElementById("rPvOverlay"); return o.style.display === "block" && o.naturalWidth > 0; }, null, { timeout: 15000 });
  check("Aperçu rapide : fenêtre dans la page (pas de nouvel onglet)", page.context().pages().length === 1 && await page.locator("#rPvVideo").isVisible());
  check("Aperçu du rendu : calque de caption du moteur réel superposé", await page.locator("#rPvOverlay").isVisible()
        && (await txt(page, "#rPvHint")).includes("moteur de rendu réel"));
  check("Aperçu du rendu : musique assignée jouée avec la vidéo", (await page.evaluate(() => document.getElementById("rPvAudio").getAttribute("src") || "")).includes("/reels_music_file/"));
  const positions = await page.locator("#rPvPos option").count();
  check("Aperçu du rendu : choix de la position du texte", positions >= 1);
  await page.click("#rPvModal .r-modal-head button");
  check("Aperçu : fermeture", await page.locator("#rPvModal").isHidden());
  await page.fill("#rRpName", "Story navigateur");
  await page.click(".r-render-opts >> text=💾 Enregistrer");
  await page.waitForFunction(() => /enregistré/.test(document.getElementById("rRpMsg").innerText));
  check("Préréglage : création", true);
  await page.reload(); await openReels();
  check("Préréglage : persistant après rechargement", await page.locator('#rRpSelect option', { hasText: "Story navigateur" }).count() === 1);
  await page.selectOption("#rRpSelect", { label: "Story navigateur" });
  await page.click(".r-render-opts >> text=📂 Charger");
  check("Préréglage : chargement", (await txt(page, "#rRpMsg")).includes("appliqué"));
  await page.fill("#rRpName", "Story renommée"); await page.click(".r-render-opts >> text=✏️ Renommer");
  await page.waitForFunction(() => /Renommé/.test(document.getElementById("rRpMsg").innerText));
  check("Préréglage : renommage", await page.locator('#rRpSelect option', { hasText: "Story renommée" }).count() === 1);
  page.once("dialog", d => d.accept());
  await page.locator(".r-render-opts button.ws-danger").click();
  await page.waitForFunction(() => /supprimé/.test(document.getElementById("rRpMsg").innerText));
  check("Préréglage : suppression", await page.locator('#rRpSelect option', { hasText: "Story renommée" }).count() === 0);
  await page.click("#rGenBtn");
  await page.waitForSelector("#rReview .ws-card", { timeout: 180000 });
  await page.locator("#rReview .ws-card video").first().click();
  await page.waitForSelector("#rPvModal", { state: "visible" });
  check("Reels : rendu final généré puis ouvert dans l'aperçu", (await txt(page, "#rPvTitle")).startsWith("Rendu final"));
  await page.click("#rPvModal .r-modal-head button");
  await page.context().close();

  // ══ MOBILE ══════════════════════════════════════════════════════════
  const m = await newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
    userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1" });
  for (const t of ["optimizer", "photo", "audio", "variation"]) {
    await tab(m, t); await sleep(150);
    const w = await m.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    check(`Mobile 390 px : pas de débordement horizontal dans la mise en page (${t})`, w[0] <= w[1] + 1, JSON.stringify(w));
  }
  await tab(m, "optimizer");
  await m.locator('label:has(input[name="vtMode"][value="optimize"])').click();
  // NB : sur téléphone, le cadre global (barre latérale de 218 px, inchangé) réduit la zone de contenu ;
  // on vérifie que les écrans modifiés s'y adaptent (une colonne, pleine largeur de la carte).
  const lay = await m.evaluate(() => {
    const card = document.querySelector("#optimizerTab .card").getBoundingClientRect();
    return { card: card.width, modes: [...document.querySelectorAll("#optimizerTab .vt-mode")].map(e => { const b = e.getBoundingClientRect(); return [b.x, b.y, b.width]; }) };
  });
  check("Mobile : les deux modes s'empilent sur une colonne, pleine largeur de la carte",
        lay.modes[0][0] === lay.modes[1][0] && lay.modes[1][1] > lay.modes[0][1] && lay.modes.every(x => x[2] > lay.card * 0.8), JSON.stringify(lay));
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
