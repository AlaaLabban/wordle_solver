#!/usr/bin/env python3
"""
AlWird setup — optimized for Intel i7-7700HQ (4c/8t, 2.8 GHz) + 32 GB RAM.
Uses numpy-vectorized entropy if numpy is available (pip install numpy).
Falls back to pure Python automatically if numpy is missing.

Produces:
  alwird_words.json   — full word list
  alwird_cache.pkl    — opener pool (pre-computed best opening words)
"""

import json
import math
import multiprocessing
import os
import pickle
import sys
import time

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import alwird_solver as solver

# ── Tuning knobs for i7-7700HQ ───────────────────────────────────────────────
# 4 physical cores × 2 HT = 8 logical. For compute-heavy work 7 is the sweet
# spot: keeps all HT pairs busy while leaving one thread free for the OS / IO.
N_WORKERS  = 7
# Larger chunks = fewer round-trips through the process pool = less IPC overhead.
# 32 GB RAM handles pre-loading 120 k words in every worker with room to spare.
CHUNK_SIZE = 3000
TOLERANCE  = 0.5   # bits — same as default solver


# ── Numpy-accelerated scorer (module-level so workers can pickle it) ──────────

def _score_chunk_numpy(args):
    """Vectorized entropy for a chunk of words against candidates."""
    import numpy as np

    chunk, candidates = args
    ARABIC_BASE = 0x0600
    MAX_LETTERS = 256        # Arabic block fits in 0x0600-0x06FF

    cand_enc = np.array([[ord(c) for c in w] for w in candidates], dtype=np.uint32)
    N        = len(cand_enc)
    cand_set = set(candidates)

    # Row indices reused across words
    rows = np.arange(N, dtype=np.int32)

    results = []
    for word in chunk:
        guess_enc = np.array([ord(c) for c in word], dtype=np.uint32)

        # ── Pass 1: greens ────────────────────────────────────────────────────
        pat        = np.zeros((N, 5), dtype=np.int8)
        green_mask = cand_enc == guess_enc[np.newaxis, :]   # (N, 5)
        pat[green_mask] = 2

        # ── Remaining letter counts (non-green positions only) ─────────────────
        rem = np.zeros((N, MAX_LETTERS), dtype=np.int8)
        for pos in range(5):
            not_green = ~green_mask[:, pos]
            li = (cand_enc[:, pos] - ARABIC_BASE).astype(np.int32)
            li = np.clip(li, 0, MAX_LETTERS - 1)
            rem[rows[not_green], li[not_green]] += 1

        # ── Pass 2: yellows (left-to-right, consuming letters) ────────────────
        for pos in range(5):
            not_green = pat[:, pos] != 2
            li = int(ord(word[pos])) - ARABIC_BASE
            if not (0 <= li < MAX_LETTERS):
                continue
            has_letter = (rem[:, li] > 0) & not_green
            pat[has_letter, pos] = 1
            rem[has_letter, li] -= 1

        # ── Entropy from pattern distribution ─────────────────────────────────
        powers  = np.array([81, 27, 9, 3, 1], dtype=np.int32)
        hashes  = (pat.astype(np.int32) * powers).sum(axis=1)
        _, cnts = np.unique(hashes, return_counts=True)
        probs   = cnts / N
        entropy = float(-np.sum(probs * np.log2(probs + 1e-15)))

        results.append((word, entropy, word in cand_set))

    return results


def _score_chunk_pure(args):
    """Pure-Python fallback (identical to solver._mp_score_chunk)."""
    return solver._mp_score_chunk(args)


def _score_chunk(args):
    try:
        import numpy  # noqa: F401
        return _score_chunk_numpy(args)
    except ImportError:
        return _score_chunk_pure(args)


# ── Fast opener builder ───────────────────────────────────────────────────────

def build_opener_pool_fast(all_words, tolerance=TOLERANCE):
    total      = len(all_words)
    chunks     = [all_words[i : i + CHUNK_SIZE] for i in range(0, total, CHUNK_SIZE)]
    n_chunks   = len(chunks)
    args       = [(chunk, all_words) for chunk in chunks]

    try:
        import numpy
        backend = f"numpy  (vectorized)"
    except ImportError:
        backend = "pure Python (install numpy for ~10× speedup)"

    print(f"\n  Workers   : {N_WORKERS}")
    print(f"  Chunk size: {CHUNK_SIZE:,} words  →  {n_chunks} chunks")
    print(f"  Backend   : {backend}")
    print(f"  Words     : {total:,}")
    print()

    bar_width  = 40
    all_scores = []
    completed  = 0
    best       = ("…", 0.0)
    t0         = time.time()

    def draw(done):
        filled  = int(bar_width * done / total)
        bar     = "█" * filled + "░" * (bar_width - filled)
        elapsed = time.time() - t0
        eta     = (elapsed / done * (total - done)) if done else 0
        print(
            f"\r  [{bar}] {100*done/total:5.1f}%  "
            f"{done:,}/{total:,}  "
            f"ETA {eta/60:.1f} min  "
            f"best: {best[0]} (H={best[1]:.3f})",
            end="", flush=True,
        )

    with multiprocessing.Pool(N_WORKERS) as pool:
        for chunk_result in pool.imap_unordered(_score_chunk, args):
            all_scores.extend(chunk_result)
            completed += len(chunk_result)
            if all_scores:
                best = max(all_scores, key=lambda x: x[1])[:2]
            draw(completed)

    print()  # newline after bar
    elapsed = time.time() - t0
    print(f"\n  Finished in {elapsed/60:.1f} min")

    all_scores.sort(key=lambda x: x[1], reverse=True)
    best_ent  = all_scores[0][1]
    pool_words = [w for w, e, _ in all_scores if e >= best_ent - tolerance]

    print(f"  Best entropy : {best_ent:.4f} bits")
    print(f"  Pool size    : {len(pool_words):,} words (±{tolerance} bits of best)")
    return pool_words


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 58)
    print("  AlWird Setup  —  i7-7700HQ optimized")
    print("=" * 58)

    # ── Step 1: download ──────────────────────────────────────────────────────
    print("\nStep 1/2 — Downloading word list …\n")
    try:
        words = solver.extract_wordlist_from_site()
        solver.save_wordlist(words)
    except Exception as e:
        print(f"\n[!] Download failed: {e}")
        sys.exit(1)

    # ── Step 2: build cache ───────────────────────────────────────────────────
    print("\nStep 2/2 — Building opener cache …")
    if os.path.exists(solver.CACHE_FILE):
        os.remove(solver.CACHE_FILE)

    pool_words = build_opener_pool_fast(words)
    solver.save_opener_cache(pool_words)

    print("\n" + "=" * 58)
    print("  Done!")
    print(f"  alwird_words.json  — {len(words):,} words")
    print(f"  alwird_cache.pkl   — {len(pool_words):,} opener words")
    print("  Copy both files back alongside alwird_solver.py + app.py")
    print("=" * 58)
