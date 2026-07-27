# Deployment

## Docker

```bash
docker build -t campsite-checker .
docker run -v $(pwd)/campsites.yaml:/app/campsites.yaml:ro campsite-checker
```

The Dockerfile defaults to `python check_campsites.py --forever --dashboard` and uses Python 3.14-slim. The container runs as the unprivileged user `app` (uid 10001); any mounted volume the checker writes (e.g. the `state/` directory) must be writable by that uid on the host.

### Runtime tunables

| Variable | Image default | Purpose |
| --- | --- | --- |
| `WORKERS` | 4 | Concurrently executing search batches, process-wide. |
| `ALERT_INTERVAL` | 1 | Minutes between alert-tier scans. |
| `SEARCH_DELAY` | 1 | Seconds of per-provider batch submission pacing. |
| `BATCH_SIZE` | 4 | Campgrounds per search batch. |
| `DASHBOARD_INTERVAL` | 10 | Minutes between dashboard-only scans. |
| `GC_INTERVAL` | 12 | Scans between forced full garbage collections. |
| `THROTTLE_BASE_DELAY` | 30 | Seconds of initial provider cooldown after a 429. |
| `THROTTLE_MAX_DELAY` | 900 | Cap on the computed exponential cooldown, in seconds. |
| `CAMPLY_CACHE_DIR` | `/app/state/camply-cache` | UseDirect offline metadata cache. |
| `SENT_KEYS_PATH` | unset | Where to persist Telegram dedup keys across restarts. |
| `PORT` | 8000 | Health and metrics HTTP server port. |

The first six are baked into the image's `CMD` as CLI flags, so overriding them requires setting the environment variable on the container.

Trading batch size against worker count is the main scan-latency lever: camply issues one request per campground per month sequentially within a batch, so smaller batches with more workers cut dashboard scan wall-clock. Dial back toward the image defaults if provider throttle cooldowns start firing.

`CAMPLY_CACHE_DIR` matters because camply would otherwise write the cache into site-packages, which the unprivileged user cannot do. Outside Docker it defaults to `.camply-cache/`.

Of these, only `THROTTLE_BASE_DELAY` and `THROTTLE_MAX_DELAY` are wired through `docker-compose.yml` to a host `.env`; the rest keep their image defaults unless added to the compose `environment:` block. `docker-compose.yml` also defines a healthcheck against the health endpoint, so a stale checker is reported as an unhealthy container.

## Homelab

Runs on the homelab box at `/srv/docker/campground-checker`, managed by `docker compose`. A successful CI run on `main` triggers `.github/workflows/deploy.yml` (via `workflow_run`), which runs on the self-hosted runner labelled `homelab`; the runner resets that directory to the CI-validated commit, builds the image first (so a broken build never replaces the running container), and then recreates it — the deployment directory, not the runner workdir, is the live checkout.

Credentials (`TELEGRAM_*`, `R2_*`) come from `/srv/docker/campground-checker/.env` (mode 600), not from Actions secrets, so a manual `docker compose up -d` behaves exactly like CI. Telegram dedup keys persist in `state/sent_keys.json` via `SENT_KEYS_PATH`; the container runs as uid 10001, so `state/` must be owned by that uid (one-time `chown -R 10001:10001 state` on the host). Health endpoint: `http://<host>:8000/`.
