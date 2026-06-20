import os
import gc
import time
import uuid
import json
import random
import shutil
import base64
import zipfile
import secrets
import sqlite3
import subprocess
from pathlib import Path
from functools import wraps
from flask import Flask, render_template, request, send_file, jsonify, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

# ── Access gate: session secret ───────────────────────────────────
# SECRET_KEY signs the login session cookie. If it isn't set in the
# Render environment, fall back to a random key generated at process
# startup — sessions just won't survive a restart/redeploy (users log
# in again, an acceptable trade-off). Setting SECRET_KEY in the Render
# env keeps sessions stable across restarts.
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# ── Team accounts: persistent user store ──────────────────────────
# Replaces the old single shared APP_PASSWORD with real per-person
# accounts (email + password), stored in a small SQLite database on a
# Render persistent disk so accounts survive restarts/redeploys.
#
# DATA_DIR must point at the mount path of that disk (set the DATA_DIR
# env var in Render to match it). Falls back to /tmp if unset — in that
# case accounts would be wiped on every deploy, so DATA_DIR should
# always be configured in production.
#
# Passwords are hashed with werkzeug's PBKDF2 (generate_password_hash)
# — never stored, logged, or hardcoded in plain text anywhere.
DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
USERS_DB_PATH = DATA_DIR / "videobot_users.db"

# Constant-effort dummy hash, checked when an email isn't found, so a
# login attempt against a nonexistent account takes the same time as
# one against a real account — response timing can't be used to
# enumerate which emails have accounts.
_DUMMY_PASSWORD_HASH = generate_password_hash("not-a-real-account-password")


def _users_db():
    conn = sqlite3.connect(USERS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_users_db():
    with _users_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role          TEXT NOT NULL DEFAULT 'member',
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _bootstrap_admin():
    """
    On startup, create the very first admin account from the
    ADMIN_EMAIL / ADMIN_PASSWORD env vars — but ONLY if no admin exists
    yet. Both are read from the environment only, never hardcoded or
    logged. Safe to leave the env vars set permanently afterwards: this
    is a no-op as soon as one admin account exists, so it never resets
    or overwrites a password an admin has since changed.
    """
    email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not email or not password:
        return
    with _users_db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
            return
        conn.execute(
            "INSERT OR IGNORE INTO users (email, password_hash, role, is_active, created_at) "
            "VALUES (?, ?, 'admin', 1, ?)",
            (email, generate_password_hash(password),
             time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())),
        )
        conn.commit()


def get_user_by_id(user_id):
    with _users_db() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_email(email):
    with _users_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE email = ?", (email.strip().lower(),)
        ).fetchone()


def list_users():
    with _users_db() as conn:
        return conn.execute("SELECT * FROM users ORDER BY created_at ASC, id ASC").fetchall()


def create_user(email, password, role="member"):
    with _users_db() as conn:
        conn.execute(
            "INSERT INTO users (email, password_hash, role, is_active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (
                email.strip().lower(),
                generate_password_hash(password),
                role,
                time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            ),
        )
        conn.commit()


def set_user_password(user_id, password):
    with _users_db() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (generate_password_hash(password), user_id),
        )
        conn.commit()


def set_user_active(user_id, active):
    with _users_db() as conn:
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))
        conn.commit()


def set_user_plan_fields(user_id, plan_name=None, monthly_price=None,
                         monthly_generation_limit=None, credits_remaining=None):
    """Additive admin-editing helper for the profitability/plan layer
    (Phase 6). Each argument is optional — only the fields actually
    provided are updated, so the existing 'reset password' / 'disable'
    / 'delete' admin actions remain completely untouched and this can be
    called from a small, separate form. Values are validated/clamped so
    a malformed submission can never corrupt the row or crash the route;
    on any unexpected error the update is simply skipped (never raises)."""
    fields, params = [], []
    try:
        if plan_name is not None:
            plan_name = (plan_name or "").strip().lower()
            if plan_name and plan_name in (set(DEFAULT_PLAN_PRICES) | {"starter", "pro", "agency", "internal", "custom"}):
                fields.append("plan_name = ?")
                params.append(plan_name)
        if monthly_price is not None:
            fields.append("monthly_price = ?")
            params.append(max(0.0, float(monthly_price)))
        if monthly_generation_limit is not None:
            fields.append("monthly_generation_limit = ?")
            params.append(max(0, int(monthly_generation_limit)))
        if credits_remaining is not None:
            fields.append("credits_remaining = ?")
            params.append(max(0, int(credits_remaining)))
        if not fields:
            return
        params.append(user_id)
        with _users_db() as conn:
            conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", params)
            conn.commit()
    except Exception as e:
        print(f"[profitability] WARNING: skipped plan-field update for user_id={user_id}: {e}")


def delete_user(user_id):
    with _users_db() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


_init_users_db()
_bootstrap_admin()

# ── Usage tracking & consumption (additive; does not touch the users/auth
# system above beyond the idempotent plan-column migration). Records one
# row per video-generation ATTEMPT (success or failure), plus per-user
# plan/credit display fields — the foundation for a future SaaS billing
# layer. Lives in the SAME SQLite file as `users` (DATA_DIR/videobot_users.db),
# so it inherits the exact same persistence guarantees: as long as DATA_DIR
# points at the Render persistent disk (required for accounts to survive
# redeploys today), usage_logs survives redeploys/restarts too — same file,
# same disk, zero new infrastructure. All writes are wrapped so a logging
# failure can NEVER break video generation — see log_usage_event().

# Rough, intentionally-simple cost approximations (USD). claude_input_tokens
# / claude_output_tokens are stored as nullable columns specifically so this
# can be replaced later with an exact $/token computation from real Anthropic
# usage data — without any schema change, just a formula change.
EST_CLAUDE_VISION_COST_PER_REQUEST = 0.01    # flat estimate per Claude Vision call
EST_PROCESSING_COST_PER_SECOND     = 0.002   # flat estimate per second of source video

# Admin-dashboard "heavy user" warning thresholds (display-only for now).
CONSUMPTION_HEAVY_GENERATIONS_THRESHOLD     = 500
CONSUMPTION_HEAVY_CLAUDE_REQUESTS_THRESHOLD = 1000
CONSUMPTION_COST_THRESHOLD_USD              = 50.0

# Future subscription plans (display-only — NOT enforced yet, per spec).
PLAN_LIMITS = {"starter": 100, "pro": 500, "agency": 2000}

# ── Profitability layer (additive, display-only — NEVER enforced) ────
# Default monthly price (EUR) used whenever a user doesn't have a custom
# monthly_price set (monthly_price stays at its DEFAULT 0 until an admin
# sets it manually). "custom"/"internal"/unknown plan names fall back to 0
# so internal/test accounts never skew revenue figures.
DEFAULT_PLAN_PRICES = {"starter": 29.0, "pro": 79.0, "agency": 199.0, "internal": 0.0, "custom": 0.0}

# Currency symbol display map — purely cosmetic, falls back to the raw
# currency code for anything not listed (no behavior depends on this).
CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}

# Profitability badge / color thresholds (display-only, per spec).
PROFITABILITY_MARGIN_GREEN_THRESHOLD  = 70.0   # margin_percent >= this → "healthy" (green)
PROFITABILITY_MARGIN_ORANGE_THRESHOLD = 30.0   # margin_percent >= this (and < green) → "low" (orange)
# margin_percent below the orange threshold, or a negative profit, → "bad" (red)


# ══════════════════════════════════════════════════════════════════
# VARIATION MODE — fully isolated, additive feature.
#
# Generates N technically-distinct-but-visually-similar copies of ONE
# uploaded video using FFmpeg only (no Claude Vision, no AI, no text
# detection/rendering of any kind). Everything below is NEW: new
# constants, new directory, new helper functions, new routes. Nothing
# in this section is read or called by /process, /analyze, /batch_*,
# caption rendering, or any existing pipeline — and nothing existing
# is read or called by it either (it never touches render_text_overlay,
# analyze_with_claude_vision*, FONT_*, or any caption/typography code).
# ══════════════════════════════════════════════════════════════════

VARIATION_DIR = Path("/tmp/videobot_variations")
VARIATION_DIR.mkdir(parents=True, exist_ok=True)

MAX_VARIATIONS = 100  # hard server-side cap — mirrors the UI's 10/25/50/100 options

# Randomization ranges per strength preset. Every "±X%" / "±X°" / "AxBx"
# value below is sampled uniformly within its range for each individual
# variation, using a per-(job, index) seeded RNG so runs are reproducible
# and debuggable. Ranges are intentionally conservative — the goal is
# "looks the same, is technically different", never visibly-degraded
# output (per spec's forbidden-transformations list).
VARIATION_STRENGTH_PRESETS = {
    "light": {
        "brightness_pct":  0.02,   # ±2%
        "contrast_pct":    0.03,   # ±3%
        "saturation_pct":  0.03,   # ±3%
        "zoom_range":      (1.00, 1.03),
        "crop_pct":        0.01,   # ±1%
        "rotation_deg":    0.0,    # not used at light strength
        "speed_range":     (0.99, 1.01),
        "volume_pct":      0.02,   # ±2%
        "pitch_pct":       0.005,  # ±0.5%
        "bitrate_pct":     0.05,   # ±5%
        "fps_delta":       2,      # ±2
    },
    "medium": {
        "brightness_pct":  0.05,
        "contrast_pct":    0.08,
        "saturation_pct":  0.08,
        "zoom_range":      (1.00, 1.05),
        "crop_pct":        0.02,
        "rotation_deg":    0.5,
        "speed_range":     (0.99, 1.02),
        "volume_pct":      0.03,
        "pitch_pct":       0.01,
        "bitrate_pct":     0.10,
        "fps_delta":       5,
    },
    "strong": {
        "brightness_pct":  0.10,
        "contrast_pct":    0.15,
        "saturation_pct":  0.15,
        "zoom_range":      (1.00, 1.08),
        "crop_pct":        0.04,
        "rotation_deg":    1.0,
        "speed_range":     (0.98, 1.03),
        "volume_pct":      0.05,
        "pitch_pct":       0.02,
        "bitrate_pct":     0.20,
        "fps_delta":       10,
    },
}
VARIATION_DEFAULT_STRENGTH = "light"  # used whenever the request omits/sends an invalid value

# Realistic device metadata profiles, randomly assigned per variation.
# Deliberately limited to real, currently-common devices — no fictional
# "future" hardware, no GPS fields (per spec's forbidden list).
VARIATION_METADATA_PROFILES = [
    {"label": "iPhone 14 Pro",     "encoder": "HEVC",       "software": "iOS 17.4",    "comment": "Recorded on iPhone 14 Pro"},
    {"label": "iPhone 15 Pro",     "encoder": "HEVC",       "software": "iOS 17.5",    "comment": "Recorded on iPhone 15 Pro"},
    {"label": "iPhone 16 Pro",     "encoder": "HEVC",       "software": "iOS 18.1",    "comment": "Recorded on iPhone 16 Pro"},
    {"label": "Samsung Galaxy S23","encoder": "Lavc60.3",   "software": "Android 14",  "comment": "Galaxy S23 camera"},
    {"label": "Samsung Galaxy S24","encoder": "Lavc60.16",  "software": "Android 14",  "comment": "Galaxy S24 camera"},
    {"label": "Google Pixel 8",    "encoder": "Lavc60.3.100","software": "Android 15", "comment": "Pixel 8 camera"},
]

# ── Advanced Mode (additive, opt-in slider system) ──────────────────
# Each of these 16 sliders runs 0-100 (default 50, "0=disabled,
# 50=normal/recommended, 100=maximum safe variation" per spec). The
# value below is each parameter's "100 = maximum safe" ceiling — every
# one was chosen to sit AT OR INSIDE the existing 'strong' preset's
# proven-safe ranges (see VARIATION_STRENGTH_PRESETS above), and every
# resulting value is still re-clamped to the SAME hard safety bounds
# inside _build_variation_filter_graph regardless of slider input — so
# slider=100 can never produce a visibly-degraded/corrupted result; it
# can only ever reach the same ceiling Preset Mode's "strong" already
# ships safely today. 'gamma' and 'blur' are the two genuinely-new
# dimensions Preset Mode never exposed — both are additive optional
# keys in the params dict that _build_variation_filter_graph only acts
# on when present, so Preset Mode's filter graph is byte-identical to
# before this feature existed.
ADVANCED_PARAM_MAX = {
    "brightness": 0.10,   # ±10%  (matches 'strong')
    "contrast":   0.15,   # ±15%
    "saturation": 0.15,   # ±15%
    "gamma":      0.10,   # ±10%  (NEW — independent of brightness)
    "noise":      6.0,    # absolute 'noise=alls=' strength (clamp ceiling is 10)
    "sharpness":  0.4,    # 'unsharp' amount (clamp ceiling is 0.5)
    "blur":       1.2,    # NEW — 'gblur=sigma=' (kept low: stays visually clean)
    "zoom":       0.08,   # scale-up factor range, e.g. 1.00..1.08
    "crop":       0.04,   # ±4% fractional re-frame
    "rotation":   1.0,    # ±1°
    "speed":      0.03,   # ±3% video/audio speed
    "volume":     0.05,   # ±5% audio gain
    "pitch":      0.02,   # ±2% audio pitch
    "fps":        10,     # ±10 fps nudge
    "bitrate":    0.20,   # ±20% target bitrate
}
# Order mirrors the UI's grouped layout (Visual / Motion / Audio /
# Encoding / Metadata) — used to validate/sanitize saved presets so a
# stored config_json can never contain unexpected keys.
ADVANCED_SLIDER_KEYS = [
    "brightness", "contrast", "saturation", "gamma", "noise", "sharpness",
    "blur", "zoom", "crop", "rotation",
    "speed",
    "volume", "pitch",
    "fps", "bitrate",
    "metadata",
]
ADVANCED_SLIDER_DEFAULT = 50

# Same FFmpeg-only cost model the rest of the app already uses for
# pure-processing work (no Claude Vision is ever called by this mode,
# so claude_requests stays 0 and estimated_claude_vision_cost stays 0).
EST_VARIATION_COST_PER_SECOND_PER_VARIANT = EST_PROCESSING_COST_PER_SECOND


