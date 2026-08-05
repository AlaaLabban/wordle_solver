#!/usr/bin/env python3
"""Flask web UI for AlWird Entropy Solver."""

import multiprocessing
import os
import random
import sys
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from math import ceil, log2

from flask import Flask, jsonify, render_template, request, session

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alwird_solver as solver

app = Flask(__name__)
app.secret_key = "alwird-solver-web-ui-2024"

# ── Globals (populated in _app_startup) ──────────────────────────────────────
ALL_WORDS: list = []      # full valid-guess vocabulary (~120k)
ALL_ANSWERS: list = []    # answers-only candidate pool (~2,354)
CANDIDATE_POOL: list = [] # active candidate source — ALL_WORDS or ALL_ANSWERS
CACHE_MODE: str = "answers"  # "full" or "answers"
OPENER: str = ""
MP_POOL = None

SESSIONS: dict = {}   # session_id → GameState
JOBS: dict = {}       # job_id → {status, progress, ...}


@dataclass
class GameState:
    candidates: list = field(default_factory=list)
    attempt: int = 1
    history: list = field(default_factory=list)
    legacy: bool = False
    hard: bool = False
    greens: dict = field(default_factory=dict)    # {pos(int): letter}
    yellows: dict = field(default_factory=dict)   # {letter: [positions]}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _suggestion_stats(word: str, candidates: list) -> tuple:
    """Return (entropy, expected_remaining, words_eliminated) for word vs candidates."""
    counts: dict = defaultdict(int)
    for ans in candidates:
        counts[solver.compute_pattern(word, ans)] += 1
    N = len(candidates)
    if N == 0:
        return 0.0, 0.0, 0.0
    ent = -sum((c / N) * log2(c / N) for c in counts.values())
    exp_rem = sum(c * c for c in counts.values()) / N
    return round(ent, 3), round(exp_rem, 1), round(N - exp_rem, 1)


def _get_state() -> "GameState | None":
    sid = session.get("sid")
    return SESSIONS.get(sid) if sid else None


# ── Background job: best_guess with progress ──────────────────────────────────

def _run_guess_job(job_id: str, candidates: list, search_pool: list,
                   hard_greens: dict, hard_yellows: dict, top_n: int = 5) -> None:
    """Score search_pool words against candidates, with optional hard-mode filtering."""
    try:
        cand_set = set(candidates)

        # Apply hard mode constraint filtering to the search pool
        if hard_greens or hard_yellows:
            yellows_sets = {k: set(v) for k, v in hard_yellows.items()}
            effective_pool = [w for w in search_pool
                              if solver.is_hard_mode_valid(w, hard_greens, yellows_sets)]
            if not effective_pool:
                effective_pool = search_pool
        else:
            effective_pool = search_pool

        total = len(effective_pool)
        all_scores: list = []

        JOBS[job_id]["progress_total"]   = total
        JOBS[job_id]["progress_current"] = 0

        if MP_POOL is not None and total >= solver.MP_THRESHOLD:
            n_cores = multiprocessing.cpu_count()
            chunk_size = max(1, ceil(total / (n_cores * 2)))
            chunks = [effective_pool[i:i + chunk_size] for i in range(0, total, chunk_size)]
            args = [(chunk, candidates) for chunk in chunks]
            completed = 0
            try:
                for chunk_result in MP_POOL.imap_unordered(solver._mp_score_chunk, args):
                    all_scores.extend(chunk_result)
                    completed += len(chunk_result)
                    JOBS[job_id]["progress"]         = int(100 * completed / total)
                    JOBS[job_id]["progress_current"] = completed
            except Exception:
                all_scores = []
                for i, word in enumerate(effective_pool):
                    e = solver.compute_entropy(word, candidates)
                    all_scores.append((word, e, word in cand_set))
                    JOBS[job_id]["progress"]         = int(100 * (i + 1) / total)
                    JOBS[job_id]["progress_current"] = i + 1
        else:
            for i, word in enumerate(effective_pool):
                e = solver.compute_entropy(word, candidates)
                all_scores.append((word, e, word in cand_set))
                JOBS[job_id]["progress"]         = int(100 * (i + 1) / total)
                JOBS[job_id]["progress_current"] = i + 1

        all_scores.sort(key=lambda x: (x[1], x[2]), reverse=True)

        suggestions = []
        for word, _, is_cand in all_scores[:top_n]:
            ent, exp_rem, eliminated = _suggestion_stats(word, candidates)
            suggestions.append({
                "word": word,
                "entropy": ent,
                "expected_remaining": exp_rem,
                "words_eliminated": eliminated,
                "is_candidate": is_cand,
            })

        JOBS[job_id] = {
            "status": "done", "progress": 100,
            "progress_current": total, "progress_total": total,
            "suggestions": suggestions,
        }

    except Exception as e:
        JOBS[job_id] = {"status": "error", "message": str(e)}


