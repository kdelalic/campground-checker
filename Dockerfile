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

COPY . .

RUN chmod +x check_campsites.py

ENV WORKERS="4"
ENV ALERT_INTERVAL="5"
ENV SEARCH_DELAY="0"
ENV BATCH_SIZE="4"
ENV DASHBOARD_INTERVAL="60"
ENV GC_INTERVAL="12"

CMD ["sh", "-c", "python check_campsites.py --forever --dashboard --workers $WORKERS --alert-interval $ALERT_INTERVAL --search-delay $SEARCH_DELAY --batch-size $BATCH_SIZE --dashboard-interval $DASHBOARD_INTERVAL --gc-interval $GC_INTERVAL"]
