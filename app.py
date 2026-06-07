import os
import gc
import time
import uuid
import json
import shutil
import base64
import zipfile
import subprocess
from pathlib import Path
from flask import Flask, render_template, request, send_file, jsonify

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024
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

MAX_BATCH_FILES  = 10   # max source (A) videos AND max target (B) videos
MAX_BATCH_COMBOS = 100  # MAX_BATCH_FILES × MAX_BATCH_FILES


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
# to video B. Bumped +15% (0.644 * 1.15 ≈ 0.741). This is purely a font-size
# multiplier; it does not touch font family, weight, stroke, alignment,
# line spacing or text position.
CAPTION_SIZE_SCALE = 0.741

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

Detect EVERY text element added as a caption or overlay — NOT text on clothing, objects, or the scene itself.

Return a JSON array. Each visually distinct text block = one separate object.

For EACH object:
- "text": exact text with ALL emojis. CRITICAL: if the text spans multiple visual lines, use \\n between each line exactly as displayed. Never merge separate visual lines into one.
- "cx_pct": CENTER x as decimal fraction of frame width (0=left, 1=right, 0.5=center)
- "cy_pct": CENTER y as decimal fraction of frame height (0=top, 1=bottom)
- "width_pct": width of the text block as fraction of frame width (how wide the text spans, 0.3–0.9)
- "fontsize_pct": font height as fraction of frame height. Typical TikTok captions: 0.030–0.055. Large title text: 0.055–0.075.
- "align": "left" | "center" | "right"
- "bold": true | false
- "color": "white" | "black"

CRITICAL RULES:
1. Blocks at DIFFERENT vertical positions = DIFFERENT JSON objects, even if all centered.
2. Multi-line text = use \\n for EVERY visual line break. Example: "me when I realize I'm\\nlosing the argument"
3. fontsize_pct must reflect actual visible font size — do not underestimate. Large text in the frame should be 0.05–0.075.
4. width_pct: estimate how wide the text block is (e.g. 0.75 if it spans 75% of frame width).
5. EMOJIS: Do not remove, replace, normalize or describe emojis. If an emoji appears on screen, return the exact Unicode emoji. Preserve emojis exactly as visible. Examples: ❤️, ⭐, 😈, 😴, 👉, 👌, 🥺, 😂, 😭 must be returned as Unicode characters. Never omit emojis.
6. Return ONLY a valid JSON array. No markdown, no explanation.