# ── Background job: full auto-solve ──────────────────────────────────────────

def _run_auto_job(job_id: str, answer: str, legacy: bool = False, hard: bool = False) -> None:
    """Auto-solve: solver plays itself to completion, updating progress per turn."""
    try:
        candidates = list(CANDIDATE_POOL)
        if answer not in candidates:
            candidates.append(answer)

        greens: dict = {}   # {pos: letter}  — tracked for hard mode
        yellows: dict = {}  # {letter: set(positions)}

        trace = []
        solved = False

        for attempt in range(1, solver.MAX_ATTEMPTS + 1):
            turn_base = int((attempt - 1) / solver.MAX_ATTEMPTS * 100)
            turn_top  = int(attempt / solver.MAX_ATTEMPTS * 100)
            JOBS[job_id]["progress"]         = turn_base
            JOBS[job_id]["progress_total"]   = len(candidates)
            JOBS[job_id]["progress_current"] = 0

            if attempt == 1:
                guess = OPENER
            else:
                search_pool = list(ALL_WORDS) if legacy else candidates
                cand_set    = set(candidates)

                # Hard mode: filter search pool to valid guesses
                if hard:
                    effective_pool = [w for w in search_pool
                                      if solver.is_hard_mode_valid(w, greens, yellows)]
                    if not effective_pool:
                        effective_pool = search_pool
                else:
                    effective_pool = search_pool

                total = len(effective_pool)
                JOBS[job_id]["progress_total"] = total
                all_scores: list = []

                if MP_POOL is not None and total >= solver.MP_THRESHOLD:
                    n_cores = multiprocessing.cpu_count()
                    chunk_size = max(1, ceil(total / (n_cores * 2)))
                    chunks = [effective_pool[i:i + chunk_size] for i in range(0, total, chunk_size)]
                    args = [(chunk, candidates) for chunk in chunks]
                    completed = 0
                    try:
                        for chunk_result in MP_POOL.imap_unordered(solver._mp_score_chunk, args):
                            all_scores.extend(chunk_result)
                            completed += len(chunk_result)
                            within = int((completed / total) * (turn_top - turn_base))
                            JOBS[job_id]["progress"]         = turn_base + within
                            JOBS[job_id]["progress_current"] = completed
                    except Exception:
                        all_scores = [(w, solver.compute_entropy(w, candidates), w in cand_set)
                                      for w in effective_pool]
                else:
                    all_scores = [(w, solver.compute_entropy(w, candidates), w in cand_set)
                                  for w in effective_pool]

                if not all_scores:
                    break
                all_scores.sort(key=lambda x: (x[1], x[2]), reverse=True)
                guess = all_scores[0][0]

            ent, exp_rem, eliminated = _suggestion_stats(guess, candidates)
            pattern = solver.compute_pattern(guess, answer)

            trace.append({
                "attempt": attempt,
                "guess": guess,
                "pattern": list(pattern),
                "entropy": ent,
                "expected_remaining": exp_rem,
                "words_eliminated": eliminated,
                "candidates_before": len(candidates),
                "is_candidate": guess in candidates,
            })

            if pattern == solver.ALL_GREEN:
                solved = True
                break

            # Update hard mode hint tracker
            if hard:
                for i, (letter, p) in enumerate(zip(guess, pattern)):
                    if p == 2:
                        greens[i] = letter
                    elif p == 1:
                        yellows.setdefault(letter, set()).add(i)

            candidates = solver.filter_candidates(candidates, guess, pattern)
            if not candidates:
                break

        JOBS[job_id] = {
            "status": "done", "progress": 100,
            "solved": solved, "attempts": len(trace), "trace": trace,
        }

    except Exception as e:
        JOBS[job_id] = {"status": "error", "message": str(e)}


# ── Best-guess helper (used by benchmark) ─────────────────────────────────────