def _init_usage_db():
    """Idempotent schema creation for usage_logs — identical
    CREATE TABLE IF NOT EXISTS / same connection pattern as
    _init_users_db, in the SAME database file."""
    with _users_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_logs (
                id                               INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id                          INTEGER NOT NULL,
                user_email                       TEXT NOT NULL,
                timestamp                        TEXT NOT NULL,
                mode                             TEXT NOT NULL,
                source_video_duration_seconds    REAL,
                generated_video_duration_seconds REAL,
                source_video_count               INTEGER NOT NULL DEFAULT 0,
                output_video_count               INTEGER NOT NULL DEFAULT 0,
                claude_requests_count            INTEGER NOT NULL DEFAULT 0,
                estimated_cost                   REAL NOT NULL DEFAULT 0,
                estimated_claude_vision_cost     REAL NOT NULL DEFAULT 0,
                estimated_processing_cost        REAL NOT NULL DEFAULT 0,
                claude_input_tokens              INTEGER,
                claude_output_tokens             INTEGER,
                generation_success               INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_logs_user_id ON usage_logs(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_logs_timestamp ON usage_logs(timestamp)")
        conn.commit()


def _migrate_user_plan_columns():
    """Idempotent ADD COLUMN migration for the future credit system.
    Reads PRAGMA table_info first, so re-running on a DB that already
    has these columns (e.g. every redeploy) is a safe no-op — never
    crashes, never duplicates, never overwrites existing values."""
    with _users_db() as conn:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "plan_name" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN plan_name TEXT NOT NULL DEFAULT 'starter'")
        if "credits_used" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN credits_used INTEGER NOT NULL DEFAULT 0")
        if "credits_remaining" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN credits_remaining INTEGER NOT NULL DEFAULT 100")
        if "monthly_generation_limit" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN monthly_generation_limit INTEGER NOT NULL DEFAULT 100")
        # Profitability layer columns (additive — see DEFAULT_PLAN_PRICES below).
        # monthly_price stays 0 until an admin sets a custom value; until then,
        # the effective price falls back to the plan's default (see
        # get_effective_monthly_price()) — so existing rows need no backfill.
        if "monthly_price" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN monthly_price REAL NOT NULL DEFAULT 0")
        if "currency" not in existing:
            conn.execute("ALTER TABLE users ADD COLUMN currency TEXT NOT NULL DEFAULT 'EUR'")
        conn.commit()


def _migrate_usage_logs_variation_columns():
    """Idempotent ADD COLUMN migration for Variation Mode's two extra
    history fields. Both are NULLABLE with no DEFAULT, so every existing
    row (Simple/Batch — any mode other than 'variation') simply gets NULL
    and is completely unaffected: no backfill, no behavior change, no
    impact on any existing query (including SELECT * and the aggregate
    queries in _usage_aggregate, which never reference these columns).
    Same PRAGMA-check-first pattern as _migrate_user_plan_columns, so
    re-running on every redeploy is a safe no-op."""
    with _users_db() as conn:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(usage_logs)").fetchall()}
        if "variation_strength" not in existing:
            conn.execute("ALTER TABLE usage_logs ADD COLUMN variation_strength TEXT")
        if "source_filename" not in existing:
            conn.execute("ALTER TABLE usage_logs ADD COLUMN source_filename TEXT")
        conn.commit()


def _migrate_variation_presets_table():
    """Idempotent CREATE TABLE IF NOT EXISTS for Advanced Mode's saved
    slider configurations — a brand-new, fully isolated table in the
    same DB file (same _users_db() connection pattern as every other
    table here). Nothing in users, usage_logs, or any existing query
    references this table, and nothing here references them beyond a
    plain user_id integer (same loose-coupling pattern usage_logs.user_id
    already uses) — so this can never affect Simple/Batch/auth/teams.
    Re-running on every redeploy is a safe no-op."""
    with _users_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS variation_presets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL,
                name        TEXT NOT NULL,
                config_json TEXT NOT NULL,
                created_at  TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_variation_presets_user_id ON variation_presets(user_id)")
        conn.commit()


# ══════════════════════════════════════════════════════════════════
# OCR CACHE (Chantier 1) — cache the caption-detection result per B-file
# content so the same video B is never re-analyzed by Claude Vision.
#
# Global by content (not user-scoped): same bytes ⇒ same captions. Keyed
# by (sha256(file), OCR_CACHE_VERSION) so bumping the version invalidates
# everything when the detection engine changes. Stores ONLY non-empty
# Vision results — never empty, never Tesseract. Purely additive: it
# changes nothing about detection, captions format, rendering, or A+B→C —
# only whether the (identical) detection result is recomputed or reused.
# ══════════════════════════════════════════════════════════════════

OCR_CACHE_VERSION = "v1"   # bump to invalidate all cached OCR when detection changes


def _migrate_ocr_cache_table():
    """Idempotent CREATE TABLE IF NOT EXISTS for the OCR caption cache —
    isolated table in the same DB; nothing else references it. Safe no-op
    on every redeploy."""
    with _users_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ocr_cache (
                hash            TEXT NOT NULL,
                engine_version  TEXT NOT NULL,
                captions_json   TEXT NOT NULL,
                video_b_height  INTEGER,
                mode            TEXT,
                created_at      TEXT NOT NULL,
                PRIMARY KEY (hash, engine_version)
            )
            """
        )
        conn.commit()


def _sha256_file(path: str) -> str:
    """Streaming sha256 of a file's content (1 MB chunks). Raises on I/O
    error so the caller can simply skip caching."""
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _ocr_cache_get(file_hash: str, cache_version: str = None):
    """Return {'lines','video_b_height','mode'} for a cache hit, else None.
    `cache_version` defaults to OCR_CACHE_VERSION ('v1', Rapide); Précis
    passes 'v1-precise' so the two modes never share cache entries.
    Never raises; a non-list/empty cached value is treated as a miss."""
    cv = cache_version or OCR_CACHE_VERSION
    try:
        with _users_db() as conn:
            row = conn.execute(
                "SELECT captions_json, video_b_height, mode FROM ocr_cache "
                "WHERE hash = ? AND engine_version = ?",
                (file_hash, cv),
            ).fetchone()
        if not row:
            return None
        lines = json.loads(row["captions_json"])
        if not isinstance(lines, list) or not lines:
            return None
        return {"lines": lines, "video_b_height": row["video_b_height"], "mode": row["mode"]}
    except Exception:
        return None


def _ocr_cache_put(file_hash: str, lines: list, video_b_height, mode: str, cache_version: str = None):
    """Store ONLY a non-empty list (caller guarantees it's a Vision result).
    `cache_version` defaults to OCR_CACHE_VERSION ('v1', Rapide); Précis
    passes 'v1-precise'. Never raises — a cache write must never break detection."""
    cv = cache_version or OCR_CACHE_VERSION
    try:
        if not file_hash or not isinstance(lines, list) or not lines:
            return
        with _users_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ocr_cache "
                "(hash, engine_version, captions_json, video_b_height, mode, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (file_hash, cv,
                 json.dumps(lines, ensure_ascii=False),
                 int(video_b_height or 0), mode,
                 time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())),
            )
            conn.commit()
    except Exception as e:
        import sys
        print(f"[ocr_cache] WARNING: store failed: {e}", file=sys.stderr)


def _get_video_duration_seconds(path):
    """Read-only helper: a video's duration in seconds via ffprobe, or
    None on any failure. Mirrors the exact ffprobe invocation already
    used by extract_frames (same flags) — does not call or modify it."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-print_format", "json", str(path)],
            capture_output=True, text=True, timeout=15
        )
        return round(float(json.loads(r.stdout)["format"]["duration"]), 2)
    except Exception:
        return None


def log_usage_event(user, mode, source_seconds=None, output_seconds=None,
                     source_count=0, output_count=0, success=False,
                     claude_requests=None, claude_input_tokens=None, claude_output_tokens=None):
    """
    Records exactly ONE row per generation attempt — success AND failure —
    in usage_logs. Called from /process and /batch_render right before
    every return path so no attempt is ever missed.

    claude_requests / claude_input_tokens / claude_output_tokens are
    intentionally-simple APPROXIMATIONS for now: the actual Claude Vision
    call happens earlier, in the separate /analyze and /batch_detect
    detection routes (untouched by this feature — caption detection must
    not change), so an exact request/token count can't yet be attributed
    to a specific generation event. When claude_requests is left as None
    it defaults to 1 if ANTHROPIC_API_KEY is configured (this generation's
    captions almost certainly originated from a Vision call) else 0; token
    counts stay NULL. The columns are nullable by design — wiring in exact
    per-event attribution later is a formula change, never a migration.

    NEVER raises. Any failure is caught, printed to stderr, and swallowed —
    usage tracking must never be able to break video generation.
    """
    try:
        if claude_requests is None:
            claude_requests = 1 if os.environ.get("ANTHROPIC_API_KEY") else 0

        vision_cost     = EST_CLAUDE_VISION_COST_PER_REQUEST * max(0, int(claude_requests))
        processing_cost = EST_PROCESSING_COST_PER_SECOND * max(0.0, float(source_seconds or 0.0))
        total_cost      = vision_cost + processing_cost

        with _users_db() as conn:
            conn.execute(
                """
                INSERT INTO usage_logs (
                    user_id, user_email, timestamp, mode,
                    source_video_duration_seconds, generated_video_duration_seconds,
                    source_video_count, output_video_count, claude_requests_count,
                    estimated_cost, estimated_claude_vision_cost, estimated_processing_cost,
                    claude_input_tokens, claude_output_tokens, generation_success
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"], user["email"],
                    time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
                    mode,
                    source_seconds, output_seconds,
                    int(source_count), int(output_count), int(claude_requests),
                    round(total_cost, 6), round(vision_cost, 6), round(processing_cost, 6),
                    claude_input_tokens, claude_output_tokens,
                    1 if success else 0,
                ),
            )
            conn.commit()
    except Exception as e:
        try:
            email = user["email"] if user is not None else "?"
        except Exception:
            email = "?"
        print(f"[usage_logs] WARNING: failed to record usage event (user={email}, mode={mode}, success={success}): {e}")


def _usage_period_cutoff(period):
    """Returns a UTC timestamp string in the exact 'YYYY-MM-DD HH:MM:SS UTC'
    format usage_logs.timestamp is stored in, so a plain string comparison
    (timestamp >= cutoff) correctly filters by period — no SQL date
    functions or timezone conversions needed at query time."""
    now = time.time()
    if period == "today":
        return time.strftime("%Y-%m-%d 00:00:00 UTC", time.gmtime(now))
    if period == "7d":
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now - 7 * 86400))
    if period == "30d":
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now - 30 * 86400))
    if period == "month":
        return time.strftime("%Y-%m-01 00:00:00 UTC", time.gmtime(now))
    return "0000-00-00 00:00:00 UTC"  # "all time" — before any real timestamp


def _usage_aggregate(where_sql, params):
    """One aggregate row (totals + sums) over usage_logs for a given WHERE
    clause — the single query shape every summary in both dashboards is
    built from, so every number on every page is computed identically."""
    with _users_db() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*)                                                             AS total,
                COALESCE(SUM(CASE WHEN generation_success = 1 THEN 1 ELSE 0 END), 0) AS successes,
                COALESCE(SUM(CASE WHEN generation_success = 0 THEN 1 ELSE 0 END), 0) AS failures,
                COALESCE(SUM(generated_video_duration_seconds), 0.0)                 AS total_seconds,
                COALESCE(SUM(estimated_cost), 0.0)                                   AS total_cost,
                COALESCE(SUM(source_video_count), 0)                                 AS total_source_videos,
                COALESCE(SUM(output_video_count), 0)                                 AS total_output_videos,
                COALESCE(SUM(claude_requests_count), 0)                              AS total_claude_requests
            FROM usage_logs WHERE {where_sql}
            """,
            params,
        ).fetchone()
    return dict(row) if row else {
        "total": 0, "successes": 0, "failures": 0, "total_seconds": 0.0, "total_cost": 0.0,
        "total_source_videos": 0, "total_output_videos": 0, "total_claude_requests": 0,
    }


def get_user_usage_summary(user_id):
    """All-time + today/7d/month aggregates for ONE user — powers
    /consumption. Every query is scoped WHERE user_id = ?, so a user can
    only ever see their own numbers (same per-user separation pattern the
    rest of the app already relies on)."""
    out = {"all_time": _usage_aggregate("user_id = ?", (user_id,))}
    for key, period in (("today", "today"), ("week", "7d"), ("month", "month")):
        out[key] = _usage_aggregate("user_id = ? AND timestamp >= ?", (user_id, _usage_period_cutoff(period)))
    return out


def get_global_usage_summary():
    """Platform-wide aggregates for the admin dashboard — all-time plus
    today / last 7 days / last 30 days breakdowns."""
    out = {"all_time": _usage_aggregate("1=1", ())}
    for key, period in (("today", "today"), ("last_7d", "7d"), ("last_30d", "30d")):
        out[key] = _usage_aggregate("timestamp >= ?", (_usage_period_cutoff(period),))
    return out


def get_usage_by_mode(scope="user", user_id=None):
    """
    All-time per-mode breakdown ('simple' / 'batch' / 'variation' / any
    future mode value found in the table) — purely additive read-only
    aggregation reusing the exact same _usage_aggregate() shape every
    other summary on both dashboards is built from, just grouped by
    `mode` instead of by time window.

    scope="user"   → only that user's rows  (powers /consumption)
    scope="global" → every user's rows      (powers /admin/consumption)

    Returns an ordered list of {"mode": ..., **aggregate} dicts, modes
    sorted by total descending so the busiest mode appears first. Modes
    with zero rows for this scope are simply absent — nothing to show.
    """
    if scope == "user":
        where_sql, base_params = "user_id = ?", (user_id,)
    else:
        where_sql, base_params = "1=1", ()

    with _users_db() as conn:
        modes = [r["mode"] for r in conn.execute(
            f"SELECT DISTINCT mode FROM usage_logs WHERE {where_sql} AND mode IS NOT NULL",
            base_params,
        ).fetchall()]

    rows = []
    for mode in modes:
        agg = _usage_aggregate(f"{where_sql} AND mode = ?", (*base_params, mode))
        agg["mode"] = mode
        rows.append(agg)
    rows.sort(key=lambda r: r["total"], reverse=True)
    return rows


def list_user_usage_table(sort_by="last_activity"):
    """One row per user with aggregate usage + plan/credit info, for the
    admin per-user consumption table. LEFT JOIN so users with zero
    generations still appear (with all-zero stats) — important for
    answering 'which users are inactive?'."""
    sort_columns = {
        "videos":        "videos_generated DESC",
        "cost":          "total_cost DESC",
        "last_activity": "last_activity DESC",
    }
    order_sql = sort_columns.get(sort_by, sort_columns["last_activity"])
    with _users_db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                u.id, u.email, u.role, u.plan_name, u.credits_used,
                u.credits_remaining, u.monthly_generation_limit,
                u.monthly_price, u.currency,
                COUNT(l.id)                                                            AS videos_generated,
                COALESCE(SUM(CASE WHEN l.generation_success = 1 THEN 1 ELSE 0 END), 0) AS successes,
                COALESCE(SUM(CASE WHEN l.generation_success = 0 THEN 1 ELSE 0 END), 0) AS failures,
                COALESCE(SUM(l.source_video_count), 0)                                 AS source_videos,
                COALESCE(SUM(l.output_video_count), 0)                                 AS output_videos,
                COALESCE(SUM(l.claude_requests_count), 0)                              AS claude_requests,
                COALESCE(SUM(l.generated_video_duration_seconds), 0.0)                 AS total_seconds,
                COALESCE(SUM(l.estimated_cost), 0.0)                                   AS total_cost,
                MAX(l.timestamp)                                                       AS last_activity
            FROM users u
            LEFT JOIN usage_logs l ON l.user_id = u.id
            GROUP BY u.id
            ORDER BY {order_sql}, u.created_at ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_activity(user_id, limit=6):
    """Most recent individual usage_logs rows for ONE user — read-only,
    scoped WHERE user_id = ? exactly like every other per-user query in
    this file (get_user_usage_summary, get_usage_by_mode). Purely exposes
    rows that already exist in usage_logs (same table the dashboards above
    already aggregate from) so the main dashboard can show a real recent-
    activity feed instead of an empty card. No new processing — just the
    same _usage_aggregate-style SELECT, unaggregated and limited."""
    with _users_db() as conn:
        rows = conn.execute(
            """
            SELECT mode, timestamp, output_video_count, source_video_count,
                   generation_success, estimated_cost, source_filename
            FROM usage_logs
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_daily_generation_counts(days=14):
    """Per-day generation counts (success vs. failure) over the last N
    days — feeds the lightweight inline-SVG bar chart on the admin
    dashboard. Days with zero activity are zero-filled so the x-axis is
    always continuous and never misleadingly compressed."""
    cutoff_day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (days - 1) * 86400))
    with _users_db() as conn:
        rows = conn.execute(
            """
            SELECT substr(timestamp, 1, 10) AS day,
                   COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN generation_success = 1 THEN 1 ELSE 0 END), 0) AS successes
            FROM usage_logs
            WHERE substr(timestamp, 1, 10) >= ?
            GROUP BY day ORDER BY day ASC
            """,
            (cutoff_day,),
        ).fetchall()
    by_day = {r["day"]: (r["total"], r["successes"]) for r in rows}
    out = []
    for i in range(days):
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time() - (days - 1 - i) * 86400))
        total, successes = by_day.get(day, (0, 0))
        out.append({"day": day, "total": total, "successes": successes})
    return out


# ── Profitability calculations (additive, display-only, NEVER raises) ──
# Pure functions over numbers already computed by the usage-aggregation
# layer above — they don't touch usage_logs, generation, or any existing
# query. Every function is defensive: missing/odd inputs degrade to safe
# defaults (0 cost, "N/A" margin) rather than ever raising or dividing by
# zero, so a malformed user row can never crash either dashboard.

def get_effective_monthly_price(user):
    """The price used for profitability math: a manually-set monthly_price
    above 0 always wins (lets the admin override per spec); otherwise fall
    back to the default price for the user's plan_name. Unknown plan names
    (e.g. 'custom' with no manual price, or anything unrecognized) default
    to 0 — shown to the user as 'Internal account / no billing'."""
    try:
        manual = float(user["monthly_price"] or 0)
    except Exception:
        manual = 0.0
    if manual > 0:
        return round(manual, 2)
    try:
        plan = (user["plan_name"] or "starter").strip().lower()
    except Exception:
        plan = "starter"
    return float(DEFAULT_PLAN_PRICES.get(plan, 0.0))


def get_user_currency(user):
    """The display currency for a user — defaults to EUR (matches the
    column's DEFAULT) and never raises on a malformed/missing value."""
    try:
        cur = (user["currency"] or "EUR").strip().upper()
        return cur if cur else "EUR"
    except Exception:
        return "EUR"


def compute_profitability(cost, monthly_revenue):
    """Pure, side-effect-free profitability math for ONE cost figure
    against ONE monthly revenue figure. Returns a plain dict:
        revenue, cost, profit, margin_percent (float, or None == 'N/A')
    Division-by-zero is explicitly handled per spec: a 0 monthly_revenue
    yields margin_percent = None (rendered as 'N/A'), never an error."""
    try:
        cost = max(0.0, float(cost or 0.0))
    except Exception:
        cost = 0.0
    try:
        revenue = max(0.0, float(monthly_revenue or 0.0))
    except Exception:
        revenue = 0.0
    profit = revenue - cost
    margin = (profit / revenue * 100.0) if revenue > 0 else None
    return {
        "revenue": round(revenue, 2),
        "cost": round(cost, 6),
        "profit": round(profit, 6),
        "margin_percent": (round(margin, 1) if margin is not None else None),
    }


def profitability_status(p):
    """Maps a compute_profitability() result to a small set of display
    states the templates use for color-coding and warning badges, per the
    thresholds in the spec:
        'good'  — margin >= 70%        (green)
        'warn'  — 30% <= margin < 70%  (orange)
        'bad'   — margin < 30% OR profit < 0   (red)
        'na'    — monthly_revenue is 0 (no billing — neutral, not an error)
    Never raises — any malformed input degrades to 'na'."""
    try:
        if p.get("revenue", 0) <= 0 or p.get("margin_percent") is None:
            return "na"
        if p.get("profit", 0) < 0:
            return "bad"
        m = p["margin_percent"]
        if m >= PROFITABILITY_MARGIN_GREEN_THRESHOLD:
            return "good"
        if m >= PROFITABILITY_MARGIN_ORANGE_THRESHOLD:
            return "warn"
        return "bad"
    except Exception:
        return "na"


def get_user_profitability_by_period(user, period_summary):
    """Builds the per-period profitability view for ONE user, reusing the
    exact same period buckets the usage dashboards already compute
    (all_time/today/week|last_7d/month|last_30d) — no new period logic.
    monthly_revenue is the user's fixed monthly price; it is intentionally
    NOT prorated per period (a 7-day cost is compared against the same
    monthly budget the user is paying, which is what 'am I still
    profitable this month so far' actually means for a small business)."""
    revenue = get_effective_monthly_price(user)
    out = {}
    for period_key, period_data in (period_summary or {}).items():
        cost = (period_data or {}).get("total_cost", 0.0)
        out[period_key] = compute_profitability(cost, revenue)
    return out


def get_platform_profitability_summary(enriched_user_rows):
    """Aggregates platform-wide revenue/cost/profit/avg-margin from a list
    of per-user rows that already carry a 'profitability' dict (all-time).
    total_revenue/total_cost/total_profit are plain sums; avg_margin is the
    mean of per-user margins for users who actually generate revenue
    (revenue > 0) — accounts with no billing don't drag the average down
    to 'N/A' nor get counted as 0% margin, since they aren't priced plans."""
    revenues, costs, margins = [], [], []
    for row in enriched_user_rows or []:
        p = row.get("profitability") or {}
        revenues.append(p.get("revenue", 0.0))
        costs.append(p.get("cost", 0.0))
        if p.get("margin_percent") is not None:
            margins.append(p["margin_percent"])
    total_revenue = round(sum(revenues), 2)
    total_cost = round(sum(costs), 6)
    total_profit = round(total_revenue - total_cost, 6)
    avg_margin = (round(sum(margins) / len(margins), 1) if margins else None)
    return {
        "total_revenue": total_revenue,
        "total_cost": total_cost,
        "total_profit": total_profit,
        "avg_margin_percent": avg_margin,
    }


_init_usage_db()
_migrate_user_plan_columns()
_migrate_usage_logs_variation_columns()
_migrate_variation_presets_table()
_migrate_ocr_cache_table()

UPLOAD_DIR = Path("/tmp/videobot_jobs")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── Batch-mode-only: A×B combination matrix ───────────────────────
# Lets the user upload several "source" (A) videos and several
# "target" (B) videos, then renders every A×B combination. Each
# unique file is staged to disk ONCE (via /batch_stage) and reused
# across every combination it appears in — instead of re-uploading
# the same A or B file up to 10x, which would multiply bandwidth and
# disk usage for a 10×10 matrix. Captions are likewise detected ONCE
# per B video (via /batch_detect) and reused for every A it's paired
# with. /batch_render then renders one combination at a time, reusing
# the exact same detection/rendering functions as /analyze and
# /process — only the orchestration and output naming are new.
BATCH_DIR = Path("/tmp/videobot_batches")
BATCH_DIR.mkdir(parents=True, exist_ok=True)

MAX_BATCH_FILES  = 50   # max source (A) videos AND max target (B) videos (MATRIX mode)
MAX_BATCH_COMBOS = 300  # hard cap on A × B outputs (A × B ≤ 300) (MATRIX mode)

# ── Batch Pairing / Shuffle (additive, NOT a matrix) ──────────────
# Pairs each A with a single B; outputs = min(#A, #B), capped at 300.
# Never A×B. Separate, higher per-axis cap than MATRIX (50) so up to 300
# files per side can be staged. The render pipeline (A+B→C) is reused
# unchanged; only staging limit + the render guard become pairing-aware.
MAX_BATCH_PAIR_FILES   = 300  # max A and max B videos in Pairing mode
MAX_BATCH_PAIR_OUTPUTS = 300  # hard cap on Pairing outputs = min(#A,#B) ≤ 300


def _cleanup_stale_batches(max_age_hours: float = 3.0):
    """
    Opportunistic disk-space safety net: remove batch directories from
    earlier sessions that were never explicitly cleaned up (e.g. the
    user closed the tab before downloading the ZIP). Runs only when a
    brand-new batch is being created, so it costs nothing on the hot
    path of staging files for an in-progress batch.
    """
    cutoff = time.time() - max_age_hours * 3600
    try:
        for d in BATCH_DIR.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass


# ── Variation Mode helpers (FFmpeg-only — no Vision, no captions) ───
# Self-contained block: nothing here calls, imports from, or mutates
# any caption/Vision/typography code, and nothing in those pipelines
# calls back into this block. Isolation is structural, not just by
# convention — nothing else in the app references VARIATION_DIR or
# any function below.

def _cleanup_stale_variation_jobs(max_age_hours: float = 3.0):
    """Identical safety-net pattern to _cleanup_stale_batches, scoped to
    VARIATION_DIR. Runs only when a brand-new variation job is staged."""
    cutoff = time.time() - max_age_hours * 3600
    try:
        for d in VARIATION_DIR.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass


def _pick_variation_params(strength: str, rng: "random.Random") -> dict:
    """
    Samples ONE concrete set of randomized values within the chosen
    strength preset's ranges (light/medium/strong — see
    VARIATION_STRENGTH_PRESETS). `rng` is a Random instance seeded by
    the caller with (job_id, index) so each variation's parameters are
    deterministic and reproducible across retries/debugging, yet
    different from every other variation in the run.

    Falls back to the 'light' preset for any unknown/missing strength —
    never raises, never returns an empty dict.
    """
    preset = VARIATION_STRENGTH_PRESETS.get(
        (strength or "").strip().lower(), VARIATION_STRENGTH_PRESETS[VARIATION_DEFAULT_STRENGTH]
    )

    def _pm(spread):  # ± spread, e.g. _pm(0.05) → a value in [-0.05, +0.05]
        return rng.uniform(-spread, spread)

    zlo, zhi = preset["zoom_range"]
    slo, shi = preset["speed_range"]

    return {
        "brightness":  _pm(preset["brightness_pct"]),                # additive, e.g. -0.02..+0.02
        "contrast":    1.0 + _pm(preset["contrast_pct"]),            # multiplicative around 1.0
        "saturation":  1.0 + _pm(preset["saturation_pct"]),
        "zoom":        rng.uniform(zlo, zhi),
        "crop_pct":    rng.uniform(0.0, preset["crop_pct"]),         # crop is one-directional (shrink)
        "rotation_deg": _pm(preset["rotation_deg"]) if preset["rotation_deg"] else 0.0,
        "speed":       rng.uniform(slo, shi),
        "volume":      1.0 + _pm(preset["volume_pct"]),
        "pitch":       1.0 + _pm(preset["pitch_pct"]),
        "bitrate_mult": 1.0 + _pm(preset["bitrate_pct"]),
        "fps_delta":   rng.randint(-preset["fps_delta"], preset["fps_delta"]) if preset["fps_delta"] else 0,
        # Light extras — only ever applied subtly, never enough to be
        # visible (kept out of the strength ranges deliberately so they
        # can't compound into visible artifacts at "strong").
        "noise_strength": rng.uniform(1, 4),
        "sharpen_amount": rng.uniform(0.0, 0.3),
    }


def _pick_metadata_profile(rng: "random.Random") -> dict:
    """Randomly selects one realistic device metadata profile. Never
    raises — VARIATION_METADATA_PROFILES is a static non-empty list."""
    return rng.choice(VARIATION_METADATA_PROFILES)


def _pick_advanced_variation_params(config: dict, rng: "random.Random") -> dict:
    """
    Advanced Mode's slider→FFmpeg mapping. Converts the 16 user-set
    slider values (0-100, see ADVANCED_PARAM_MAX) into EXACTLY the same
    params dict shape _pick_variation_params already produces, so
    _build_variation_filter_graph and _build_variation_ffmpeg_cmd need
    NO changes to consume it (only two new optional keys are added —
    'gamma' and 'blur_sigma' — which those functions only act on when
    present; Preset Mode never sets them, so its output is unaffected).

    Mapping per slider, for each parameter's max-safe ceiling C:
        slider 0   -> sampled range is [0, 0]   -> filter fully disabled
        slider 50  -> sampled range is ±0.5×C   -> "normal" (~'medium' preset)
        slider 100 -> sampled range is ±C       -> "maximum safe variation"
                      (matches the proven-safe 'strong' preset ceiling —
                      _build_variation_filter_graph re-clamps every value
                      to the SAME hard bounds either way, so 100 can
                      never produce a visibly-degraded/corrupted result)

    Never raises: missing/non-numeric/out-of-range slider values fall
    back to ADVANCED_SLIDER_DEFAULT (50) before mapping.
    """
    def _norm(key):  # slider value -> 0.0..1.0
        try:
            raw = float(config.get(key, ADVANCED_SLIDER_DEFAULT))
        except Exception:
            raw = ADVANCED_SLIDER_DEFAULT
        return max(0.0, min(100.0, raw)) / 100.0

    def _spread(key):  # ± range scaled by slider position, e.g. rng.uniform(-spread, +spread)
        return rng.uniform(-1.0, 1.0) * (_norm(key) * ADVANCED_PARAM_MAX[key])

    zoom_top   = 1.0 + _norm("zoom") * ADVANCED_PARAM_MAX["zoom"]
    speed_amt  = _norm("speed") * ADVANCED_PARAM_MAX["speed"]
    fps_amt    = int(round(_norm("fps") * ADVANCED_PARAM_MAX["fps"]))

    return {
        "brightness":     _spread("brightness"),                 # additive, e.g. -0.10..+0.10
        "contrast":       1.0 + _spread("contrast"),             # multiplicative around 1.0
        "saturation":     1.0 + _spread("saturation"),
        "gamma":          1.0 + _spread("gamma"),                # NEW — independent of brightness
        "zoom":           rng.uniform(1.0, zoom_top) if zoom_top > 1.0005 else 1.0,
        "crop_pct":       rng.uniform(0.0, _norm("crop") * ADVANCED_PARAM_MAX["crop"]),
        "rotation_deg":   _spread("rotation"),
        "speed":          rng.uniform(1.0 - speed_amt, 1.0 + speed_amt) if speed_amt > 0.0005 else 1.0,
        "volume":         1.0 + _spread("volume"),
        "pitch":          1.0 + _spread("pitch"),
        "bitrate_mult":   1.0 + _spread("bitrate"),
        "fps_delta":      rng.randint(-fps_amt, fps_amt) if fps_amt else 0,
        "noise_strength": _norm("noise") * ADVANCED_PARAM_MAX["noise"],
        "sharpen_amount": _norm("sharpness") * ADVANCED_PARAM_MAX["sharpness"],
        "blur_sigma":     _norm("blur") * ADVANCED_PARAM_MAX["blur"],   # NEW
    }


def _pick_advanced_metadata_profile(level: float, rng: "random.Random") -> dict:
    """
    Maps the 'Metadata Randomization' slider (passed in already-normalized
    as 0.0-1.0) onto device-profile selection. At level 0 every variant in
    the run keeps the same baseline profile (no metadata variation); at
    level 1.0 each variant independently gets a fully random profile —
    identical odds to _pick_metadata_profile's plain rng.choice. In
    between, each variant has `level` probability of being randomized and
    otherwise keeps the baseline. Never raises — the profiles list is a
    static non-empty constant."""
    baseline = VARIATION_METADATA_PROFILES[0]
    level = max(0.0, min(1.0, float(level or 0.0)))
    if level <= 0.0:
        return baseline
    if rng.random() < level:
        return rng.choice(VARIATION_METADATA_PROFILES)
    return baseline


def _build_variation_filter_graph(params: dict, src_w: int, src_h: int):
    """
    Builds the -vf / -af FFmpeg filter strings for ONE variation from an
    already-sampled params dict (see _pick_variation_params). Pure string
    construction — runs nothing, touches no files. Returns (vf, af, out_fps).

    Filter choices map 1:1 onto the spec's "allowed transformations" list
    (brightness/contrast/saturation/gamma/noise/sharpen/blur/zoom/crop/
    rotation/fps/audio gain/pitch — all via standard libavfilter filters
    that ship with any normal FFmpeg build, same as the overlay/aac/x264
    filters the rest of the app already depends on).
    """
    src_w = max(2, int(src_w or 1280))
    src_h = max(2, int(src_h or 720))

    # ── Video filter chain ──
    vf_parts = []

    # Zoom + crop-back-to-source-size: scale up slightly then crop to the
    # original dimensions. This is what changes "what's visible at the
    # edges" by a tiny amount without ever changing the aspect ratio or
    # adding borders (both explicitly forbidden).
    zoom = max(1.0, float(params.get("zoom", 1.0)))
    if zoom > 1.0005:
        zw, zh = int(src_w * zoom) | 1, int(src_h * zoom) | 1
        vf_parts.append(f"scale={zw}:{zh}")
        vf_parts.append(f"crop={src_w}:{src_h}")

    # Independent fractional crop (subtle re-frame), applied after any
    # zoom — also lands back at the original size via the final scale.
    crop_pct = max(0.0, min(0.08, float(params.get("crop_pct", 0.0))))
    if crop_pct > 0.0005:
        vf_parts.append(f"crop=iw*{1 - crop_pct:.4f}:ih*{1 - crop_pct:.4f}")

    rot = float(params.get("rotation_deg", 0.0))
    if abs(rot) > 0.01:
        # rotate fills corners with the edge pixel (no black borders),
        # then we scale back to source size to guarantee identical AR/dims.
        vf_parts.append(f"rotate={rot:.3f}*PI/180:fillcolor=black@0:ow=rotw(iw):oh=roth(ih)")

    # Color: brightness (additive, eq's range is roughly -1..1),
    # contrast & saturation (multiplicative around 1.0), gamma nudged
    # in lockstep with brightness for a natural look.
    brightness = max(-0.15, min(0.15, float(params.get("brightness", 0.0))))
    contrast   = max(0.5,  min(1.5,  float(params.get("contrast", 1.0))))
    saturation = max(0.5,  min(1.5,  float(params.get("saturation", 1.0))))
    # 'gamma' is an OPTIONAL key — only Advanced Mode's
    # _pick_advanced_variation_params ever sets it (independent slider).
    # _pick_variation_params (Preset Mode) never includes this key, so
    # .get(...) returns None and the exact original brightness-derived
    # formula runs — Preset Mode's eq= string is byte-identical to before.
    _gamma_override = params.get("gamma")
    if _gamma_override is not None:
        gamma = max(0.85, min(1.15, float(_gamma_override)))
    else:
        gamma = max(0.85, min(1.15, 1.0 + brightness * 0.3))
    vf_parts.append(f"eq=brightness={brightness:.4f}:contrast={contrast:.4f}:saturation={saturation:.4f}:gamma={gamma:.4f}")

    # Subtle grain/noise — technical fingerprint, imperceptible at low strength.
    noise = max(0.0, min(10.0, float(params.get("noise_strength", 0.0))))
    if noise > 0.05:
        vf_parts.append(f"noise=alls={noise:.2f}:allf=t+u")

    # Gentle sharpen (never blur enough to visibly soften — spec forbids
    # visible degradation; unsharp with a small amount reads as a faint
    # re-encode fingerprint, not a visual change).
    sharpen = max(0.0, min(0.5, float(params.get("sharpen_amount", 0.0))))
    if sharpen > 0.02:
        vf_parts.append(f"unsharp=5:5:{sharpen:.3f}:5:5:0.0")

    # 'blur_sigma' is an OPTIONAL key — only Advanced Mode's Blur slider
    # ever sets it (_pick_variation_params/Preset Mode never does, so
    # .get(...) returns 0.0 and this branch never fires for Preset Mode —
    # its filter chain is unaffected). Capped low (ADVANCED_PARAM_MAX caps
    # the slider's ceiling at 1.2, hard-clamped to 2.0 here too) so even
    # slider=100 reads as a faint softening, never a degraded/blurry video.
    blur_sigma = max(0.0, min(2.0, float(params.get("blur_sigma", 0.0))))
    if blur_sigma > 0.05:
        vf_parts.append(f"gblur=sigma={blur_sigma:.3f}")

    # Speed change (video side) — setpts inversely scales presentation
    # timestamps; audio side is handled in the audio chain with atempo
    # so picture and sound stay in sync.
    speed = max(0.9, min(1.1, float(params.get("speed", 1.0))))
    if abs(speed - 1.0) > 0.0005:
        vf_parts.append(f"setpts=PTS/{speed:.5f}")

    # Always finish by pinning back to the exact source resolution, so
    # zoom/crop/rotate can never change output dimensions or aspect
    # ratio (both explicitly forbidden — "distort aspect ratio",
    # "create black borders").
    vf_parts.append(f"scale={src_w}:{src_h}")

    # Frame-rate nudge, applied last in the chain.
    out_fps = None
    fps_delta = int(params.get("fps_delta", 0))
    if fps_delta:
        out_fps = max(15, 24 + fps_delta)
        vf_parts.append(f"fps={out_fps}")

    vf = ",".join(vf_parts)

    # ── Audio filter chain ──
    af_parts = []

    # Pitch-only shift via the standard asetrate+aresample+atempo trick:
    # asetrate changes both pitch AND speed, so atempo by the inverse
    # factor restores the original tempo, leaving only the pitch shifted.
    # No 'rubberband' filter dependency (not guaranteed to be compiled
    # into every FFmpeg build) — this combo ships with any standard build.
    pitch = max(0.95, min(1.05, float(params.get("pitch", 1.0))))
    if abs(pitch - 1.0) > 0.0005:
        base_rate = 44100
        af_parts.append(f"asetrate={int(base_rate * pitch)}")
        af_parts.append(f"aresample={base_rate}")
        af_parts.append(f"atempo={1.0 / pitch:.5f}")

    # Tempo change to stay in sync with the video-side speed change.
    # atempo only accepts 0.5–2.0 — our speed range is always within
    # that, so a single atempo call is always sufficient.
    if abs(speed - 1.0) > 0.0005:
        af_parts.append(f"atempo={speed:.5f}")

    volume = max(0.85, min(1.15, float(params.get("volume", 1.0))))
    if abs(volume - 1.0) > 0.0005:
        af_parts.append(f"volume={volume:.4f}")

    af = ",".join(af_parts) if af_parts else None

    return vf, af, out_fps


def _build_variation_ffmpeg_cmd(path_in: str, path_out: str, params: dict,
                                profile: dict, src_w: int, src_h: int,
                                src_bitrate_kbps: int = 2500) -> list:
    """
    Assembles the full FFmpeg command for ONE variation: video+audio
    filter graphs (from _build_variation_filter_graph), a randomized
    target bitrate, randomized creation-time/encoder/software/comment
    metadata from the chosen device profile, output pinned to the exact
    source resolution/aspect-ratio. Mirrors the
    subprocess-list-of-args + '-y' + '-loglevel error' style /process
    and /batch_render already use, so it runs through the exact same
    timeout-bounded subprocess.run(...) pattern.
    """
    vf, af, _out_fps = _build_variation_filter_graph(params, src_w, src_h)

    bitrate_mult = max(0.7, min(1.4, float(params.get("bitrate_mult", 1.0))))
    target_kbps  = max(400, int(src_bitrate_kbps * bitrate_mult))

    # Slightly jittered creation_time within the last ~30 days, so a
    # batch of variants doesn't all carry the exact same timestamp.
    jitter_seconds = random.randint(0, 30 * 86400)
    creation_time  = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime(time.time() - jitter_seconds))

    cmd = ["ffmpeg", "-y", "-i", str(path_in)]

    if vf:
        cmd += ["-vf", vf]
    if af:
        cmd += ["-af", af]

    cmd += [
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{target_kbps}k", "-maxrate", f"{int(target_kbps * 1.2)}k", "-bufsize", f"{target_kbps * 2}k",
        "-c:a", "aac", "-b:a", "128k",
        "-metadata", f"creation_time={creation_time}",
        "-metadata", f"encoder={profile.get('encoder', '')}",
    ]
    if profile.get("software"):
        cmd += ["-metadata", f"com.apple.quicktime.software={profile['software']}"]
    cmd += [
        "-metadata", f"title={profile.get('label', '')}",
        "-metadata", f"comment={profile.get('comment', '')}",
        "-movflags", "+faststart",
        "-loglevel", "error",
        str(path_out),
    ]
    return cmd


FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_REG  = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"

# Caption-style font: TikTok/IG captions render in a narrow, modern grotesque
# (TikTok Sans / SF Pro Display / Helvetica Neue family) — visibly NARROWER and
# LIGHTER than Liberation Sans Bold (an Arial-Black-ish metrics clone), which made
# batch-mode captions look too wide/heavy and wrap differently than the source.
# Inter is the closest readily-available open font to that family. It ships here
# as a single variable-weight TTF; we select the SemiBold/Regular instance at runtime.
FONT_CAPTION = str(Path(__file__).parent / "fonts" / "Inter-Variable.ttf")

# Side-by-side comparison against source TikTok captions showed generated text
# still reading ~30% larger/heavier than native captions even after switching to
# Inter. CAPTION_SIZE_SCALE shrinks the vision-estimated font size proportionally
# so batch output matches native TikTok caption proportions instead of looking
# like meme-generator text.
# Final refinement pass: still ~8% larger than native captions at 0.70, so
# nudge the scale down once more (0.70 * 0.92 ≈ 0.644).
# User feedback on video C: detection, wrapping, outline, weight and
# positioning are all good — text just needs to read a bit larger, closer
# to video B. Bumped +15% (0.644 * 1.15 ≈ 0.741).
# Follow-up readability pass: side-by-side B-vs-C comparisons across
# multiple source videos showed the remaining gap is size + contrast, not
# weight (weight stays at 675 — see _load_caption_font). Bumped +8% again
# (0.741 * 1.08 ≈ 0.800). This is purely a font-size multiplier; it does
# not touch font family, weight, stroke, alignment, line spacing or text
# position.
CAPTION_SIZE_SCALE = 0.800

# ── Positioning debug tooling (OFF by default; batch mode only) ───
# CAPTION_VISUAL_DEBUG=1 makes /batch_render save one annotated frame
# from source video B (red box = the DETECTED caption position, mapped
# into B's own pixel space) and one from the rendered output C (green
# box = the position the renderer actually used) per combo, so the two
# can be placed side by side to confirm the caption lands in the same
# relative zone in both. Purely an investigation aid for the "caption
# sometimes appears in the wrong place" issue — it does not change
# detection, rendering, typography or any existing behaviour, and is
# inert unless the env var is explicitly set.
CAPTION_VISUAL_DEBUG = os.environ.get("CAPTION_VISUAL_DEBUG", "") == "1"

# Side-channel populated by render_text_overlay() with one structured
# debug record per caption block it draws (cleared at the start of each
# call). Lets callers that need it (the visual debug snapshot above)
# inspect the exact source-vs-final coordinates without changing
# render_text_overlay's signature or return type — so every existing
# call site keeps working byte-for-byte unchanged.
_caption_debug_log = []

VISION_PROMPT = """These images are frames from the same TikTok/Reel video.

Detect ONLY real overlay CAPTIONS — the meme-style / commentary text the
creator deliberately typed on top of the video to be read as a caption
(e.g. "volume up ❗❗", "that moment when you realize you're wrong",
"\\"we're just friends\\" / also us:").

DO NOT DETECT (these are not captions — never return them as text objects):
- Watermarks, app logos, or brand marks (e.g. a small "NO GLYPH ON" logo
  badge, a TikTok/CapCut/InShot watermark)
- Usernames, handles, or @ tags
- Small VERTICAL text/labels (text rotated sideways or running top-to-bottom)
- Stickers, emoji-stickers, or decorative graphic elements that aren't
  typed caption text
- App UI chrome (progress bars, icons, buttons, timestamps, view counts)
- Any text that is small, faint, in a corner/edge, or clearly decorative
  rather than a deliberate meme-style caption — even if it is technically
  readable, IGNORE it if it isn't a caption a viewer is meant to read as
  the "point" of the overlay.
Also do NOT report text on clothing, objects, or the scene itself.

Return a JSON array. Each visually distinct CAPTION block = one separate object.

For EACH object:
- "text": exact text with ALL emojis. CRITICAL: if the text spans multiple visual lines, use \\n between each line exactly as displayed. Never merge separate visual lines into one.
- "cx_pct": the ACTUAL visual CENTER x of THIS caption's text block, AS YOU SEE IT IN THE IMAGE — measured, as a decimal fraction of frame width (0=left, 1=right, 0.5=center).
- "cy_pct": the ACTUAL visual CENTER y of THIS caption's text block, AS YOU SEE IT IN THE IMAGE — measured, as a decimal fraction of frame height (0=top, 1=bottom).
- "width_pct": width of the text block as fraction of frame width (how wide the text spans, 0.3–0.9)
- "fontsize_pct": font height as fraction of frame height. Typical TikTok captions: 0.030–0.055. Large title text: 0.055–0.075.
- "align": "left" | "center" | "right"
- "bold": true | false
- "color": "white" | "black"

CRITICAL RULES:
1. Blocks at DIFFERENT vertical positions = DIFFERENT JSON objects, even if all centered.
2. Multi-line text = use \\n for EVERY visual line break. Example: "first visual line\\nsecond visual line"
3. fontsize_pct must reflect actual visible font size — do not underestimate. Large text in the frame should be 0.05–0.075.
4. width_pct: estimate how wide the text block is (e.g. 0.75 if it spans 75% of frame width).
5. POSITION = MEASUREMENT, NOT A GUESS. For cx_pct/cy_pct: look at where THIS caption's text block actually starts and ends — its top edge, bottom edge, left edge, and right edge in THIS frame — then report the CENTER of that exact box. Do NOT round to a generic zone like "near the top", "the middle", or "near the bottom". Do NOT estimate based on where captions usually go on TikTok/Reels. Do NOT reuse, infer, or pattern-match a position from a caption's wording, from a similar-looking caption you've processed before, or from any example in this prompt — every video and every caption gets its own fresh measurement of what is actually visible. A caption sitting at chest height with empty space below it must be reported near the frame's vertical center (cy_pct ≈ 0.5–0.6), NOT near the bottom (cy_pct ≈ 0.8+), even if similar-sounding captions are sometimes placed lower elsewhere.
6. CAPTION FILTER: Only return real overlay captions (see definition above). Never return watermarks, logos, usernames, stickers, small vertical labels, app UI, brand marks, tags, or other decorative/non-caption text — not even small ones that are technically legible. When in doubt whether something is a caption or a watermark/sticker/logo, DO NOT include it.
7. EMOJIS — apply this to EVERY emoji you see, not just the examples below: Do not remove, replace, normalize, convert or describe emojis. If an emoji (including symbol-style ones like ❗, ‼️, ✨, 💯) appears on screen as part of or next to caption text, you MUST include it in the returned "text" string, in its exact position, using the exact Unicode character(s) — never as a description, never omitted, never substituted with a different emoji. Examples of correct output:
   - on-screen "volume up ❗❗" → "text": "volume up ❗❗"   (NOT "volume up")
   - on-screen "i was 👉👌ing myself..." → "text": "i was 👉👌ing myself..."   (NOT "i was ing myself...")
   - ❤️, ⭐, 😈, 😴, 🥺, 😂, 😭 must all be returned as the exact Unicode characters shown here.
8. Return ONLY a valid JSON array. No markdown, no explanation.

The example below shows the JSON FORMAT only. Its "text" strings are
generic placeholders that will not match any real video, and its cx_pct/
cy_pct values are arbitrary numbers chosen only to show that different
blocks can sit at different coordinates — they are NOT typical positions
and must NEVER be copied, reused, or treated as a hint about where a real
caption "should" be. Every coordinate you output must come from measuring
the actual frame(s) you were given, every single time.

Example (format illustration only — do not reuse these values):
[
  {"text": "placeholder caption line one\\nplaceholder caption line two", "cx_pct": 0.5, "cy_pct": 0.43, "width_pct": 0.80, "fontsize_pct": 0.048, "align": "center", "bold": true, "color": "white"},
  {"text": "placeholder overlay text ❗❗", "cx_pct": 0.5, "cy_pct": 0.71, "width_pct": 0.65, "fontsize_pct": 0.058, "align": "center", "bold": true, "color": "white"},
  {"text": "placeholder list:\\nfirst entry\\nsecond entry\\nthird entry", "cx_pct": 0.55, "cy_pct": 0.18, "width_pct": 0.50, "fontsize_pct": 0.038, "align": "left", "bold": true, "color": "white"}
]

(Note: a small vertical logo badge like "NO GLYPH ON" floating mid-frame, or
a username/watermark in a corner, would NOT appear in this array — those are
not captions.)"""


# ── Batch-mode-only: timed caption detection ──────────────────────
# Simple mode keeps the single-pass VISION_PROMPT above (one overlay
# rendered for the whole video). Batch mode needs each caption to
# appear/disappear at the right moment, so this prompt asks Claude to
# report which captions are visible in EACH sampled frame individually;
# the per-frame detections are then merged server-side into timed spans
# (see _merge_timed_captions). This file/runtime is never used by the
# simple-mode code path.
VISION_PROMPT_TIMED = """These are {n} frames sampled from the SAME TikTok/Reel video, in chronological order. Each frame's capture time (seconds) is:
{timestamps}

For EACH frame, detect ONLY real overlay CAPTIONS that are VISIBLE IN THAT
SPECIFIC FRAME — the meme-style / commentary text the creator deliberately
typed on top of the video to be read as a caption (e.g. "volume up ❗❗",
"\\"we're just friends\\" / also us:").

DO NOT DETECT (these are not captions — never return them as text objects):
- Watermarks, app logos, or brand marks (e.g. a small "NO GLYPH ON" logo
  badge, a TikTok/CapCut/InShot watermark)
- Usernames, handles, or @ tags
- Small VERTICAL text/labels (text rotated sideways or running top-to-bottom)
- Stickers, emoji-stickers, or decorative graphic elements that aren't
  typed caption text
- App UI chrome (progress bars, icons, buttons, timestamps, view counts)
- Any text that is small, faint, in a corner/edge, or clearly decorative
  rather than a deliberate meme-style caption — even if technically
  readable, IGNORE it if it isn't a caption a viewer is meant to read as
  the "point" of the overlay.
Also do not report text on clothing, objects, or the scene itself.

Return a JSON array. Each object = ONE caption visible in ONE frame:
- "frame_index": the 1-based index of the frame (1 to {n}) this caption is visible in
- "text": exact text with ALL emojis. Use \\n between visual lines exactly as displayed.
- "cx_pct": the ACTUAL visual CENTER x of THIS caption's text block, AS YOU SEE IT IN THIS FRAME — measured, as a decimal fraction of frame width (0=left, 1=right, 0.5=center).
- "cy_pct": the ACTUAL visual CENTER y of THIS caption's text block, AS YOU SEE IT IN THIS FRAME — measured, as a decimal fraction of frame height (0=top, 1=bottom).
- "width_pct": width of the text block as fraction of frame width (0.3-0.9)
- "fontsize_pct": font height as fraction of frame height (typical 0.030-0.055; large titles 0.055-0.075)
- "align": "left" | "center" | "right"
- "bold": true | false
- "color": "white" | "black"

CRITICAL RULES:
1. If the SAME caption is visible across several consecutive frames, output ONE object per frame it appears in (repeat the same text/position/style, just change frame_index). This is how we learn when it appears and disappears.
2. If a caption is replaced by a DIFFERENT caption at the same position, treat them as separate texts with their own frame_index entries.
3. Only report captions that are ACTUALLY visible in that frame — do not guess or carry text into frames where it isn't shown.
4. Multi-line text = use \\n for every visual line break.
5. POSITION = MEASUREMENT, NOT A GUESS. For cx_pct/cy_pct: look at where THIS caption's text block actually starts and ends in THIS frame — its top, bottom, left and right edges — then report the CENTER of that exact box. Do NOT round to a generic zone ("top"/"middle"/"bottom"). Do NOT estimate from where captions usually sit on TikTok/Reels, and do NOT carry over a position from a similar-sounding caption elsewhere — measure fresh, every frame, every caption. A caption sitting at chest height with empty space below it must be reported near the frame's vertical center (cy_pct ≈ 0.5–0.6), NOT near the bottom (cy_pct ≈ 0.8+).
6. CAPTION FILTER: Only return real overlay captions (see definition above). Never return watermarks, logos, usernames, stickers, small vertical labels, app UI, brand marks, tags, or other decorative/non-caption text — not even small ones that are technically legible. When in doubt whether something is a caption or a watermark/sticker/logo, DO NOT include it.
7. EMOJIS — apply this to EVERY emoji you see, not just the examples below: Do not remove, replace, normalize, convert or describe emojis. If an emoji (including symbol-style ones like ❗, ‼️, ✨, 💯) appears on screen as part of or next to caption text, you MUST include it in the returned "text" string, in its exact position, using the exact Unicode character(s) — never as a description, never omitted, never substituted with a different emoji. Examples of correct output:
   - on-screen "volume up ❗❗" → "text": "volume up ❗❗"   (NOT "volume up")
   - on-screen "i was 👉👌ing myself..." → "text": "i was 👉👌ing myself..."   (NOT "i was ing myself...")
   - ❤️, ⭐, 😈, 😴, 🥺, 😂, 😭 must all be returned as the exact Unicode characters shown here.
8. Return ONLY a valid JSON array. No markdown, no explanation."""


# OCR Pro "Précis" — same schema/position rules as VISION_PROMPT_TIMED,
# but the caption filter is RELAXED to recover hard cases: small text,
# low-contrast text, text in corners or at the very bottom of the frame,
# and very short captions visible in only one frame. Watermarks/usernames/
# app-UI/stickers are still excluded to avoid noise. Used only in Précis
# mode (16 frames, 1080p); Rapide keeps VISION_PROMPT_TIMED unchanged.
VISION_PROMPT_TIMED_PRECISE = """These are {n} frames sampled from the SAME TikTok/Reel video, in chronological order. Each frame's capture time (seconds) is:
{timestamps}

For EACH frame, detect ALL real overlay CAPTIONS visible IN THAT SPECIFIC
FRAME — the meme-style / commentary text the creator typed on top of the
video to be read (e.g. "volume up ❗❗", "\\"we're just friends\\" / also us:").

PRECISE MODE — be THOROUGH. Capture captions even when they are:
- SMALL or thin
- LOW-CONTRAST (white-on-light, dark-on-dark, faint)
- in a CORNER or near the EDGES
- at the VERY BOTTOM of the screen
- VERY SHORT or visible in only ONE frame (fast / brief captions)
- partially overlapping the subject
If text reads like a deliberate caption a viewer is meant to read, INCLUDE it.

STILL DO NOT DETECT (noise, never captions):
- Watermarks, app logos, brand marks (TikTok/CapCut/InShot badges)
- Usernames, handles, @ tags
- Stickers / emoji-stickers / decorative graphics that aren't typed text
- App UI chrome (progress bars, icons, buttons, timestamps, view counts)
- Text rotated sideways / running top-to-bottom (vertical labels)
- Text on clothing, objects, or the scene itself

Return a JSON array. Each object = ONE caption visible in ONE frame:
- "frame_index": 1-based index of the frame (1 to {n})
- "text": exact text with ALL emojis. Use \\n between visual lines exactly as displayed.
- "cx_pct": ACTUAL visual CENTER x of THIS caption in THIS frame, fraction of width (0=left, 1=right).
- "cy_pct": ACTUAL visual CENTER y of THIS caption in THIS frame, fraction of height (0=top, 1=bottom).
- "width_pct": width of the text block as fraction of frame width (0.3-0.9)
- "fontsize_pct": font height as fraction of frame height (typical 0.030-0.055; large titles 0.055-0.075; small captions may be 0.020-0.030)
- "align": "left" | "center" | "right"
- "bold": true | false
- "color": "white" | "black"

CRITICAL RULES:
1. If the SAME caption is visible across several consecutive frames, output ONE object per frame it appears in (same text/position/style, change only frame_index).
2. If a caption is replaced by a DIFFERENT caption at the same position, treat them as separate texts.
3. Only report captions ACTUALLY visible in that frame — never guess or carry text into frames where it isn't shown.
4. Multi-line text = use \\n for every visual line break.
5. POSITION = MEASUREMENT, NOT A GUESS. Report the CENTER of the caption's exact box in THIS frame; do not round to a generic zone, do not assume the usual TikTok position. A caption at chest height with empty space below must be near cy_pct ≈ 0.5–0.6, NOT 0.8+.
6. Return ONLY a valid JSON array. No markdown, no explanation."""


# ── Global JSON error handler ─────────────────────────────────────
@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"error": str(e)}), 500

@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": "Fichier trop grand (max 200 MB)"}), 413


# ── Helpers ───────────────────────────────────────────────────────

def get_video_dims(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", path],
            capture_output=True, text=True, timeout=30
        )
        for s in json.loads(r.stdout).get("streams", []):
            if s.get("codec_type") == "video":
                return int(s["width"]), int(s["height"])
    except Exception:
        pass
    return 576, 1024


def extract_frames(video_path: str, count: int = 4, scale: str = "scale=720:-1") -> list:
    """Extract evenly-spaced frames from video. `scale` defaults to the
    current 720-wide downscale (Rapide); OCR Pro Précis passes a 1080p
    scale. Default call is byte-identical to before."""
    # Get duration
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-print_format", "json", video_path],
        capture_output=True, text=True, timeout=15
    )
    try:
        duration = float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        duration = 3.0

    frames = []
    step = max(0.5, duration / (count + 1))
    for i in range(1, count + 1):
        t = min(step * i, duration - 0.1)
        out = f"/tmp/frame_{uuid.uuid4().hex}.png"
        subprocess.run(
            ["ffmpeg", "-ss", str(t), "-i", video_path,
             "-vframes", "1", "-vf", scale, "-y", out],
            capture_output=True, timeout=15
        )
        if Path(out).exists():
            frames.append(out)
    return frames


