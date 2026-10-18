#!/usr/bin/env node
/**
 * test_vexport_cleanup_browser.mjs — Nettoyage automatique du stockage
 * (Batch Video Export) dans un VRAI navigateur (Chromium via Playwright), sur
 * un vrai serveur Gunicorn (options du Dockerfile) avec FFmpeg réel.
 *
 *  - batch terminé : « J'ai sauvegardé mes ZIP » + archives retrouvées après actualisation ;
 *  - ZIP non sauvegardés : confirmation, « Annuler » ne supprime rien,
 *    « Supprimer et continuer » nettoie puis lance le nouveau batch ;
 *  - sauvegarde validée : nouveau batch sans confirmation, « Ancien batch nettoyé » ;
 *  - export actif (autre onglet / appareil) : « Patienter » ou « Annuler
 *    l'ancien export » (arrêt propre, aucun FFmpeg orphelin) ;
 *  - l'autre onglet n'affiche plus l'ancien batch nettoyé ;
 *  - actualisation pendant l'envoi : envoi abandonné nettoyé côté serveur ;
 *  - message d'espace disque insuffisant (réponse 507 simulée : affichage seulement) ;
 *  - mobile 390 px : fenêtre de confirmation lisible, pas de défilement horizontal.
 *
 * Usage :
 *   PYTHON=/chemin/python PLAYWRIGHT_MODULE=/chemin/playwright/index.mjs node test_vexport_cleanup_browser.mjs
 */
import { spawn, execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || "playwright");
const REPO = path.dirname(fileURLToPath(import.meta.url));
const PY = process.env.PYTHON || "python3";
const DATA = fs.mkdtempSync(path.join(os.tmpdir(), "vexport_clean_browser_"));
const EXPORT = path.join(DATA, "videobot_video_exports");
const SRC = fs.mkdtempSync(path.join(os.tmpdir(), "vexport_clean_browser_src_"));
const ADMIN = { email: "admin-cleanb@local.test", password: "admin-cleanb-password" };
const PORT = 55000 + Math.floor(Math.random() * 3000);
const BASE = `http://127.0.0.1:${PORT}`;

let PASS = 0, FAIL = 0;
const check = (name, cond, extra = "") => { if (cond) { PASS++; console.log(`✅ ${name}`); } else { FAIL++; console.log(`❌ ${name}   ${extra}`); } };
const sleep = ms => new Promise(r => setTimeout(r, ms));

const ff = args => execFileSync("ffmpeg", ["-y", "-loglevel", "error", ...args]);
const mkvid = (name, w, h, dur, noise = false) => { const p = path.join(SRC, name);
  ff(["-f", "lavfi", "-i", `testsrc2=s=${w}x${h}:d=${dur}:r=30` + (noise ? ",noise=alls=25:allf=t+u" : ""),
      "-f", "lavfi", "-i", `sine=frequency=440:duration=${dur}`,
      "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", p]); return p; };
const S1 = mkvid("court_a.mp4", 360, 640, 1), S2 = mkvid("court_b.mp4", 640, 360, 1);
const LONG = mkvid("long.mp4", 1080, 1920, 4, true);
const MEDIUM = mkvid("moyen.mp4", 720, 1280, 2, true);

const server = spawn(PY, ["-m", "gunicorn", "app:app", "--bind", `127.0.0.1:${PORT}`, "--workers", "1", "--threads", "4",
  "--timeout", "0", "--keep-alive", "65", "--log-level", "warning"], {
  cwd: REPO, detached: true, stdio: ["ignore", fs.openSync(path.join(DATA, "gunicorn.log"), "w"), fs.openSync(path.join(DATA, "gunicorn.err"), "w")],
  env: { ...process.env, DATA_DIR: DATA, ADMIN_EMAIL: ADMIN.email, ADMIN_PASSWORD: ADMIN.password, SECRET_KEY: "clean-browser-test",
         VEXPORT_FFMPEG_THREADS: "2" } });
const stopServer = () => { try { process.kill(-server.pid, "SIGKILL"); } catch (e) {} };
process.on("exit", stopServer);
for (let i = 0; i < 150; i++) { try { if ((await fetch(BASE + "/login")).ok) break; } catch (e) {} await sleep(100); }