def _best_word(effective_pool: list, candidates: list) -> str:
    """Return the highest-entropy word in effective_pool against candidates."""
    total = len(effective_pool)
    if MP_POOL is not None and total >= solver.MP_THRESHOLD:
        n_cores = multiprocessing.cpu_count()
        chunk_size = max(1, ceil(total / (n_cores * 2)))
        chunks = [effective_pool[i:i + chunk_size] for i in range(0, total, chunk_size)]
        args = [(chunk, candidates) for chunk in chunks]
        best, best_key = None, (-1.0, False)
        try:
            for chunk_result in MP_POOL.imap_unordered(solver._mp_score_chunk, args):
                for w, e, is_cand in chunk_result:
                    if (e, is_cand) > best_key:
                        best_key, best = (e, is_cand), w
            if best is not None:
                return best
        except Exception:
            pass
    cand_set = set(candidates)
    best, best_key = effective_pool[0], (-1.0, False)
    for w in effective_pool:
        e = solver.compute_entropy(w, candidates)
        key = (e, w in cand_set)
        if key > best_key:
            best_key, best = key, w
    return best


# ── Background job: benchmark (test a mode on N random answers) ────────────────

def _run_benchmark_job(job_id: str, n: int, legacy: bool, hard: bool) -> None:
    """Simulate the solver on N random answers; collect a guess-count distribution."""
    try:
        sample = random.sample(CANDIDATE_POOL, min(n, len(CANDIDATE_POOL)))
        total  = len(sample)
        results: list = []
        curve_sum: dict = defaultdict(float)   # attempt → Σ candidates before that guess
        curve_cnt: dict = defaultdict(int)

        JOBS[job_id]["progress_total"] = total

        for idx, answer in enumerate(sample):
            candidates = list(CANDIDATE_POOL)
            if answer not in candidates:
                candidates.append(answer)
            greens: dict = {}
            yellows: dict = {}
            used = solver.MAX_ATTEMPTS + 1   # default = fail

            for attempt in range(1, solver.MAX_ATTEMPTS + 1):
                curve_sum[attempt] += len(candidates)
                curve_cnt[attempt] += 1

                if attempt == 1:
                    guess = OPENER
                else:
                    search_pool = ALL_WORDS if legacy else candidates
                    if hard:
                        effective = [w for w in search_pool
                                     if solver.is_hard_mode_valid(w, greens, yellows)]
                        if not effective:
                            effective = search_pool
                    else:
                        effective = search_pool
                    guess = _best_word(effective, candidates)

                pattern = solver.compute_pattern(guess, answer)
                if pattern == solver.ALL_GREEN:
                    used = attempt
                    break

                if hard:
                    for i, (letter, p) in enumerate(zip(guess, pattern)):
                        if p == 2:
                            greens[i] = letter
                        elif p == 1:
                            yellows.setdefault(letter, set()).add(i)

                candidates = solver.filter_candidates(candidates, guess, pattern)
                if not candidates:
                    break

            results.append(used)
            JOBS[job_id]["progress"]         = int(100 * (idx + 1) / total)
            JOBS[job_id]["progress_current"] = idx + 1

        solved_list = [r for r in results if r <= solver.MAX_ATTEMPTS]
        fails       = sum(1 for r in results if r > solver.MAX_ATTEMPTS)
        dist        = {k: 0 for k in range(1, solver.MAX_ATTEMPTS + 1)}
        for r in solved_list:
            dist[r] += 1
        avg_curve = [{"attempt": a, "avg": round(curve_sum[a] / curve_cnt[a], 1)}
                     for a in sorted(curve_cnt)]

        JOBS[job_id] = {
            "status": "done", "progress": 100,
            "n": total,
            "solved": len(solved_list), "failed": fails,
            "avg": round(sum(solved_list) / len(solved_list), 3) if solved_list else None,
            "max": max(solved_list) if solved_list else None,
            "distribution": dist, "avg_curve": avg_curve,
        }

    except Exception as e:
        JOBS[job_id] = {"status": "error", "message": str(e)}


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info")
def api_info():
    return jsonify(
        word_count=len(ALL_WORDS), answer_count=len(ALL_ANSWERS),
        candidate_count=len(CANDIDATE_POOL), cache_mode=CACHE_MODE,
        max_attempts=solver.MAX_ATTEMPTS, opener=OPENER,
    )


