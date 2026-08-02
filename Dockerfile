# ---------- build stage ----------
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY campsite_checker/ ./campsite_checker/
COPY check_campsites.py ./

ENV UV_COMPILE_BYTECODE=1
ENV UV_PROJECT_ENVIRONMENT=/install
RUN uv sync --frozen --no-dev --no-install-package campsite-checker

# ---------- runtime stage ----------
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

# Reduce memory: use system malloc so glibc can return pages to the OS,
# and tell glibc to trim the heap aggressively.
ENV PYTHONMALLOC=malloc
ENV MALLOC_TRIM_THRESHOLD_=65536
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy the venv from builder
COPY --from=builder /install /install
ENV PATH="/install/bin:$PATH"
ENV PYTHONPATH="/install/lib/python3.14/site-packages"

# Only runtime files; nothing else from the build context belongs in the image.
COPY campsite_checker/ ./campsite_checker/
COPY check_campsites.py campsites.yaml ./

# The unprivileged user needs to own /app so the dashboard HTML can be written
# there. The mounted state/ volume must be writable by uid 10001 on the host.
RUN useradd --system --uid 10001 --user-group app && chown app:app /app
USER app

ENV WORKERS="4"
ENV ALERT_INTERVAL="1"
ENV SEARCH_DELAY="1"
ENV BATCH_SIZE="4"
ENV DASHBOARD_INTERVAL="30"
ENV GC_INTERVAL="12"
ENV THROTTLE_BASE_DELAY="30"
ENV THROTTLE_MAX_DELAY="900"
# Camply's UseDirect metadata cache defaults to site-packages, which is not
# writable by the unprivileged user; keep it on the persistent state mount.
ENV CAMPLY_CACHE_DIR="/app/state/camply-cache"

CMD ["sh", "-c", "python check_campsites.py --forever --dashboard --workers $WORKERS --alert-interval $ALERT_INTERVAL --search-delay $SEARCH_DELAY --batch-size $BATCH_SIZE --dashboard-interval $DASHBOARD_INTERVAL --gc-interval $GC_INTERVAL"]
