#!/usr/bin/env python3
"""
AlWird Entropy Solver — Arabic Wordle (arwordle.netlify.app)
============================================================
Solves the AlWird game using information-theoretic entropy.

HOW IT WORKS:
  For each candidate guess, we compute the *expected information gain* (entropy).
  The guess that maximally partitions the remaining candidates wins.

  Entropy H = -Σ p(pattern) * log2(p(pattern))
  where pattern is the green/yellow/gray feedback vector.

MODES:
  Normal  — solver searches all candidates for best guess each turn
  Hard    — any revealed hint (green/yellow) must be used in the next guess
  Legacy  — solver searches full 119k vocabulary every turn (slower, theoretically optimal)

FEEDBACK ENCODING (per letter):
  2 = Green  (correct letter, correct position)
  1 = Yellow (correct letter, wrong position)
  0 = Gray   (letter not in word)
"""

import json
import multiprocessing
import multiprocessing.sharedctypes
import math
import os
import pickle
import random
import re
import sys
import urllib.request
from collections import defaultdict
from typing import Any, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

WORDLIST_FILE = "alwird_words.json"
CACHE_FILE    = "alwird_cache.pkl"
WORD_LEN      = 5
MAX_ATTEMPTS  = 8
SITE_URL      = "https://arwordle.netlify.app/"
MP_THRESHOLD  = 500   # min search-pool size to justify parallel dispatch overhead

Pattern   = tuple[int, ...]
ALL_GREEN : Pattern = (2,) * WORD_LEN

# ─────────────────────────────────────────────────────────────────────────────
# Arabic normalization
# ─────────────────────────────────────────────────────────────────────────────

ARABIC_DIACRITICS = re.compile(r"[\u064B-\u065F\u0670]")

def normalize(word: str) -> str:
    """
    Strip diacritics (fatha, kasra, shadda etc.) only.
    Every base letter — including أ إ آ ا ة ؤ ئ — is kept as-is,
    because alwird_words.json confirms the game treats them as distinct.
    """
    return ARABIC_DIACRITICS.sub("", word)

# ─────────────────────────────────────────────────────────────────────────────
# Word list
# ─────────────────────────────────────────────────────────────────────────────

def load_wordlist() -> list[str]:
    if not os.path.exists(WORDLIST_FILE):
        print(f"  [!] Word list not found ({WORDLIST_FILE}).")
        print("      Go to the main menu and choose option 6 to download it first.")
        return []
    with open(WORDLIST_FILE, encoding="utf-8") as f:
        words = json.load(f)
    # Strip diacritics and filter to exactly WORD_LEN characters
    # Some entries in the raw list are shorter/longer after diacritic removal
    cleaned = []
    skipped = 0
    for w in words:
        w = ARABIC_DIACRITICS.sub("", w)   # strip diacritics only
        if len(w) == WORD_LEN:
            cleaned.append(w)
        else:
            skipped += 1
    print(f"  [✓] Loaded {len(cleaned):,} words  ({skipped} skipped — wrong length after cleaning)")
    return cleaned


def save_wordlist(words: list[str]) -> None:
    with open(WORDLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False, indent=2)
    print(f"  [✓] Saved {len(words):,} words to {WORDLIST_FILE}")


