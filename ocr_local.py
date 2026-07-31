"""
ocr_local.py — Moteur OCR LOCAL professionnel pour ViralScale (0 coût API).

Pipeline conçu comme un produit (pas un simple fallback Tesseract) :

  1. Échantillonnage temporel  : extraction de N frames réparties sur toute la
     durée de la vidéo (ffmpeg), densité adaptée à la durée.
  2. Détection de changement   : on ne relance l'OCR que quand la bande basse
     (zone captions) change réellement (diff numpy sur la zone de texte),
     ce qui réduit fortement le coût CPU et le bruit.
  3. Localisation + OCR         : masque "texte clair + contour sombre"
     (style CapCut/TikTok) → Tesseract image_to_data (FR+EN), mots + boîtes.
  4. Regroupement en lignes     : mots fusionnés par proximité verticale →
     lignes (support multi-lignes).
  5. Fusion temporelle          : suivi des textes identiques entre frames
     (normalisation + similarité) → segments avec start_time / end_time.
  6. Estimation géométrique     : cx_pct, cy_pct, width_pct, fontsize_pct,
     align — tout en pourcentage, format identique à Claude Vision.
  7. Score de confiance         : permet un routage HYBRIDE (bascule vers
     Vision uniquement quand le local est jugé insuffisant).

Sorties au MÊME format que le pipeline Vision (clés :
text, start_time, end_time, cx_pct, cy_pct, width_pct, fontsize_pct, align,
bold, color) → compatible render_text_overlay sans aucune adaptation.

Dépendances : numpy, Pillow, pytesseract (déjà présentes). Aucune dépendance
lourde (pas de PyTorch/Paddle) → déployable sur un dyno Render standard.
"""

import os
import re
import subprocess
import tempfile
import difflib


# ── util ffmpeg/ffprobe ────────────────────────────────────────────
def _probe_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-print_format", "json", path],
            capture_output=True, text=True, timeout=15)
        import json
        d = float(json.loads(r.stdout)["format"]["duration"])
        return d if d > 0 else 0.0
    except Exception:
        return 0.0


def _frame_count_for(duration):
    """Densité d'échantillonnage adaptée (comme le path Vision timed)."""
    if duration <= 0:
        return 6
    if duration <= 15:
        return 18
    if duration <= 60:
        return max(18, int(duration * 1.2))
    if duration <= 180:
        return 90
    return 120


def _extract_frames(path, n, scale_w=720):
    """Extrait n frames réparties uniformément → (paths, times[s])."""
    duration = _probe_duration(path)
    if duration <= 0:
        duration = 3.0
    tmpdir = tempfile.mkdtemp(prefix="ocrloc_")
    times = [duration * (i + 0.5) / n for i in range(n)]
    paths = []
    for i, t in enumerate(times):
        outp = os.path.join(tmpdir, f"f{i:04d}.png")
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", path,
                 "-frames:v", "1", "-vf", f"scale={scale_w}:-2",
                 "-loglevel", "error", outp],
                capture_output=True, timeout=30)
            if os.path.exists(outp):
                paths.append(outp)
            else:
                times[i] = None
        except Exception:
            times[i] = None
    times = [t for t in times if t is not None]
    return paths, times, duration, tmpdir


# ── prétraitement + OCR d'une frame ────────────────────────────────
def _text_mask(arr):
    """Masque binaire du texte clair (blanc) — inversé pour Tesseract
    (texte noir sur fond blanc). Cible les captions blanches à contour."""
    import numpy as np
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    white = (r > 170) & (g > 170) & (b > 170)
    inv = np.full(arr.shape[:2], 255, dtype=np.uint8)
    inv[white] = 0
    return inv, white


def _band_signature(arr):
    """Signature grossière de la bande basse (55–97% hauteur) pour la
    détection de changement de texte entre frames (peu coûteux)."""
    import numpy as np
    h = arr.shape[0]
    band = arr[int(h * 0.55):int(h * 0.97), :, :]
    _, white = _text_mask(band)
    # signature = fraction de pixels blancs par colonne (profil horizontal)
    col = white.mean(axis=0)
    # sous-échantillonne à 64 valeurs
    idx = np.linspace(0, len(col) - 1, 64).astype(int)
    return (white.mean(), col[idx])


def _sig_changed(a, b, thr=0.12):
    import numpy as np
    if a is None or b is None:
        return True
    fa, ca = a
    fb, cb = b
    if abs(fa - fb) > 0.01:
        return True
    return float(np.mean(np.abs(ca - cb))) > thr


