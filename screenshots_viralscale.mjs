#!/usr/bin/env node
/**
 * screenshots_viralscale.mjs — Captures du VRAI site ViralScale (Playwright).
 *
 * Ce script ne crée AUCUNE maquette : il pilote un vrai navigateur Chromium
 * sur ton instance ViralScale réellement lancée, se connecte, ouvre chaque
 * écran modifié et enregistre des PNG du rendu réel.
 *
 * Prérequis :
 *   npm i -D playwright
 *   npx playwright install chromium
 *
 * Lancement (ViralScale doit tourner, ex. http://localhost:10000) :
 *   BASE_URL=http://localhost:10000 \
 *   VS_EMAIL=ton@email VS_PASSWORD=tonmdp \
 *   node screenshots_viralscale.mjs
 *
 * Sorties : ./_shots/*.png
 */
import { chromium } from "playwright";
import fs from "fs";

const BASE = process.env.BASE_URL || "http://localhost:10000";
const EMAIL = process.env.VS_EMAIL || "";
const PASSWORD = process.env.VS_PASSWORD || "";
const OUT = "./_shots";
fs.mkdirSync(OUT, { recursive: true });

const shot = async (page, name) => {
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
  console.log("✓", name);
};

const run = async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

  // 1) Login (best effort — adapte les sélecteurs si besoin)
  await page.goto(BASE, { waitUntil: "networkidle" });
  if (EMAIL && (await page.locator('input[type="email"], input[name="email"]').count())) {
    await page.fill('input[type="email"], input[name="email"]', EMAIL);
    await page.fill('input[type="password"], input[name="password"]', PASSWORD);
    await page.click('button[type="submit"], .primary-btn');
    await page.waitForLoadState("networkidle");
  }

  // Helper : activer un onglet applicatif s'il existe (tabs internes)
  const openTab = async (id) => {
    await page.evaluate((tid) => {
      const el = document.getElementById(tid);
      if (el) el.classList.add("active");
      document.querySelectorAll(".tab-content").forEach((t) => {
        if (t.id !== tid) t.classList.remove("active");
      });
      if (typeof window.showTab === "function") { try { window.showTab(tid.replace(/Tab$/, "")); } catch (e) {} }
    }, id);
    await page.waitForTimeout(400);
  };

  // 2) LOT 5 — Batch : 3 cartes lisibles
  await openTab("batchTab");
  await shot(page, "lot5_batch_settings");

  // 3) LOT 4 — OCR simplifié (Batch + Simple)
  //    Ouvre les paramètres avancés OCR pour prouver Sonnet/Opus cachés par défaut
  await page.evaluate(() => { const d = document.getElementById("ocrAdvB"); if (d) d.open = true; });
  await shot(page, "lot4_ocr_batch_advanced_open");

  // 4) LOT 1 + 2 — Reels Studio + éditeur (onglets, espace max)
  await openTab("reelsTab");
  await shot(page, "reels_studio_home");
  // Ouvre l'éditeur de captions (bouton + / créer)
  const addBtn = page.locator("#rCapAddBtn");
  if (await addBtn.count()) {
    await addBtn.first().click();
    await page.waitForTimeout(500);
    await shot(page, "lot1_editor_open_maxspace");      // colonne Position repliée
    // Onglet Musiques instantané
    await page.click("#rEditTabMusic");
    await page.waitForTimeout(300);
    await shot(page, "lot2_editor_music_tab");
    await page.click("#rEditTabCaptions");
    // Ré-affiche la colonne Position
    await page.click("#rCapPosToggle");
    await page.waitForTimeout(300);
    await shot(page, "lot1_editor_position_shown");
    // LOT 3 — ouvre le picker emoji si présent
    const emojiBtn = page.locator('[onclick*="rEmojiOpen"], .r-emoji-open, #rEmojiBtn');
    if (await emojiBtn.count()) {
      await emojiBtn.first().click();
      await page.waitForTimeout(400);
      await shot(page, "lot3_emoji_picker");
    }
  }

  await browser.close();
  console.log("\nCaptures dans", OUT);
};

run().catch((e) => { console.error(e); process.exit(1); });