def analyze_with_claude_vision(frame_paths: list) -> list:
    """Use Claude Vision to detect text blocks with emojis and positions."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return []

    import anthropic

    content = []
    for path in frame_paths[:4]:
        try:
            with open(path, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode()
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": b64}
            })
        except Exception:
            pass

    if not content:
        return []

    content.append({"type": "text", "text": VISION_PROMPT})

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": content}]
    )

    raw = response.content[0].text.strip()
    # Remove markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    # ── Raw Vision debug log ──────────────────────────────────────────
    # Dumps the EXACT JSON string Claude Vision returned (post fence-
    # stripping, pre-parsing, pre-rendering) so caption-filtering and
    # emoji-loss issues can be pinpointed to detection vs. parsing vs.
    # rendering. Always on (cheap, stderr-only) — does not alter `raw`.
    import sys as _sys
    print(f"[VISION_RAW] analyze_with_claude_vision returned {len(raw)} chars: {raw}",
          file=_sys.stderr)

    blocks = json.loads(raw)

    # Normalize: handle both old y_pct/x_pct and new cx_pct/cy_pct schemas
    normalized = []
    for block in blocks:
        text = block.get("text", "").strip()
        if not text:
            continue
        b = dict(block)
        # Support both naming conventions
        if "cx_pct" not in b:
            b["cx_pct"] = b.get("x_pct", 0.5)
        if "cy_pct" not in b:
            b["cy_pct"] = b.get("y_pct", 0.5)
        if "fontsize_pct" not in b:
            # Convert fontsize_b (px in 1280 frame) to fraction
            b["fontsize_pct"] = b.get("fontsize_b", 36) / 1280
        # width_pct defaults to 0.85 if not provided
        if "width_pct" not in b:
            b["width_pct"] = 0.85
        normalized.append(b)

    normalized.sort(key=lambda l: l.get("cy_pct", 0))

    # ── Static-overlay deduplication (last-resort fallback only) ────────────
    # This function is now only reached when analyze_with_claude_vision_timed
    # found nothing (tesseract fallback, API unavailable, or fully static video).
    # In the normal path, timed detection handles same-slot captions with proper
    # start_time/end_time windows. Here, as a safety net for the static path:
    # if multiple blocks still share the same screen slot (cy_pct within 0.10,
    # cx_pct within 0.20), keep only the longest text per slot to avoid
    # character overlap on the single static overlay. Genuinely distinct captions
    # at different vertical positions are unaffected.
    deduped: list = []
    for block in normalized:
        matched = False
        for i, existing in enumerate(deduped):
            if (abs(block.get("cy_pct", 0.5) - existing.get("cy_pct", 0.5)) < 0.10
                    and abs(block.get("cx_pct", 0.5) - existing.get("cx_pct", 0.5)) < 0.20):
                # Same caption slot — keep whichever has the longest text
                # (most informative representative of that time-varying slot).
                if len(block.get("text", "")) > len(existing.get("text", "")):
                    deduped[i] = block
                matched = True
                break
        if not matched:
            deduped.append(block)
    normalized = deduped
    # ──────────────────────────────────────────────────────────────────────

    return normalized


def _emoji_cluster_count(text: str) -> int:
    """
    Batch-merge helper (emoji-preservation fix): counts emoji clusters in
    `text` using the exact same detector as the [EMOJI_DEBUG] log
    (_extract_emojis -> _is_emoji_codepoint / _EMOJI_RANGES below). Used
    only for COMPARISON/SELECTION inside _merge_timed_captions — never
    strips, removes or rewrites any text that reaches the renderer.
    """
    return len(_extract_emojis(text)[0])


def _strip_emoji_for_key(text: str) -> str:
    """
    Batch-merge helper (emoji-preservation fix): returns `text` with emoji
    clusters removed, for use ONLY as a grouping-comparison key in
    _merge_timed_captions — see the note at the `key = (...)` line below
    for why exact-text matching fragments emoji-bearing captions. The
    ORIGINAL text (emojis intact) is always what is stored/rendered;
    this function's output never reaches the caption JSON or the renderer.
    """
    return "".join(ch for ch in text if not _is_emoji_codepoint(ch)).strip()


def _merge_timed_captions(detections: list, frame_times: list, duration: float) -> list:
    """
    Batch-mode-only helper: turn a flat list of per-frame caption
    detections (each tagged with a 1-based frame_index) into timed
    caption spans with start_time/end_time.

    Captions with the same text at roughly the same position that show
    up across consecutive sampled frames are merged into ONE span; the
    boundary times are placed at the midpoint between the last frame
    where the caption was NOT seen and the first/last frame where it WAS
    seen (falling back to 0 / video duration at the clip edges). A small
    gap tolerance (skip at most one sampled frame) absorbs occasional
    missed detections without splitting one caption into several spans.

    Two targeted fixes live here (see inline notes at each site):
      1. Emoji-insensitive grouping key + richest-emoji base selection,
         so "volume up" / "volume up ❗❗" detections of the same on-screen
         caption merge into one span that keeps the emoji.
      2. A same-spot overlap guard after span construction, so two
         different captions detected at the same screen position never
         end up with overlapping [start_time, end_time] windows (which
         would make _build_timed_overlay_cmd render both at once).
    """
    from collections import defaultdict

    groups = defaultdict(list)
    for d in detections:
        text = (d.get("text") or "").strip()
        if not text:
            continue
        # Emoji-preservation fix — grouping key, part 1: compare on the
        # EMOJI-STRIPPED text rather than the exact string. Vision can
        # transcribe the SAME on-screen caption as "volume up" on one
        # sampled frame and "volume up ❗❗" on another (the same kind of
        # frame-to-frame inconsistency proven in the positioning audit to
        # affect text, just applied to emoji glyphs specifically). An
        # exact-text key would put these in two different groups, each
        # producing its own (likely shorter/fragmented) span, and risking
        # the emoji-bearing variant being dropped entirely. Comparing on
        # `_strip_emoji_for_key(text)` merges them into ONE run; which
        # variant's text is actually kept is decided below by emoji count,
        # not by this key — the key only controls what merges together.
        key = (_strip_emoji_for_key(text), round(d.get("cx_pct", 0.5), 1), round(d.get("cy_pct", 0.5), 1))
        groups[key].append(d)

    n_frames = len(frame_times)
    captions = []

    for _, items in groups.items():
        items.sort(key=lambda x: x["frame_index"])

        # Split into consecutive runs, tolerating a 1-frame gap
        runs = [[items[0]]]
        for it in items[1:]:
            if it["frame_index"] - runs[-1][-1]["frame_index"] <= 2:
                runs[-1].append(it)
            else:
                runs.append([it])

        for run in runs:
            first_idx = run[0]["frame_index"]
            last_idx  = run[-1]["frame_index"]
            first_t   = frame_times[first_idx - 1]
            last_t    = frame_times[last_idx - 1]

            start_time = 0.0 if first_idx <= 1 else (frame_times[first_idx - 2] + first_t) / 2.0
            end_time   = duration if last_idx >= n_frames else (last_t + frame_times[last_idx]) / 2.0

            if end_time <= start_time:
                end_time = min(duration, start_time + max(0.5, last_t - first_t + 0.5))

            # Emoji-preservation fix — base selection, part 2: if ANY
            # detection in this run contains emoji clusters, use the
            # richest one (most emoji clusters, per _extract_emojis — the
            # very same counter [EMOJI_DEBUG] already trusts) as the
            # span's base text/style/position, instead of unconditionally
            # `run[len(run)//2]`. This guarantees that if a single sampled
            # frame caught the emoji version, the merged caption keeps it.
            # On a tie, prefer whichever candidate sits closest to the
            # run's middle frame — preserving the exact behavior (and the
            # positioning-audit-proven stability) of the original
            # middle-frame pick for every other field. Runs with NO
            # emoji anywhere fall through to that original pick completely
            # unchanged — byte-for-byte identical to before this change.
            emoji_counts = [_emoji_cluster_count(d.get("text", "")) for d in run]
            max_emojis = max(emoji_counts)
            if max_emojis > 0:
                mid_idx  = len(run) // 2
                richest  = [i for i, c in enumerate(emoji_counts) if c == max_emojis]
                chosen   = min(richest, key=lambda i: abs(i - mid_idx))
                base = dict(run[chosen])
            else:
                base = dict(run[len(run) // 2])

            base.pop("frame_index", None)
            base["start_time"] = round(max(0.0, start_time), 2)
            base["end_time"]   = round(min(duration, end_time), 2)
            captions.append(base)

    # ── Timing fix: same-spot overlap guard ───────────────────────────
    # Each span above derives start_time/end_time independently, from
    # only ITS OWN group's first/last detected frame index. When Vision
    # detects two DIFFERENT captions at essentially the same screen spot
    # across an overlapping range of sampled frames — e.g. both visible
    # on the transition frame between them, the same kind of frame-to-
    # frame detection inconsistency the emoji fix above accounts for —
    # their independently-computed windows can overlap. Because
    # _build_timed_overlay_cmd enables each overlay with its own
    # `between(t,start,end)` and stacks them in a filter chain, an
    # overlap means BOTH render simultaneously: captions visibly stack
    # instead of replacing one another ("rendered all at once").
    #
    # Group the finished spans using the SAME position bucket as the
    # grouping key above, sort each bucket chronologically, and trim each
    # span's end_time to the next same-spot span's start_time. This
    # restores the invariant _build_timed_overlay_cmd's own docstring
    # already assumes ("Captions at the same spot naturally replace one
    # another because only one window is ever active at a given
    # timestamp") — which only actually held when spans never overlapped.
    # Captions at genuinely different screen positions are left
    # completely untouched and may still legitimately appear together.
    by_pos = defaultdict(list)
    for c in captions:
        by_pos[(round(c.get("cx_pct", 0.5), 1), round(c.get("cy_pct", 0.5), 1))].append(c)
    for spans in by_pos.values():
        spans.sort(key=lambda c: c["start_time"])
        for i in range(len(spans) - 1):
            if spans[i]["end_time"] > spans[i + 1]["start_time"]:
                spans[i]["end_time"] = spans[i + 1]["start_time"]
    # Drop any span that the trim above collapsed to zero/negative length
    # (only possible if two same-spot detections reported identical or
    # inverted frame ranges — an edge case, not the common case).
    captions = [c for c in captions if c["end_time"] > c["start_time"]]

    captions.sort(key=lambda c: (c.get("start_time", 0.0), c.get("cy_pct", 0.0)))
    return captions


def analyze_with_claude_vision_timed(video_path: str, precise: bool = False):
    """
    OCR Pro: `precise=False` (default) is the EXACT current path — 8 frames,
    720p, VISION_PROMPT_TIMED, Haiku. `precise=True` (opt-in) uses 16 frames,
    1080p and the relaxed VISION_PROMPT_TIMED_PRECISE (still Haiku — no Sonnet
    in V1). Everything else (merging, schema, fallbacks) is identical.

    Batch-mode-only detection path: samples several frames spread across
    the video's timeline (instead of the simple-mode path's 4 frames
    covering the whole clip), asks Vision which captions are visible in
    EACH sampled frame, then merges consecutive per-frame detections into
    timed caption spans (start_time/end_time) via _merge_timed_captions.

    Returns (captions, duration). Falls back to ([], duration) on any
    failure so callers can fall back to the existing static detection.
    Simple mode never calls this function — analyze_with_claude_vision
    (single-pass, no timing) remains its sole detection path.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return [], 0.0

    import anthropic

    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-print_format", "json", video_path],
        capture_output=True, text=True, timeout=15
    )
    try:
        duration = float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        duration = 3.0
    if duration <= 0:
        duration = 3.0

    # Frame count + resolution depend on the OCR mode. Rapide (default):
    # 8 frames, 720p (byte-identical to before). Précis: 16 frames, 1080p.
    count  = 16 if precise else 8
    _scale = "scale=1080:-2" if precise else "scale=720:-1"
    step  = max(0.35, duration / count)
    frame_times, frame_paths = [], []
    for i in range(count):
        t = min(step * i + step / 2.0, max(0.1, duration - 0.1))
        out = f"/tmp/tframe_{uuid.uuid4().hex}.png"
        subprocess.run(
            ["ffmpeg", "-ss", str(t), "-i", video_path,
             "-vframes", "1", "-vf", _scale, "-y", out],
            capture_output=True, timeout=15
        )
        if Path(out).exists():
            frame_times.append(round(t, 2))
            frame_paths.append(out)

    if not frame_paths:
        return [], duration

    try:
        content = []
        for path in frame_paths:
            try:
                with open(path, "rb") as f:
                    b64 = base64.standard_b64encode(f.read()).decode()
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64}
                })
            except Exception:
                pass

        if not content:
            return [], duration

        timestamps_str = "\n".join(f"frame {i+1}: {t}s" for i, t in enumerate(frame_times))
        _prompt_tpl = VISION_PROMPT_TIMED_PRECISE if precise else VISION_PROMPT_TIMED
        prompt = _prompt_tpl.format(n=len(frame_times), timestamps=timestamps_str)
        content.append({"type": "text", "text": prompt})

        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            messages=[{"role": "user", "content": content}]
        )

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        # ── Raw Vision debug log (see analyze_with_claude_vision for the
        # rationale — same idea, batch/timed path) ───────────────────────
        import sys as _sys
        print(f"[VISION_RAW] analyze_with_claude_vision_timed ({len(frame_paths)} frames) "
              f"returned {len(raw)} chars: {raw}", file=_sys.stderr)

        detections = json.loads(raw)

        n = len(frame_times)
        normalized = []
        for d in detections:
            text = (d.get("text") or "").strip()
            idx  = d.get("frame_index")
            if not text or not isinstance(idx, int) or idx < 1 or idx > n:
                continue
            b = dict(d)
            b["text"]         = text
            b["frame_index"]  = idx
            if "cx_pct" not in b:
                b["cx_pct"] = b.get("x_pct", 0.5)
            if "cy_pct" not in b:
                b["cy_pct"] = b.get("y_pct", 0.5)
            if "fontsize_pct" not in b:
                b["fontsize_pct"] = b.get("fontsize_b", 36) / 1280
            if "width_pct" not in b:
                b["width_pct"] = 0.85
            normalized.append(b)

        if not normalized:
            return [], duration

        return _merge_timed_captions(normalized, frame_times, duration), duration

    except Exception:
        return [], duration
    finally:
        for path in frame_paths:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass


def analyze_with_tesseract_fallback(frame_paths: list) -> list:
    """Fallback OCR using Tesseract when no API key."""
    import numpy as np
    import pytesseract
    from PIL import Image

    if not frame_paths:
        return []

    img  = Image.open(frame_paths[0]).convert("RGB")
    arr  = np.array(img)
    h, w = arr.shape[:2]

    white = (arr[:,:,0] > 175) & (arr[:,:,1] > 175) & (arr[:,:,2] > 175)
    inv   = np.full((h, w), 255, dtype=np.uint8)
    inv[white] = 0
    from PIL import Image as PIL
    pil_inv = PIL.fromarray(inv).resize((w*2, h*2), PIL.NEAREST)

    data = pytesseract.image_to_data(pil_inv, config="--psm 3 --oem 3",
                                      output_type=pytesseract.Output.DICT)
    raw_words = []
    for i in range(len(data['text'])):
        txt = data['text'][i].strip()
        if not txt or int(data['conf'][i]) < 20: continue
        wx, wy = data['left'][i]//2, data['top'][i]//2
        ww, wh = data['width'][i]//2, data['height'][i]//2
        if wh < 4 or ww < 2: continue
        raw_words.append({'text': txt, 'x': wx, 'y': wy, 'w': ww, 'h': wh})

    if not raw_words: return []
    import numpy as np2
    med_h = float(np2.median([wd['h'] for wd in raw_words]))
    words = [wd for wd in raw_words if med_h*0.40 <= wd['h'] <= med_h*2.5]
    if not words: return []
    med_h2 = float(np2.median([wd['h'] for wd in words]))

    words.sort(key=lambda wd: wd['y'])
    groups = [[words[0]]]
    for wd in words[1:]:
        if abs(wd['y'] - groups[-1][-1]['y']) < med_h2 * 0.7:
            groups[-1].append(wd)
        else:
            groups.append([wd])

    lines = []
    for grp in groups:
        grp.sort(key=lambda wd: wd['x'])
        text = " ".join(wd['text'] for wd in grp)
        alpha_r = sum(c.isalpha() for c in text) / max(1, len(text))
        if alpha_r < 0.25 and len(text) < 4: continue
        y_l = min(wd['y'] for wd in grp) / h
        x_l = min(wd['x'] for wd in grp) / w
        font_est = max(10, int(np2.median([wd['h'] for wd in grp]) * 0.90))
        lines.append({"text": text, "y_pct": round(y_l,4),
                      "x_pct": round(max(0,(x_l-5)/w),4),
                      "fontsize_b": font_est, "align": "left", "bold": False})
    lines.sort(key=lambda l: l["y_pct"])
    return lines


# ── Emoji investigation: debug-only detection helper ──────────────
# Pure observation utility — scans a string and reports which Unicode
# codepoints fall in the emoji ranges (grouping adjacent codepoints
# into clusters so combos like "❤️" = U+2764 U+FE0F or ZWJ sequences
# are reported as one emoji rather than split apart). Used ONLY to
# populate the [EMOJI_DEBUG] log below; it never filters, removes or
# rewrites any text — the renderer always receives the original string
# untouched.
_EMOJI_RANGES = (
    (0x2190, 0x21FF),   # arrows
    (0x2300, 0x23FF),   # misc technical (⌚⏰…)
    (0x25A0, 0x25FF),   # geometric shapes
    (0x2600, 0x27BF),   # misc symbols & dingbats (☀️❤️⭐✨…)
    (0x2B00, 0x2BFF),   # misc symbols & arrows (⬆️⭕…)
    (0x2B05, 0x2B07),
    (0x1F000, 0x1FFFF), # all emoji planes (emoticons, pictographs, supplemental, extended-A…)
    (0xFE00, 0xFE0F),   # variation selectors (text/emoji presentation)
    (0x1F1E6, 0x1F1FF), # regional indicators (flag pairs)
    (0x200D, 0x200D),   # zero-width joiner (combines emoji into one glyph, e.g. 👨‍👩‍👧)
)


def _is_emoji_codepoint(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES)


def _extract_emojis(text: str):
    """
    Debug helper only (see [EMOJI_DEBUG] log in render_text_overlay).
    Returns (emoji_clusters, unicode_codes):
      - emoji_clusters: list of emoji strings found, e.g. ["❤️", "👉👌"]
        (adjacent emoji codepoints — including ZWJ/variation-selector
        joins — are grouped into one cluster, matching how they'd
        visually appear as single glyphs)
      - unicode_codes: matching list of "U+XXXX U+YYYY" strings, one
        per cluster, e.g. ["U+2764 U+FE0F", "U+1F449 U+1F44C"]
    Never modifies or filters `text` — purely observational.
    """
    clusters, codes = [], []
    current = ""
    for ch in text:
        if _is_emoji_codepoint(ch):
            current += ch
        else:
            if current:
                clusters.append(current)
                codes.append(" ".join(f"U+{ord(c):04X}" for c in current))
                current = ""
    if current:
        clusters.append(current)
        codes.append(" ".join(f"U+{ord(c):04X}" for c in current))
    return clusters, codes


def _measure_text(font, text: str) -> int:
    """Return rendered pixel width of text."""
    try:
        bb = font.getbbox(text)
        return bb[2] - bb[0]
    except Exception:
        return max(1, len(text)) * getattr(font, "size", 30) // 2


def _wrap_lines(orig_lines: list, font, max_w: int) -> list:
    """
    Word-wrap lines that are wider than max_w.
    Preserves intentional \\n breaks; only adds wraps for overflowing single lines.
    """
    result = []
    for line in orig_lines:
        if _measure_text(font, line) <= max_w:
            result.append(line)
            continue
        words = line.split(" ")
        current = ""
        for word in words:
            candidate = (current + " " + word).strip() if current else word
            if _measure_text(font, candidate) <= max_w:
                current = candidate
            else:
                if current:
                    result.append(current)
                current = word  # single word wider than max_w: accept it
        if current:
            result.append(current)
    return result if result else orig_lines


# ── Font Studio (optional) — candidate font files per choice ───────
# Used ONLY when a non-"default" font is explicitly chosen in the UI.
# Each list is tried in order; the first that loads wins; if NONE load
# (e.g. an optional .ttf the user hasn't supplied), _load_caption_font
# falls straight through to the EXACT current default behaviour. Some
# choices already work out of the box via the fonts the Dockerfile
# installs (Liberation Serif/Sans); others degrade gracefully to the
# default until the matching .ttf is dropped into fonts/. No font files
# are bundled or shared by this code.
_FONT_DIR_FS = Path(__file__).parent / "fonts"
_FONT_STUDIO_CANDIDATES = {
    "tiktok_bold": {
        "regular": [str(_FONT_DIR_FS / "TikTokBold.ttf"), str(_FONT_DIR_FS / "Montserrat-Bold.ttf"), FONT_BOLD],
        "bold":    [str(_FONT_DIR_FS / "TikTokBold.ttf"), str(_FONT_DIR_FS / "Montserrat-Bold.ttf"), FONT_BOLD],
    },
    "clean_sans": {
        "regular": [str(_FONT_DIR_FS / "CleanSans-Regular.ttf"), str(_FONT_DIR_FS / "Roboto-Regular.ttf"), FONT_REG],
        "bold":    [str(_FONT_DIR_FS / "CleanSans-Bold.ttf"), str(_FONT_DIR_FS / "Roboto-Bold.ttf"), FONT_BOLD],
    },
    "rounded": {
        "regular": [str(_FONT_DIR_FS / "Rounded-Regular.ttf"), str(_FONT_DIR_FS / "Nunito-Regular.ttf")],
        "bold":    [str(_FONT_DIR_FS / "Rounded-Bold.ttf"), str(_FONT_DIR_FS / "Nunito-Bold.ttf")],
    },
    "serif": {
        "regular": [str(_FONT_DIR_FS / "Serif-Regular.ttf"), "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"],
        "bold":    [str(_FONT_DIR_FS / "Serif-Bold.ttf"), "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"],
    },
    "handwritten": {
        "regular": [str(_FONT_DIR_FS / "Handwritten.ttf"), str(_FONT_DIR_FS / "Caveat-Regular.ttf")],
        "bold":    [str(_FONT_DIR_FS / "Handwritten.ttf"), str(_FONT_DIR_FS / "Caveat-Bold.ttf")],
    },
}


def _parse_hex_rgba(s):
    """Parse '#RRGGBB' (or 'RRGGBB') into an opaque RGBA tuple. Returns
    None for anything invalid/empty so callers keep their current color."""
    if not s or not isinstance(s, str):
        return None
    h = s.strip().lstrip("#")
    if len(h) == 6:
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255)
        except Exception:
            return None
    return None


def _caption_style_from_request():
    """Read the OPTIONAL Font Studio 'caption_style' JSON from the current
    request form. Returns a non-empty dict, or None when absent/invalid —
    and None means render_text_overlay runs its exact pre-Font-Studio path.
    Used only by /process and /batch_render (Simple + Batch)."""
    try:
        raw = request.form.get("caption_style")
        if not raw:
            return None
        cs = json.loads(raw)
        return cs if isinstance(cs, dict) and cs else None
    except Exception:
        return None


def _load_caption_font(fontsize: int, bold: bool, font_key: str = "default"):
    """
    Load the caption font at the requested pixel size, selecting the
    Bold or Regular weight from the bundled Inter variable font.

    TikTok/IG captions render in a narrow modern grotesque (TikTok Sans /
    SF Pro Display / Helvetica Neue). Liberation Sans (an Arial-Black-ish
    metrics clone) is noticeably wider and heavier, which made batch-mode
    text look bulkier and wrap differently than the source. Inter is the
    closest open-source match to that family, so we prefer it and only
    fall back to Liberation if the bundled font can't be loaded/instanced.
    """
    from PIL import ImageFont

    # Font Studio (optional): a non-"default" choice tries its candidate
    # files first. If none load, we fall straight through to the EXACT
    # current default path below — Inter is never forced, the fallback is
    # never broken.
    if font_key and font_key != "default":
        for cand in _FONT_STUDIO_CANDIDATES.get(font_key, {}).get("bold" if bold else "regular", []):
            try:
                return ImageFont.truetype(cand, fontsize)
            except Exception:
                continue

    try:
        font = ImageFont.truetype(FONT_CAPTION, fontsize)
        try:
            # Variable font: pick the named instance matching the requested weight.
            # Native TikTok/IG captions read closer to SemiBold than full Bold —
            # Bold/ExtraBold instances were part of why generated text looked
            # heavier and more "meme-generator" than the source captions.
            # User feedback after the 625 tuning pass: captions still read too
            # thin for native TikTok/Reels presence on short captions/emoji
            # lists. Bumped to 675 — between SemiBold (600) and Bold (700) —
            # directly on the variable font's continuous wght axis. Size,
            # scale, stroke, spacing, wrapping, positioning and font family
            # are all unchanged.
            font.set_variation_by_axes([675 if bold else 400])
        except Exception:
            try:
                # Fallback for static/non-variable instances: SemiBold (600)
                # is the closest named instance to ~575.
                font.set_variation_by_name("SemiBold" if bold else "Regular")
            except Exception:
                pass  # static instance (e.g. default Regular) — still usable
        return font
    except Exception:
        # Bundled font missing/unreadable — fall back to the prior Liberation fonts
        try:
            return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, fontsize)
        except Exception:
            return ImageFont.load_default()


# ── Emoji investigation — Step 3 finding ──────────────────────────
# Searched the entire pipeline (detection normalization in
# analyze_with_claude_vision[_timed], and the text handling below) for
# anything that could drop emojis before they reach the renderer:
# unicode normalization (unicodedata.normalize), non-ASCII filtering
# (.encode("ascii", ...), regex strips, str.isascii checks), emoji
# libraries that substitute/describe emojis, etc.
# CONFIRMED: no such code exists anywhere in app.py. The only two
# transformations ever applied to a detected caption's text are
# `.strip()` and the `\\n`-split + per-line `.strip()` used to respect
# manual line breaks (see `text` / `orig_lines` below) — neither touches
# emoji codepoints. Nothing to disable; documenting this here so no one
# adds such a step by mistake. (The [EMOJI_DEBUG] log below proves this
# at runtime: raw_text_from_vision == text_sent_to_renderer ==
# text_after_cleanup for any caption containing emojis.)
# ══════════════════════════════════════════════════════════════════
# LOCAL TWEMOJI EMOJI SOURCE — offline, no CDN, no Apple assets.
#
# pilmoji's default (TwitterEmojiSource) fetches each emoji from a CDN at
# render time; when the CDN is unreachable on Render it returns None and
# the renderer falls back to the system Noto Color Emoji font (Android
# look). This local source reads bundled Twemoji 72x72 PNGs from disk
# instead — deterministic, identical on Render and locally, zero network.
# If a PNG is missing (or the assets aren't deployed yet) it returns None,
# so the existing fallback (and Noto as the ultimate net) still applies —
# never a crash. It ONLY affects emoji glyphs; captions WITHOUT emojis are
# byte-identical regardless of the source (pilmoji never queries the source
# for plain text). Graphics: Twemoji, CC-BY 4.0 (see static/emoji/twemoji/
# NOTICE.txt). No Apple emoji, no Apple CDN, no Apple files.
# ══════════════════════════════════════════════════════════════════

_TWEMOJI_DIR  = Path(__file__).parent / "static" / "emoji" / "twemoji" / "72x72"
_LOCAL_TWEMOJI = None              # cached source instance (lazy)
_LOCAL_TWEMOJI_BUILT = False       # whether we've attempted to build it


def _twemoji_codepoints(emoji_str: str) -> str:
    """Map an emoji string to Twemoji's filename codepoints, replicating
    Twemoji's `grabTheRightIcon` rule: if there is NO ZWJ (U+200D), strip
    every variation selector U+FE0F; otherwise keep them. Then join the
    lowercase hex code points with '-'. e.g. 😭→'1f62d', ❤️→'2764',
    👍🏽→'1f44d-1f3fd', 👨‍👩‍👧→'1f468-200d-1f469-200d-1f467'."""
    s = emoji_str
    if "\u200d" not in s:          # no ZWJ -> drop VS16 (U+FE0F)
        s = s.replace("\ufe0f", "")
    return "-".join(f"{ord(ch):x}" for ch in s)


def _get_local_twemoji_source():
    """Return a cached LocalTwemojiSource instance, or None if pilmoji
    isn't importable. Never raises. When None is returned, callers pass
    source=None to Pilmoji, which keeps pilmoji's current default."""
    global _LOCAL_TWEMOJI, _LOCAL_TWEMOJI_BUILT
    if _LOCAL_TWEMOJI_BUILT:
        return _LOCAL_TWEMOJI
    _LOCAL_TWEMOJI_BUILT = True
    import sys as _sys
    _dbg = (os.environ.get("OCR_EMOJI_DEBUG", "") == "1")
    try:
        from io import BytesIO
        from pilmoji.source import BaseSource

        class LocalTwemojiSource(BaseSource):
            """Serves Twemoji 72x72 PNGs from disk; None for any missing
            emoji (→ fallback handles it). No network, never raises.
            Logs: MISS + source/mode always (diagnostic), HIT only when
            OCR_EMOJI_DEBUG=1 (avoids per-emoji spam in prod)."""
            def get_emoji(self, emoji, /):
                try:
                    name = _twemoji_codepoints(emoji) + ".png"
                    p = _TWEMOJI_DIR / name
                    if p.exists():
                        if _dbg:
                            print(f"[TWEMOJI] HIT emoji={emoji!r} file={name}", file=_sys.stderr)
                        return BytesIO(p.read_bytes())
                    print(f"[TWEMOJI] MISS emoji={emoji!r} file={name}", file=_sys.stderr)
                except Exception as e:
                    print(f"[TWEMOJI] MISS emoji={emoji!r} (error: {e})", file=_sys.stderr)
                return None

            def get_discord_emoji(self, id, /):
                return None

        _LOCAL_TWEMOJI = LocalTwemojiSource()
        print(f"[TWEMOJI] source locale construite, dir={_TWEMOJI_DIR}, "
              f"exists={_TWEMOJI_DIR.exists()}", file=_sys.stderr)
    except Exception as e:
        _LOCAL_TWEMOJI = None
        print(f"[TWEMOJI] source locale NON construite (source=None) : {e}", file=_sys.stderr)
    return _LOCAL_TWEMOJI