@app.route("/api/start", methods=["POST"])
def api_start():
    data   = request.get_json() or {}
    legacy = bool(data.get("legacy", False))
    hard   = bool(data.get("hard",   False))

    sid = str(uuid.uuid4())
    session["sid"] = sid
    SESSIONS[sid] = GameState(candidates=list(CANDIDATE_POOL), legacy=legacy, hard=hard)

    ent, exp_rem, eliminated = _suggestion_stats(OPENER, CANDIDATE_POOL)
    return jsonify(
        opener=OPENER, opener_entropy=ent,
        expected_remaining=exp_rem, words_eliminated=eliminated,
        candidates_remaining=len(CANDIDATE_POOL),
        max_attempts=solver.MAX_ATTEMPTS,
    )


@app.route("/api/submit", methods=["POST"])
def api_submit():
    state = _get_state()
    if not state:
        return jsonify(error="No active game — click New Game first."), 400

    data = request.get_json()
    guess = solver.normalize((data.get("guess") or "").strip())
    pattern_raw = data.get("pattern", [])

    if len(guess) != solver.WORD_LEN:
        return jsonify(error="Guess must be exactly 5 Arabic letters."), 400
    if len(pattern_raw) != solver.WORD_LEN or not all(p in (0, 1, 2) for p in pattern_raw):
        return jsonify(error="Pattern must be 5 values of 0, 1, or 2."), 400

    pattern = tuple(pattern_raw)

    # Hard mode: reject guess if it violates revealed hints
    if state.hard and (state.greens or state.yellows):
        yellows_sets = {k: set(v) for k, v in state.yellows.items()}
        if not solver.is_hard_mode_valid(guess, state.greens, yellows_sets):
            return jsonify(error="Hard mode: your guess must use all revealed hints."), 400

    # Record stats for this guess before filtering
    ent, exp_rem, eliminated = _suggestion_stats(guess, state.candidates)
    state.history.append({
        "guess": guess, "pattern": list(pattern),
        "entropy": ent, "expected_remaining": exp_rem,
        "words_eliminated": eliminated, "candidates_before": len(state.candidates),
    })

    # Filter candidates
    state.candidates = solver.filter_candidates(state.candidates, guess, pattern)
    state.attempt += 1

    # Update hard mode hints
    if state.hard:
        for i, (letter, p) in enumerate(zip(guess, pattern)):
            if p == 2:
                state.greens[i] = letter
            elif p == 1:
                state.yellows.setdefault(letter, []).append(i)

    if pattern == solver.ALL_GREEN:
        return jsonify(status="solved", attempt=state.attempt - 1)
    if not state.candidates:
        return jsonify(status="no_candidates", message="No candidates left — check your feedback.")
    if state.attempt > solver.MAX_ATTEMPTS:
        return jsonify(status="failed", message="Out of attempts.")

    search_pool = list(ALL_WORDS) if state.legacy else list(state.candidates)

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {
        "status": "pending", "progress": 0,
        "progress_current": 0, "progress_total": len(search_pool),
    }
    threading.Thread(
        target=_run_guess_job,
        args=(job_id, list(state.candidates), search_pool,
              dict(state.greens), dict(state.yellows), 5),
        daemon=True,
    ).start()

    return jsonify(
        job_id=job_id,
        candidates_remaining=len(state.candidates),
        attempt=state.attempt,
        max_attempts=solver.MAX_ATTEMPTS,
    )


@app.route("/api/status/<job_id>")
def api_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(status="error", message="Unknown job."), 404
    return jsonify(job)


@app.route("/api/auto", methods=["POST"])
def api_auto():
    data   = request.get_json()
    answer = solver.normalize((data.get("answer") or "").strip())
    legacy = bool(data.get("legacy", False))
    hard   = bool(data.get("hard",   False))

    if len(answer) != solver.WORD_LEN:
        return jsonify(error="Answer must be exactly 5 Arabic letters."), 400

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "pending", "progress": 0, "progress_current": 0, "progress_total": 0}
    threading.Thread(target=_run_auto_job, args=(job_id, answer, legacy, hard), daemon=True).start()
    return jsonify(job_id=job_id)


