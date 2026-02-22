import http.server
import logging
import os
import socketserver
import threading


class HealthCheckHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")


def start_healthcheck_server() -> None:
    """Start a simple HTTP server to pass health checks (e.g. Koyeb)."""
    port = int(os.environ.get("PORT", "8000"))
    socketserver.TCPServer.allow_reuse_address = True
    try:
        httpd = socketserver.TCPServer(("", port), HealthCheckHandler)
        logging.getLogger().info(f"Starting healthcheck server on port {port}")
        
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        print(f"   Healthcheck HTTP server running on port {port}")
    except Exception as e:
        print(f"   [ERROR] Failed to start healthcheck server on port {port}: {e}")
        logging.getLogger().error(f"Failed to start healthcheck server: {e}")