Example:
[
  {"text": "me when I realize I'm\\nlosing the argument", "cx_pct": 0.5, "cy_pct": 0.82, "width_pct": 0.80, "fontsize_pct": 0.048, "align": "center", "bold": true, "color": "white"},
  {"text": "volume up ❗❗", "cx_pct": 0.5, "cy_pct": 0.22, "width_pct": 0.65, "fontsize_pct": 0.058, "align": "center", "bold": true, "color": "white"},
  {"text": "❤️: Lover\\n❤: Romantic\\n⭐: Arrogant\\n😉: Boring\\n😴: Tender\\n😛: Eater\\n😈: Receiver", "cx_pct": 0.55, "cy_pct": 0.55, "width_pct": 0.50, "fontsize_pct": 0.038, "align": "left", "bold": true, "color": "white"}
]"""


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

For EACH frame, detect every text caption/overlay that is VISIBLE IN THAT SPECIFIC FRAME — not text on clothing, objects, or the scene itself.

Return a JSON array. Each object = ONE caption visible in ONE frame:
- "frame_index": the 1-based index of the frame (1 to {n}) this caption is visible in
- "text": exact text with ALL emojis. Use \\n between visual lines exactly as displayed.
- "cx_pct": CENTER x as decimal fraction of frame width (0=left, 1=right, 0.5=center)
- "cy_pct": CENTER y as decimal fraction of frame height (0=top, 1=bottom)
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
5. EMOJIS: Do not remove, replace, normalize or describe emojis. If an emoji appears on screen, return the exact Unicode emoji. Preserve emojis exactly as visible. Examples: ❤️, ⭐, 😈, 😴, 👉, 👌, 🥺, 😂, 😭 must be returned as Unicode characters. Never omit emojis.
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


def extract_frames(video_path: str, count: int = 4) -> list:
    """Extract evenly-spaced frames from video."""
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
             "-vframes", "1", "-vf", "scale=720:-1", "-y", out],
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
    return normalized


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
    """
    from collections import defaultdict

    groups = defaultdict(list)
    for d in detections:
        text = (d.get("text") or "").strip()
        if not text:
            continue
        key = (text, round(d.get("cx_pct", 0.5), 1), round(d.get("cy_pct", 0.5), 1))
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

            base = dict(run[len(run) // 2])
            base.pop("frame_index", None)
            base["start_time"] = round(max(0.0, start_time), 2)
            base["end_time"]   = round(min(duration, end_time), 2)
            captions.append(base)

    captions.sort(key=lambda c: (c.get("start_time", 0.0), c.get("cy_pct", 0.0)))
    return captions


def analyze_with_claude_vision_timed(video_path: str):
    """
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

    # Sample more frames than the static-overlay path (8 vs 4), spread
    # evenly across the whole timeline, so caption appear/disappear
    # boundaries can be localized to roughly duration/8 precision.
    count = 8
    step  = max(0.35, duration / count)
    frame_times, frame_paths = [], []
    for i in range(count):
        t = min(step * i + step / 2.0, max(0.1, duration - 0.1))
        out = f"/tmp/tframe_{uuid.uuid4().hex}.png"
        subprocess.run(
            ["ffmpeg", "-ss", str(t), "-i", video_path,
             "-vframes", "1", "-vf", "scale=720:-1", "-y", out],
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
        prompt = VISION_PROMPT_TIMED.format(n=len(frame_times), timestamps=timestamps_str)
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


def _load_caption_font(fontsize: int, bold: bool):
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
def render_text_overlay(blocks: list, wa: int, ha: int, wb: int, hb: int) -> str:
    """
    Render all text objects onto a transparent RGBA image.
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

    use_pilmoji = True
    try:
        from pilmoji import Pilmoji
    except ImportError:
        use_pilmoji = False

    for block in blocks:
        text = block.get("text", "").strip()
        if not text:
            continue

        # ── Font size ─────────────────────────────────────────────────
        # Scale down from the vision-estimated size — generated captions were
        # consistently reading larger/heavier than native TikTok captions.
        fontsize_pct   = max(0.022, min(block.get("fontsize_pct", 0.035), 0.08)) * CAPTION_SIZE_SCALE
        target_fontsize = int(ha * fontsize_pct)
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
        font  = _load_caption_font(fontsize, bold)
        lines = _wrap_lines(orig_lines, font, max_w)

        # ── Phase 2: shrink only if still overflowing, respect floor ──
        for _ in range(30):
            font = _load_caption_font(fontsize, bold)
            max_line_w = max((_measure_text(font, ln) for ln in lines), default=0)
            if max_line_w <= max_w or fontsize <= min_fontsize:
                break
            fontsize = max(min_fontsize, fontsize - 2)

        # ── Layout ────────────────────────────────────────────────────
        # Native TikTok/IG caption outlines read as a thin hairline stroke.
        # The 1/11 ratio was still ~2x thicker than source captions in
        # side-by-side comparison, so we halve it again to ~1/22.
        # Final refinement: nudge ~10% thinner still (1/22 -> 1/24) to match
        # the subtle native stroke even more closely.
        border  = max(1, fontsize // 24)
        shadow  = (0, 0, 0, 225)
        # Slightly more breathing room between lines than native captions'
        # tightest spacing — keeps multi-line blocks from reading as a dense
        # slab of text the way the generated output previously did.
        line_h  = int(fontsize * 1.34)
        total_h = len(lines) * line_h
        y_start = cy - total_h // 2

        margin  = border + 4
        y_start = max(margin, min(y_start, ha - total_h - margin))

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
                    with Pilmoji(overlay) as pm:
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


# ── Routes ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


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

        # ui_mode distinguishes Batch from Simple so we can run the new
        # timed-caption detection ONLY for batch — simple mode keeps using
        # the exact same single-pass flow it always has.
        ui_mode = (request.form.get("mode") or "simple").strip().lower()
        has_key = bool(os.environ.get("ANTHROPIC_API_KEY", ""))

        lines = []
        if ui_mode == "batch":
            # Batch-only: sample frames across the timeline and detect
            # WHEN each caption appears/disappears (start_time/end_time).
            try:
                lines, _ = analyze_with_claude_vision_timed(path_b)
            except Exception:
                lines = []

        if lines:
            # Timed detection succeeded — use it as-is (already includes
            # start_time/end_time alongside the usual position/style keys).
            pass
        else:
            # Either simple mode, or batch timed-detection found nothing —
            # fall back to the original single-pass flow (identical to the
            # pre-existing behavior; produces lines WITHOUT timing info).
            frames = extract_frames(path_b, count=4)
            if has_key:
                lines = analyze_with_claude_vision(frames)
            else:
                lines = []
            if not lines:
                lines = analyze_with_tesseract_fallback(frames)
            for f in frames:
                try:
                    Path(f).unlink(missing_ok=True)
                except Exception:
                    pass

        _, hb = get_video_dims(path_b)
        return jsonify({
            "lines":          lines,
            "video_b_height": hb,
            "mode":           "vision" if has_key else "tesseract"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/process", methods=["POST"])
def process():
    try:
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

        wa, ha = get_video_dims(path_a)
        wb, hb = get_video_dims(path_b)

        # Get text lines from form
        lines_json = request.form.get("lines_json", "[]")
        try:
            lines = json.loads(lines_json)
        except Exception:
            lines = []

        if not lines:
            return jsonify({"error": "Aucune ligne de texte fournie."}), 400

        # Batch-only timed-caption path: only taken when the request is
        # explicitly flagged as batch AND every line carries start_time/
        # end_time (i.e. came from analyze_with_claude_vision_timed).
        # Simple mode never sends mode="batch", so it always falls through
        # to the original single-static-overlay path below, byte-for-byte
        # unchanged — including which function renders the overlay(s).
        ui_mode = (request.form.get("mode") or "simple").strip().lower()
        has_timing = (
            ui_mode == "batch"
            and bool(lines)
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
                    op = render_text_overlay([line], wa, ha, wb, hb)
                    overlay_paths.append(op)
                    overlay_specs.append((op, start, end))

                if not overlay_specs:
                    return jsonify({"error": "Aucune ligne de texte fournie."}), 400

                cmd = _build_timed_overlay_cmd(path_a, path_b, overlay_specs, path_out)
            else:
                # ── Original single-overlay path (simple mode + batch
                # fallback when timing wasn't available) — unchanged. ──
                overlay_path = render_text_overlay(lines, wa, ha, wb, hb)
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
            return jsonify({"error": err}), 500

        return jsonify({"job_id": job_id})

    except Exception as e:
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
        if index < 0 or index >= MAX_BATCH_FILES:
            return jsonify({"error": f"Index invalide (0 à {MAX_BATCH_FILES - 1})"}), 400

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

        # Identical sequence to /analyze's ui_mode == "batch" branch:
        # timed detection first, falling back to the original single-pass
        # detection (Vision or Tesseract) if it finds nothing.
        try:
            lines, _ = analyze_with_claude_vision_timed(path_b)
        except Exception:
            lines = []

        frames = []
        if not lines:
            frames = extract_frames(path_b, count=4)
            if has_key:
                lines = analyze_with_claude_vision(frames)
            else:
                lines = []
            if not lines:
                lines = analyze_with_tesseract_fallback(frames)

        for f in frames:
            try:
                Path(f).unlink(missing_ok=True)
            except Exception:
                pass

        _, hb = get_video_dims(path_b)
        return jsonify({
            "lines":          lines,
            "video_b_height": hb,
            "mode":           "vision" if has_key else "tesseract"
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
        batch_id = (request.form.get("batch_id") or "").strip()
        if not batch_id or ".." in batch_id or "/" in batch_id:
            return jsonify({"error": "batch_id invalide"}), 400
        try:
            a_index = int(request.form.get("a_index", "-1"))
            b_index = int(request.form.get("b_index", "-1"))
        except Exception:
            return jsonify({"error": "Index invalide"}), 400
        if not (0 <= a_index < MAX_BATCH_FILES) or not (0 <= b_index < MAX_BATCH_FILES):
            return jsonify({"error": "Index hors limites"}), 400

        bdir   = BATCH_DIR / batch_id
        path_a = bdir / "A" / f"{a_index:02d}.mp4"
        path_b = bdir / "B" / f"{b_index:02d}.mp4"
        if not path_a.exists() or not path_b.exists():
            return jsonify({"error": "Vidéo source introuvable (étape de staging manquante)"}), 404
        path_a, path_b = str(path_a), str(path_b)

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
                    op = render_text_overlay([line], wa, ha, wb, hb)
                    overlay_paths.append(op)
                    overlay_specs.append((op, start, end))
                    if CAPTION_VISUAL_DEBUG and debug_capture is None and _caption_debug_log:
                        mid = max(0.05, (start + end) / 2.0)
                        debug_capture = (dict(_caption_debug_log[0]), mid)

                if not overlay_specs:
                    return jsonify({"error": "Aucune ligne de texte fournie."}), 400

                cmd = _build_timed_overlay_cmd(path_a, path_b, overlay_specs, path_out)
            else:
                overlay_path = render_text_overlay(lines, wa, ha, wb, hb)
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
            return jsonify({"error": err}), 500

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