def _ocr_frame(path):
    """Retourne (lines, w, h) : lignes de texte détectées avec bbox px."""
    import numpy as np
    import pytesseract
    from PIL import Image

    img = Image.open(path).convert("RGB")
    arr = np.array(img)
    h, w = arr.shape[:2]
    inv, _ = _text_mask(arr)
    pil_inv = Image.fromarray(inv).resize((w * 2, h * 2), Image.NEAREST)

    try:
        data = pytesseract.image_to_data(pil_inv, lang="eng+fra",
                                          config="--psm 6 --oem 3",
                                          output_type=pytesseract.Output.DICT)
    except Exception:
        try:
            data = pytesseract.image_to_data(pil_inv, config="--psm 6 --oem 3",
                                              output_type=pytesseract.Output.DICT)
        except Exception:
            return [], w, h

    words = []
    for i in range(len(data["text"])):
        txt = (data["text"][i] or "").strip()
        try:
            conf = int(data["conf"][i])
        except Exception:
            conf = -1
        if not txt or conf < 25:
            continue
        wx, wy = data["left"][i] // 2, data["top"][i] // 2
        ww, wh = data["width"][i] // 2, data["height"][i] // 2
        if wh < 5 or ww < 3:
            continue
        words.append({"t": txt, "x": wx, "y": wy, "w": ww, "h": wh, "c": conf})

    if not words:
        return [], w, h

    med_h = float(np.median([wd["h"] for wd in words]))
    words = [wd for wd in words if med_h * 0.40 <= wd["h"] <= med_h * 2.6]
    if not words:
        return [], w, h
    med_h2 = float(np.median([wd["h"] for wd in words]))

    # regroupement en lignes par proximité verticale
    words.sort(key=lambda wd: wd["y"])
    groups = [[words[0]]]
    for wd in words[1:]:
        if abs(wd["y"] - groups[-1][-1]["y"]) < med_h2 * 0.75:
            groups[-1].append(wd)
        else:
            groups.append([wd])

    lines = []
    for grp in groups:
        grp.sort(key=lambda wd: wd["x"])
        text = " ".join(wd["t"] for wd in grp)
        alpha_r = sum(c.isalpha() for c in text) / max(1, len(text))
        if alpha_r < 0.25 and len(text) < 4:
            continue
        x0 = min(wd["x"] for wd in grp)
        x1 = max(wd["x"] + wd["w"] for wd in grp)
        y0 = min(wd["y"] for wd in grp)
        y1 = max(wd["y"] + wd["h"] for wd in grp)
        conf = float(np.mean([wd["c"] for wd in grp]))
        lines.append({"text": text, "x0": x0, "x1": x1, "y0": y0, "y1": y1,
                      "fh": float(np.median([wd["h"] for wd in grp])), "conf": conf})
    return lines, w, h


# ── fusion temporelle + géométrie ──────────────────────────────────
_WS = re.compile(r"\s+")


def _norm(s):
    return _WS.sub(" ", (s or "").strip().lower())


def _similar(a, b):
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _merge_multiline(line_group):
    """Fusionne des lignes proches verticalement en un bloc multi-ligne."""
    line_group.sort(key=lambda l: l["y0"])
    text = "\n".join(l["text"] for l in line_group)
    x0 = min(l["x0"] for l in line_group)
    x1 = max(l["x1"] for l in line_group)
    y0 = min(l["y0"] for l in line_group)
    y1 = max(l["y1"] for l in line_group)
    fh = sum(l["fh"] for l in line_group) / len(line_group)
    conf = sum(l["conf"] for l in line_group) / len(line_group)
    return {"text": text, "x0": x0, "x1": x1, "y0": y0, "y1": y1, "fh": fh, "conf": conf}


def _block_geometry(blk, w, h):
    cx = ((blk["x0"] + blk["x1"]) / 2.0) / w
    cy = ((blk["y0"] + blk["y1"]) / 2.0) / h
    width_pct = min(1.0, max(0.05, (blk["x1"] - blk["x0"]) / w))
    fontsize_pct = min(0.14, max(0.02, blk["fh"] / h))
    # alignement d'après la position du centre horizontal
    if cx < 0.40:
        align = "left"
    elif cx > 0.60:
        align = "right"
    else:
        align = "center"
    return {
        "cx_pct": round(cx, 4), "cy_pct": round(cy, 4),
        "width_pct": round(width_pct, 4), "fontsize_pct": round(fontsize_pct, 4),
        "align": align,
    }


