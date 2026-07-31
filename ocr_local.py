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
        toks = [t for t in line.split() if re.search(r"[^\W_]", t)]  # garde tout token alphanumérique
        if toks:
            out_lines.append(" ".join(toks))
    return "\n".join(out_lines).strip()


def _keep_segment(text, conf, persist, cy_std):
    """Décision FINALE de conservation — fondée UNIQUEMENT sur les SIGNAUX OCR,
    jamais sur le texte lui-même :
      • présence d'alphanumérique (sinon pur symbole → rejet immédiat) ;
      • confiance OCR (Tesseract) ;
      • répétition temporelle (persist = nb de frames où le texte réapparaît) ;
      • cohérence de la zone d'affichage (faible variance verticale cy_std).
    Un fragment COURT (1 caractère inclus : « I », « A », « À », « J », « LES »,
    « RN », « Th »…) est CONSERVÉ dès qu'il est corroboré (répété sur ≥2 frames
    OU confiance élevée) et affiché dans une zone stable ; les MÊMES fragments,
    vus une seule fois, peu fiables et dans une zone instable, sont rejetés.
    La longueur ne sert qu'à calibrer l'EXIGENCE de corroboration (un fragment
    court est plus bruité qu'une phrase), pas à filtrer un texte précis."""
    if not _has_usable_text(text):
        return False
    alnum = sum(c.isalnum() for c in text)
    n_tok = len([x for x in text.split() if x])
    zone_ok = (cy_std is None) or (cy_std <= 0.06)
    is_short = (n_tok <= 1) or (alnum <= 4)
    if is_short:
        # Fragment court (« No », « my », « 5 », « I », « RN », « A »… — traités
        # à l'identique, sans regarder QUEL texte) : conservé s'il est corroboré
        # (répété sur ≥2 frames OU confiance haute) ET affiché dans une zone
        # stable ; rejeté s'il est vu une seule fois, peu fiable, zone instable.
        return ((persist >= 2) or (conf >= 72)) and zone_ok
    # Phrase : corroborée par la répétition OU une confiance correcte.
    return (persist >= 2) or (conf >= 55)


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

    # ── fusion temporelle : suivi des textes entre frames ──
    # tracks : {"key_lines":[...], "geom_samples":[...], "first_t","last_t","confs"}
    tracks = []
    for (t, lines, fw, fh) in per_frame:
        # multi-ligne : regrouper les lignes verticalement proches en blocs
        if lines:
            lines_sorted = sorted(lines, key=lambda l: l["y0"])
            blocks = [[lines_sorted[0]]]
            for ln in lines_sorted[1:]:
                gap = ln["y0"] - blocks[-1][-1]["y1"]
                lh = blocks[-1][-1]["fh"]
                if gap < lh * 0.9:
                    blocks[-1].append(ln)
                else:
                    blocks.append([ln])
            frame_blocks = [_merge_multiline(b) for b in blocks]
        else:
            frame_blocks = []

        matched_keys = set()
        for blk in frame_blocks:
            best, best_s = None, 0.0
            for tr in tracks:
                s = _similar(blk["text"], tr["text"])
                # même texte ET même zone verticale
                if s > best_s and abs(blk["y0"] - tr["y0"]) < h * 0.12:
                    best, best_s = tr, s
            if best is not None and best_s >= 0.72:
                best["last_t"] = t
                best["confs"].append(blk["conf"])
                best["geom"].append((blk, fw, fh))
                if blk["conf"] > best["best_conf"]:
                    best["text"] = blk["text"]; best["best_conf"] = blk["conf"]; best["y0"] = blk["y0"]
                matched_keys.add(id(best))
            else:
                tracks.append({
                    "text": blk["text"], "y0": blk["y0"], "best_conf": blk["conf"],
                    "first_t": t, "last_t": t, "confs": [blk["conf"]],
                    "geom": [(blk, fw, fh)],
                })

    # ── construction des segments finaux ──
    frame_dt = (duration / max(1, len(per_frame)))
    out = []
    all_conf = []
    for tr in tracks:
        # géométrie médiane sur tous les échantillons du track
        import numpy as np
        cxs, cys, wps, fps_, aligns = [], [], [], [], []
        for (blk, fw, fh) in tr["geom"]:
            g = _block_geometry(blk, fw or w, fh or h)
            cxs.append(g["cx_pct"]); cys.append(g["cy_pct"])
            wps.append(g["width_pct"]); fps_.append(g["fontsize_pct"]); aligns.append(g["align"])
        align = max(set(aligns), key=aligns.count)
        conf = float(np.mean(tr["confs"])) if tr["confs"] else 0.0
        cy_std = float(np.std(cys)) if len(cys) > 1 else 0.0

        # ── FILTRAGE ANTI-PARASITE multi-signaux (sans pénaliser les captions
        # courtes) : texte nettoyé, puis décision via _keep_segment qui combine
        # lexical + confiance OCR + répétition temporelle + stabilité de zone. ──
        cleaned = _clean_caption_text(tr["text"])
        persist = len(tr["confs"])
        if not _keep_segment(cleaned, conf, persist, cy_std):
            continue

        all_conf.append(conf)
        start = max(0.0, tr["first_t"] - frame_dt * 0.5)
        end = min(duration, tr["last_t"] + frame_dt * 0.5)
        if end <= start:
            end = start + max(0.6, frame_dt)
        out.append({
            "text": cleaned,
            "start_time": round(start, 2),
            "end_time": round(end, 2),
            "cx_pct": round(float(np.median(cxs)), 4),
            "cy_pct": round(float(np.median(cys)), 4),
            "width_pct": round(float(np.median(wps)), 4),
            "fontsize_pct": round(float(np.median(fps_)), 4),
            "align": align,
            "bold": True,
            "color": "white",
            "_conf": round(conf, 1),
        })

    # ── DÉDUPLICATION par MOTS-CLÉS : les variantes bruitées d'une même caption
    # (« ty fa 5 types of men: », « al at a ia oa 5 types of men: if »…) partagent
    # le même noyau de vrais mots ({types, men}) → on les fusionne en UNE seule
    # caption (la plus fiable). Pour les captions très courtes (noyau vide, ex.
    # « No »), on retombe sur l'égalité de texte + même zone verticale.
    def _core(txt):
        return set(w for w in (re.sub(r"[^a-z]", "", x) for x in _norm(txt).split()) if len(w) >= 3)
    out.sort(key=lambda l: (-l["_conf"], l["start_time"]))   # les plus fiables d'abord
    deduped = []
    for seg in out:
        ca = _core(seg["text"])
        hit = None
        for m in deduped:
            cm = _core(m["text"])
            if ca and cm:
                union = len(ca | cm)
                # fusion si fort recouvrement OU si un noyau est inclus dans
                # l'autre (vraie caption ⊆ variante bruitée « + mots junk »).
                if (ca <= cm or cm <= ca) or (union and len(ca & cm) / union >= 0.6):
                    hit = m; break
            elif _norm(seg["text"]) == _norm(m["text"]) and abs(seg["cy_pct"] - m["cy_pct"]) < 0.12:
                hit = m; break
        if hit is not None:
            hit["start_time"] = min(hit["start_time"], seg["start_time"])
            hit["end_time"] = max(hit["end_time"], seg["end_time"])
            if seg["_conf"] > hit["_conf"]:
                hit["text"] = seg["text"]; hit["_conf"] = seg["_conf"]
                hit["cx_pct"] = seg["cx_pct"]; hit["cy_pct"] = seg["cy_pct"]
                hit["width_pct"] = seg["width_pct"]; hit["fontsize_pct"] = seg["fontsize_pct"]
                hit["align"] = seg["align"]
        else:
            deduped.append(seg)
    out = deduped

    out.sort(key=lambda l: (l["start_time"], l["cy_pct"]))
    confidence = round(sum(all_conf) / len(all_conf), 1) if all_conf else 0.0
    meta = {
        "duration": round(duration, 2),
        "frames": len(per_frame),
        "confidence": confidence,
        "needs_vision": (confidence < hybrid_threshold) or (len(out) == 0),
    }
    return out, meta