const state = bid => { try { return JSON.parse(fs.readFileSync(path.join(EXPORT, bid, "state.json"), "utf8")); } catch (e) { return null; } };
const files = bid => {
  const out = [], root = path.join(EXPORT, bid);
  const walk = d => { for (const e of fs.readdirSync(d, { withFileTypes: true })) { const p = path.join(d, e.name); e.isDirectory() ? walk(p) : out.push(path.relative(root, p)); } };
  try { walk(root); } catch (e) {}
  return out.sort();
};
const tombOnly = bid => files(bid).every(f => f === "state.json" || f === ".state.lock") && state(bid)?.cleaned === true;
const exportFfmpeg = bid => execFileSync("ps", ["-A", "-o", "command="]).toString().split("\n")
  .filter(l => l.startsWith("ffmpeg ") && l.includes(path.join(EXPORT, bid || "")));

const browser = await chromium.launch();
const jsErrors = [];
async function newPage(opts = {}) {
  const ctx = await browser.newContext({ acceptDownloads: true, viewport: { width: 1366, height: 900 }, ...opts });
  const page = await ctx.newPage();
  page.on("pageerror", e => jsErrors.push(e.message));
  await page.goto(BASE + "/login");
  await page.fill("#email", ADMIN.email); await page.fill("#password", ADMIN.password);
  await Promise.all([page.waitForURL(BASE + "/"), page.click("button[type=submit]")]);
  await page.evaluate(() => switchTab("optimizer"));
  return page;
}
const txt = (page, sel) => page.locator(sel).innerText();
const modalBtn = (page, label) => page.locator("#veModalActions button", { hasText: label });
const waitSettled = (page, notId = null, timeout = 240000) => page.waitForFunction(id =>
  veBatchId && veBatchId !== id && veLastStatus && veLastStatus.batch_id === veBatchId
  && veLastStatus.progress.finished && veLastStatus.archives_settled, notId, { timeout });