def _has_usable_text(text):
    """True dès qu'il reste AU MOINS un caractère alphanumérique (lettre ou
    chiffre, Unicode → « À », « É »… inclus). C'est le SEUL critère de contenu :
    on ne juge JAMAIS un fragment sur QUELS caractères il contient. Seules les
    chaînes 100% symboles (« ~ | € ™ », « /// », « »« ») — sans aucun
    alphanumérique exploitable — sont rejetées ici. Tout le reste (y compris
    « I », « A », « À », « J », « LES », « RN », « Th ») passe par la décision
    fondée sur les SIGNAUX OCR (_keep_segment). Aucune blacklist de mots/lettres."""
    return bool(re.search(r"[^\W_]", text or ""))   # \w Unicode, hors underscore


# Alias historique (le texte n'est jugé que sur « contient de l'alphanumérique »).
_is_text_like = _has_usable_text


def _clean_caption_text(text):
    """Nettoie le bruit de BORD sans jamais juger le contenu alphabétique : on
    retire uniquement les tokens 100% symboles (| ~ ' " € ™ …). Tout token
    contenant une lettre ou un chiffre est conservé — « RN », « Th », « J »,
    « À » restent (leur sort est décidé par les signaux OCR, pas par leur texte)."""
    out_lines = []
    for line in (text or "").split("\n"):
        toks = []
        for t in line.split():
            # retire les symboles PARASITES collés en bordure de token
            # (« |have » → « have »), sans toucher la ponctuation de phrase
            # (. , ? ! ' -) ni le contenu interne.
            t = t.strip("|~€™/\\»«·•*_=+<>°¬`^§¤")
            if re.search(r"[^\W_]", t):        # garde tout token alphanumérique
                toks.append(t)
        if toks:
            out_lines.append(" ".join(toks))
    return "\n".join(out_lines).strip()


def _trim_edge_noise(text):
    """Rognage SÛR du bruit résiduel en bordure : on retire un token d'UN SEUL
    caractère alphanumérique en tête/queue (ex. « v Be… », « Q I'm… », « y and… »)
    — sauf les vrais mots d'une lettre « I », « a », « A ». Aucun mot de ≥2
    caractères n'est jamais touché. Appliqué au texte final d'une caption."""
    def _one(tok):
        a = re.sub(r"[^\w]", "", tok)
        return len(a) == 1 and a not in ("I", "a", "A")
    lines = [l for l in (text or "").split("\n")]
    if lines:
        t = lines[0].split()
        while len(t) > 1 and _one(t[0]):
            t = t[1:]
        lines[0] = " ".join(t)
        t = lines[-1].split()
        while len(t) > 1 and _one(t[-1]):
            t = t[:-1]
        lines[-1] = " ".join(t)
    return "\n".join(l for l in lines if l.strip()).strip()


