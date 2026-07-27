"""Health check and metrics HTTP server.

Serves the JSON health payload from `status.py` and the Prometheus exposition
from `metrics.py`. Deliberately has no filesystem access and no other routes.
"""

import http.server
import json
import logging
import os
import threading

from .status import scan_status

logger = logging.getLogger(__name__)


class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
    """Serves health JSON and Prometheus metrics only — no filesystem access."""

    def do_GET(self):
        if self.path.partition("?")[0] == "/metrics":
            body = scan_status.to_prometheus().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        data = scan_status.to_dict()
        code = 200 if scan_status.is_healthy() else 503
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress default per-request logging."""
        pass


def start_healthcheck_server() -> None:
    """Start a threaded HTTP server so one slow client cannot block probes."""
    port = int(os.environ.get("PORT", "8000"))
    try:
        httpd = http.server.ThreadingHTTPServer(("", port), HealthCheckHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        logger.info("Healthcheck HTTP server running on port %d", port)
    except Exception as e:
        logger.error("Failed to start healthcheck server on port %d: %s", port, e)