try {
  let page = await newPage();

  // ══ Batch terminé + archives retrouvées après actualisation ═════════
  await page.setInputFiles("#veInput", [S1, S2]);
  await page.click("#veBtn");
  await waitSettled(page);
  const b1 = await page.evaluate(() => veBatchId);
  check("Premier batch : aucune confirmation (rien à nettoyer)", await page.locator("#veModal").isHidden());
  check("Batch terminé : bloc « J'ai sauvegardé mes ZIP » affiché", await page.locator("#veSavedBtn").isVisible()
        && (await txt(page, "#veSaveInfo")).includes("Archives téléchargées : 0 / 1"), await txt(page, "#veSaveInfo"));
  await page.reload(); await page.evaluate(() => switchTab("optimizer"));
  await page.waitForFunction(id => veLastStatus && veLastStatus.batch_id === id, b1, { timeout: 15000 });
  check("7. actualisation après traitement : batch et archives retrouvés",
        (await txt(page, "#veArchList")).includes("ZIP 1") && await page.locator("#veArchList a.zip-btn").isVisible());
  const [dl] = await Promise.all([page.waitForEvent("download", { timeout: 30000 }), page.click("#veArchList a.zip-btn")]);
  check("7. ZIP téléchargeable après actualisation", dl.suggestedFilename() === "Batch_001.zip");

  // ══ ZIP non sauvegardés : confirmation, Annuler, puis Supprimer et continuer ══
  const snap1 = files(b1);
  await page.setInputFiles("#veInput", [S1]);
  await page.click("#veBtn");
  await page.waitForSelector("#veModal", { state: "visible", timeout: 15000 });
  check("2. confirmation affichée avant suppression",
        (await txt(page, "#veModalText")) === "Le lancement d'un nouveau batch supprimera les fichiers de votre précédent export. Avez-vous sauvegardé vos ZIP ?",
        await txt(page, "#veModalText"));
  check("2. boutons « Annuler » et « Supprimer et continuer »",
        JSON.stringify(await page.locator("#veModalActions button").allInnerTexts()) === JSON.stringify(["Annuler", "Supprimer et continuer"]));
  check("2. détail de l'ancien export (vidéos, ZIP téléchargés, taille)",
        /2 vidéos exportées · 1 \/ 1 ZIP téléchargé · \d/.test(await txt(page, "#veModalDetails")), await txt(page, "#veModalDetails"));
  await modalBtn(page, "Annuler").click();
  await page.waitForSelector("#veModal", { state: "hidden" });
  await sleep(500);
  check("3. « Annuler » : aucun fichier supprimé", JSON.stringify(files(b1)) === JSON.stringify(snap1) && !state(b1).cleaned);
  check("3. « Annuler » : l'ancien batch reste affiché, bouton réactivé",
        await page.evaluate(id => veBatchId === id, b1) && (await txt(page, "#veArchList")).includes("ZIP 1") && await page.locator("#veBtn").isEnabled());
  await page.click("#veBtn");
  await page.waitForSelector("#veModal", { state: "visible", timeout: 15000 });
  await modalBtn(page, "Supprimer et continuer").click();
  await waitSettled(page, b1);
  const b2 = await page.evaluate(() => veBatchId);
  check("2. « Supprimer et continuer » : ancien batch nettoyé, nouveau batch traité", tombOnly(b1) && b2 !== b1);
  check("Message « Ancien batch nettoyé »", /Ancien batch nettoyé · .+ libérés/.test(await txt(page, "#veCleanNote")), await txt(page, "#veCleanNote"));
  check("Nouveau batch : aucune archive de l'ancien batch affichée", (await page.locator("#veArchList .ve-arch-row").count()) === 1
        && (await txt(page, "#veArchList")).includes("1 / 1"));

  // ══ Validation de sauvegarde (avertissement si un ZIP n'a jamais été téléchargé) ══
  await page.click("#veSavedBtn");
  await page.waitForFunction(() => /jamais téléchargé/.test(document.getElementById("veSaveInfo").innerText), null, { timeout: 15000 });
  check("Validation : prévient qu'un ZIP n'a jamais été téléchargé", (await txt(page, "#veSavedBtn")) === "Confirmer quand même");
  await page.click("#veSavedBtn");
  await page.waitForFunction(() => /Sauvegarde confirmée/.test(document.getElementById("veSaveInfo").innerText), null, { timeout: 15000 });
  check("Validation enregistrée côté serveur", state(b2).saved_confirmed === true && await page.locator("#veSavedBtn").isHidden());

  // ══ Sauvegarde validée → nouveau batch sans confirmation ═══════════
  await page.setInputFiles("#veInput", [LONG, LONG, LONG, LONG]);
  await page.click("#veBtn");
  await page.waitForFunction(id => veBatchId && veBatchId !== id && veLastStatus && veLastStatus.batch_id === veBatchId
    && veLastStatus.progress.processing >= 1, b2, { timeout: 120000 });
  const b3 = await page.evaluate(() => veBatchId);
  check("1. sauvegarde validée : nettoyage automatique, sans confirmation", await page.locator("#veModal").isHidden() && tombOnly(b2)
        && (await txt(page, "#veCleanNote")).includes("Ancien batch nettoyé"));

  // ══ Export actif : autre appareil, Patienter, puis Annuler l'ancien export ══
  const page2 = await newPage();
  await page2.waitForFunction(id => veLastStatus && veLastStatus.batch_id === id, b3, { timeout: 15000 });
  check("6. actualisation / autre appareil pendant le traitement : progression reprise",
        !(await page2.evaluate(() => veLastStatus.progress.finished)) && (await txt(page2, "#veProgressTitle")).includes("Traitement"));
  await page2.setInputFiles("#veInput", [S1]);
  await page2.click("#veBtn");
  await page2.waitForSelector("#veModal", { state: "visible", timeout: 15000 });
  check("4. export actif : « Patienter » ou « Annuler l'ancien export »",
        JSON.stringify(await page2.locator("#veModalActions button").allInnerTexts()) === JSON.stringify(["Patienter", "Annuler l'ancien export"])
        && (await txt(page2, "#veModalText")).includes("ne seront pas supprimés"));
  await modalBtn(page2, "Patienter").click();
  await sleep(800);
  check("4. « Patienter » : rien annulé ni supprimé", !state(b3).cancelled && !state(b3).cleaned && files(b3).some(f => f.startsWith("input/")));
  await page2.click("#veBtn");
  await page2.waitForSelector("#veModal", { state: "visible", timeout: 15000 });
  await modalBtn(page2, "Annuler l'ancien export").click();
  // Vidéos déjà exportées dans l'export annulé → la confirmation de suppression suit.
  const t0 = Date.now();
  let sawConfirm = false;
  while (Date.now() - t0 < 90000) {
    if (await page2.locator("#veModal").isVisible() && await modalBtn(page2, "Supprimer et continuer").count()) {
      sawConfirm = true; await modalBtn(page2, "Supprimer et continuer").click(); break;
    }
    if (await page2.evaluate(id => veBatchId && veBatchId !== id, b3)) break;
    await sleep(200);
  }
  await waitSettled(page2, b3);
  const b4 = await page2.evaluate(() => veBatchId);
  check("4. annulation : export arrêté proprement puis nettoyé", state(b3)?.cancelled === true && tombOnly(b3) && b4 !== b3,
        JSON.stringify({ cancelled: state(b3)?.cancelled, files: files(b3).length, sawConfirm }));
  check("4. aucun FFmpeg orphelin de l'export annulé", exportFfmpeg(b3).length === 0, exportFfmpeg(b3).join("\n"));
  // Premier onglet : son suivi s'est arrêté à l'annulation ; au retour sur
  // l'onglet (événement focus déclenché par le test), l'état est revérifié.
  await page.bringToFront();
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await page.waitForFunction(() => veBatchId === null, null, { timeout: 15000 });
  check("8. premier onglet (retour sur l'onglet) : l'ancien batch nettoyé n'est plus affiché",
        await page.locator("#veProgressWrap").isHidden() && (await txt(page, "#veArchList")) === "");

  // ══ Actualisation après nettoyage : jamais l'ancien batch ══════════
  await page.reload(); await page.evaluate(() => switchTab("optimizer"));
  await page.waitForFunction(id => veLastStatus && veLastStatus.batch_id === id, b4, { timeout: 15000 });
  check("8. actualisation : batch actuel affiché, jamais l'ancien", await page.evaluate(id => veBatchId === id, b4));

  // ══ Actualisation pendant l'envoi : envoi abandonné nettoyé ════════
  await page.setInputFiles("#veInput", Array(40).fill(MEDIUM));
  await page.click("#veBtn");
  await page.waitForSelector("#veModal", { state: "visible", timeout: 15000 });
  await modalBtn(page, "Supprimer et continuer").click();
  await page.waitForFunction(() => { const m = /^(\d+) \//.exec(document.getElementById("veProgressStatus").innerText);
    return document.getElementById("veProgressTitle").innerText.includes("Envoi") && m && +m[1] >= 4; }, null, { timeout: 60000 });
  const b5 = await page.evaluate(() => veBatchId);
  await page.reload(); await page.evaluate(() => switchTab("optimizer"));
  await sleep(2500);
  check("Actualisation pendant l'envoi : envoi abandonné nettoyé côté serveur", tombOnly(b5) && state(b5).clean_auth === "no_output",
        JSON.stringify(state(b5)?.clean_auth));
  check("Actualisation pendant l'envoi : interface vide, prête pour un nouvel import",
        await page.evaluate(() => veBatchId === null) && await page.locator("#veProgressWrap").isHidden());
  check("Ancien batch b4 nettoyé à la confirmation précédente", tombOnly(b4));

  // ══ Espace disque insuffisant (réponse 507 simulée : affichage seulement) ══
  await page.route("**/vexport_create", r => r.fulfill({ status: 507, contentType: "application/json",
    body: JSON.stringify({ code: "disk_full", error: "Espace disque serveur insuffisant pour ce batch : environ 9.0 Go nécessaires pour 500 vidéos, 3.1 Go disponibles après nettoyage de vos anciens exports. Réduis la sélection (≈ 160 vidéos maximum) ou réessaie plus tard." }) }));
  await page.setInputFiles("#veInput", [S1]);
  await page.click("#veBtn");
  await page.waitForSelector("#veErr", { state: "visible", timeout: 15000 });
  check("Message d'espace disque insuffisant affiché (507 simulé)", (await txt(page, "#veErr")).includes("Espace disque serveur insuffisant")
        && await page.locator("#veBtn").isEnabled() && await page.locator("#veModal").isHidden());
  await page.unroute("**/vexport_create");

  // ══ Admin : « Libérer de l'espace » (ancien batch + export actif protégé) ══
  await page.setInputFiles("#veInput", [S1, S2]);
  await page.click("#veBtn");
  await waitSettled(page);
  const bOld = await page.evaluate(() => veBatchId);
  const ck = (await page.context().cookies()).map(x => `${x.name}=${x.value}`).join("; ");
  const api = async (p, fd) => (await fetch(BASE + p, { method: "POST", headers: { Cookie: ck }, body: fd })).json();
  const bAct = (await api("/vexport_create", new FormData())).batch_id;     // export actif (autre onglet / API)
  for (let i = 0; i < 3; i++) { const f = new FormData(); f.append("batch_id", bAct); f.append("file", new Blob([fs.readFileSync(LONG)]), `l${i}.mp4`); await api("/vexport_upload", f); }
  { const f = new FormData(); f.append("batch_id", bAct); await api("/vexport_start", f); }
  const snapAct = files(bAct).filter(f => f.startsWith("input/"));
  check("Admin : bouton « Libérer de l'espace » visible", await page.locator("#veFreeBtn").isVisible());
  await page.click("#veFreeBtn");
  await page.waitForSelector("#veModal", { state: "visible", timeout: 15000 });
  const det = await txt(page, "#veModalDetails");
  check("Libérer : espace libre affiché, ancien batch listé avec sa taille", det.includes("Espace libre") && det.includes("2 / 2 vidéos exportées")
        && await page.locator(`#veModalDetails input[value="${bOld}"]`).count() === 1, det);
  check("Libérer : ZIP non sauvegardés non cochés par défaut", !(await page.locator(`#veModalDetails input[value="${bOld}"]`).isChecked()));
  check("Libérer : export actif protégé, non sélectionnable", det.includes("protégé") && await page.locator(`#veModalDetails input[value="${bAct}"]`).count() === 0, det);
  await page.locator(`#veModalDetails input[value="${bOld}"]`).check();
  await modalBtn(page, "Supprimer la sélection").click();
  await page.waitForFunction(() => document.getElementById("veModalTitle").innerText === "Confirmer la suppression", null, { timeout: 15000 });
  check("Libérer : confirmation explicite (ZIP non sauvegardés signalés)", (await txt(page, "#veModalText")).includes("non confirmés comme sauvegardés"));
  check("Libérer : rien supprimé avant confirmation", !state(bOld).cleaned);
  await modalBtn(page, "Supprimer").click();
  await page.waitForFunction(() => /libérés/.test(document.getElementById("veCleanNote").innerText), null, { timeout: 30000 });
  check("Libérer : batch sélectionné supprimé, espace affiché", tombOnly(bOld) && /libérés · .+ disponibles/.test(await txt(page, "#veCleanNote")), await txt(page, "#veCleanNote"));
  check("Libérer : export actif intact (ni annulé ni supprimé)", !state(bAct).cancelled && !state(bAct).cleaned
        && snapAct.every(f => fs.existsSync(path.join(EXPORT, bAct, f)) || state(bAct).items.some(it => `input/${it.in_name}` === f && it.status !== "pending")));
  check("Libérer : le batch supprimé n'est plus affiché", await page.evaluate(() => veBatchId === null) && await page.locator("#veProgressWrap").isHidden());
  { const f = new FormData(); f.append("batch_id", bAct); await api("/vexport_cancel", f); }

  // ══ Mobile 390 px ═══════════════════════════════════════════════════
  const m = await newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  m.evaluate(() => veAsk("Nouveau batch", "Le lancement d'un nouveau batch supprimera les fichiers de votre précédent export. Avez-vous sauvegardé vos ZIP ?",
    veBatchLine({ created: Date.now() / 1000, done: 500, zips_ready: 5, zips_downloaded: 3, bytes: 3.2e9 }),
    [{ label: "Annuler", value: "no" }, { label: "Supprimer et continuer", value: "yes", primary: true }]));
  await m.waitForSelector("#veModal", { state: "visible" });
  const geo = await m.evaluate(() => { const b = document.querySelector("#veModal .ve-modal-box").getBoundingClientRect();
    const btns = [...document.querySelectorAll("#veModalActions button")].map(x => x.getBoundingClientRect());
    return { left: b.left, right: b.right, vw: innerWidth, sw: document.documentElement.scrollWidth, btnOk: btns.every(r => r.right <= b.right + 1 && r.left >= b.left - 1) }; });
  check("Mobile 390 px : fenêtre de confirmation dans l'écran, boutons visibles", geo.left >= 0 && geo.right <= geo.vw && geo.btnOk, JSON.stringify(geo));
  check("Mobile 390 px : pas de défilement horizontal", geo.sw <= geo.vw, JSON.stringify(geo));
  await modalBtn(m, "Annuler").click();

  check("Aucune erreur JavaScript de page", jsErrors.length === 0, jsErrors.join(" | "));
} catch (e) {
  check("Exécution sans exception", false, e.stack || String(e));
} finally {
  await browser.close();
  stopServer();
  await sleep(300);
  check("Aucun FFmpeg d'export résiduel", exportFfmpeg().length === 0, exportFfmpeg().join("\n"));
  fs.rmSync(SRC, { recursive: true, force: true });
  console.log(`\n${"=".repeat(40)}\n${PASS} tests OK / ${FAIL} KO`);
  process.exit(FAIL ? 1 : 0);
}