def analyze_video_local(video_path, hybrid_threshold=55.0):
    """
    OCR local complet. Retourne (lines, meta) :
      lines : liste de dicts au format Vision (text, start_time, end_time,
              cx_pct, cy_pct, width_pct, fontsize_pct, align, bold, color).
      meta  : {"duration", "frames", "confidence" (0-100), "needs_vision"}.

    `needs_vision` = True quand la confiance moyenne est sous le seuil →
    signal pour un routage HYBRIDE (n'appeler Vision que dans ce cas).
    """
    duration = _probe_duration(video_path) or 3.0
    n = _frame_count_for(duration)
    paths, times, duration, tmpdir = _extract_frames(video_path, n)

    per_frame = []   # (time, lines[], w, h)
    try:
        # On lit CHAQUE frame échantillonnée (recall prioritaire : deux captions
        # successives au même endroit ne doivent jamais être fusionnées à tort).
        # La fusion temporelle en aval regroupe les textes réellement identiques.
        for p, t in zip(paths, times):
            lines, w, hh = _ocr_frame(p)
            per_frame.append((t, lines, w, hh))
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

    if not per_frame:
        return [], {"duration": duration, "frames": 0, "confidence": 0.0, "needs_vision": True}

    # dimension de référence
    w = next((f[2] for f in per_frame if f[2]), 720)
    h = next((f[3] for f in per_frame if f[3]), 1280)

    import numpy as np
    frame_dt = (duration / max(1, len(per_frame)))

    # ── ÉTAPE 1 — blocs multiline par frame (mots→lignes→blocs), géométrie % ──
    frames_blocks = []   # [(t, [block,...])]
    for (t, lines, fw, fh0) in per_frame:
        fw = fw or w; fh0 = fh0 or h
        blks = []
        if lines:
            lines_sorted = sorted(lines, key=lambda l: l["y0"])
            groups = [[lines_sorted[0]]]
            for ln in lines_sorted[1:]:
                gap = ln["y0"] - groups[-1][-1]["y1"]
                lh = groups[-1][-1]["fh"]
                if gap < lh * 0.9:
                    groups[-1].append(ln)
                else:
                    groups.append([ln])
            for grp in groups:
                mb = _merge_multiline(grp)
                g = _block_geometry(mb, fw, fh0)
                blks.append({
                    "raw": mb["text"], "clean": _clean_caption_text(mb["text"]),
                    "conf": float(mb["conf"]), "t": t,
                    "cy": g["cy_pct"], "cx": g["cx_pct"], "wp": g["width_pct"],
                    "fp": g["fontsize_pct"], "fhp": (mb["fh"] / fh0), "align": g["align"],
                })
        frames_blocks.append((t, blks))

    all_blocks = [b for (_, bl) in frames_blocks for b in bl]

    def _core(txt):
        return set(x for x in (re.sub(r"[^a-z]", "", y) for y in _norm(txt).split()) if len(x) >= 3)

    def _meta(out):
        confs = [seg["_conf"] for seg in out] if out else []
        confidence = round(sum(confs) / len(confs), 1) if confs else 0.0
        return {"duration": round(duration, 2), "frames": len(per_frame),
                "confidence": confidence,
                "needs_vision": (confidence < hybrid_threshold) or (len(out) == 0)}

    # blocs candidats = ceux qui portent du texte exploitable
    cand = [b for b in all_blocks if b["conf"] >= 30 and _has_usable_text(b["clean"])]
    if not cand:
        return [], _meta([])

    # ── ÉTAPE 2 — PISTE DOMINANTE : bande verticale qui concentre les vraies
    # captions (couverture temporelle × confiance la plus forte). Les vraies
    # captions restent au même endroit sur toute la vidéo ; le bruit (bords haut/
    # bas, reflets) est dispersé et peu couvrant. ──
    HW = 0.09
    best_center, best_score = None, -1.0
    for c in sorted(set(round(b["cy"], 3) for b in cand)):
        inb = [b for b in cand if abs(b["cy"] - c) <= HW]
        frames_cov = len(set(round(b["t"], 4) for b in inb))
        mean_conf = sum(b["conf"] for b in inb) / len(inb)
        score = frames_cov * mean_conf
        if score > best_score:
            best_score, best_center = score, c
    band = [b for b in cand if abs(b["cy"] - best_center) <= HW]
    dom_cy = float(np.median([b["cy"] for b in band]))
    dom_cx = float(np.median([b["cx"] for b in band]))
    dom_wp = float(np.median([b["wp"] for b in band]))
    dom_fp = float(np.median([b["fp"] for b in band]))
    dom_fh = float(np.median([b["fhp"] for b in band]))
    _al = [b["align"] for b in band]
    dom_align = max(set(_al), key=_al.count)

    def _in_track(b):
        # dans la zone dominante ET taille de police cohérente (rejette les
        # détections ÉNORMES/minuscules qui produisent des textes dispersés).
        if abs(b["cy"] - dom_cy) > max(0.10, 4 * dom_fh):
            return False
        if not (0.45 * dom_fh <= b["fhp"] <= 2.2 * dom_fh):
            return False
        return True

    def _same_caption(txt, ref):
        if _similar(_norm(txt), _norm(ref)) >= 0.45:
            return True
        a, b = _core(txt), _core(ref)
        if a and b:
            u = len(a | b)
            return u and len(a & b) / u >= 0.5    # recouvrement fort (pas un simple mot commun)
        return False

    # ── ÉTAPE 3 — CONSOLIDATION TEMPORELLE : une seule caption active à la fois.
    # On parcourt les frames dans l'ordre ; tant que le texte reste « la même
    # caption », on l'étend et on retient la MEILLEURE lecture ; dès qu'il change,
    # on ferme la précédente et on en ouvre une nouvelle. Jamais deux variantes
    # simultanées. ──
    caps = []
    for (t, bl) in frames_blocks:
        cands = [b for b in bl if _in_track(b) and _has_usable_text(b["clean"])]
        if not cands:
            continue
        b = max(cands, key=lambda x: x["conf"])
        txt = b["clean"]
        if caps and _same_caption(txt, caps[-1]["best_text"]):
            cur = caps[-1]
            cur["end"] = t
            cur["reads"] += 1
            cur["readlist"].append((b["conf"], txt))
            sc = b["conf"] * len(txt)
            if sc > cur["best_score"]:
                cur["best_score"] = sc; cur["best_text"] = txt; cur["best_conf"] = b["conf"]
        else:
            caps.append({"start": t, "end": t, "reads": 1,
                         "best_text": txt, "best_score": b["conf"] * len(txt),
                         "best_conf": b["conf"], "readlist": [(b["conf"], txt)]})

    # ── Fusion des captions ADJACENTES sur-segmentées : Tesseract lit parfois
    # une même caption très différemment d'une frame à l'autre (texte stylisé).
    # On refusionne deux captions voisines qui partagent un mot « fort » (≥5
    # lettres, ex. « check », « profil ») OU une similarité de séquence, tout en
    # ne fusionnant JAMAIS deux vraies captions distinctes (mots courts communs
    # comme « bby », « like » ignorés). ──
    def _strong(txt):
        return set(x for x in (re.sub(r"[^a-z]", "", y) for y in _norm(txt).split()) if len(x) >= 5)

    def _mergeable(a, b):
        if _similar(_norm(a["best_text"]), _norm(b["best_text"])) >= 0.5:
            return True
        return bool(_strong(a["best_text"]) & _strong(b["best_text"]))

    merged = []
    for c in caps:
        if merged and _mergeable(merged[-1], c):
            m = merged[-1]
            m["end"] = c["end"]; m["reads"] += c["reads"]; m["readlist"] += c["readlist"]
            if c["best_score"] > m["best_score"]:
                m["best_score"] = c["best_score"]; m["best_text"] = c["best_text"]; m["best_conf"] = c["best_conf"]
        else:
            merged.append(c)
    caps = merged

    # une caption vue sur UNE seule frame reste presque toujours un résidu de
    # piste (transition/garble) → écartée (décision par les signaux).
    caps = [c for c in caps if c["reads"] >= 2]
    if os.environ.get("OCR_TRACK_DEBUG"):
        import sys as _s
        for c in caps:
            print(f"[TRACK] reads={c['reads']} conf={c['best_conf']:.0f} «{c['best_text'][:50]}»", file=_s.stderr)

    # ── INCERTITUDE par caption (pour le mode Auto : fallback Vision ciblé). ──
    # Signaux : confiance OCR moyenne + DÉSACCORD entre les lectures successives
    # (mots instables / fragments ajoutés / troncatures se traduisent par des
    # lectures divergentes). NE juge pas le contenu (pas de dictionnaire).
    def _uncertainty(readlist, best):
        confs = [c for c, _ in readlist] or [0.0]
        mean_conf = sum(confs) / len(confs)
        texts = [t for _, t in readlist]
        # désaccord = 1 - similarité moyenne au texte représentatif
        if len(texts) > 1:
            sims = [_similar(best, t) for t in texts]
            agree = sum(sims) / len(sims)
        else:
            agree = 1.0
        disagree = 1.0 - agree
        uncertain = (mean_conf < 84.0) or (disagree > 0.30)
        return round(mean_conf, 1), round(disagree, 3), bool(uncertain)

    # ── ÉTAPE 4 — géométrie COHÉRENTE (médianes de la piste dominante) pour
    # TOUTES les captions : même taille, même alignement, même position. ──
    out = []
    for c in caps:
        start = max(0.0, c["start"] - frame_dt * 0.5)
        end = min(duration, c["end"] + frame_dt * 0.5)
        if end <= start:
            end = start + max(0.6, frame_dt)
        mconf, disagree, uncertain = _uncertainty(c["readlist"], c["best_text"])
        out.append({
            "text": _trim_edge_noise(c["best_text"]),
            "start_time": round(start, 2), "end_time": round(end, 2),
            "cx_pct": round(dom_cx, 4), "cy_pct": round(dom_cy, 4),
            "width_pct": round(dom_wp, 4), "fontsize_pct": round(dom_fp, 4),
            "align": dom_align, "bold": True, "color": "white",
            "_conf": round(c["best_conf"], 1),
            "_mean_conf": mconf, "_disagree": disagree, "_uncertain": uncertain,
        })

    # non-chevauchement strict : chaque caption se ferme avant la suivante.
    out.sort(key=lambda l: l["start_time"])
    for i in range(len(out) - 1):
        if out[i]["end_time"] > out[i + 1]["start_time"]:
            out[i]["end_time"] = round(out[i + 1]["start_time"], 2)

    return out, _meta(out)