@app.route("/api/set-cache", methods=["POST"])
def api_set_cache():
    global OPENER, CANDIDATE_POOL, CACHE_MODE
    data = request.get_json() or {}
    mode = data.get("mode", "full")
    if mode == "answers":
        if not os.path.exists(solver.CACHE_FILE_ANSWERS):
            return jsonify(error=f"Answers cache not found: {solver.CACHE_FILE_ANSWERS}"), 404
        if not ALL_ANSWERS:
            return jsonify(error=f"Answers word list not found: {solver.ANSWERS_FILE}"), 404
        solver.CACHE_FILE = solver.CACHE_FILE_ANSWERS
        CANDIDATE_POOL    = ALL_ANSWERS
        CACHE_MODE        = "answers"
    else:
        solver.CACHE_FILE = "alwird_cache.pkl"
        CANDIDATE_POOL    = ALL_WORDS
        CACHE_MODE        = "full"
    OPENER, _ = solver.get_best_opener(CANDIDATE_POOL)
    ent, exp_rem, eliminated = _suggestion_stats(OPENER, CANDIDATE_POOL)
    return jsonify(
        mode=mode, opener=OPENER,
        opener_entropy=ent, expected_remaining=exp_rem,
        candidate_count=len(CANDIDATE_POOL),
    )


@app.route("/api/score", methods=["POST"])
def api_score():
    """Live analysis of a single guess against the active game's candidates."""
    state = _get_state()
    if not state:
        return jsonify(error="No active game."), 400

    data  = request.get_json() or {}
    guess = solver.normalize((data.get("guess") or "").strip())
    if len(guess) != solver.WORD_LEN:
        return jsonify(valid=False)

    cands = state.candidates
    N = len(cands)
    if N == 0:
        return jsonify(valid=True, entropy=0, expected_remaining=0, words_eliminated=0,
                       is_candidate=False, n=0, max_entropy=0, num_buckets=0, buckets=[])

    counts: dict = defaultdict(int)
    for ans in cands:
        counts[solver.compute_pattern(guess, ans)] += 1

    ent     = -sum((c / N) * log2(c / N) for c in counts.values())
    exp_rem = sum(c * c for c in counts.values()) / N
    buckets = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    bucket_list = [{"pattern": list(p), "size": c} for p, c in buckets[:16]]

    return jsonify(
        valid=True,
        guess=guess,
        entropy=round(ent, 3),
        expected_remaining=round(exp_rem, 1),
        words_eliminated=round(N - exp_rem, 1),
        is_candidate=guess in set(cands),
        n=N,
        max_entropy=round(log2(N), 3),
        num_buckets=len(counts),
        buckets=bucket_list,
    )


@app.route("/api/benchmark", methods=["POST"])
def api_benchmark():
    data   = request.get_json() or {}
    legacy = bool(data.get("legacy", False))
    hard   = bool(data.get("hard",   False))
    try:
        n = int(data.get("n", 30))
    except (TypeError, ValueError):
        n = 30
    n = max(1, min(n, len(CANDIDATE_POOL)))

    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"status": "pending", "progress": 0,
                    "progress_current": 0, "progress_total": n}
    threading.Thread(target=_run_benchmark_job, args=(job_id, n, legacy, hard),
                     daemon=True).start()
    return jsonify(job_id=job_id, n=n, cache_mode=CACHE_MODE)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    sid = session.pop("sid", None)
    if sid:
        SESSIONS.pop(sid, None)
    return jsonify(ok=True)


# ── Startup ───────────────────────────────────────────────────────────────────

def _app_startup() -> None:
    global ALL_WORDS, ALL_ANSWERS, CANDIDATE_POOL, OPENER, MP_POOL, CACHE_MODE
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ALL_WORDS = solver.load_wordlist()
    if not ALL_WORDS:
        print("[!] Word list not found. Run the CLI and choose option 6 first.")
        sys.exit(1)
    ALL_ANSWERS = solver.load_answers()
    if ALL_ANSWERS:
        print(f"  [✓] Answers list available: {len(ALL_ANSWERS):,} words (toggle in the UI)")
    # Default to the official answers list: it matches the real game, so the
    # advice and the accuracy numbers are the realistic ones. Fall back to the
    # full vocabulary only if the answers list is missing.
    CANDIDATE_POOL = ALL_ANSWERS if ALL_ANSWERS else ALL_WORDS
    if not ALL_ANSWERS:
        CACHE_MODE = "full"
    OPENER, _ = solver.get_best_opener(CANDIDATE_POOL)
    MP_POOL = multiprocessing.Pool(multiprocessing.cpu_count())
    print(f"  [✓] Web UI ready — http://127.0.0.1:5050   (opener: {OPENER})")


if __name__ == "__main__":
    _app_startup()
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