def extract_wordlist_from_site() -> list[str]:
    print(f"\n  [*] Fetching {SITE_URL} …")
    req = urllib.request.Request(SITE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        html = r.read().decode("utf-8")

    all_js_re = re.compile(r'["\'](/static/js/[^"\']+\.js)["\']')
    paths     = list(dict.fromkeys(all_js_re.findall(html)))
    if not paths:
        raise RuntimeError("Could not find any JS bundle URLs in page HTML.")

    print(f"  [*] Found {len(paths)} JS file(s)")
    all_words: set[str] = set()
    base = SITE_URL.rstrip("/")

    for path in paths:
        url = base + path
        print(f"  [*] Downloading {url} …")
        try:
            req2 = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req2, timeout=30) as r2:
                js = r2.read().decode("utf-8")
        except Exception as e:
            print(f"      ↳ skip ({e})")
            continue

        found_literal = re.findall(r'[؀-ۿ]{5}', js)

        escape_re     = re.compile(r'(?:\\u0[6-9A-Fa-f][0-9A-Fa-f]{2}){5}')
        found_escaped = []
        for m in escape_re.findall(js):
            try:
                found_escaped.append(m.encode().decode("unicode_escape"))
            except Exception:
                pass

        arabic_filter = re.compile(r'^[؀-ۿ]{5}$')
        found_quoted  = []
        for m in re.findall(r'"([^"]{5})"', js):
            try:
                decoded = json.loads(f'"{m}"')
                if arabic_filter.match(decoded):
                    found_quoted.append(decoded)
            except Exception:
                pass

        found_all = found_literal + found_escaped + found_quoted
        all_words.update(found_all)
        print(f"      ↳ {len(found_all)} Arabic 5-letter strings  (total: {len(all_words)})")

    if not all_words:
        raise RuntimeError("No words found. Try the manual DevTools method.")

    normalised = sorted({normalize(w) for w in all_words})
    print(f"\n  [✓] Total unique words after normalisation: {len(normalised):,}")
    return normalised

# ─────────────────────────────────────────────────────────────────────────────
# Core solver logic
# ─────────────────────────────────────────────────────────────────────────────

def compute_pattern(guess: str, answer: str) -> Pattern:
    if len(guess) != WORD_LEN or len(answer) != WORD_LEN:
        raise ValueError(f"compute_pattern got wrong lengths: {guess!r}({len(guess)}) vs {answer!r}({len(answer)})")
    result           = [0] * WORD_LEN
    answer_remaining : list[Optional[str]] = list(answer)
    for i in range(WORD_LEN):                        # pass 1: greens
        if guess[i] == answer[i]:
            result[i]           = 2
            answer_remaining[i] = None
    for i in range(WORD_LEN):                        # pass 2: yellows
        if result[i] == 2:
            continue
        if guess[i] in answer_remaining:
            result[i] = 1
            answer_remaining[answer_remaining.index(guess[i])] = None
    return tuple(result)


def compute_entropy(guess: str, candidates: list[str]) -> float:
    counts: dict[Pattern, int] = defaultdict(int)
    for answer in candidates:
        counts[compute_pattern(guess, answer)] += 1
    total   = len(candidates)
    entropy = 0.0
    for count in counts.values():
        p        = count / total
        entropy -= p * math.log2(p)
    return entropy


def filter_candidates(candidates: list[str], guess: str, pattern: Pattern) -> list[str]:
    return [w for w in candidates if compute_pattern(guess, w) == pattern]


def is_hard_mode_valid(guess: str, greens: dict[int, str], yellows: dict[str, set[int]]) -> bool:
    """
    Hard mode constraint: the guess must reuse all revealed hints.
      greens  = {position: letter} that were confirmed green
      yellows = {letter: set_of_positions_where_it_was_guessed_yellow}
                (letter must appear somewhere, just not those positions)
    """
    for pos, letter in greens.items():
        if guess[pos] != letter:
            return False
    for letter, wrong_positions in yellows.items():
        if letter not in guess:
            return False
        for pos in wrong_positions:
            if pos < len(guess) and guess[pos] == letter:
                return False
    return True


