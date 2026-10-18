#!/usr/bin/env node
/**
 * test_video_export_ui.mjs — Logique front du Batch Video Export V2
 * (« Archives disponibles » + « Tout télécharger »), exécutée sur le VRAI
 * code de templates/index.html avec un DOM simulé (aucune dépendance).
 *
 * Vérifie : bouton désactivé pendant le traitement, lignes d'archives
 * (En attente / En cours / Terminé, « 99 vidéos exportées — 1 erreur »),
 * boutons Télécharger seulement pour les ZIP prêts, téléchargements espacés
 * sur ordinateur avec message de repli, archive indisponible signalée,
 * mode guidé sur mobile (1 appui = 1 ZIP), ZIP unique direct.
 *
 * Usage : node test_video_export_ui.mjs
 */
import { readFileSync } from "node:fs";
import vm from "node:vm";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(here, "templates/index.html"), "utf8");
const start = html.indexOf("// ═══════════ BATCH VIDEO EXPORT");
const end = html.indexOf("// ═══════════ WORKSPACE BATCH", start);
if (start < 0 || end < 0) { console.error("Section JS introuvable"); process.exit(1); }
const code = html.slice(start, end);

let PASS = 0, FAIL = 0;
const check = (name, cond, extra = "") => {
  if (cond) { PASS++; console.log(`✅ ${name}`); } else { FAIL++; console.log(`❌ ${name}   ${extra}`); }
};

function makeEnv(ua) {
  const els = {};
  const el = id => (els[id] ||= { id, style: {}, disabled: false, textContent: "", innerHTML: "", title: "",
                                  addEventListener() {}, remove() {} });
  const clicks = [], heads = [], delays = [];
  const headStatus = {};
  const ctx = {
    console, Object, Math, JSON, Promise, Array, encodeURIComponent,
    navigator: { userAgent: ua, maxTouchPoints: /iPhone|Android/.test(ua) ? 5 : 0 },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    escapeHtml: s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]),
    setTimeout: (fn, ms) => { delays.push(ms); fn(); return 0; },   // temps simulé (on enregistre les délais)
    document: {
      getElementById: el,
      createElement: () => ({ style: {}, click() { clicks.push({ href: this.href, download: this.download }); }, remove() {} }),
      body: { appendChild() {} },
    },
    fetch: async (url, opts = {}) => {
      if ((opts.method || "GET") === "HEAD") { heads.push(url); const s = headStatus[url] || 200; return { ok: s === 200, status: s, redirected: false }; }
      return { ok: true, status: 200, redirected: false, headers: { get: () => "application/json" }, json: async () => ({}) };
    },
  };
  vm.createContext(ctx);
  vm.runInContext(code + "\n;globalThis.__api = { veApplyStatus, veDownloadAll, get q() { return veDlQueue; } };", ctx);
  return { ctx, els, el, clicks, heads, delays, headStatus, api: ctx.__api };
}

const zip = (n, status, extra = {}) => ({ status, name: `Batch_${String(n).padStart(3, "0")}.zip`,
  url: status === "ready" ? `/vexport_group_zip/abc/${n}` : "", size: 1, count: 1, message: "", ...extra });
const group = (n, total, done, error, processing, zst, zextra) => ({ n, first: (n - 1) * 100 + 1, last: (n - 1) * 100 + total,
  total, done, error, processing, final: done + error === total, zip: zip(n, zst, zextra) });
const statusPayload = (groups, extra = {}) => ({
  batch_id: "abc", started: true, paused: false, waiting_other: false, retryable: 0, disk_low: false,
  progress: { total: 500, done: 247, error: 0, processing: 1, pending: 252, finished: false, percent: 49.4 },
  items: [], groups, archives_settled: false, all_zips_ready: false, ...extra,
});

// ── Pendant le traitement (exemple 247 / 500) ─────────────────────────
{
  const E = makeEnv("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Chrome/130");
  E.api.veApplyStatus(statusPayload([
    group(1, 100, 100, 0, 0, "ready"), group(2, 100, 99, 1, 0, "ready"),
    group(3, 100, 47, 0, 1, "waiting"), group(4, 100, 0, 0, 0, "waiting"), group(5, 100, 0, 0, 0, "waiting"),
  ]));
  const list = E.el("veArchList").innerHTML;
  check("Section « Archives disponibles » affichée", E.el("veArchives").style.display === "block");
  check("5 lignes ZIP 1 → ZIP 5", [1, 2, 3, 4, 5].every(n => list.includes(`<b>ZIP ${n}</b>`)));
  check("ZIP 1 : 100 / 100 · Terminé", /ZIP 1<\/b>[^]*?100 \/ 100[^]*?Terminé/.test(list));
  check("ZIP 2 : « 99 vidéos exportées — 1 erreur »", list.includes("99 vidéos exportées — 1 erreur"));
  check("ZIP 3 : 47 / 100 · En cours", /ZIP 3<\/b>[^]*?47 \/ 100[^]*?En cours/.test(list));
  check("ZIP 4 et 5 : En attente", /ZIP 4<\/b>[^]*?En attente/.test(list) && /ZIP 5<\/b>[^]*?En attente/.test(list));
  check("Boutons Télécharger uniquement pour les ZIP prêts",
        (list.match(/⬇️ Télécharger/g) || []).length === 2 && list.includes("/vexport_group_zip/abc/1")
        && list.includes("/vexport_group_zip/abc/2") && !list.includes("/vexport_group_zip/abc/3"));
  check("« Tout télécharger » désactivé pendant le traitement", E.el("veZipBtn").disabled === true);
  await E.api.veDownloadAll();
  check("Clic sur « Tout télécharger » désactivé : aucun téléchargement", E.clicks.length === 0);
}