def render_text_overlay(blocks: list, wa: int, ha: int, wb: int, hb: int, style: dict = None) -> str:
    """
    Render all text objects onto a transparent RGBA image.

    `style` is an OPTIONAL Font Studio override dict (global per render).
    When it is None or every key is the default sentinel, EVERY computed
    value below equals the original hard-coded expression, so the output
    is byte-identical to the pre-Font-Studio behaviour. Only explicitly
    non-default keys change anything.
    Improvements over v1:
    - Word-wrap BEFORE shrinking font (preserves readability)
    - Font size floor: never below max(24px, 60% of target size)
    - Proper left/center/right alignment
    - Stroke width proportional to font size
    - Better vertical clamping
    """
    import sys
    from PIL import Image, ImageDraw, ImageFont

    overlay = Image.new("RGBA", (wa, ha), (0, 0, 0, 0))
    _caption_debug_log.clear()

    # ── Font Studio (optional) — parse overrides once. Every default
    # sentinel maps to the original hard-coded value, so style=None (or
    # all-defaults) ⇒ byte-identical render. ──
    style = style or {}
    _fs_font    = (style.get("font") or "default")
    _fs_sizef   = style.get("size_factor")
    _fs_size    = float(_fs_sizef) if isinstance(_fs_sizef, (int, float)) and _fs_sizef > 0 else 1.0
    _fs_contour = (style.get("contour") or "default")
    _fs_shadow  = (style.get("shadow") or "off")
    _fs_txt     = _parse_hex_rgba(style.get("text_color"))
    _fs_stroke  = _parse_hex_rgba(style.get("stroke_color"))
    _FS_CONTOUR_DIV = {"default": 22, "fin": 30, "moyen": 16, "epais": 11}
    _FS_SHADOW_CFG  = {"legere": (2, 110), "forte": (4, 175)}  # (offset_px_per_unit, alpha)

    use_pilmoji = True
    try:
        from pilmoji import Pilmoji
    except ImportError:
        use_pilmoji = False
    # Local Twemoji source (offline). When unavailable we pass NO source
    # kwarg so Pilmoji keeps its current default — never worse than before
    # (passing source=None would raise inside Pilmoji.__init__).
    _src = _get_local_twemoji_source() if use_pilmoji else None
    _emoji_kw = {"source": _src} if _src is not None else {}
    import sys as _sys_tw
    print(f"[TWEMOJI] mode={'local' if _src is not None else 'fallback'}", file=_sys_tw.stderr)

    for block in blocks:
        text = block.get("text", "").strip()
        if not text:
            continue

        # ── Font size ─────────────────────────────────────────────────
        # Scale down from the vision-estimated size — generated captions were
        # consistently reading larger/heavier than native TikTok captions.
        fontsize_pct   = max(0.022, min(block.get("fontsize_pct", 0.035), 0.08)) * CAPTION_SIZE_SCALE
        target_fontsize = int(ha * fontsize_pct * _fs_size)  # _fs_size=1.0 by default ⇒ unchanged
        fontsize        = max(24, target_fontsize)
        # Floor: never shrink below 60% of the requested size (or 24px)
        min_fontsize    = max(24, int(target_fontsize * 0.60))

        # ── Position ──────────────────────────────────────────────────
        cx = int(wa * block.get("cx_pct", 0.5))
        cy = int(ha * block.get("cy_pct", 0.5))

        # ── Style ─────────────────────────────────────────────────────
        bold      = block.get("bold", False)
        color_str = block.get("color", "white")
        color     = (255, 255, 255, 255) if "white" in color_str.lower() else (0, 0, 0, 255)
        if _fs_txt:
            color = _fs_txt   # Font Studio text-color override (else unchanged)
        align     = block.get("align", "center")

        # ── Max width: use width_pct if provided, else 88% of frame ──
        width_pct = block.get("width_pct", 0.88)
        max_w     = int(wa * min(max(width_pct, 0.30), 0.92))

        # ── Parse lines (respect existing \n) ─────────────────────────
        orig_lines = text.replace("\\n", "\n").split("\n")
        orig_lines = [l.strip() for l in orig_lines if l.strip()]
        if not orig_lines:
            continue

        # ── Phase 1: word-wrap at target font size ────────────────────
        font  = _load_caption_font(fontsize, bold, _fs_font)
        lines = _wrap_lines(orig_lines, font, max_w)

        # ── Phase 2: shrink only if still overflowing, respect floor ──
        for _ in range(30):
            font = _load_caption_font(fontsize, bold, _fs_font)
            max_line_w = max((_measure_text(font, ln) for ln in lines), default=0)
            if max_line_w <= max_w or fontsize <= min_fontsize:
                break
            fontsize = max(min_fontsize, fontsize - 2)

        # ── Layout ────────────────────────────────────────────────────
        # Native TikTok/IG caption outlines read as a thin hairline stroke.
        # The 1/11 ratio was still ~2x thicker than source captions in
        # side-by-side comparison, so we halved it again to ~1/22, then
        # nudged ~10% thinner still to ~1/24.
        # Follow-up readability pass: B-vs-C contrast comparison showed
        # source captions still read with slightly stronger contrast, so
        # we strengthen the stroke ~10% back toward 1/22 (24 -> 22).
        # Weight (675), font family (Inter), color (pure white), position,
        # wrapping and spacing are unaffected — this only thickens the
        # existing outline.
        # Contrast-only follow-up: thickness (border) was already close to
        # native captions, but the outline itself read as slightly faded —
        # the stroke colour was translucent black (alpha 225/255 ≈ 88%),
        # which lets background show through at the edge on bright scenes /
        # skin tones. Bumped alpha to 255 (fully opaque black) so the edge
        # reads as a crisp, solid line — darker edge separation without
        # adding any extra pixels of width. Border math, weight, size,
        # colour, font family, position, wrap and spacing untouched.
        border  = max(1, fontsize // _FS_CONTOUR_DIV.get(_fs_contour, 22))  # //22 by default ⇒ unchanged
        shadow  = _fs_stroke or (0, 0, 0, 255)  # Font Studio stroke-color override (else unchanged)
        # Slightly more breathing room between lines than native captions'
        # tightest spacing — keeps multi-line blocks from reading as a dense
        # slab of text the way the generated output previously did.
        line_h  = int(fontsize * 1.34)
        total_h = len(lines) * line_h
        y_start = cy - total_h // 2

        margin  = border + 4
        # Bottom clamp uses the REAL visual block height (last line's glyphs)
        # instead of total_h, which includes the trailing 1.34 line-spacing
        # pad below the text. That pad was over-reserved at the bottom and
        # pushed low captions upward by tens of px. Centering (y_start above)
        # and the top clamp (max(margin, …)) are unchanged.
        visual_h = (len(lines) - 1) * line_h + fontsize
        y_start = max(margin, min(y_start, ha - visual_h - margin))

        # Pre-compute widths for alignment
        line_widths = [_measure_text(font, ln) for ln in lines]
        block_w     = max(line_widths) if line_widths else max_w

        # ── Draw ──────────────────────────────────────────────────────
        for i, line in enumerate(lines):
            y  = y_start + i * line_h
            tw = line_widths[i]

            if align == "center":
                x = cx - tw // 2
            elif align == "right":
                x = cx + block_w // 2 - tw
            else:  # left
                x = cx - block_w // 2

            x = max(margin, min(x, wa - tw - margin))

            # ── Font Studio (optional) drop shadow — OFF by default, so
            # this block is fully skipped in the default path. When on,
            # draws the line offset behind the stroke/fill in soft black. ──
            if _fs_shadow in _FS_SHADOW_CFG:
                _soff, _salpha = _FS_SHADOW_CFG[_fs_shadow]
                _sdraw = ImageDraw.Draw(overlay)
                _sdraw.text((x + _soff, y + _soff), line, font=font, fill=(0, 0, 0, _salpha))

            # NOTE (emoji-loss fix): the fallback used to flip the shared
            # `use_pilmoji` flag to False on ANY exception, which silently
            # disabled emoji-aware rendering for every remaining line AND
            # every remaining caption in the whole video — so a single
            # transient failure (e.g. one emoji image fetch timing out)
            # would make unrelated emojis later in the video "disappear"
            # too. The fallback is now scoped to THIS LINE only
            # (`line_rendered_with_emoji`); `use_pilmoji` still gates
            # whether pilmoji is even attempted (e.g. if the import
            # failed) but a per-line failure no longer poisons the rest
            # of the render. Drawing itself — position, font, border,
            # shadow, fill — is byte-for-byte identical either way.
            line_rendered_with_emoji = False
            if use_pilmoji:
                try:
                    with Pilmoji(overlay, **_emoji_kw) as pm:
                        for dx in range(-border, border + 1):
                            for dy in range(-border, border + 1):
                                if abs(dx) + abs(dy) <= border + 1 and (dx or dy):
                                    pm.text((x+dx, y+dy), line, font=font, fill=shadow)
                        pm.text((x, y), line, font=font, fill=color)
                    line_rendered_with_emoji = True
                except Exception as _pilmoji_exc:
                    print(f"[EMOJI_DEBUG] pilmoji failed for line {i} of '{text[:40]}': "
                          f"{type(_pilmoji_exc).__name__}: {_pilmoji_exc} — "
                          f"falling back to plain text for THIS LINE only",
                          file=sys.stderr)

            if not line_rendered_with_emoji:
                draw = ImageDraw.Draw(overlay)
                for dx in range(-border, border + 1):
                    for dy in range(-border, border + 1):
                        if abs(dx) + abs(dy) <= border + 1 and (dx or dy):
                            draw.text((x+dx, y+dy), line, font=font, fill=shadow)
                draw.text((x, y), line, font=font, fill=color)

        # ── Emoji investigation debug log (per caption object) ────────
        # Traces the text end-to-end through every transformation this
        # pipeline actually applies, so emoji loss can be pinpointed to
        # one of: Vision output, JSON parsing, this cleanup step, or the
        # renderer/font itself:
        #   raw_text_from_vision  — block["text"] exactly as Vision/JSON
        #                           produced it (before .strip()).
        #   text_sent_to_renderer — `text` after .strip(), the value this
        #                           function actually works with.
        #   text_after_cleanup    — the \\n-split + per-line-trim result
        #                           (`orig_lines`, the ONLY normalization
        #                           this pipeline performs) re-joined.
        # If raw == sent == cleaned but the emoji is still missing from
        # the rendered frame, the loss is in the renderer/font fallback,
        # not detection or text handling. This block never modifies the
        # text — purely observational, mirrors [CAPTION_DEBUG]'s pattern.
        _raw_text_fv   = block.get("text", "")
        _cleaned_text  = "\n".join(orig_lines)
        _emojis, _codes = _extract_emojis(_raw_text_fv)
        emoji_debug_obj = {
            "raw_text_from_vision":  _raw_text_fv[:200],
            "emojis_detected":       _emojis,
            "emoji_unicode_codes":   _codes,
            "text_sent_to_renderer": text[:200],
            "text_after_cleanup":    _cleaned_text[:200],
        }
        print(f"[EMOJI_DEBUG] {json.dumps(emoji_debug_obj, ensure_ascii=False)}", file=sys.stderr)

        # ── Debug log ─────────────────────────────────────────────────
        print(
            f"[RENDER] '{text[:50]}' | cy={block.get('cy_pct',0):.2f} cx={block.get('cx_pct',0):.2f}"
            f" | fontsize_pct={fontsize_pct:.3f} → {fontsize}px (min={min_fontsize})"
            f" | lines={len(lines)} align={align} width_pct={width_pct:.2f}",
            file=sys.stderr
        )

        # ── Structured positioning debug log (per caption object) ─────
        # Maps the SAME detected percentages onto the SOURCE frame
        # (wb×hb — the resolution Vision actually saw when it produced
        # cx_pct/cy_pct/width_pct) and reports the FINAL pixel box this
        # function drew onto the wa×ha overlay canvas (which becomes
        # video C's canvas, since the overlay is composited onto A at
        # 0:0 with no scaling). Comparing source_* vs final_* — once
        # both are expressed back as percentages of their own frame —
        # is exactly how to confirm whether the renderer is faithfully
        # reproducing the detected position or silently reinterpreting
        # it. height_pct does not exist in the detection schema (Vision
        # only reports width_pct/fontsize_pct); it is reported here as
        # the rendered block's height as a fraction of the frame, for
        # the same source-vs-final comparison.
        block_left    = cx - block_w // 2
        src_width_px  = wb * width_pct
        src_height_px = hb * (total_h / ha) if ha else 0.0
        debug_obj = {
            "text":          text[:80],
            "source_x":      round(wb * block.get("cx_pct", 0.5) - src_width_px / 2, 1),
            "source_y":      round(hb * block.get("cy_pct", 0.5) - src_height_px / 2, 1),
            "source_width":  round(src_width_px, 1),
            "source_height": round(src_height_px, 1),
            "cx_pct":        round(block.get("cx_pct", 0.5), 4),
            "cy_pct":        round(block.get("cy_pct", 0.5), 4),
            "width_pct":     round(width_pct, 4),
            "height_pct":    round(total_h / ha, 4) if ha else 0.0,
            "final_x":       block_left,
            "final_y":       y_start,
            "final_width":   block_w,
            "final_height":  total_h,
        }
        _caption_debug_log.append(debug_obj)
        print(f"[CAPTION_DEBUG] {json.dumps(debug_obj, ensure_ascii=False)}", file=sys.stderr)

    out_path = f"/tmp/overlay_{uuid.uuid4().hex}.png"
    overlay.save(out_path, "PNG")
    return out_path


def _save_caption_debug_frames(out_dir: Path, tag: str, path_b: str, path_out: str,
                               debug_obj: dict, t: float,
                               wb: int, hb: int, wa: int, ha: int) -> None:
    """
    CAPTION_VISUAL_DEBUG=1 only — batch mode investigation aid.

    Grabs one full-resolution frame from source video B at time `t` and
    draws the DETECTED caption box on it in RED, using source_x/source_y
    /source_width/source_height (debug_obj, already expressed in B's own
    w×h pixel space — see render_text_overlay's [CAPTION_DEBUG] log).

    Grabs the matching frame from rendered output C and draws the box
    the renderer actually used in GREEN, using final_x/final_y/
    final_width/final_height (already in the wa×ha canvas's pixel space
    — which is video C's pixel space too, since the overlay is
    composited onto A at offset 0:0 with no scaling).

    Save both PNGs so the two boxes' positions — as a fraction of their
    own frame — can be compared side by side. Never modifies detection,
    rendering or the output video; purely observational, and any
    failure here is swallowed so it can never affect a real render job.
    """
    import sys
    from PIL import Image, ImageDraw

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        b_frame = str(out_dir / f"{tag}_debug_B_source.png")
        c_frame = str(out_dir / f"{tag}_debug_C_rendered.png")

        for src, dst in ((path_b, b_frame), (path_out, c_frame)):
            subprocess.run(
                ["ffmpeg", "-ss", str(max(0.0, t)), "-i", src,
                 "-vframes", "1", "-y", dst],
                capture_output=True, timeout=20
            )

        if Path(b_frame).exists():
            img = Image.open(b_frame).convert("RGB")
            draw = ImageDraw.Draw(img)
            sx, sy = debug_obj["source_x"], debug_obj["source_y"]
            sw, sh = debug_obj["source_width"], debug_obj["source_height"]
            draw.rectangle([sx, sy, sx + sw, sy + sh], outline=(255, 0, 0), width=4)
            img.save(b_frame)

        if Path(c_frame).exists():
            img = Image.open(c_frame).convert("RGB")
            draw = ImageDraw.Draw(img)
            fx, fy = debug_obj["final_x"], debug_obj["final_y"]
            fw, fh = debug_obj["final_width"], debug_obj["final_height"]
            draw.rectangle([fx, fy, fx + fw, fy + fh], outline=(0, 255, 0), width=4)
            img.save(c_frame)

        print(
            f"[CAPTION_VISUAL_DEBUG] '{tag}' saved {b_frame} (red=detected, B is "
            f"{wb}x{hb}) and {c_frame} (green=rendered, C is {wa}x{ha}) at t={t:.2f}s",
            file=sys.stderr
        )
    except Exception as e:
        import sys as _sys
        print(f"[CAPTION_VISUAL_DEBUG] '{tag}' failed: {e}", file=_sys.stderr)


def _build_timed_overlay_cmd(path_a: str, path_b: str, overlay_specs: list, path_out: str) -> list:
    """
    Batch-mode-only ffmpeg command builder: composite video A with N
    separate transparent overlay PNGs (one per timed caption, each
    produced by the SAME render_text_overlay used everywhere else — so
    font, stroke, size, wrap and position are pixel-identical to the
    static single-overlay path), each gated to be visible only during
    its own [start_time, end_time] window via the overlay filter's
    `enable='between(t,start,end)'` expression. Captions at the same
    spot naturally replace one another because only one window is ever
    active at a given timestamp. Audio still comes from video B.

    overlay_specs: list of (overlay_png_path, start_time, end_time)
    """
    cmd = ["ffmpeg", "-y", "-i", path_a, "-i", path_b]
    for png_path, _, _ in overlay_specs:
        cmd += ["-i", png_path]

    filters = []
    prev = "[0:v]"
    last = len(overlay_specs) - 1
    for i, (_, start, end) in enumerate(overlay_specs):
        in_label  = f"[{i + 2}:v]"
        out_label = "[ovout]" if i == last else f"[ov{i + 1}]"
        filters.append(
            f"{prev}{in_label}overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'{out_label}"
        )
        prev = out_label

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[ovout]",
        "-map", "1:a",
        "-shortest",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
        "-c:a", "aac", "-b:a", "128k",
        "-loglevel", "error",
        path_out
    ]
    return cmd


# ── Access gate (team accounts) ───────────────────────────────────
# Every request is checked against the signed session cookie's
# user_id. The user row is re-fetched from the database on EVERY
# request (never cached), so disabling or deleting an account takes
# effect on that very next request — even if the browser still holds
# a "valid" signed cookie. Only /login and static assets are reachable
# while logged out. This gate runs strictly BEFORE route handlers; it
# does not change, wrap, or touch any simple-mode or batch-mode logic
# — those run completely unchanged once request.current_user holds an
# active account.
# ── Generation History (additive — new read-only queries for /history) ───────
# These functions only SELECT from usage_logs. No existing code is modified.