def best_guess(
    candidates   : list[str],
    search_pool  : list[str],
    top_n        : int  = 5,
    show_progress: bool = False,
    greens       : Optional[dict[int, str]]      = None,
    yellows      : Optional[dict[str, set[int]]] = None,
    mp_pool      : Any  = None,
) -> list[tuple[str, float, bool]]:
    """
    Score every word in search_pool by the entropy it produces over candidates.

    Normal mode  : search_pool = candidates          (fast)
    Legacy mode  : search_pool = all_words           (slow, thorough)
    Hard mode    : same as above but only words that reuse all revealed hints
    mp_pool      : live multiprocessing.Pool — uses parallel scoring when the
                   search pool exceeds MP_THRESHOLD words
    """
    if greens is not None and yellows is not None:
        effective_pool = [w for w in search_pool if is_hard_mode_valid(w, greens, yellows)]
    else:
        effective_pool = search_pool

    cand_set = set(candidates)

    # ── Parallel path ─────────────────────────────────────────────────────────
    if mp_pool is not None and len(effective_pool) >= MP_THRESHOLD:
        n_cores    = multiprocessing.cpu_count()
        chunk_size = max(1, math.ceil(len(effective_pool) / (n_cores * 2)))
        chunks     = [effective_pool[i : i + chunk_size]
                      for i in range(0, len(effective_pool), chunk_size)]
        args = [(chunk, candidates) for chunk in chunks]

        all_scores: list[tuple[str, float, bool]] = []
        try:
            for chunk_result in mp_pool.imap_unordered(_mp_score_chunk, args):
                all_scores.extend(chunk_result)
        except Exception:
            all_scores = [(w, compute_entropy(w, candidates), w in cand_set)
                          for w in effective_pool]

        all_scores.sort(key=lambda x: (x[1], x[2]), reverse=True)
        return all_scores[:top_n]

    # ── Single-threaded path ───────────────────────────────────────────────────
    scores    = []
    total     = len(effective_pool)
    bar_width = 40

    for i, word in enumerate(effective_pool):
        e = compute_entropy(word, candidates)
        scores.append((word, e, word in cand_set))

        if show_progress:
            filled      = int(bar_width * (i + 1) / total)
            bar         = "█" * filled + "░" * (bar_width - filled)
            pct         = 100 * (i + 1) / total
            best_so_far = max(scores, key=lambda x: x[1]) if scores else (word, 0, False)
            print(
                f"\r  [{bar}] {pct:5.1f}%  "
                f"{i+1:,}/{total:,}  "
                f"best so far: {best_so_far[0]} (H={best_so_far[1]:.3f})",
                end="", flush=True,
            )

    if show_progress:
        print()

    scores.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return scores[:top_n]

# ─────────────────────────────────────────────────────────────────────────────
# Hard mode hint tracker
# ─────────────────────────────────────────────────────────────────────────────

class HintTracker:
    """Tracks all green and yellow hints revealed so far."""

    def __init__(self):
        self.greens  : dict[int, str]       = {}   # pos → letter
        self.yellows : dict[str, set[int]]  = {}   # letter → positions where it was yellow

    def update(self, guess: str, pattern: Pattern) -> None:
        for i, (letter, p) in enumerate(zip(guess, pattern)):
            if p == 2:
                self.greens[i] = letter
            elif p == 1:
                if letter not in self.yellows:
                    self.yellows[letter] = set()
                self.yellows[letter].add(i)

    def summary(self) -> str:
        parts = []
        if self.greens:
            g = "  ".join(f"pos{p}={l}" for p, l in sorted(self.greens.items()))
            parts.append(f"Greens: {g}")
        if self.yellows:
            y = "  ".join(f"{l}≠{sorted(ps)}" for l, ps in self.yellows.items())
            parts.append(f"Yellows: {y}")
        return "  |  ".join(parts) if parts else "none yet"

# ─────────────────────────────────────────────────────────────────────────────
# Opener cache
# ─────────────────────────────────────────────────────────────────────────────

def load_opener_cache() -> Optional[list[str]]:
    if not os.path.exists(CACHE_FILE):
        return None
    with open(CACHE_FILE, "rb") as f:
        data = pickle.load(f)
    if isinstance(data, str):      # backward-compat: old cache stored a single string
        return [data]
    return data


def save_opener_cache(pool: list[str]) -> None:
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(pool, f)



# ─────────────────────────────────────────────────────────────────────────────
# Multiprocessing worker  (must be module-level for pickling)
# ─────────────────────────────────────────────────────────────────────────────

def _mp_score_chunk(args: tuple) -> list[tuple[str, float, bool]]:
    """
    Score a chunk of words against candidates.
    Called by each worker process independently.
    Returns list of (word, entropy, is_candidate).
    """
    chunk, candidates = args
    cand_set = set(candidates)
    results  = []
    for word in chunk:
        e = compute_entropy(word, candidates)
        results.append((word, e, word in cand_set))
    return results

