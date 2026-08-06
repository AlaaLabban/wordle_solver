# ── AlWird Solver — production image ────────────────────────────────────────
#   Build:  docker build -t alwird-solver .
#   Run:    docker run -d --name alwird-app -p 5050:5050 --shm-size=1g \
#               --restart unless-stopped alwird-solver
# ────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

# Predictable, log-friendly Python with no stray .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy the dependency manifest first so Docker caches the pip layer and only
# reinstalls when requirements.txt actually changes. Gunicorn is declared
# there too, so requirements.txt stays the single source of truth.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application (scripts, word lists, .pkl caches, templates).
COPY . .

# Run as an unprivileged user rather than root.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5050

# Liveness probe: the container is healthy only once the app answers on /.
# Check /api/info rather than /, and assert the word lists actually loaded.
# "/" only renders a static template, so it returned 200 from a container whose
# solver had never initialised — a broken image reported itself healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request,json,sys; d=json.load(urllib.request.urlopen('http://127.0.0.1:5050/api/info', timeout=4)); sys.exit(0 if d.get('candidate_count',0) > 0 and d.get('opener') else 1)"

# IMPORTANT — a SINGLE worker (with threads), not multiple workers.
# app.py keeps session/job state in-process (the SESSIONS/JOBS dicts) and
# streams progress via polling, so every request must reach the same process.
# Threads handle concurrent requests; the heavy entropy math still uses all
# CPU cores via the internal multiprocessing pool.
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "120", "-b", "0.0.0.0:5050", "app:app"]