def get_history_rows(user_id=None, period="all", mode_filter="all",
                     page=1, per_page=50, admin=False):
    """Paginated list of individual usage_log rows for the /history page.
    admin=True → returns all users' rows (user_id param ignored unless set).
    admin=False → always scoped to user_id.
    period: 'all' / 'today' / '7d' / '30d'
    mode_filter: 'all' / 'simple' / 'batch' / 'variation'
    Returns dict with rows, total, pages, page, per_page.
    """
    where_clauses, params = [], []
    if not admin and user_id is not None:
        where_clauses.append("l.user_id = ?")
        params.append(user_id)
    if period == "today":
        where_clauses.append("l.timestamp >= ?")
        params.append(_usage_period_cutoff("today"))
    elif period == "7d":
        where_clauses.append("l.timestamp >= ?")
        params.append(_usage_period_cutoff("7d"))
    elif period == "30d":
        where_clauses.append("l.timestamp >= ?")
        params.append(_usage_period_cutoff("30d"))
    if mode_filter in ("simple", "batch", "variation", "variation_multi", "audio_converter", "photo_metadata"):
        where_clauses.append("l.mode = ?")
        params.append(mode_filter)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    with _users_db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM usage_logs l {where_sql}", params
        ).fetchone()[0]
        offset = (page - 1) * per_page
        rows = conn.execute(
            f"""
            SELECT l.id, l.user_id, l.user_email, l.timestamp, l.mode,
                   l.generation_success, l.source_video_count, l.output_video_count,
                   l.source_video_duration_seconds, l.generated_video_duration_seconds,
                   l.estimated_cost, l.claude_requests_count,
                   l.variation_strength, l.source_filename,
                   u.email AS account_email
            FROM usage_logs l
            LEFT JOIN users u ON u.id = l.user_id
            {where_sql}
            ORDER BY l.timestamp DESC
            LIMIT ? OFFSET ?
            """,
            params + [per_page, offset],
        ).fetchall()
    pages = max(1, (total + per_page - 1) // per_page)
    return {
        "rows": [dict(r) for r in rows],
        "total": total,
        "pages": pages,
        "page": page,
        "per_page": per_page,
    }


def get_history_kpis(user_id=None, period="all", mode_filter="all", admin=False):
    """KPI aggregates for the /history page header.
    Uses the same _usage_aggregate() pattern as the consumption pages.
    """
    where_clauses, params = [], []
    if not admin and user_id is not None:
        where_clauses.append("user_id = ?")
        params.append(user_id)
    if period == "today":
        where_clauses.append("timestamp >= ?")
        params.append(_usage_period_cutoff("today"))
    elif period == "7d":
        where_clauses.append("timestamp >= ?")
        params.append(_usage_period_cutoff("7d"))
    elif period == "30d":
        where_clauses.append("timestamp >= ?")
        params.append(_usage_period_cutoff("30d"))
    if mode_filter in ("simple", "batch", "variation", "variation_multi", "audio_converter", "photo_metadata"):
        where_clauses.append("mode = ?")
        params.append(mode_filter)
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    return _usage_aggregate(where_sql, tuple(params))


_PUBLIC_ENDPOINTS = {"login", "static"}


@app.before_request
def _require_login():
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    user = get_user_by_id(session["user_id"]) if "user_id" in session else None
    if user is None or not user["is_active"]:
        session.clear()
        return redirect(url_for("login"))
    request.current_user = user
    return None


def admin_required(view):
    """Gate a route to admins only. Runs after _require_login has
    already populated request.current_user for this request."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = getattr(request, "current_user", None)
        if user is None or user["role"] != "admin":
            return ("Accès refusé : réservé aux administrateurs.", 403)
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "")
        submitted = request.form.get("password", "")
        user = get_user_by_email(email) if email else None
        # Always run check_password_hash — against the real hash if the
        # account exists, against a dummy hash otherwise — so a wrong
        # password and a nonexistent email take the same time and can't
        # be distinguished from response timing (no account enumeration).
        password_ok = check_password_hash(
            user["password_hash"] if user else _DUMMY_PASSWORD_HASH, submitted
        )
        if user is not None and user["is_active"] and password_ok:
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            return redirect(url_for("index"))
        error = "Identifiants incorrects, ou compte désactivé."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Consumption dashboard (every authenticated user; own data only) ──
# Reuses the global _require_login gate — no new auth code. A regular user
# only ever sees aggregates scoped to their own user_id (get_user_usage_summary
# filters with WHERE user_id = ?), mirroring how the rest of the app already
# separates per-user data from admin-only data.

@app.route("/consumption")
def consumption():
    user = request.current_user
    summary = get_user_usage_summary(user["id"])

    # ── Profitability layer (additive, own-data-only): the same
    # get_effective_monthly_price/compute_profitability helpers the admin
    # dashboard uses, scoped to nothing but this user's own all-time cost —
    # mirrors the existing per-user separation pattern (WHERE user_id = ?).
    try:
        monthly_price = get_effective_monthly_price(user)
        currency = get_user_currency(user)
        profitability = compute_profitability(summary["all_time"]["total_cost"], monthly_price)
        profitability_state = profitability_status(profitability)
    except Exception as e:
        print(f"[profitability] WARNING: failed to compute profitability for user={user.get('email', '?')}: {e}")
        monthly_price, currency = 0.0, "EUR"
        profitability = compute_profitability(0.0, 0.0)
        profitability_state = "na"

    return render_template(
        "consumption.html",
        current_user=user,
        summary=summary,
        plan_limits=PLAN_LIMITS,
        monthly_price=monthly_price,
        currency=currency,
        currency_symbols=CURRENCY_SYMBOLS,
        profitability=profitability,
        profitability_state=profitability_state,
        usage_by_mode=get_usage_by_mode(scope="user", user_id=user["id"]),
    )


# ── Admin: team account management ────────────────────────────────
# Reachable only by an authenticated admin (admin_required stacks on
# top of the global _require_login gate). Regular team members never
# see these routes, any link to them, or any admin-only data.

@app.route("/admin/users")
@admin_required
def admin_users():
    return render_template("admin_users.html", users=list_users())


# ── Admin: global consumption dashboard ──────────────────────
# Gated by the existing admin_required decorator — zero new authorization
# code. Shows platform-wide stats, time-period breakdowns, a sortable
# per-user table with heavy-usage warning badges, and a lightweight inline
# SVG daily-activity chart (no external chart library / no CDN dependency).

@app.route("/admin/consumption")
@admin_required
def admin_consumption():
    sort_by = request.args.get("sort", "last_activity")
    if sort_by not in ("videos", "cost", "last_activity"):
        sort_by = "last_activity"

    user_table = list_user_usage_table(sort_by=sort_by)

    # ── Profitability layer (additive): enrich each row with revenue/cost/
    # profit/margin computed from data the table already carries (all-time
    # cost + plan/price columns) — no extra queries, no new aggregation.
    # Wrapped defensively per-row so one malformed row can never blank the
    # whole dashboard; a row that fails just gets a neutral 'na' entry.
    for row in user_table:
        try:
            revenue = get_effective_monthly_price(row)
            row["monthly_price_effective"] = revenue
            row["currency"] = get_user_currency(row)
            row["profitability"] = compute_profitability(row.get("total_cost", 0.0), revenue)
            row["profitability_status"] = profitability_status(row["profitability"])
        except Exception as e:
            print(f"[profitability] WARNING: failed to compute profitability for user row {row.get('id')}: {e}")
            row["monthly_price_effective"] = 0.0
            row["currency"] = "EUR"
            row["profitability"] = compute_profitability(0.0, 0.0)
            row["profitability_status"] = "na"

    return render_template(
        "admin_consumption.html",
        current_user=request.current_user,
        global_summary=get_global_usage_summary(),
        user_table=user_table,
        daily=get_daily_generation_counts(days=14),
        sort_by=sort_by,
        total_users=len(list_users()),
        heavy_generations_threshold=CONSUMPTION_HEAVY_GENERATIONS_THRESHOLD,
        heavy_claude_requests_threshold=CONSUMPTION_HEAVY_CLAUDE_REQUESTS_THRESHOLD,
        cost_threshold=CONSUMPTION_COST_THRESHOLD_USD,
        platform_profitability=get_platform_profitability_summary(user_table),
        currency_symbols=CURRENCY_SYMBOLS,
        margin_green_threshold=PROFITABILITY_MARGIN_GREEN_THRESHOLD,
        margin_orange_threshold=PROFITABILITY_MARGIN_ORANGE_THRESHOLD,
        usage_by_mode=get_usage_by_mode(scope="global"),
    )


@app.route("/history")
def history():
    user = request.current_user
    period = request.args.get("period", "all")
    if period not in ("all", "today", "7d", "30d"):
        period = "all"
    mode_filter = request.args.get("mode", "all")
    if mode_filter not in ("all", "simple", "batch", "variation", "variation_multi", "audio_converter", "photo_metadata"):
        mode_filter = "all"
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    is_admin = user["role"] == "admin"
    result = get_history_rows(
        user_id=user["id"] if not is_admin else None,
        period=period,
        mode_filter=mode_filter,
        page=page,
        per_page=50,
        admin=is_admin,
    )
    kpis = get_history_kpis(
        user_id=user["id"] if not is_admin else None,
        period=period,
        mode_filter=mode_filter,
        admin=is_admin,
    )
    return render_template(
        "history.html",
        current_user=user,
        rows=result["rows"],
        total=result["total"],
        pages=result["pages"],
        page=result["page"],
        per_page=result["per_page"],
        kpis=kpis,
        period=period,
        mode_filter=mode_filter,
        is_admin=is_admin,
    )


@app.route("/history/delete/<int:log_id>", methods=["POST"])
def history_delete(log_id):
    """Delete a single usage_log row. Non-admins can only delete their own rows."""
    user = request.current_user
    with _users_db() as conn:
        row = conn.execute(
            "SELECT user_id FROM usage_logs WHERE id = ?", (log_id,)
        ).fetchone()
        if row is None:
            return redirect(url_for("history"))
        if user["role"] != "admin" and row["user_id"] != user["id"]:
            return ("Accès refusé.", 403)
        conn.execute("DELETE FROM usage_logs WHERE id = ?", (log_id,))
        conn.commit()
    return redirect(request.referrer or url_for("history"))


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_users_create():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    role = request.form.get("role", "member")
    if role not in ("admin", "member"):
        role = "member"
    if email and password and get_user_by_email(email) is None:
        create_user(email, password, role)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/reset_password", methods=["POST"])
@admin_required
def admin_users_reset_password(user_id):
    new_password = request.form.get("password", "")
    if new_password:
        set_user_password(user_id, new_password)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/disable", methods=["POST"])
@admin_required
def admin_users_disable(user_id):
    if user_id != request.current_user["id"]:  # an admin can't lock themselves out
        set_user_active(user_id, False)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/enable", methods=["POST"])
@admin_required
def admin_users_enable(user_id):
    set_user_active(user_id, True)
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_users_delete(user_id):
    if user_id != request.current_user["id"]:  # an admin can't delete themselves
        delete_user(user_id)
    return redirect(url_for("admin_users"))


# ── Admin: edit plan/pricing fields (Phase 6 — profitability layer) ──
# Small, additive, separate form/route — completely independent from the
# create/reset/disable/delete actions above (none of which are touched).
# Reuses @admin_required (zero new auth code) and the same redirect-back
# pattern. set_user_plan_fields() validates/clamps everything and silently
# no-ops on bad input, so a malformed submission can never corrupt a row,
# 500, or affect login/team-management/generation in any way.
@app.route("/admin/users/<int:user_id>/edit_plan", methods=["POST"])
@admin_required
def admin_users_edit_plan(user_id):
    plan_name = request.form.get("plan_name")
    monthly_price_raw = request.form.get("monthly_price", "").strip()
    limit_raw = request.form.get("monthly_generation_limit", "").strip()
    credits_raw = request.form.get("credits_remaining", "").strip()

    monthly_price = None
    if monthly_price_raw != "":
        try:
            monthly_price = float(monthly_price_raw.replace(",", "."))
        except ValueError:
            monthly_price = None

    monthly_generation_limit = None
    if limit_raw != "":
        try:
            monthly_generation_limit = int(limit_raw)
        except ValueError:
            monthly_generation_limit = None

    credits_remaining = None
    if credits_raw != "":
        try:
            credits_remaining = int(credits_raw)
        except ValueError:
            credits_remaining = None

    set_user_plan_fields(
        user_id,
        plan_name=plan_name,
        monthly_price=monthly_price,
        monthly_generation_limit=monthly_generation_limit,
        credits_remaining=credits_remaining,
    )
    return redirect(url_for("admin_users"))


# ── Routes ────────────────────────────────────────────────────────

@app.route("/")
def index():
    user = request.current_user
    # ── Dashboard overview data (additive, read-only): reuses the exact
    # same helpers /consumption already calls (get_user_usage_summary,
    # get_usage_by_mode) plus a small new read-only query
    # (get_recent_activity) that mirrors their WHERE user_id = ? scoping —
    # so the landing dashboard can show real KPI numbers and a real
    # recent-activity feed instead of empty space. Wrapped defensively so
    # a query hiccup can never blank the page (mirrors the try/except
    # pattern already used around profitability in /consumption).
    dashboard_summary = None
    dashboard_usage_by_mode = []
    dashboard_recent_activity = []
    if user:
        try:
            dashboard_summary = get_user_usage_summary(user["id"])
            dashboard_usage_by_mode = get_usage_by_mode(scope="user", user_id=user["id"])
            dashboard_recent_activity = get_recent_activity(user["id"], limit=6)
        except Exception as e:
            print(f"[dashboard] WARNING: failed to load overview data for user={user.get('email', '?')}: {e}")

    return render_template(
        "index.html",
        current_user=user,
        dashboard_summary=dashboard_summary,
        dashboard_usage_by_mode=dashboard_usage_by_mode,
        dashboard_recent_activity=dashboard_recent_activity,
    )


@app.route("/analyze", methods=["POST"])
def analyze():
    """Upload Video B → detect text blocks via Claude Vision (or Tesseract fallback)."""
    try:
        if "video_b" not in request.files:
            return jsonify({"error": "video_b manquant"}), 400

        vb     = request.files["video_b"]
        tmp_id = str(uuid.uuid4())
        tmp    = UPLOAD_DIR / tmp_id
        tmp.mkdir(parents=True, exist_ok=True)
        path_b = str(tmp / "b.mp4")
        vb.save(path_b)

        # Both Simple and Batch run timed detection first: it correctly
        # handles videos where a caption slot shows different text at
        # different times (e.g. "1. Young guy" → "2. Shy one" → "3. Older man"
        # all at the same vertical position). Timed detection produces
        # non-overlapping start_time/end_time windows so each caption renders
        # sequentially; the static single-pass fallback is kept for videos
        # where timed detection finds nothing (fully static captions, or API
        # unavailable).
        ui_mode = (request.form.get("mode") or "simple").strip().lower()
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY", ""))

        # OCR Pro: Rapide (default) = exact current path + cache 'v1'.
        # Précis (opt-in) = 16 frames, 1080p, relaxed prompt, cache
        # 'v1-precise' (kept strictly separate so the two never mix).
        ocr_mode   = (request.form.get("ocr_mode") or "rapide").strip().lower()
        _precise   = (ocr_mode == "precis")
        _cache_ver = "v1-precise" if _precise else OCR_CACHE_VERSION
        _fallback_scale = "scale=1080:-2" if _precise else "scale=720:-1"

        # ── OCR cache lookup (Chantier 1) — before any Vision call. On a
        # hit we return the EXACT cached detection (same lines format),
        # skipping Vision + frame extraction entirely. ──
        b_hash = None
        if has_key:
            try:
                b_hash = _sha256_file(path_b)
            except Exception:
                b_hash = None
            if b_hash:
                cached = _ocr_cache_get(b_hash, _cache_ver)
                if cached is not None:
                    return jsonify({
                        "lines":          cached["lines"],
                        "video_b_height": cached["video_b_height"],
                        "mode":           cached["mode"] or "vision",
                        "cached":         True,
                        "source":         "cache",
                    })

        source = None
        lines = []
        if has_key:
            # Timed detection for ALL modes: sample frames across the timeline
            # and detect WHEN each caption appears/disappears (start_time/end_time).
            try:
                lines, _ = analyze_with_claude_vision_timed(path_b, precise=_precise)
            except Exception:
                lines = []
            if lines:
                source = "vision"

        if not lines:
            # Fall back to the original single-pass flow (identical to the
            # pre-existing behavior; produces lines WITHOUT timing info).
            frames = extract_frames(path_b, count=4, scale=_fallback_scale)
            if has_key:
                lines = analyze_with_claude_vision(frames)
                if lines:
                    source = "vision"
            else:
                lines = []
            if not lines:
                lines = analyze_with_tesseract_fallback(frames)
                if lines:
                    source = "tesseract"
            for f in frames:
                try:
                    Path(f).unlink(missing_ok=True)
                except Exception:
                    pass

        _, hb = get_video_dims(path_b)

        # ── Store ONLY a non-empty Vision result (never empty, never
        # Tesseract). Defensive: a cache write can never break detection. ──
        if has_key and source == "vision" and lines and b_hash:
            _ocr_cache_put(b_hash, lines, hb, "vision", _cache_ver)

        return jsonify({
            "lines":          lines,
            "video_b_height": hb,
            "mode":           "vision" if has_key else "tesseract",
            "cached":         False,
            "source":         source or "tesseract",
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/process", methods=["POST"])
def process():
    try:
        # ── Usage-tracking context (additive; purely local bookkeeping —
        # does not alter any control flow below). _usage_attempt_started
        # only flips to True once both source files are actually saved to
        # disk, so trivial client errors (missing files) aren't logged as
        # generation attempts, while every real attempt — success or
        # failure — is. ──
        _usage_mode            = (request.form.get("mode") or "simple").strip().lower()
        _usage_attempt_started = False
        _usage_source_seconds  = None

        if "video_a" not in request.files or "video_b" not in request.files:
            return jsonify({"error": "Les deux videos sont requises."}), 400

        va = request.files["video_a"]
        vb = request.files["video_b"]

        job_id  = str(uuid.uuid4())
        job_dir = UPLOAD_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        path_a   = str(job_dir / "a.mp4")
        path_b   = str(job_dir / "b.mp4")
        path_out = str(job_dir / "c.mp4")

        va.save(path_a)
        vb.save(path_b)
        _usage_attempt_started = True
        _usage_source_seconds  = _get_video_duration_seconds(path_a)

        wa, ha = get_video_dims(path_a)
        wb, hb = get_video_dims(path_b)

        # Get text lines from form
        lines_json = request.form.get("lines_json", "[]")
        try:
            lines = json.loads(lines_json)
        except Exception:
            lines = []

        if not lines:
            log_usage_event(request.current_user, _usage_mode,
                            source_seconds=_usage_source_seconds, output_seconds=None,
                            source_count=2, output_count=0, success=False)
            return jsonify({"error": "Aucune ligne de texte fournie."}), 400

        # Timed-caption path: taken for ANY mode (Simple or Batch) when
        # every line carries start_time/end_time (i.e. came from
        # analyze_with_claude_vision_timed). This lets Simple mode handle
        # videos where captions change over time — each caption renders on
        # its own non-overlapping time window instead of all being stacked
        # on a single static overlay. Falls through to the static path only
        # when timing info is absent (tesseract fallback, or API unavailable).
        ui_mode = (request.form.get("mode") or "simple").strip().lower()
        has_timing = (
            bool(lines)
            and all(isinstance(l, dict) and "start_time" in l and "end_time" in l for l in lines)
        )

        overlay_paths = []
        try:
            if has_timing:
                overlay_specs = []
                for line in lines:
                    try:
                        start = max(0.0, float(line.get("start_time", 0.0)))
                        end   = max(start + 0.05, float(line.get("end_time", start + 0.05)))
                    except Exception:
                        continue
                    # Render EACH caption alone on its own transparent layer
                    # using the exact same render_text_overlay function/logic
                    # as every other mode — only the time window differs.
                    op = render_text_overlay([line], wa, ha, wb, hb, style=_caption_style_from_request())
                    overlay_paths.append(op)
                    overlay_specs.append((op, start, end))

                if not overlay_specs:
                    return jsonify({"error": "Aucune ligne de texte fournie."}), 400

                cmd = _build_timed_overlay_cmd(path_a, path_b, overlay_specs, path_out)
            else:
                # ── Original single-overlay path (simple mode + batch
                # fallback when timing wasn't available) — unchanged. ──
                overlay_path = render_text_overlay(lines, wa, ha, wb, hb, style=_caption_style_from_request())
                overlay_paths.append(overlay_path)

                cmd = [
                    "ffmpeg", "-y",
                    "-i", path_a,
                    "-i", path_b,
                    "-i", overlay_path,
                    "-filter_complex",
                    "[0:v][2:v]overlay=0:0[out]",
                    "-map", "[out]",
                    "-map", "1:a",
                    "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                    "-c:a", "aac", "-b:a", "128k",
                    "-loglevel", "error",
                    path_out
                ]

            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                log_usage_event(request.current_user, _usage_mode,
                                source_seconds=_usage_source_seconds, output_seconds=None,
                                source_count=2, output_count=0, success=False)
                return jsonify({"error": "Timeout (>3 min)"}), 500
        finally:
            for op in overlay_paths:
                try:
                    Path(op).unlink(missing_ok=True)
                except Exception:
                    pass
            gc.collect()

        if proc.returncode != 0 or not Path(path_out).exists():
            err = proc.stderr[-800:] if proc.stderr else "ffmpeg a echoue"
            log_usage_event(request.current_user, _usage_mode,
                            source_seconds=_usage_source_seconds, output_seconds=None,
                            source_count=2, output_count=0, success=False)
            return jsonify({"error": err}), 500

        log_usage_event(request.current_user, _usage_mode,
                        source_seconds=_usage_source_seconds,
                        output_seconds=_get_video_duration_seconds(path_out),
                        source_count=2, output_count=1, success=True)
        return jsonify({"job_id": job_id})

    except Exception as e:
        try:
            if _usage_attempt_started:
                log_usage_event(request.current_user, _usage_mode,
                                source_seconds=_usage_source_seconds, output_seconds=None,
                                source_count=2, output_count=0, success=False)
        except Exception:
            pass
        return jsonify({"error": f"Erreur serveur: {str(e)}"}), 500


@app.route("/download/<job_id>")
def download(job_id):
    if ".." in job_id or "/" in job_id:
        return jsonify({"error": "Invalid"}), 400
    path = UPLOAD_DIR / job_id / "c.mp4"
    if path.exists():
        return send_file(str(path), as_attachment=True,
                         download_name="video_C.mp4", mimetype="video/mp4")
    return jsonify({"error": "Not found"}), 404


@app.route("/batch_zip", methods=["POST"])
def batch_zip():
    """
    Two supported request shapes:
      - {"batch_id": "..."} — NEW A×B combination-matrix flow: zips every
        rendered file in that batch's out/ folder. Filenames already
        encode both source and target (e.g. A01_B02_output.mp4), so no
        renaming is needed here.
      - {"job_ids": [...]} — original single-source batch flow, kept for
        backward compatibility; zips each job's c.mp4 as video_C_N.mp4.
    """
    try:
        data = request.get_json(force=True) or {}

        batch_id = (data.get("batch_id") or "").strip()
        if batch_id:
            if ".." in batch_id or "/" in batch_id:
                return jsonify({"error": "batch_id invalide"}), 400

            out_dir = BATCH_DIR / batch_id / "out"
            files = sorted(out_dir.glob("*.mp4")) if out_dir.exists() else []
            if not files:
                return jsonify({"error": "Aucun fichier valide trouvé"}), 400

            zip_path = f"/tmp/batch_{uuid.uuid4().hex}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
                for p in files:
                    zf.write(str(p), p.name)

            return send_file(
                zip_path,
                as_attachment=True,
                download_name="videos_C.zip",
                mimetype="application/zip"
            )

        # ── Original job_ids-based flow (backward compatible) ──
        job_ids = data.get("job_ids", [])

        valid = []
        for jid in job_ids:
            # Sanitize
            if not jid or ".." in jid or "/" in jid:
                continue
            p = UPLOAD_DIR / jid / "c.mp4"
            if p.exists():
                valid.append((jid, p))

        if not valid:
            return jsonify({"error": "Aucun fichier valide trouvé"}), 400

        zip_path = f"/tmp/batch_{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for i, (jid, p) in enumerate(valid, 1):
                zf.write(str(p), f"video_C_{i}.mp4")

        return send_file(
            zip_path,
            as_attachment=True,
            download_name="videos_C.zip",
            mimetype="application/zip"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Batch-mode-only: A×B combination matrix routes ────────────────
# These four routes orchestrate the new "N source videos × M target
# videos → N×M outputs" batch flow. None of them duplicate detection
# or rendering LOGIC — they call the exact same functions that
# /analyze and /process already use (analyze_with_claude_vision_timed,
# analyze_with_claude_vision, analyze_with_tesseract_fallback,
# extract_frames, render_text_overlay, _build_timed_overlay_cmd), so
# caption fidelity (font, stroke, size, wrap, position, timing) is
# pixel-identical to the existing single-source batch path. Simple
# mode and the original /analyze, /process, /batch_zip(job_ids) paths
# are completely untouched.

@app.route("/batch_stage", methods=["POST"])
def batch_stage():
    """
    Stage ONE source (A) or target (B) video for the combination matrix.
    Each unique file is uploaded exactly once and saved to a per-batch
    folder (A/<index>.mp4 or B/<index>.mp4); /batch_render then
    references these staged copies by index instead of re-uploading the
    same file for every combination — keeping total upload bandwidth and
    disk usage bounded even at the maximum 10×10 = 100 matrix (vs. up to
    ~10x redundant re-uploads with a naive per-combo upload approach).

    First call for a batch may omit batch_id; the server creates one and
    returns it for subsequent staging/detect/render/zip calls.
    """
    try:
        kind = (request.form.get("kind") or "").strip().lower()
        if kind not in ("a", "b"):
            return jsonify({"error": "Paramètre 'kind' invalide (a ou b attendu)"}), 400

        try:
            index = int(request.form.get("index", "-1"))
        except Exception:
            index = -1
        # Pairing mode raises the per-axis staging ceiling to 300; MATRIX
        # mode (no/absent flag) keeps its 50 cap unchanged.
        _is_pairing = (request.form.get("pairing", "0") or "0").strip().lower() in ("1", "true", "on", "yes")
        _stage_max  = MAX_BATCH_PAIR_FILES if _is_pairing else MAX_BATCH_FILES
        if index < 0 or index >= _stage_max:
            return jsonify({"error": f"Index invalide (0 à {_stage_max - 1})"}), 400

        if "file" not in request.files:
            return jsonify({"error": "Fichier manquant"}), 400

        batch_id = (request.form.get("batch_id") or "").strip()
        is_new   = not batch_id or ".." in batch_id or "/" in batch_id
        if is_new:
            _cleanup_stale_batches()
            batch_id = uuid.uuid4().hex

        bdir = BATCH_DIR / batch_id
        (bdir / "A").mkdir(parents=True, exist_ok=True)
        (bdir / "B").mkdir(parents=True, exist_ok=True)
        (bdir / "out").mkdir(parents=True, exist_ok=True)

        sub  = "A" if kind == "a" else "B"
        dest = bdir / sub / f"{index:02d}.mp4"
        request.files["file"].save(str(dest))

        return jsonify({"batch_id": batch_id, "kind": kind, "index": index})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/batch_detect", methods=["POST"])
def batch_detect():
    """
    Run caption detection ONCE for a staged target (B) video so the same
    detected captions are reused for every source (A) it gets combined
    with, instead of re-running Vision detection up to 10x for the same
    B file. Mirrors EXACTLY the detection sequence /analyze uses for
    ui_mode == "batch" — timed detection first, falling back to the
    original static single-pass flow — by calling the same unmodified
    detection functions. Only the file source (staged path vs. fresh
    upload) differs.
    """
    try:
        batch_id = (request.form.get("batch_id") or "").strip()
        if not batch_id or ".." in batch_id or "/" in batch_id:
            return jsonify({"error": "batch_id invalide"}), 400
        try:
            b_index = int(request.form.get("b_index", "-1"))
        except Exception:
            b_index = -1

        path_b = BATCH_DIR / batch_id / "B" / f"{b_index:02d}.mp4"
        if not path_b.exists():
            return jsonify({"error": "Vidéo B introuvable (étape de staging manquante)"}), 404
        path_b = str(path_b)

        has_key = bool(os.environ.get("ANTHROPIC_API_KEY", ""))

        # OCR Pro: Rapide (default) = exact current path + cache 'v1' ;
        # Précis (opt-in) = 16 frames, 1080p, relaxed prompt, cache
        # 'v1-precise'. Applies to Batch Matrice AND Pairing (both call
        # this route). Strictly separate cache namespaces.
        ocr_mode   = (request.form.get("ocr_mode") or "rapide").strip().lower()
        _precise   = (ocr_mode == "precis")
        _cache_ver = "v1-precise" if _precise else OCR_CACHE_VERSION
        _fallback_scale = "scale=1080:-2" if _precise else "scale=720:-1"

        # ── OCR cache lookup (Chantier 1) — before any Vision call. ──
        b_hash = None
        if has_key:
            try:
                b_hash = _sha256_file(path_b)
            except Exception:
                b_hash = None
            if b_hash:
                cached = _ocr_cache_get(b_hash, _cache_ver)
                if cached is not None:
                    return jsonify({
                        "lines":          cached["lines"],
                        "video_b_height": cached["video_b_height"],
                        "mode":           cached["mode"] or "vision",
                        "cached":         True,
                        "source":         "cache",
                    })

        # Identical sequence to /analyze: timed detection first, falling
        # back to the original single-pass detection if it finds nothing.
        source = None
        try:
            lines, _ = analyze_with_claude_vision_timed(path_b, precise=_precise)
        except Exception:
            lines = []
        if lines:
            source = "vision"

        frames = []
        if not lines:
            frames = extract_frames(path_b, count=4, scale=_fallback_scale)
            if has_key:
                lines = analyze_with_claude_vision(frames)
                if lines:
                    source = "vision"
            else:
                lines = []
            if not lines:
                lines = analyze_with_tesseract_fallback(frames)
                if lines:
                    source = "tesseract"

        for f in frames:
            try:
                Path(f).unlink(missing_ok=True)
            except Exception:
                pass

        _, hb = get_video_dims(path_b)

        # ── Store ONLY a non-empty Vision result (never empty, never Tesseract). ──
        if has_key and source == "vision" and lines and b_hash:
            _ocr_cache_put(b_hash, lines, hb, "vision", _cache_ver)

        return jsonify({
            "lines":          lines,
            "video_b_height": hb,
            "mode":           "vision" if has_key else "tesseract",
            "cached":         False,
            "source":         source or "tesseract",
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/batch_render", methods=["POST"])
def batch_render():
    """
    Render ONE (A_i, B_j) combination from already-staged files. Reuses
    the EXACT SAME rendering path as /process's batch branch — the
    has_timing check, render_text_overlay (one call per caption for
    timed captions, identical to /process), _build_timed_overlay_cmd /
    the original static-overlay ffmpeg command — so caption fidelity is
    pixel-identical to the single-source batch path. Only file sourcing
    (staged paths, reused — not re-uploaded) and output naming
    (A<i>_B<j>_output.mp4, so every file in the final ZIP clearly
    identifies both its source and target) are new.

    Processes ONE combination per request — the client drives the A×B
    loop sequentially (one ffmpeg job in flight at a time), exactly like
    the existing single-source batch flow, so memory/disk stay bounded
    even for the maximum 100-combination matrix. Cleans up overlay PNGs
    and runs gc.collect() after every job, same as /process.
    """
    try:
        # ── Usage-tracking context (additive; purely local bookkeeping —
        # mirrors the same pattern used in /process). Batch mode is the
        # only mode that reaches this route, so _usage_mode is fixed. ──
        _usage_mode            = "batch"
        _usage_attempt_started = False
        _usage_source_seconds  = None

        batch_id = (request.form.get("batch_id") or "").strip()
        if not batch_id or ".." in batch_id or "/" in batch_id:
            return jsonify({"error": "batch_id invalide"}), 400
        try:
            a_index = int(request.form.get("a_index", "-1"))
            b_index = int(request.form.get("b_index", "-1"))
        except Exception:
            return jsonify({"error": "Index invalide"}), 400
        # Pairing mode raises index bounds to 300; MATRIX keeps 50.
        _is_pairing = (request.form.get("pairing", "0") or "0").strip().lower() in ("1", "true", "on", "yes")
        _idx_max = MAX_BATCH_PAIR_FILES if _is_pairing else MAX_BATCH_FILES
        if not (0 <= a_index < _idx_max) or not (0 <= b_index < _idx_max):
            return jsonify({"error": "Index hors limites"}), 400

        # Server-side TOTAL cap. The client sends num_a/num_b on every
        # render call so the server can independently enforce the limit —
        # never trusting the client. Validation only — the A+B→C render
        # path below is unchanged.
        try:
            num_a = int(request.form.get("num_a", "0"))
            num_b = int(request.form.get("num_b", "0"))
        except Exception:
            num_a = num_b = 0
        if _is_pairing:
            # Pairing: outputs = min(#A,#B) ≤ 300. NEVER the product.
            if num_a > MAX_BATCH_PAIR_FILES or num_b > MAX_BATCH_PAIR_FILES:
                return jsonify({"error": f"Maximum {MAX_BATCH_PAIR_FILES} vidéos A et {MAX_BATCH_PAIR_FILES} vidéos B."}), 400
            if num_a > 0 and num_b > 0 and min(num_a, num_b) > MAX_BATCH_PAIR_OUTPUTS:
                return jsonify({"error": f"Maximum autorisé : {MAX_BATCH_PAIR_OUTPUTS} vidéos générées."}), 400
        else:
            # MATRIX (unchanged): A × B ≤ 300, per-axis ≤ 50.
            if num_a > 0 and num_b > 0:
                if num_a > MAX_BATCH_FILES or num_b > MAX_BATCH_FILES:
                    return jsonify({"error": f"Maximum {MAX_BATCH_FILES} vidéos A et {MAX_BATCH_FILES} vidéos B."}), 400
                if num_a * num_b > MAX_BATCH_COMBOS:
                    return jsonify({"error": f"Maximum autorisé : {MAX_BATCH_COMBOS} vidéos générées."}), 400

        bdir   = BATCH_DIR / batch_id
        path_a = bdir / "A" / f"{a_index:02d}.mp4"
        path_b = bdir / "B" / f"{b_index:02d}.mp4"
        if not path_a.exists() or not path_b.exists():
            return jsonify({"error": "Vidéo source introuvable (étape de staging manquante)"}), 404
        path_a, path_b = str(path_a), str(path_b)
        _usage_attempt_started = True
        _usage_source_seconds  = _get_video_duration_seconds(path_a)

        out_name = f"A{a_index + 1:02d}_B{b_index + 1:02d}_output.mp4"
        path_out = str(bdir / "out" / out_name)

        wa, ha = get_video_dims(path_a)
        wb, hb = get_video_dims(path_b)

        lines_json = request.form.get("lines_json", "[]")
        try:
            lines = json.loads(lines_json)
        except Exception:
            lines = []
        if not lines:
            log_usage_event(request.current_user, _usage_mode,
                            source_seconds=_usage_source_seconds, output_seconds=None,
                            source_count=2, output_count=0, success=False)
            return jsonify({"error": "Aucune ligne de texte fournie."}), 400

        # Same has_timing detection /process uses — renders via the timed
        # multi-overlay path when every line carries start_time/end_time,
        # falling back to the original single static overlay otherwise.
        has_timing = (
            bool(lines)
            and all(isinstance(l, dict) and "start_time" in l and "end_time" in l for l in lines)
        )

        overlay_paths = []
        # Snapshot of (debug_obj, timestamp) for the FIRST caption only —
        # used solely by the opt-in visual debug mode below to grab a
        # representative frame pair. Untouched (stays None) unless
        # CAPTION_VISUAL_DEBUG=1, so it can never affect a normal render.
        debug_capture = None
        try:
            if has_timing:
                overlay_specs = []
                for line in lines:
                    try:
                        start = max(0.0, float(line.get("start_time", 0.0)))
                        end   = max(start + 0.05, float(line.get("end_time", start + 0.05)))
                    except Exception:
                        continue
                    op = render_text_overlay([line], wa, ha, wb, hb, style=_caption_style_from_request())
                    overlay_paths.append(op)
                    overlay_specs.append((op, start, end))
                    if CAPTION_VISUAL_DEBUG and debug_capture is None and _caption_debug_log:
                        mid = max(0.05, (start + end) / 2.0)
                        debug_capture = (dict(_caption_debug_log[0]), mid)

                if not overlay_specs:
                    return jsonify({"error": "Aucune ligne de texte fournie."}), 400

                cmd = _build_timed_overlay_cmd(path_a, path_b, overlay_specs, path_out)
            else:
                overlay_path = render_text_overlay(lines, wa, ha, wb, hb, style=_caption_style_from_request())
                overlay_paths.append(overlay_path)
                if CAPTION_VISUAL_DEBUG and _caption_debug_log:
                    debug_capture = (dict(_caption_debug_log[0]), 0.5)

                cmd = [
                    "ffmpeg", "-y",
                    "-i", path_a,
                    "-i", path_b,
                    "-i", overlay_path,
                    "-filter_complex",
                    "[0:v][2:v]overlay=0:0[out]",
                    "-map", "[out]",
                    "-map", "1:a",
                    "-shortest",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                    "-c:a", "aac", "-b:a", "128k",
                    "-loglevel", "error",
                    path_out
                ]

            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                log_usage_event(request.current_user, _usage_mode,
                                source_seconds=_usage_source_seconds, output_seconds=None,
                                source_count=2, output_count=0, success=False)
                return jsonify({"error": "Timeout (>3 min)"}), 500
        finally:
            for op in overlay_paths:
                try:
                    Path(op).unlink(missing_ok=True)
                except Exception:
                    pass
            gc.collect()

        if proc.returncode != 0 or not Path(path_out).exists():
            err = proc.stderr[-800:] if proc.stderr else "ffmpeg a echoue"
            log_usage_event(request.current_user, _usage_mode,
                            source_seconds=_usage_source_seconds, output_seconds=None,
                            source_count=2, output_count=0, success=False)
            return jsonify({"error": err}), 500

        log_usage_event(request.current_user, _usage_mode,
                        source_seconds=_usage_source_seconds,
                        output_seconds=_get_video_duration_seconds(path_out),
                        source_count=2, output_count=1, success=True)

        # ── Opt-in visual positioning debug (CAPTION_VISUAL_DEBUG=1) ──
        # Saves an annotated source-B frame + rendered-C frame so the
        # detected vs. final caption boxes can be compared visually.
        # Wrapped so any failure here can never fail the actual render.
        if CAPTION_VISUAL_DEBUG and debug_capture:
            try:
                dbg, snap_t = debug_capture
                _save_caption_debug_frames(
                    bdir / "debug",
                    f"A{a_index + 1:02d}_B{b_index + 1:02d}",
                    path_b, path_out, dbg, snap_t, wb, hb, wa, ha
                )
            except Exception:
                pass

        return jsonify({"ok": True, "filename": out_name})

    except Exception as e:
        try:
            if _usage_attempt_started:
                log_usage_event(request.current_user, _usage_mode,
                                source_seconds=_usage_source_seconds, output_seconds=None,
                                source_count=2, output_count=0, success=False)
        except Exception:
            pass
        return jsonify({"error": f"Erreur serveur: {str(e)}"}), 500


@app.route("/batch_file/<batch_id>/<filename>")
def batch_file(batch_id, filename):
    """Download a single rendered combination output by its A/B-identifying filename."""
    if ".." in batch_id or "/" in batch_id or ".." in filename or "/" in filename:
        return jsonify({"error": "Invalid"}), 400
    path = BATCH_DIR / batch_id / "out" / filename
    if path.exists():
        return send_file(str(path), as_attachment=True, download_name=filename, mimetype="video/mp4")
    return jsonify({"error": "Not found"}), 404


@app.route("/batch_cleanup", methods=["POST"])
def batch_cleanup():
    """
    Delete all staged inputs and rendered outputs for a finished batch to
    free disk space (called by the client right after the ZIP has been
    produced). Safe to call more than once / on an unknown batch_id.
    """
    try:
        data = request.get_json(force=True) or {}
        batch_id = (data.get("batch_id") or "").strip()
        if not batch_id or ".." in batch_id or "/" in batch_id:
            return jsonify({"error": "batch_id invalide"}), 400
        bdir = BATCH_DIR / batch_id
        if bdir.exists():
            shutil.rmtree(bdir, ignore_errors=True)
        gc.collect()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# VARIATION MODE — routes
#
# Five new, independent routes. None of them is referenced by, calls,
# or is called from /process, /analyze, /batch_*, or any caption/Vision
# code — the only things they share with the rest of the app are
# generic, side-effect-free utilities that already existed for every
# mode (request.current_user via the global before_request gate,
# get_video_dims, _get_video_duration_seconds, log_usage_event,
# gc.collect). Each call to /variation_run produces exactly ONE output
# file and returns — the client loops over it sequentially with
# `await`, exactly like /batch_render — so at most one FFmpeg encode
# is ever in flight for this feature, by construction.
# ══════════════════════════════════════════════════════════════════

@app.route("/variation_stage", methods=["POST"])
def variation_stage():
    """
    Upload the ONE source video for a new variation job. Creates
    VARIATION_DIR/<job_id>/{in,out}/, saves the source as in/source.mp4,
    and returns job_id + probed duration/dimensions so the client can
    show a live preview and validate the variation count client-side
    too (server-side validation happens again in /variation_run).
    """
    try:
        if "file" not in request.files:
            return jsonify({"error": "Fichier manquant"}), 400

        try:
            requested_count = int(request.form.get("count", "0"))
        except Exception:
            requested_count = 0
        if requested_count > MAX_VARIATIONS:
            return jsonify({"error": "Maximum 100 variations allowed."}), 400

        _cleanup_stale_variation_jobs()
        job_id = uuid.uuid4().hex
        jdir = VARIATION_DIR / job_id
        (jdir / "in").mkdir(parents=True, exist_ok=True)
        (jdir / "out").mkdir(parents=True, exist_ok=True)

        source_filename = (request.files["file"].filename or "source.mp4").strip()
        path_in = str(jdir / "in" / "source.mp4")
        request.files["file"].save(path_in)

        duration = _get_video_duration_seconds(path_in)
        width, height = get_video_dims(path_in)

        # Persist small bits of job context the subsequent sequential
        # /variation_run calls need (source path is already on disk;
        # this tiny JSON just avoids re-probing on every single call).
        meta = {
            "source_filename": source_filename[:200],
            "duration": duration,
            "width": width,
            "height": height,
            "created_at": time.time(),
        }
        with open(jdir / "meta.json", "w", encoding="utf-8") as f:
            json.dump(meta, f)

        return jsonify({
            "job_id": job_id,
            "duration": duration,
            "width": width,
            "height": height,
            "source_filename": meta["source_filename"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/variation_run", methods=["POST"])
def variation_run():
    """
    Generate exactly ONE variant (params: job_id, index, strength,
    count — count/strength are repeated on every call so the very last
    call can log the summary usage event without needing extra state).
    Designed to be called sequentially, once per index, and awaited by
    the client before the next call — never in parallel.

    Per requirement #7: an FFmpeg failure for this ONE variant is
    reported back as a per-item error (so the client can mark that row
    failed and continue) and never crashes the route or the app — it
    is handled exactly like a single failed combo in /batch_render.
    """
    try:
        job_id = (request.form.get("job_id") or "").strip()
        if not job_id or ".." in job_id or "/" in job_id:
            return jsonify({"error": "job_id invalide"}), 400

        try:
            index = int(request.form.get("index", "-1"))
        except Exception:
            index = -1
        try:
            count = int(request.form.get("count", "0"))
        except Exception:
            count = 0
        if index < 0 or count <= 0 or count > MAX_VARIATIONS or index >= count:
            return jsonify({"error": "Paramètres de variation invalides (max 100)."}), 400

        strength = (request.form.get("strength") or VARIATION_DEFAULT_STRENGTH).strip().lower()
        if strength not in VARIATION_STRENGTH_PRESETS:
            strength = VARIATION_DEFAULT_STRENGTH

        # ── Advanced Mode (additive, opt-in) ────────────────────────
        # Only takes over when the client explicitly sends
        # config_mode=advanced WITH a parsable, non-empty advanced_config
        # JSON object. Any other value — including every request from an
        # older/cached client that has never heard of this field — falls
        # straight through to the untouched Preset Mode path below, with
        # byte-identical behavior to before this feature existed.
        config_mode = (request.form.get("config_mode") or "preset").strip().lower()
        advanced_config = None
        if config_mode == "advanced":
            try:
                _raw_cfg = json.loads(request.form.get("advanced_config") or "{}")
                if isinstance(_raw_cfg, dict) and _raw_cfg:
                    advanced_config = _raw_cfg
            except Exception:
                advanced_config = None

        jdir = VARIATION_DIR / job_id
        path_in = jdir / "in" / "source.mp4"
        if not path_in.exists():
            return jsonify({"error": "Vidéo source introuvable (étape de staging manquante)"}), 404

        try:
            with open(jdir / "meta.json", "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
        width  = int(meta.get("width")  or 0) or get_video_dims(str(path_in))[0]
        height = int(meta.get("height") or 0) or get_video_dims(str(path_in))[1]

        filename  = f"video_{index + 1:03d}.mp4"
        path_out  = jdir / "out" / filename

        # Deterministic-per-(job, index) RNG either way — same params if
        # this single index is ever retried, different from every other
        # index in the run (and from every other run — job_id is fresh).
        rng = random.Random(f"{job_id}:{index}")
        if advanced_config is not None:
            params         = _pick_advanced_variation_params(advanced_config, rng)
            metadata_level = max(0.0, min(1.0, float(advanced_config.get("metadata", ADVANCED_SLIDER_DEFAULT)) / 100.0))
            profile        = _pick_advanced_metadata_profile(metadata_level, rng)
            strength_label = "advanced"   # what gets stamped into usage_logs.variation_strength
        else:
            params         = _pick_variation_params(strength, rng)
            profile        = _pick_metadata_profile(rng)
            strength_label = strength
        cmd = _build_variation_ffmpeg_cmd(str(path_in), str(path_out), params, profile, width, height)

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Timeout (>3 min)", "index": index, "filename": filename}), 200
        finally:
            gc.collect()

        ok = (proc.returncode == 0 and path_out.exists())

        # ── Last call of the run: record exactly ONE usage_logs row for
        # the whole job (per spec — "one row per attempt", and a
        # variation run is one logical attempt that produces N outputs).
        # Counts successes by scanning out/ — never trusts client state. ──
        if index == count - 1:
            try:
                produced = sorted((jdir / "out").glob("video_*.mp4"))
                success_count = len(produced)
                src_seconds = meta.get("duration")
                log_usage_event(
                    request.current_user, "variation",
                    source_seconds=src_seconds, output_seconds=src_seconds,
                    source_count=1, output_count=success_count,
                    success=(success_count > 0),
                    claude_requests=0,
                )
                # Stamp the strength/source-filename onto that just-written
                # row (the two additive nullable columns — see
                # _migrate_usage_logs_variation_columns), AND correct
                # estimated_cost to the spec formula
                # (EST_PROCESSING_COST_PER_SECOND * source_seconds * count).
                # log_usage_event's shared cost formula multiplies by
                # source_seconds exactly once (correct for Simple/Batch,
                # which each produce one output per source) — Variation
                # Mode produces `count` outputs from one source, so its
                # true cost is `count`× that. Rather than touch the shared,
                # every-mode function (forbidden — would risk Simple/Batch
                # regressions), this follow-up UPDATE of the row
                # log_usage_event just inserted overwrites only the two
                # cost columns with the spec-correct total. Never raises.
                with _users_db() as conn:
                    src_for_cost = float(src_seconds or 0.0)
                    correct_cost = round(EST_VARIATION_COST_PER_SECOND_PER_VARIANT * src_for_cost * count, 6)
                    conn.execute(
                        """
                        UPDATE usage_logs
                        SET variation_strength = ?, source_filename = ?,
                            estimated_cost = ?, estimated_processing_cost = ?
                        WHERE id = (SELECT MAX(id) FROM usage_logs WHERE user_id = ? AND mode = 'variation')
                        """,
                        (strength_label, meta.get("source_filename"),
                         correct_cost, correct_cost,
                         request.current_user["id"]),
                    )
                    conn.commit()
            except Exception as e:
                print(f"[variation] WARNING: failed to log usage for job={job_id}: {e}")

        if not ok:
            err = (proc.stderr or "ffmpeg a échoué")[-500:]
            try:
                path_out.unlink(missing_ok=True)
            except Exception:
                pass
            return jsonify({"error": err, "index": index, "filename": filename}), 200

        return jsonify({"index": index, "filename": filename, "ok": True})
    except Exception as e:
        return jsonify({"error": str(e), "index": request.form.get("index")}), 200


# ── Advanced Mode preset storage — three new, fully isolated routes ──
# Operate ONLY on the brand-new variation_presets table (scoped by
# user_id, exactly like every per-user query elsewhere in the app).
# They never read or write users, usage_logs, or any generation-pipeline
# state — saving/loading a preset cannot affect Simple, Batch, or any
# in-flight variation job.

@app.route("/variation_preset_save", methods=["POST"])
def variation_preset_save():
    """Save (or overwrite-by-name, for this user) one Advanced Mode
    slider configuration. Body: {"name": "...", "config": {slider: 0-100, ...}}.
    Every value is sanitized to a known slider key clamped to 0-100
    before being stored as JSON text — a malformed/garbage config can
    never be persisted or replayed."""
    try:
        data = request.get_json(force=True) or {}
        name = (data.get("name") or "").strip()[:80]
        config = data.get("config")
        if not name:
            return jsonify({"error": "Nom de preset requis"}), 400
        if not isinstance(config, dict) or not config:
            return jsonify({"error": "Configuration invalide"}), 400

        clean = {}
        for k in ADVANCED_SLIDER_KEYS:
            try:
                clean[k] = max(0, min(100, int(round(float(config.get(k, ADVANCED_SLIDER_DEFAULT))))))
            except Exception:
                clean[k] = ADVANCED_SLIDER_DEFAULT

        with _users_db() as conn:
            conn.execute(
                "DELETE FROM variation_presets WHERE user_id = ? AND name = ?",
                (request.current_user["id"], name),
            )
            conn.execute(
                "INSERT INTO variation_presets (user_id, name, config_json, created_at) VALUES (?, ?, ?, ?)",
                (request.current_user["id"], name, json.dumps(clean),
                 time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())),
            )
            conn.commit()
        return jsonify({"ok": True, "name": name, "config": clean})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/variation_presets", methods=["GET"])
def variation_presets_list():
    """List the current user's saved Advanced Mode presets (newest
    first) — scoped strictly to user_id, mirrors every other per-user
    listing query in the app (e.g. get_user_usage_summary)."""
    try:
        with _users_db() as conn:
            rows = conn.execute(
                "SELECT id, name, config_json, created_at FROM variation_presets "
                "WHERE user_id = ? ORDER BY id DESC",
                (request.current_user["id"],),
            ).fetchall()
        out = []
        for r in rows:
            try:
                cfg = json.loads(r["config_json"])
            except Exception:
                cfg = {}
            out.append({"id": r["id"], "name": r["name"], "config": cfg, "created_at": r["created_at"]})
        return jsonify({"presets": out})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/variation_preset_delete", methods=["POST"])
def variation_preset_delete():
    """Delete one of the current user's saved presets by id. The
    WHERE clause requires BOTH id AND user_id = ? — a user can never
    delete (or even address) another user's preset row."""
    try:
        data = request.get_json(force=True) or {}
        try:
            preset_id = int(data.get("id"))
        except Exception:
            return jsonify({"error": "id invalide"}), 400
        with _users_db() as conn:
            conn.execute(
                "DELETE FROM variation_presets WHERE id = ? AND user_id = ?",
                (preset_id, request.current_user["id"]),
            )
            conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/variation_zip", methods=["POST"])