def build_opener_pool(all_words: list[str], tolerance: float = 0.5) -> list[str]:
    """
    Score every word as a potential opener using all available CPU cores,
    then return all words within `tolerance` bits of the best score.

    Architecture:
      - Split all_words into N chunks (one per core)
      - Each worker scores its chunk independently via _mp_score_chunk
      - Main process draws a live progress bar while workers run
      - Results are merged, sorted, and filtered by tolerance
    """
    n_cores    = max(1, multiprocessing.cpu_count())
    total      = len(all_words)
    # Use small chunks (~600 words each) so workers report back frequently
    # giving smooth progress bar updates rather than one jump at the end.
    # n_cores workers pull from the queue continuously as chunks finish.
    chunk_size = max(1, min(600, math.ceil(total / (n_cores * 25))))
    chunks     = [all_words[i:i+chunk_size] for i in range(0, total, chunk_size)]

    print(f"\n  [*] Computing opener pool using {n_cores} CPU core(s).")
    print(f"      {total:,} words  ·  {len(chunks)} chunks of ~{chunk_size:,}  ·  {n_cores} parallel workers")
    print(f"      Grab a coffee ☕  (runs once, cached forever)\n")

    bar_width    = 40
    all_scores   : list[tuple[str, float, bool]] = []
    completed    = 0            # chunks finished
    best_so_far  = ("…", 0.0)

    def _draw_bar(done_words: int, best: tuple[str, float]) -> None:
        filled = int(bar_width * done_words / total)
        bar    = "█" * filled + "░" * (bar_width - filled)
        pct    = 100 * done_words / total
        print(
            f"\r  [{bar}] {pct:5.1f}%  "
            f"{done_words:,}/{total:,}  "
            f"best so far: {best[0]} (H={best[1]:.3f})",
            end="", flush=True,
        )

    try:
        with multiprocessing.Pool(n_cores) as pool:
            # imap_unordered returns results as each chunk completes
            # — lets us update the progress bar incrementally
            args = [(chunk, all_words) for chunk in chunks]
            for chunk_result in pool.imap_unordered(_mp_score_chunk, args):
                all_scores.extend(chunk_result)
                completed += len(chunk_result)

                # Update best seen so far across all returned results
                if all_scores:
                    best_so_far = max(all_scores, key=lambda x: x[1])[:2]

                _draw_bar(completed, best_so_far)

    except Exception as e:
        # Fallback: single-core with progress bar
        print(f"\n  [!] Multiprocessing failed ({e}), falling back to single core …\n")
        cand_set = set(all_words)
        for i, word in enumerate(all_words):
            entropy = compute_entropy(word, all_words)
            all_scores.append((word, entropy, word in cand_set))
            if entropy > best_so_far[1]:
                best_so_far = (word, entropy)
            _draw_bar(i + 1, best_so_far)

    print()  # newline after bar

    all_scores.sort(key=lambda x: x[1], reverse=True)
    best_entropy = all_scores[0][1]
    threshold    = best_entropy - tolerance
    pool_words   = [word for word, ent, _ in all_scores if ent >= threshold]

    print(f"\n  [✓] Best entropy : {best_entropy:.4f} bits")
    print(f"  [✓] Tolerance    : ±{tolerance} bits")
    print(f"  [✓] Pool size    : {len(pool_words):,} words  (all within {tolerance} bits of best)")
    return pool_words


def get_best_opener(all_words: list[str], n: int = 10) -> tuple[str, list[str]]:
    """
    Returns (chosen_opener, full_pool).
    Loads pool from cache if available, computes and caches it otherwise.
    Displays n random suggestions from the pool each run.
    """
    pool = load_opener_cache()
    if not pool:
        pool = build_opener_pool(all_words)
        save_opener_cache(pool)
        print(f"  [✓] Pool cached to {CACHE_FILE}")

    display = random.sample(pool, min(n, len(pool)))
    chosen  = display[0]

    print(f"\n  [✓] Opener pool: {len(pool):,} near-optimal words  (tolerance ±0.5 bits)")
    print(f"  Today's random suggestions:\n")
    for i, word in enumerate(display, 1):
        marker = "◆" if i == 1 else " "
        print(f"    {marker} {i:2}. {word}")
    print(f"\n  Recommended opener this run: {chosen}")
    return chosen, pool

# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────

COLOR = {2: "\033[42m", 1: "\033[43m", 0: "\033[100m"}
RESET = "\033[0m"

def render_pattern(guess: str, pattern: Pattern) -> str:
    return "".join(COLOR[p] + f" {c} " + RESET for c, p in zip(guess, pattern))

def parse_feedback(s: str) -> Optional[Pattern]:
    s = s.strip()
    if len(s) != WORD_LEN or not all(c in "012" for c in s):
        return None
    return tuple(int(c) for c in s)

def divider(char: str = "─", width: int = 58) -> None:
    print(char * width)

def header(title: str) -> None:
    divider("═")
    print(f"  {title}")
    divider("═")

# ─────────────────────────────────────────────────────────────────────────────
# Shared game engine
# ─────────────────────────────────────────────────────────────────────────────

def run_interactive_game(all_words: list[str], legacy: bool, hard: bool) -> None:
    """
    Core interactive loop shared by all three interactive modes.

    legacy=False  → search_pool = candidates  (fast, normal/hard)
    legacy=True   → search_pool = all_words   (slow, legacy)
    hard=True     → guesses must reuse revealed hints
    """
    mode_label = ("Legacy · " if legacy else "") + ("Hard Mode" if hard else "Normal Mode")
    header(f"Interactive  ·  {mode_label}")
    print("  Play on the site and enter your guesses + feedback here.")
    print("  Feedback: 2 = 🟩 Green   1 = 🟨 Yellow   0 = ⬜ Gray")
    if hard:
        print("  ⚠  Hard mode: you must reuse every revealed green and yellow letter.")
    divider()

    opener, _  = get_best_opener(all_words)
    candidates = list(all_words)
    tracker    = HintTracker()
    attempt    = 1

    with multiprocessing.Pool(max(1, multiprocessing.cpu_count())) as mp_pool:
        while attempt <= MAX_ATTEMPTS:
            search_pool = all_words if legacy else candidates
            print(f"\n  Attempt {attempt}/{MAX_ATTEMPTS}  —  {len(candidates):,} candidates remaining")
            if hard and (tracker.greens or tracker.yellows):
                print(f"  Required hints  : {tracker.summary()}")
            if len(candidates) <= 12:
                print("  Still possible  : " + "  ".join(candidates))

            if attempt == 1:
                suggestions = [(opener, compute_entropy(opener, candidates), opener in candidates)]
            else:
                suggestions = best_guess(
                    candidates, search_pool, top_n=5,
                    greens=tracker.greens if hard else None,
                    yellows=tracker.yellows if hard else None,
                    mp_pool=mp_pool,
                )

            print("\n  Top suggestions:")
            for rank, (word, ent, is_cand) in enumerate(suggestions, 1):
                marker = "★" if is_cand else "◇"
                print(f"    {rank}. {marker} {word}   H = {ent:.3f} bits")

            print()
            guess = input("  Your guess : ").strip()
            guess = normalize(guess)
            if len(guess) != WORD_LEN:
                print("  ✗ Must be exactly 5 Arabic letters.")
                continue

            if hard and attempt > 1 and not is_hard_mode_valid(guess, tracker.greens, tracker.yellows):
                print("  ✗ Hard mode violation — your guess doesn't reuse all revealed hints.")
                print(f"    Required : {tracker.summary()}")
                continue

            raw_fb  = input("  Feedback   : ").strip()
            pattern = parse_feedback(raw_fb)
            if pattern is None:
                print("  ✗ Invalid — enter exactly 5 digits from {0, 1, 2}.")
                continue

            print("  →", render_pattern(guess, pattern))

            if pattern == ALL_GREEN:
                print(f"\n  🎉  Solved in {attempt} attempt(s)!")
                return

            tracker.update(guess, pattern)
            candidates = filter_candidates(candidates, guess, pattern)
            if not candidates:
                print("\n  ✗ No candidates left — double-check your feedback.")
                return

            attempt += 1

    print("\n  ✗ Out of attempts. Remaining:", candidates)


