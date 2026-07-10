#!/usr/bin/env python3
"""
bench_ocr.py — Benchmark du moteur OCR LOCAL (ocr_local.py) vs Claude Vision.

Usage :
    python bench_ocr.py video1.mp4 [video2.mp4 ...]

Pour chaque vidéo, mesure :
    - temps total du pipeline local ;
    - RAM approximative (si psutil dispo) ;
    - nombre de segments détectés ;
    - textes + positions (cx/cy/width/fontsize/align) + start/end ;
    - score de confiance + recommandation hybride (needs_vision) ;
    - comparaison avec Claude Vision si ANTHROPIC_API_KEY est défini
      (similarité texte moyenne, écart de nombre de segments).

Aucune dépendance nouvelle obligatoire (psutil et anthropic sont optionnels).
"""
import sys
import time
import difflib


def _rss_mb():
    try:
        import psutil, os
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def _norm(s):
    return " ".join((s or "").split()).lower()


def _best_match(text, pool):
    best = 0.0
    for p in pool:
        best = max(best, difflib.SequenceMatcher(None, _norm(text), _norm(p)).ratio())
    return best


def run_local(video):
    import ocr_local
    m0 = _rss_mb()
    t0 = time.time()
    lines, meta = ocr_local.analyze_video_local(video)
    dt = time.time() - t0
    m1 = _rss_mb()
    return lines, meta, dt, (None if (m0 is None or m1 is None) else max(m0, m1))


def run_vision(video):
    """Vision timed (si clé API) — sinon None."""
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import app
        lines, _dur = app.analyze_with_claude_vision_timed(video)
        return lines
    except Exception as e:
        print(f"   [vision] erreur: {e}")
        return None


def main(videos):
    print("=" * 72)
    print("BENCHMARK OCR LOCAL — ViralScale")
    print("=" * 72)
    for v in videos:
        print(f"\n▶ {v}")
        try:
            lines, meta, dt, rss = run_local(v)
        except Exception as e:
            print(f"   ERREUR pipeline local: {e}")
            continue
        print(f"   durée vidéo   : {meta.get('duration')} s")
        print(f"   frames OCR    : {meta.get('frames')}")
        print(f"   temps local   : {dt:.2f} s"
              + (f"  ({dt / max(0.01, meta.get('duration', 1)):.2f}× temps réel)" if meta.get('duration') else ""))
        if rss is not None:
            print(f"   RAM (RSS)     : ~{rss:.0f} Mo")
        print(f"   confiance     : {meta.get('confidence')}/100"
              f"   → needs_vision={meta.get('needs_vision')}")
        print(f"   segments      : {len(lines)}")
        for i, l in enumerate(lines, 1):
            print(f"     {i}. [{l['start_time']:.2f}–{l['end_time']:.2f}s] "
                  f"cx={l['cx_pct']} cy={l['cy_pct']} w={l['width_pct']} "
                  f"fs={l['fontsize_pct']} {l['align']}  «{l['text'][:60].replace(chr(10),' / ')}»")

        vis = run_vision(v)
        if vis is not None:
            vtexts = [x.get("text", "") for x in vis]
            sims = [_best_match(l["text"], vtexts) for l in lines] or [0.0]
            avg = sum(sims) / len(sims)
            print(f"   — vs VISION — segments vision={len(vis)}  "
                  f"similarité texte moyenne local→vision={avg:.2f}")
            print("     verdict:", "≈ équivalent" if avg >= 0.8 and abs(len(vis) - len(lines)) <= 1
                  else "local insuffisant → route hybride recommandée")
        else:
            print("   (Vision non comparé — ANTHROPIC_API_KEY absente)")
    print("\n" + "=" * 72)
    print("Rappel : le mode local ne coûte AUCUNE API, mais consomme CPU/RAM/temps")
    print("serveur. Architecture recommandée : HYBRIDE (local par défaut, bascule")
    print("Vision uniquement quand needs_vision=True).")
    print("=" * 72)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python bench_ocr.py video1.mp4 [video2.mp4 ...]")
        sys.exit(1)
    main(sys.argv[1:])