const settled = () => statusPayload([1, 2, 3, 4, 5].map(n => group(n, 100, 100, 0, 0, "ready")),
  { archives_settled: true, all_zips_ready: true, progress: { total: 500, done: 500, error: 0, processing: 0, pending: 0, finished: true, percent: 100 } });

// ── Ordinateur : téléchargements espacés + message de repli ───────────
{
  const E = makeEnv("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Chrome/130");
  E.api.veApplyStatus(settled());
  check("« Tout télécharger » activé quand les 5 ZIP sont prêts", E.el("veZipBtn").disabled === false);
  await E.api.veDownloadAll();
  check("Ordinateur : 5 ZIP existants téléchargés (aucun ZIP géant)",
        E.clicks.length === 5 && E.clicks.every((c, i) => c.href === `/vexport_group_zip/abc/${i + 1}` && c.download === `Batch_00${i + 1}.zip`),
        JSON.stringify(E.clicks));
  check("Ordinateur : disponibilité vérifiée avant chaque téléchargement (HEAD)", E.heads.length === 5);
  check("Ordinateur : téléchargements espacés (1,5 s)", E.delays.filter(d => d === 1500).length === 4, JSON.stringify(E.delays));
  check("Ordinateur : message de repli si le navigateur bloque",
        /autorisation de télécharger plusieurs fichiers/.test(E.el("veAllNote").innerHTML) && E.el("veAllNote").style.display === "block");
  check("Ordinateur : bouton réactivé après la série", E.el("veZipBtn").disabled === false);
}

// ── Ordinateur : une archive indisponible est signalée, les autres partent ──
{
  const E = makeEnv("Mozilla/5.0 (Windows NT 10.0) Firefox/131");
  E.api.veApplyStatus(settled());
  E.headStatus["/vexport_group_zip/abc/3"] = 409;
  await E.api.veDownloadAll();
  check("Archive indisponible : 4 téléchargements lancés, ZIP 3 sauté", E.clicks.length === 4
        && !E.clicks.some(c => c.href.endsWith("/3")));
  check("Archive indisponible : erreur affichée", /Batch_003\.zip indisponible/.test(E.el("veAllNote").innerHTML), E.el("veAllNote").innerHTML);
}

// ── Mobile : mode guidé, 1 appui = 1 archive ──────────────────────────
{
  const E = makeEnv("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1");
  E.api.veApplyStatus(settled());
  await E.api.veDownloadAll();
  check("Mobile : 1er appui → 1 seul téléchargement", E.clicks.length === 1 && E.clicks[0].href.endsWith("/1"));
  check("Mobile : bouton devient « Télécharger ZIP 2 (2 / 5) »", E.el("veZipBtn").textContent.includes("ZIP 2 (2 / 5)"), E.el("veZipBtn").textContent);
  check("Mobile : consigne claire affichée", /qu'un téléchargement par appui/.test(E.el("veAllNote").innerHTML));
  E.api.veApplyStatus(settled());                      // rafraîchissement auto pendant le mode guidé
  check("Mobile : le rafraîchissement n'écrase pas le mode guidé", E.el("veZipBtn").textContent.includes("ZIP 2"));
  for (let i = 0; i < 4; i++) await E.api.veDownloadAll();
  check("Mobile : 5 appuis → 5 archives, dans l'ordre", E.clicks.length === 5 && E.clicks.every((c, i) => c.href.endsWith(`/${i + 1}`)));
  check("Mobile : aucun appel réseau intermédiaire (reste dans le geste utilisateur)", E.heads.length === 0);
  check("Mobile : fin de série → bouton réinitialisé", E.el("veZipBtn").textContent === "📦 Tout télécharger" && E.api.q === null);
}

// ── ZIP unique : téléchargement direct ────────────────────────────────
{
  const E = makeEnv("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1");
  E.api.veApplyStatus(statusPayload([group(1, 2, 2, 0, 0, "ready")], { archives_settled: true, all_zips_ready: true }));
  await E.api.veDownloadAll();
  check("1 seul ZIP : téléchargement direct (pas de mode guidé)", E.clicks.length === 1 && E.api.q === null);
}

// ── États d'archive particuliers ──────────────────────────────────────
{
  const E = makeEnv("Mozilla/5.0 Chrome/130");
  E.api.veApplyStatus(statusPayload([
    group(1, 100, 100, 0, 0, "no_space", { message: "Espace disque serveur insuffisant pour créer l'archive" }),
    group(2, 100, 100, 0, 0, "building"), group(3, 3, 0, 3, 0, "empty"),
  ]));
  const list = E.el("veArchList").innerHTML;
  check("Disque plein : état + message explicites", list.includes("Espace disque insuffisant") && list.includes("insuffisant pour créer l&#39;archive"));
  check("Création en cours : « Création du ZIP… » sans bouton", list.includes("Création du ZIP…") && !list.includes("abc/2"));
  check("Groupe sans réussite : « Aucune vidéo exportée »", list.includes("Aucune vidéo exportée") && list.includes("0 vidéo exportée — 3 erreurs"));
  E.api.veApplyStatus({ batch_id: "abc", started: true, progress: { total: 1, done: 0, error: 0, processing: 1, pending: 0, finished: false }, items: [] });
  check("Ancien format de statut (sans groupes) : pas d'erreur, section masquée", E.el("veArchives").style.display === "none");
}

console.log(`\n${"=".repeat(40)}\n${PASS} tests OK / ${FAIL} KO`);
process.exit(FAIL ? 1 : 0);