def run_auto_game(all_words: list[str], legacy: bool, hard: bool) -> Optional[int]:
    """
    Core automatic simulation loop shared by all three automatic modes.

"""
    mode_label = ("Legacy · " if legacy else "") + ("Hard Mode" if hard else "Normal Mode")
    header(f"Automatic  ·  {mode_label}")
    word = input("  Enter the answer word to simulate against: ").strip()
    word = normalize(word)

    if len(word) != WORD_LEN:
        print("  ✗ Must be exactly 5 Arabic letters.")
        return None

    if word not in answers:
        print(f"  ⚠  '{word}' not in answers list — adding for this simulation.")
        answers = answers + [word]

    opener, _  = get_best_opener(all_words)
    candidates = list(all_words)
    tracker    = HintTracker()
    attempt    = 1

    print(f"\n  Answer: {word}")
    if hard:
        print("  ⚠  Hard mode: solver reuses all revealed hints each turn.")
    divider()

    with multiprocessing.Pool(max(1, multiprocessing.cpu_count())) as mp_pool:
        while attempt <= MAX_ATTEMPTS:
            search_pool = all_words if legacy else candidates

            if attempt == 1:
                guess = opener
            else:
                results = best_guess(
                    candidates, search_pool, top_n=1,
                    greens=tracker.greens if hard else None,
                    yellows=tracker.yellows if hard else None,
                    mp_pool=mp_pool,
                )
                if not results:
                    print("  ✗ Hard mode constraint left no valid guesses — stuck.")
                    return MAX_ATTEMPTS + 1
                guess = results[0][0]

            ent    = compute_entropy(guess, candidates)
            pat    = compute_pattern(guess, word)
            marker = "★" if guess in candidates else "◇"
            print(f"  Guess {attempt}: {marker} {guess}  (H={ent:.3f})  →  {render_pattern(guess, pat)}")

            if pat == ALL_GREEN:
                print(f"\n  ✓  Solved in {attempt} guess(es)!")
                return attempt

            tracker.update(guess, pat)
            candidates = filter_candidates(candidates, guess, pat)
            attempt   += 1

    print(f"  ✗  Failed. Remaining: {candidates}")
    return MAX_ATTEMPTS + 1

# ─────────────────────────────────────────────────────────────────────────────
# Benchmark
# ─────────────────────────────────────────────────────────────────────────────

def run_benchmark(all_words: list[str], legacy: bool, hard: bool) -> None:
    mode_label = ("Legacy · " if legacy else "") + ("Hard Mode" if hard else "Normal Mode")
    header(f"Benchmark  ·  {mode_label}")

    raw = input("  How many words to test? (default 100): ").strip()
    try:
        n = int(raw) if raw else 100
    except ValueError:
        n = 100

    opener, _ = get_best_opener(all_words)
    sample    = random.sample(all_words, min(n, len(all_words)))
    results   : list[int] = []

    print(f"\n  Running on {len(sample)} words …\n")

    with multiprocessing.Pool(max(1, multiprocessing.cpu_count())) as mp_pool:
        for i, answer in enumerate(sample, 1):
            candidates = list(all_words)
            tracker    = HintTracker()
            attempt    = 1
            solved     = False

            while attempt <= MAX_ATTEMPTS:
                search_pool = all_words if legacy else candidates

                if attempt == 1:
                    guess = opener
                else:
                    res = best_guess(
                        candidates, search_pool, top_n=1,
                        greens=tracker.greens if hard else None,
                        yellows=tracker.yellows if hard else None,
                        mp_pool=mp_pool,
                    )
                    if not res:
                        break
                    guess = res[0][0]

                pat = compute_pattern(guess, answer)
                if pat == ALL_GREEN:
                    solved = True
                    break
                tracker.update(guess, pat)
                candidates = filter_candidates(candidates, guess, pat)
                attempt   += 1

            results.append(attempt if solved else MAX_ATTEMPTS + 1)
            if i % 10 == 0:
                s   = [r for r in results if r <= MAX_ATTEMPTS]
                avg = sum(s) / len(s) if s else 0
                print(f"  {i}/{len(sample)}  avg guesses: {avg:.2f}")

    divider()
    solved_list = [r for r in results if r <= MAX_ATTEMPTS]
    failed_list = [r for r in results if r  > MAX_ATTEMPTS]
    dist        : dict[int, int] = defaultdict(int)
    for r in solved_list:
        dist[r] += 1

    print(f"\n  Words tested : {len(results)}")
    print(f"  Solved       : {len(solved_list)}  ({100*len(solved_list)/len(results):.1f}%)")
    print(f"  Failed       : {len(failed_list)}")
    if solved_list:
        print(f"  Avg guesses  : {sum(solved_list)/len(solved_list):.3f}")
        print(f"  Max guesses  : {max(solved_list)}")
    print("\n  Distribution:")
    for k in sorted(dist):
        print(f"    {k} guess(es): {'█' * dist[k]} {dist[k]}")

