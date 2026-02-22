# ---------- build stage (compile C extensions, then discard toolchain) ----------
FROM python:3.14-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- runtime stage (no compiler, smaller image) ----------
FROM python:3.14-slim

# Reduce memory: use system malloc so glibc can return pages to the OS,
# and tell glibc to trim the heap aggressively.
ENV PYTHONMALLOC=malloc
ENV MALLOC_TRIM_THRESHOLD_=65536
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Copy only the installed packages from the builder stage
COPY --from=builder /install /usr/local

COPY . .

RUN chmod +x check_campsites.py

# Tunable via Railway / docker run -e WORKERS=2
ENV WORKERS="10"
ENV INTERVAL="5"
ENV SEARCH_DELAY="0"

CMD ["sh", "-c", "python check_campsites.py --forever --dashboard --workers $WORKERS --interval $INTERVAL --search-delay $SEARCH_DELAY"]
