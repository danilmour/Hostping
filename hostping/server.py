"""HTTP server for hostping (SPEC §6): JSON API + static frontend.

Uses only ``http.server.ThreadingHTTPServer`` from the standard library.
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import checks
from . import hosts_file as hosts_file_module

LOGGER = logging.getLogger("hostping.server")

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 1.0

# Fixed path -> (filename, content type). No user input ever reaches the path,
# so directory traversal is not possible.
_STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"


def default_web_dir():
    """Return the ``web/`` directory that ships next to the package."""
    package_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(package_dir), "web")


class HostpingHandler(BaseHTTPRequestHandler):
    server_version = "hostping/1.0"

    # -- request routing ---------------------------------------------------

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler naming
        path = urlsplit(self.path).path
        try:
            self._route(path)
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.debug("client disconnected for %s", path)
        except Exception as exc:  # noqa: BLE001 - never crash a request thread
            LOGGER.exception("unhandled error for %s", path)
            try:
                self._send_json(500, {"error": "internal server error: %s" % exc})
            except Exception:  # pragma: no cover - headers already sent
                pass

    def _route(self, path):
        if path == "/api/hosts":
            return self._api_hosts()
        if path == "/api/health":
            return self._send_json(200, {"status": "ok"})
        if path in _STATIC_FILES:
            return self._static(path)
        if path.startswith("/api/"):
            return self._send_json(404, {"error": "not found"})
        return self._send_text(404, "404 Not Found\n")

    # -- handlers ----------------------------------------------------------

    def _api_hosts(self):
        config = self.server
        try:
            payload = checks.run_scan(
                hosts_file=config.hosts_file,
                timeout=config.timeout,
                checker=config.checker,
                max_workers=config.max_workers,
            )
        except Exception as exc:  # noqa: BLE001 - e.g. unreadable hosts file
            LOGGER.warning("could not build hosts payload: %s", exc)
            return self._send_json(
                500,
                {
                    "error": str(exc) or exc.__class__.__name__,
                    "hosts_file": str(config.hosts_file),
                },
            )
        return self._send_json(200, payload)

    def _static(self, path):
        filename, content_type = _STATIC_FILES[path]
        full_path = os.path.join(self.server.web_dir, filename)
        try:
            with open(full_path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self._send_text(404, "404 Not Found: %s\n" % filename)
        return self._send_bytes(200, body, content_type)

    # -- low-level helpers -------------------------------------------------

    def _send_bytes(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(code, body, _JSON_CONTENT_TYPE)

    def _send_text(self, code, text):
        self._send_bytes(code, text.encode("utf-8"), _TEXT_CONTENT_TYPE)

    def log_message(self, fmt, *args):  # noqa: A003 - base class signature
        LOGGER.debug("%s - %s", self.address_string(), fmt % args)


class HostpingServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the runtime configuration for requests."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address,
        handler_class=HostpingHandler,
        *,
        hosts_file=None,
        timeout=DEFAULT_TIMEOUT,
        web_dir=None,
        checker=None,
        max_workers=checks.DEFAULT_WORKERS,
    ):
        super().__init__(server_address, handler_class)
        self.hosts_file = hosts_file or hosts_file_module.DEFAULT_HOSTS_FILE
        self.timeout = timeout
        self.web_dir = web_dir or default_web_dir()
        self.checker = checker
        self.max_workers = max_workers


def create_server(
    host=DEFAULT_BIND,
    port=DEFAULT_PORT,
    hosts_file=None,
    timeout=DEFAULT_TIMEOUT,
    *,
    web_dir=None,
    checker=None,
    max_workers=checks.DEFAULT_WORKERS,
):
    """Create (but do not start) a :class:`HostpingServer`."""
    return HostpingServer(
        (host, port),
        hosts_file=hosts_file,
        timeout=timeout,
        web_dir=web_dir,
        checker=checker,
        max_workers=max_workers,
    )


def serve(
    host=DEFAULT_BIND,
    port=DEFAULT_PORT,
    hosts_file=None,
    timeout=DEFAULT_TIMEOUT,
    *,
    web_dir=None,
    checker=None,
    max_workers=checks.DEFAULT_WORKERS,
):
    """Create a server and serve requests until interrupted (blocking)."""
    httpd = create_server(
        host,
        port,
        hosts_file,
        timeout,
        web_dir=web_dir,
        checker=checker,
        max_workers=max_workers,
    )
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
    return httpd


# Alias matching the alternate name used in the spec/team notes.
run = serve