# ─────────────────────────────────────────────────────────────────────────────
# Utility modes
# ─────────────────────────────────────────────────────────────────────────────

def run_download() -> None:
    header("Download Word List  —  extract from arwordle.netlify.app")
    if os.path.exists(WORDLIST_FILE):
        ans = input(f"  {WORDLIST_FILE} already exists. Re-download? (y/n): ").strip().lower()
        if ans != "y":
            print("  Skipped.")
            return
    try:
        words = extract_wordlist_from_site()
        save_wordlist(words)
    except Exception as e:
        print(f"\n  [!] Failed: {e}")
        print("      Try the manual DevTools method instead.")


def run_warmup(all_words: list[str]) -> None:
    header("Warm Up Cache  —  pre-compute the opener pool")
    pool = load_opener_cache()
    if pool:
        ans = input(f"  Cache exists ({len(pool)} openers). Recompute? (y/n): ").strip().lower()
        if ans != "y":
            print("  Skipped.")
            return
        os.remove(CACHE_FILE)
    get_best_opener(all_words)

# ─────────────────────────────────────────────────────────────────────────────
# Sub-menus
# ─────────────────────────────────────────────────────────────────────────────

def submenu(title: str) -> Optional[bool]:
    """
    Show a normal/hard submenu for a given mode title.
    Returns hard flag (True/False), or None if user goes back.
    """
    print(f"""
  ┌─────────────────────────────────────────┐
  │  {title:<39}│
  ├─────────────────────────────────────────┤
  │  1 · Normal mode                        │
  │      Solver picks best guess freely     │
  │                                         │
  │  2 · Hard mode                          │
  │      Must reuse all revealed hints      │
  │                                         │
  │  0 · Back                               │
  └─────────────────────────────────────────┘""")
    choice = input("  Choose: ").strip()
    if choice == "1":
        return False
    elif choice == "2":
        return True
    else:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Main menu
# ─────────────────────────────────────────────────────────────────────────────

MENU = """
  ╔══════════════════════════════════════════════╗
  ║        AlWird Entropy Solver  🟩🟨⬜          ║
  ╠══════════════════════════════════════════════╣
  ║                                              ║
  ║  1 · Interactive mode                        ║
  ║      Play on site, solver guides you         ║
  ║       ├ 1a · Normal                          ║
  ║       └ 1b · Hard                            ║
  ║                                              ║
  ║  2 · Automatic mode                          ║
  ║      Solver plays itself against a word      ║
  ║       ├ 2a · Normal                          ║
  ║       └ 2b · Hard                            ║
  ║                                              ║
  ║  3 · Legacy mode  (slower, more thorough)    ║
  ║      Searches full vocabulary every turn     ║
  ║       ├ 3a · Interactive Normal              ║
  ║       ├ 3b · Interactive Hard                ║
  ║       ├ 3c · Automatic Normal                ║
  ║       └ 3d · Automatic Hard                  ║
  ║                                              ║
  ║  4 · Benchmark                               ║
  ║      Test accuracy on a random sample        ║
  ║       ├ 4a · Normal  ├ 4b · Hard             ║
  ║       └ 4c · Legacy Normal                   ║
  ║         4d · Legacy Hard                     ║
  ║                                              ║
  ║  5 · Warm up cache                           ║
  ║      Pre-compute best opening guess pool     ║
  ║                                              ║
  ║  6 · Download word list                      ║
  ║      Fetch words from the live site          ║
  ║                                              ║
  ║  0 · Exit                                    ║
  ╚══════════════════════════════════════════════╝
"""