def variation_zip():
    """
    Zips every successfully-generated variant for a job. Per requirement
    #8, this only ever looks at files that actually exist in out/ —
    failed variants were never written (or were removed) by
    /variation_run, so the ZIP can only ever contain successes.
    """
    try:
        data = request.get_json(force=True) or {}
        job_id = (data.get("job_id") or "").strip()
        if not job_id or ".." in job_id or "/" in job_id:
            return jsonify({"error": "job_id invalide"}), 400

        out_dir = VARIATION_DIR / job_id / "out"
        files = sorted(out_dir.glob("video_*.mp4")) if out_dir.exists() else []
        if not files:
            return jsonify({"error": "Aucun fichier valide trouvé"}), 400

        zip_path = f"/tmp/variations_{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for p in files:
                zf.write(str(p), p.name)

        return send_file(
            zip_path,
            as_attachment=True,
            download_name="variations.zip",
            mimetype="application/zip"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/variation_file/<job_id>/<filename>")
def variation_file(job_id, filename):
    """Download a single generated variant by filename."""
    if ".." in job_id or "/" in job_id or ".." in filename or "/" in filename:
        return jsonify({"error": "Invalid"}), 400
    path = VARIATION_DIR / job_id / "out" / filename
    if path.exists():
        return send_file(str(path), as_attachment=True, download_name=filename, mimetype="video/mp4")
    return jsonify({"error": "Not found"}), 404


@app.route("/variation_cleanup", methods=["POST"])
def variation_cleanup():
    """Delete all staged input/output for a finished variation job, mirrors batch_cleanup."""
    try:
        data = request.get_json(force=True) or {}
        job_id = (data.get("job_id") or "").strip()
        if not job_id or ".." in job_id or "/" in job_id:
            return jsonify({"error": "job_id invalide"}), 400
        jdir = VARIATION_DIR / job_id
        if jdir.exists():
            shutil.rmtree(jdir, ignore_errors=True)
        gc.collect()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# VARIATION STUDIO MULTI — fully isolated, additive (multi-file).
#
# Processes 1..N uploaded videos, each producing its own set of variants,
# in a SINGLE logical operation. It REUSES the existing, unchanged
# Variation Studio engine end-to-end: each video is staged with the
# untouched /variation_stage (one VARIATION_DIR/<job_id> per video) and
# every variant runs through /variation_multi_run, which is a byte-for-
# byte clone of /variation_run's generation path (same pure helpers:
# _pick_variation_params, _pick_advanced_variation_params,
# _pick_metadata_profile, _pick_advanced_metadata_profile,
# _build_variation_ffmpeg_cmd) — the ONLY difference is the analytics
# mode string it logs ("variation_multi" instead of "variation"), so the
# two can be told apart in Consommation/Historique. Nothing here touches
# Mode Batch, /batch_render, /process, /analyze, captions/OCR, the
# A+B→C pipeline, or the existing Variation Studio routes/JS. The new
# /variation_multi_zip builds ONE structured ZIP (per-video subfolders).
# Cleanup reuses the existing /variation_cleanup (one call per job_id).
# ══════════════════════════════════════════════════════════════════

MAX_MULTI_VIDEOS = 100                      # max source videos per multi run
MAX_MULTI_TOTAL  = 300                      # hard cap: videos × variants-per-video


@app.route("/variation_multi_run", methods=["POST"])
def variation_multi_run():
    """
    Generate exactly ONE variant of ONE staged video, identical in every
    way to /variation_run EXCEPT it logs usage under mode
    "variation_multi". Same params (job_id, index, count, strength,
    config_mode, advanced_config); same deterministic per-(job,index)
    RNG; same hard 100-cap per video; same per-item-error contract.
    Additionally enforces the multi TOTAL cap (num_videos × count ≤
    MAX_MULTI_TOTAL) server-side, independent of the client.
    """
    try:
        job_id = (request.form.get("job_id") or "").strip()
        if not job_id or ".." in job_id or "/" in job_id:
            return jsonify({"error": "job_id invalide"}), 400

        try:
            index = int(request.form.get("index", "-1"))
        except Exception:
            index = -1
        try:
            count = int(request.form.get("count", "0"))
        except Exception:
            count = 0
        if index < 0 or count <= 0 or count > MAX_VARIATIONS or index >= count:
            return jsonify({"error": "Paramètres de variation invalides (max 100)."}), 400

        # ── Server-side TOTAL cap (videos × variants-per-video) ──
        # The client sends num_videos on every call so the server can
        # independently enforce the same hard limit shown in the UI —
        # never trusting the client. 100 videos × 3 = 300 is allowed;
        # 10 × 31 = 310 is rejected here regardless of what the client did.
        try:
            num_videos = int(request.form.get("num_videos", "1"))
        except Exception:
            num_videos = 1
        if num_videos < 1 or num_videos > MAX_MULTI_VIDEOS:
            return jsonify({"error": f"Nombre de vidéos invalide (1..{MAX_MULTI_VIDEOS})."}), 400
        if num_videos * count > MAX_MULTI_TOTAL:
            return jsonify({"error": f"Total {num_videos}×{count} dépasse la limite de {MAX_MULTI_TOTAL} variantes."}), 400

        strength = (request.form.get("strength") or VARIATION_DEFAULT_STRENGTH).strip().lower()
        if strength not in VARIATION_STRENGTH_PRESETS:
            strength = VARIATION_DEFAULT_STRENGTH

        config_mode = (request.form.get("config_mode") or "preset").strip().lower()
        advanced_config = None
        if config_mode == "advanced":
            try:
                _raw_cfg = json.loads(request.form.get("advanced_config") or "{}")
                if isinstance(_raw_cfg, dict) and _raw_cfg:
                    advanced_config = _raw_cfg
            except Exception:
                advanced_config = None

        jdir = VARIATION_DIR / job_id
        path_in = jdir / "in" / "source.mp4"
        if not path_in.exists():
            return jsonify({"error": "Vidéo source introuvable (étape de staging manquante)"}), 404

        try:
            with open(jdir / "meta.json", "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
        width  = int(meta.get("width")  or 0) or get_video_dims(str(path_in))[0]
        height = int(meta.get("height") or 0) or get_video_dims(str(path_in))[1]

        filename  = f"video_{index + 1:03d}.mp4"
        path_out  = jdir / "out" / filename

        rng = random.Random(f"{job_id}:{index}")
        if advanced_config is not None:
            params         = _pick_advanced_variation_params(advanced_config, rng)
            metadata_level = max(0.0, min(1.0, float(advanced_config.get("metadata", ADVANCED_SLIDER_DEFAULT)) / 100.0))
            profile        = _pick_advanced_metadata_profile(metadata_level, rng)
            strength_label = "advanced"
        else:
            params         = _pick_variation_params(strength, rng)
            profile        = _pick_metadata_profile(rng)
            strength_label = strength
        cmd = _build_variation_ffmpeg_cmd(str(path_in), str(path_out), params, profile, width, height)

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Timeout (>3 min)", "index": index, "filename": filename}), 200
        finally:
            gc.collect()

        ok = (proc.returncode == 0 and path_out.exists())

        # One usage_logs row per video (at its last index), mode
        # "variation_multi" — mirrors /variation_run's per-job logging but
        # with the distinct analytics mode. Never raises.
        if index == count - 1:
            try:
                produced = sorted((jdir / "out").glob("video_*.mp4"))
                success_count = len(produced)
                src_seconds = meta.get("duration")
                log_usage_event(
                    request.current_user, "variation_multi",
                    source_seconds=src_seconds, output_seconds=src_seconds,
                    source_count=1, output_count=success_count,
                    success=(success_count > 0),
                    claude_requests=0,
                )
                with _users_db() as conn:
                    src_for_cost = float(src_seconds or 0.0)
                    correct_cost = round(EST_VARIATION_COST_PER_SECOND_PER_VARIANT * src_for_cost * count, 6)
                    conn.execute(
                        """
                        UPDATE usage_logs
                        SET variation_strength = ?, source_filename = ?,
                            estimated_cost = ?, estimated_processing_cost = ?
                        WHERE id = (SELECT MAX(id) FROM usage_logs WHERE user_id = ? AND mode = 'variation_multi')
                        """,
                        (strength_label, meta.get("source_filename"),
                         correct_cost, correct_cost,
                         request.current_user["id"]),
                    )
                    conn.commit()
            except Exception as e:
                print(f"[variation_multi] WARNING: failed to log usage for job={job_id}: {e}")

        if not ok:
            err = (proc.stderr or "ffmpeg a échoué")[-500:]
            try:
                path_out.unlink(missing_ok=True)
            except Exception:
                pass
            return jsonify({"error": err, "index": index, "filename": filename}), 200

        return jsonify({"index": index, "filename": filename, "ok": True})
    except Exception as e:
        return jsonify({"error": str(e), "index": request.form.get("index")}), 200


@app.route("/variation_multi_zip", methods=["POST"])
def variation_multi_zip():
    """
    Build ONE structured ZIP from several finished per-video jobs. Body:
    {"items": [{"job_id": "...", "label": "video1"}, ...]}. Each job's
    out/video_*.mp4 is written under its own sanitized subfolder
    (label/video_001.mp4). Only files that actually exist are zipped, so
    failed variants are simply absent. Job ids and labels are sanitized
    against path traversal.
    """
    try:
        data = request.get_json(force=True) or {}
        items = data.get("items") or []
        if not isinstance(items, list) or not items:
            return jsonify({"error": "Aucune vidéo fournie"}), 400

        zip_path = f"/tmp/variations_multi_{uuid.uuid4().hex}.zip"
        used_labels = set()
        total_files = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for i, it in enumerate(items):
                if not isinstance(it, dict):
                    continue
                job_id = (it.get("job_id") or "").strip()
                if not job_id or ".." in job_id or "/" in job_id:
                    continue
                # Sanitize the folder label; fall back to videoN; de-dupe.
                raw_label = (it.get("label") or "").strip() or f"video{i + 1}"
                label = "".join(c for c in raw_label if c.isalnum() or c in (" ", "-", "_")).strip()
                label = label[:60] or f"video{i + 1}"
                base_label = label
                n = 2
                while label in used_labels:
                    label = f"{base_label}_{n}"; n += 1
                used_labels.add(label)

                out_dir = VARIATION_DIR / job_id / "out"
                files = sorted(out_dir.glob("video_*.mp4")) if out_dir.exists() else []
                for p in files:
                    zf.write(str(p), f"{label}/{p.name}")
                    total_files += 1

        if total_files == 0:
            try:
                os.remove(zip_path)
            except Exception:
                pass
            return jsonify({"error": "Aucun fichier valide trouvé"}), 400

        return send_file(
            zip_path,
            as_attachment=True,
            download_name="variations_multi.zip",
            mimetype="application/zip"
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# AUDIO CONVERTER — fully isolated, additive (audio-only).
#
# Bulk-converts WhatsApp/voice audio files (.opus/.ogg/.m4a/.aac/.wav/
# .mp3) to .mp3 via FFmpeg's libmp3lame. A COMPLETELY separate pipeline:
# its own /tmp dir, its own 5 routes, its own cleanup. It NEVER calls or
# is called by /process, /analyze, /batch_*, /variation_*, captions, OCR,
# Claude Vision, or any video pipeline. No AI, no credits. The only shared
# utilities are generic, side-effect-free ones (request.current_user via
# the global gate, log_usage_event, gc.collect) used by every mode.
# Logged under the distinct analytics mode "audio_converter".
# ══════════════════════════════════════════════════════════════════

AUDIO_DIR = Path("/tmp/videobot_audio")
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

MAX_AUDIO_FILES       = 100                       # max files per conversion job
MAX_AUDIO_TOTAL_BYTES = 500 * 1024 * 1024         # 500 MB total per job
AUDIO_ALLOWED_EXT     = {".opus", ".ogg", ".m4a", ".aac", ".wav", ".mp3"}


def _cleanup_stale_audio_jobs(max_age_hours: float = 3.0):
    """Opportunistic disk-space safety net: remove audio job dirs left by
    earlier sessions. Runs only when a brand-new audio job is staged."""
    try:
        cutoff = time.time() - max_age_hours * 3600
        for d in AUDIO_DIR.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass


def _audio_sanitize_name(name: str) -> str:
    """Keep a readable, path-safe basename (no dirs, no traversal)."""
    base = os.path.basename(name or "")
    base = base.replace("..", "").replace("/", "").replace("\\", "")
    cleaned = "".join(c for c in base if c.isalnum() or c in (" ", ".", "-", "_", "(", ")")).strip()
    return cleaned[:120]


@app.route("/audio_stage", methods=["POST"])
def audio_stage():
    """
    Upload ONE audio file into a conversion job. The first call (no
    job_id) creates a fresh job; later calls reuse it. Enforces the
    allowed-extension set, the per-job file count (≤100) and the 500 MB
    total cap, server-side. Returns job_id + the stored input filename.
    """
    try:
        if "file" not in request.files:
            return jsonify({"error": "Fichier manquant"}), 400

        f = request.files["file"]
        orig = f.filename or "audio"
        ext = os.path.splitext(orig)[1].lower()
        if ext not in AUDIO_ALLOWED_EXT:
            return jsonify({"error": f"Format non supporté ({ext or 'inconnu'})"}), 400

        job_id = (request.form.get("job_id") or "").strip()
        if job_id and (".." in job_id or "/" in job_id):
            return jsonify({"error": "job_id invalide"}), 400
        if not job_id:
            _cleanup_stale_audio_jobs()
            job_id = uuid.uuid4().hex
        jdir = AUDIO_DIR / job_id
        in_dir = jdir / "in"
        in_dir.mkdir(parents=True, exist_ok=True)
        (jdir / "out").mkdir(parents=True, exist_ok=True)

        existing = sorted(in_dir.glob("*"))
        if len(existing) >= MAX_AUDIO_FILES:
            return jsonify({"error": f"Maximum {MAX_AUDIO_FILES} fichiers."}), 400

        idx = len(existing)
        stored_name = f"{idx:03d}_{_audio_sanitize_name(orig)}"
        path_in = in_dir / stored_name
        f.save(str(path_in))

        # Enforce the cumulative 500 MB cap (sum of all staged inputs).
        try:
            total = sum(p.stat().st_size for p in in_dir.glob("*") if p.is_file())
        except Exception:
            total = 0
        if total > MAX_AUDIO_TOTAL_BYTES:
            try:
                path_in.unlink(missing_ok=True)
            except Exception:
                pass
            return jsonify({"error": "Taille totale dépasse 500 Mo."}), 400

        return jsonify({
            "job_id": job_id,
            "stored_name": stored_name,
            "original_name": orig[:160],
            "size": path_in.stat().st_size,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/audio_convert", methods=["POST"])
def audio_convert():
    """
    Convert ONE staged audio file to MP3 (libmp3lame -q:a 2). Designed to
    be called sequentially, once per file, and awaited by the client.
    Params: job_id, stored_name (input), out_name (desired mp3 base),
    index, count. An FFmpeg failure for this ONE file is a per-item error
    (HTTP 200), never a crash. The last call (index==count-1) logs ONE
    usage_logs row under mode "audio_converter" (claude_requests=0).
    """
    try:
        job_id = (request.form.get("job_id") or "").strip()
        if not job_id or ".." in job_id or "/" in job_id:
            return jsonify({"error": "job_id invalide"}), 400
        stored_name = _audio_sanitize_name(request.form.get("stored_name") or "")
        if not stored_name:
            return jsonify({"error": "stored_name invalide"}), 400

        try:
            index = int(request.form.get("index", "-1"))
        except Exception:
            index = -1
        try:
            count = int(request.form.get("count", "0"))
        except Exception:
            count = 0

        jdir = AUDIO_DIR / job_id
        path_in = jdir / "in" / stored_name
        if not path_in.exists():
            return jsonify({"error": "Fichier source introuvable"}), 404

        out_dir = jdir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Output name: keep the original basename where possible; ensure
        # .mp3; de-dupe against existing outputs; fallback audioN.mp3.
        base = _audio_sanitize_name(request.form.get("out_name") or os.path.splitext(stored_name)[0])
        base = os.path.splitext(base)[0] or f"audio{index + 1}"
        out_name = f"{base}.mp3"
        n = 2
        while (out_dir / out_name).exists():
            out_name = f"{base}_{n}.mp3"; n += 1
        path_out = out_dir / out_name

        cmd = ["ffmpeg", "-y", "-i", str(path_in),
               "-vn", "-codec:a", "libmp3lame", "-q:a", "2",
               "-loglevel", "error", str(path_out)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Timeout (>3 min)", "index": index}), 200
        finally:
            gc.collect()

        ok = (proc.returncode == 0 and path_out.exists())

        if index == count - 1:
            try:
                produced = [p for p in out_dir.glob("*.mp3")]
                success_count = len(produced)
                log_usage_event(
                    request.current_user, "audio_converter",
                    source_seconds=None, output_seconds=None,
                    source_count=count, output_count=success_count,
                    success=(success_count > 0),
                    claude_requests=0,
                )
            except Exception as e:
                print(f"[audio_converter] WARNING: usage log failed job={job_id}: {e}")

        if not ok:
            err = (proc.stderr or "ffmpeg a échoué")[-500:]
            try:
                path_out.unlink(missing_ok=True)
            except Exception:
                pass
            return jsonify({"error": err, "index": index}), 200

        return jsonify({"index": index, "filename": out_name, "ok": True})
    except Exception as e:
        return jsonify({"error": str(e), "index": request.form.get("index")}), 200


@app.route("/audio_zip", methods=["POST"])
def audio_zip():
    """Zip every produced MP3 for a job (flat, no subfolders)."""
    try:
        data = request.get_json(force=True) or {}
        job_id = (data.get("job_id") or "").strip()
        if not job_id or ".." in job_id or "/" in job_id:
            return jsonify({"error": "job_id invalide"}), 400
        out_dir = AUDIO_DIR / job_id / "out"
        files = sorted(out_dir.glob("*.mp3")) if out_dir.exists() else []
        if not files:
            return jsonify({"error": "Aucun fichier valide trouvé"}), 400
        zip_path = f"/tmp/audio_{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for p in files:
                zf.write(str(p), p.name)
        return send_file(zip_path, as_attachment=True, download_name="audio_mp3.zip", mimetype="application/zip")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/audio_file/<job_id>/<filename>")
def audio_file(job_id, filename):
    """Download a single converted MP3."""
    if ".." in job_id or "/" in job_id or ".." in filename or "/" in filename:
        return jsonify({"error": "Invalid"}), 400
    path = AUDIO_DIR / job_id / "out" / filename
    if path.exists():
        return send_file(str(path), as_attachment=True, download_name=filename, mimetype="audio/mpeg")
    return jsonify({"error": "Not found"}), 404


@app.route("/audio_cleanup", methods=["POST"])
def audio_cleanup():
    """Delete all staged inputs/outputs for a finished audio job."""
    try:
        data = request.get_json(force=True) or {}
        job_id = (data.get("job_id") or "").strip()
        if not job_id or ".." in job_id or "/" in job_id:
            return jsonify({"error": "job_id invalide"}), 400
        jdir = AUDIO_DIR / job_id
        if jdir.exists():
            shutil.rmtree(jdir, ignore_errors=True)
        gc.collect()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════════
# PHOTO METADATA STUDIO — fully isolated, additive (photo only).
#
# Cleans / rewrites image metadata (EXIF) for up to 300 photos
# (jpg/jpeg/png/webp) using Pillow ONLY — no new dependency, no
# Dockerfile change, no AI, no credits. A COMPLETELY separate pipeline:
# its own /tmp dir, its own 5 routes, its own cleanup. It NEVER calls or
# is called by any video/audio route, captions, OCR, or Claude Vision.
# Logged under the distinct analytics mode "photo_metadata".
# ══════════════════════════════════════════════════════════════════

PHOTO_DIR = Path("/tmp/videobot_photos")
PHOTO_DIR.mkdir(parents=True, exist_ok=True)

MAX_PHOTO_FILES       = 300                       # max photos per job
MAX_PHOTO_TOTAL_BYTES = 500 * 1024 * 1024         # 500 MB total per job
PHOTO_ALLOWED_EXT     = {".jpg", ".jpeg", ".png", ".webp"}
PHOTO_RECOMPRESS_QUALITY = 90                     # used only when recompress is on

# EXIF tag ids (TIFF/EXIF standard) used by the rewrite/strip logic.
_EXIF_SOFTWARE   = 0x0131
_EXIF_ARTIST     = 0x013B
_EXIF_COPYRIGHT  = 0x8298
_EXIF_DATETIME   = 0x0132   # IFD0 DateTime (modification)
_EXIF_GPS_IFD    = 0x8825   # GPSInfo pointer
_EXIF_DT_ORIG    = 0x9003   # DateTimeOriginal (Exif IFD)
_EXIF_DT_DIGIT   = 0x9004   # DateTimeDigitized (Exif IFD)


def _cleanup_stale_photo_jobs(max_age_hours: float = 3.0):
    """Remove photo job dirs left by earlier sessions. Runs on new stage."""
    try:
        cutoff = time.time() - max_age_hours * 3600
        for d in PHOTO_DIR.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    except Exception:
        pass


def _photo_sanitize_name(name: str) -> str:
    base = os.path.basename(name or "")
    base = base.replace("..", "").replace("/", "").replace("\\", "")
    cleaned = "".join(c for c in base if c.isalnum() or c in (" ", ".", "-", "_", "(", ")")).strip()
    return cleaned[:120]


def _process_one_photo(path_in: str, path_out: str, opts: dict) -> None:
    """
    Apply the requested metadata operations to ONE image with Pillow only,
    keeping pixels visually identical. Raises on failure (caller handles).

    opts keys (all booleans unless noted):
      strip_all, strip_gps, rewrite_dates, rewrite_software, rewrite_author,
      recompress, plus string values: software, author, copyright, datetime.
    """
    from PIL import Image

    img = Image.open(path_in)
    fmt = (img.format or "").upper()
    ext = os.path.splitext(path_out)[1].lower()

    # Decide the output EXIF block.
    if opts.get("strip_all"):
        exif = None  # drop everything
    else:
        exif = img.getexif()
        if opts.get("strip_gps") and _EXIF_GPS_IFD in exif:
            try:
                del exif[_EXIF_GPS_IFD]
            except Exception:
                pass
        if opts.get("rewrite_software"):
            exif[_EXIF_SOFTWARE] = str(opts.get("software", "") or "")
        if opts.get("rewrite_author"):
            exif[_EXIF_ARTIST] = str(opts.get("author", "") or "")
            exif[_EXIF_COPYRIGHT] = str(opts.get("copyright", "") or "")
        if opts.get("rewrite_dates"):
            dt = str(opts.get("datetime", "") or "")
            exif[_EXIF_DATETIME] = dt
            try:
                exif_ifd = exif.get_ifd(0x8769)  # Exif sub-IFD
                exif_ifd[_EXIF_DT_ORIG] = dt
                exif_ifd[_EXIF_DT_DIGIT] = dt
            except Exception:
                pass

    # Build save kwargs. Recompression only when explicitly requested;
    # otherwise keep the most visually-faithful save for the format.
    save_kwargs = {}
    out_fmt = "JPEG" if ext in (".jpg", ".jpeg") else ("PNG" if ext == ".png" else "WEBP")
    if out_fmt == "JPEG":
        save_kwargs["quality"] = PHOTO_RECOMPRESS_QUALITY if opts.get("recompress") else "keep"
        save_kwargs["subsampling"] = "keep" if not opts.get("recompress") else 2
    elif out_fmt == "WEBP":
        if opts.get("recompress"):
            save_kwargs["quality"] = PHOTO_RECOMPRESS_QUALITY
        else:
            save_kwargs["lossless"] = True   # metadata-only: keep pixels exact
    elif out_fmt == "PNG":
        save_kwargs["optimize"] = bool(opts.get("recompress"))  # PNG is lossless either way

    save_img = img
    # JPEG cannot carry alpha; convert if needed (rare for jpg sources).
    if out_fmt == "JPEG" and img.mode in ("RGBA", "P", "LA"):
        save_img = img.convert("RGB")

    if exif is not None:
        try:
            save_kwargs["exif"] = exif.tobytes()
        except Exception:
            # If a particular EXIF block can't be serialized, fall back to
            # stripping it rather than failing the whole conversion.
            save_kwargs.pop("exif", None)

    # "quality='keep'" is only valid for JPEG re-encode of a JPEG source;
    # guard against it on a non-JPEG source.
    if out_fmt == "JPEG" and save_kwargs.get("quality") == "keep" and fmt != "JPEG":
        save_kwargs["quality"] = 95
        save_kwargs.pop("subsampling", None)

    save_img.save(path_out, out_fmt, **save_kwargs)

    # Optionally align the output file's mtime/atime with the rewritten
    # EXIF date (best-effort, never fatal).
    if opts.get("rewrite_dates") and not opts.get("strip_all"):
        try:
            ts = time.mktime(time.strptime(str(opts.get("datetime", "")), "%Y:%m:%d %H:%M:%S"))
            os.utime(path_out, (ts, ts))
        except Exception:
            pass


@app.route("/photo_stage", methods=["POST"])
def photo_stage():
    """
    Upload ONE photo into a job. First call (no job_id) creates a fresh
    job; later calls reuse it. Enforces allowed extensions, the 300-file
    count and the 500 MB total cap, server-side. Returns job_id + stored
    input filename.
    """
    try:
        if "file" not in request.files:
            return jsonify({"error": "Fichier manquant"}), 400
        f = request.files["file"]
        orig = f.filename or "photo"
        ext = os.path.splitext(orig)[1].lower()
        if ext not in PHOTO_ALLOWED_EXT:
            return jsonify({"error": f"Format non supporté ({ext or 'inconnu'})"}), 400

        job_id = (request.form.get("job_id") or "").strip()
        if job_id and (".." in job_id or "/" in job_id):
            return jsonify({"error": "job_id invalide"}), 400
        if not job_id:
            _cleanup_stale_photo_jobs()
            job_id = uuid.uuid4().hex
        jdir = PHOTO_DIR / job_id
        in_dir = jdir / "in"
        in_dir.mkdir(parents=True, exist_ok=True)
        (jdir / "out").mkdir(parents=True, exist_ok=True)

        existing = sorted(in_dir.glob("*"))
        if len(existing) >= MAX_PHOTO_FILES:
            return jsonify({"error": f"Maximum {MAX_PHOTO_FILES} photos."}), 400

        idx = len(existing)
        stored_name = f"{idx:03d}_{_photo_sanitize_name(orig)}"
        path_in = in_dir / stored_name
        f.save(str(path_in))

        try:
            total = sum(p.stat().st_size for p in in_dir.glob("*") if p.is_file())
        except Exception:
            total = 0
        if total > MAX_PHOTO_TOTAL_BYTES:
            try:
                path_in.unlink(missing_ok=True)
            except Exception:
                pass
            return jsonify({"error": "Taille totale dépasse 500 Mo."}), 400

        return jsonify({
            "job_id": job_id,
            "stored_name": stored_name,
            "original_name": orig[:160],
            "size": path_in.stat().st_size,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/photo_process", methods=["POST"])
def photo_process():
    """
    Process ONE staged photo (Pillow EXIF ops). Sequential, one per call.
    Params: job_id, stored_name, out_name, index, count, and the option
    flags (mode + toggles + rewrite values). Per-item error → HTTP 200.
    Logs one usage_logs row under mode "photo_metadata" at the last call.
    """
    try:
        job_id = (request.form.get("job_id") or "").strip()
        if not job_id or ".." in job_id or "/" in job_id:
            return jsonify({"error": "job_id invalide"}), 400
        stored_name = _photo_sanitize_name(request.form.get("stored_name") or "")
        if not stored_name:
            return jsonify({"error": "stored_name invalide"}), 400

        try:
            index = int(request.form.get("index", "-1"))
        except Exception:
            index = -1
        try:
            count = int(request.form.get("count", "0"))
        except Exception:
            count = 0

        def _b(key):
            return (request.form.get(key, "0") or "0").strip().lower() in ("1", "true", "on", "yes")

        opts = {
            "strip_all":        _b("strip_all"),
            "strip_gps":        _b("strip_gps"),
            "rewrite_dates":    _b("rewrite_dates"),
            "rewrite_software": _b("rewrite_software"),
            "rewrite_author":   _b("rewrite_author"),
            "recompress":       _b("recompress"),
            "software":         (request.form.get("software") or "")[:120],
            "author":           (request.form.get("author") or "")[:120],
            "copyright":        (request.form.get("copyright") or "")[:160],
            "datetime":         (request.form.get("datetime") or "")[:32],
        }

        jdir = PHOTO_DIR / job_id
        path_in = jdir / "in" / stored_name
        if not path_in.exists():
            return jsonify({"error": "Fichier source introuvable"}), 404
        out_dir = jdir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        in_ext = os.path.splitext(stored_name)[1].lower()
        if in_ext not in PHOTO_ALLOWED_EXT:
            in_ext = ".jpg"
        base = _photo_sanitize_name(request.form.get("out_name") or os.path.splitext(stored_name)[0])
        base = os.path.splitext(base)[0] or f"photo{index + 1}"
        out_name = f"{base}{in_ext}"
        n = 2
        while (out_dir / out_name).exists():
            out_name = f"{base}_{n}{in_ext}"; n += 1
        path_out = out_dir / out_name

        ok = True
        err = ""
        try:
            _process_one_photo(str(path_in), str(path_out), opts)
            ok = path_out.exists()
        except Exception as e:
            ok = False
            err = str(e)[-300:]
        finally:
            gc.collect()

        if index == count - 1:
            try:
                produced = [p for p in out_dir.glob("*") if p.is_file()]
                success_count = len(produced)
                log_usage_event(
                    request.current_user, "photo_metadata",
                    source_seconds=None, output_seconds=None,
                    source_count=count, output_count=success_count,
                    success=(success_count > 0),
                    claude_requests=0,
                )
            except Exception as e:
                print(f"[photo_metadata] WARNING: usage log failed job={job_id}: {e}")

        if not ok:
            try:
                path_out.unlink(missing_ok=True)
            except Exception:
                pass
            return jsonify({"error": err or "Échec du traitement", "index": index}), 200

        return jsonify({"index": index, "filename": out_name, "ok": True})
    except Exception as e:
        return jsonify({"error": str(e), "index": request.form.get("index")}), 200


@app.route("/photo_zip", methods=["POST"])
def photo_zip():
    """Zip every processed photo for a job (flat, no subfolders)."""
    try:
        data = request.get_json(force=True) or {}
        job_id = (data.get("job_id") or "").strip()
        if not job_id or ".." in job_id or "/" in job_id:
            return jsonify({"error": "job_id invalide"}), 400
        out_dir = PHOTO_DIR / job_id / "out"
        files = sorted(p for p in out_dir.glob("*") if p.is_file()) if out_dir.exists() else []
        if not files:
            return jsonify({"error": "Aucun fichier valide trouvé"}), 400
        zip_path = f"/tmp/photos_{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            for p in files:
                zf.write(str(p), p.name)
        return send_file(zip_path, as_attachment=True, download_name="photos.zip", mimetype="application/zip")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/photo_file/<job_id>/<filename>")
def photo_file(job_id, filename):
    """Download a single processed photo."""
    if ".." in job_id or "/" in job_id or ".." in filename or "/" in filename:
        return jsonify({"error": "Invalid"}), 400
    path = PHOTO_DIR / job_id / "out" / filename
    if path.exists():
        return send_file(str(path), as_attachment=True, download_name=filename)
    return jsonify({"error": "Not found"}), 404


@app.route("/photo_cleanup", methods=["POST"])
def photo_cleanup():
    """Delete all staged inputs/outputs for a finished photo job."""
    try:
        data = request.get_json(force=True) or {}
        job_id = (data.get("job_id") or "").strip()
        if not job_id or ".." in job_id or "/" in job_id:
            return jsonify({"error": "job_id invalide"}), 400
        jdir = PHOTO_DIR / job_id
        if jdir.exists():
            shutil.rmtree(jdir, ignore_errors=True)
        gc.collect()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
