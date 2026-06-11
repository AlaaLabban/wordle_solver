#!/usr/bin/env python3
"""Flask web UI for AlWird Entropy Solver."""

import multiprocessing
import os
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
ALL_WORDS: list = []
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
        candidates = list(ALL_WORDS)
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


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info")
def api_info():
    return jsonify(word_count=len(ALL_WORDS), max_attempts=solver.MAX_ATTEMPTS, opener=OPENER)


@app.route("/api/start", methods=["POST"])
def api_start():
    data   = request.get_json() or {}
    legacy = bool(data.get("legacy", False))
    hard   = bool(data.get("hard",   False))

    sid = str(uuid.uuid4())
    session["sid"] = sid
    SESSIONS[sid] = GameState(candidates=list(ALL_WORDS), legacy=legacy, hard=hard)

    ent, exp_rem, eliminated = _suggestion_stats(OPENER, ALL_WORDS)
    return jsonify(
        opener=OPENER, opener_entropy=ent,
        expected_remaining=exp_rem, words_eliminated=eliminated,
        candidates_remaining=len(ALL_WORDS),
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


@app.route("/api/reset", methods=["POST"])
def api_reset():
    sid = session.pop("sid", None)
    if sid:
        SESSIONS.pop(sid, None)
    return jsonify(ok=True)


# ── Startup ───────────────────────────────────────────────────────────────────

def _app_startup() -> None:
    global ALL_WORDS, OPENER, MP_POOL
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    ALL_WORDS = solver.load_wordlist()
    if not ALL_WORDS:
        print("[!] Word list not found. Run the CLI and choose option 6 first.")
        sys.exit(1)
    OPENER, _ = solver.get_best_opener(ALL_WORDS)
    MP_POOL = multiprocessing.Pool(multiprocessing.cpu_count())
    print(f"  [✓] Web UI ready — http://127.0.0.1:5050   (opener: {OPENER})")


if __name__ == "__main__":
    _app_startup()
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