# Map of shortcut inputs → (interactive, legacy, hard)
SHORTCUTS = {
    "1a": (True,  False, False),
    "1b": (True,  False, True),
    "2a": (False, False, False),
    "2b": (False, False, True),
    "3a": (True,  True,  False),
    "3b": (True,  True,  True),
    "3c": (False, True,  False),
    "3d": (False, True,  True),
    "4a": (None,  False, False),
    "4b": (None,  False, True),
    "4c": (None,  True,  False),
    "4d": (None,  True,  True),
}


def main() -> None:
    while True:
        print(MENU)
        choice = input("  Choose (e.g. 1, 1a, 2b, 4c …): ").strip().lower()

        if choice == "0":
            print("\n  Goodbye!\n")
            sys.exit(0)

        # ── Utility options ───────────────────────────────────────────────────
        if choice == "6":
            run_download()
            input("\n  Press Enter to return to menu …")
            continue

        if choice == "5":
            words = load_wordlist()
            if words:
                run_warmup(words)
            input("\n  Press Enter to return to menu …")
            continue

        # ── Shortcut: user typed e.g. "1a", "2b", "4d" directly ──────────────
        if choice in SHORTCUTS:
            words = load_wordlist()
            if not words:
                input("\n  Press Enter to return to menu …")
                continue
            interactive, legacy, hard = SHORTCUTS[choice]
            if interactive is None:
                run_benchmark(words, legacy=legacy, hard=hard)
            elif interactive:
                run_interactive_game(words, legacy=legacy, hard=hard)
            else:
                run_auto_game(words, legacy=legacy, hard=hard)
            input("\n  Press Enter to return to menu …")
            continue

        # ── Top-level numbers → show submenu ──────────────────────────────────
        if choice in ("1", "2", "3", "4"):
            words = load_wordlist()
            if not words:
                input("\n  Press Enter to return to menu …")
                continue

            if choice in ("1", "2"):
                label = {"1": "Interactive mode", "2": "Automatic mode"}[choice]
                hard = submenu(label)
                if hard is None:
                    continue
                if choice == "1":
                    run_interactive_game(words, legacy=False, hard=hard)
                else:
                    run_auto_game(words, legacy=False, hard=hard)

            elif choice == "3":
                print(f"""
  ┌─────────────────────────────────────────┐
  │  Legacy mode                            │
  ├─────────────────────────────────────────┤
  │  a · Interactive Normal                 │
  │  b · Interactive Hard                   │
  │  c · Automatic Normal                   │
  │  d · Automatic Hard                     │
  │  0 · Back                               │
  └─────────────────────────────────────────┘""")
                sub = input("  Choose: ").strip().lower()
                legacy_map = {
                    "a": (True,  False),
                    "b": (True,  True),
                    "c": (False, False),
                    "d": (False, True),
                }
                if sub in legacy_map:
                    interactive, hard = legacy_map[sub]
                    if interactive:
                        run_interactive_game(words, legacy=True, hard=hard)
                    else:
                        run_auto_game(words, legacy=True, hard=hard)
                else:
                    continue

            elif choice == "4":
                print(f"""
  ┌─────────────────────────────────────────┐
  │  Benchmark                              │
  ├─────────────────────────────────────────┤
  │  a · Normal                             │
  │  b · Hard                               │
  │  c · Legacy Normal                      │
  │  d · Legacy Hard                        │
  │  0 · Back                               │
  └─────────────────────────────────────────┘""")
                sub = input("  Choose: ").strip().lower()
                bench_map = {
                    "a": (False, False),
                    "b": (False, True),
                    "c": (True,  False),
                    "d": (True,  True),
                }
                if sub in bench_map:
                    lg, hd = bench_map[sub]
                    run_benchmark(words, legacy=lg, hard=hd)
                else:
                    continue

            input("\n  Press Enter to return to menu …")
            continue

        print("  ✗ Invalid — try 1, 1a, 2b, 3, 4c …")
        input("\n  Press Enter to return to menu …")


if __name__ == "__main__":
    main()
