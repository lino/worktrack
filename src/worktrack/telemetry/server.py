"""Lightweight HTTP server exposing `/metrics` and `/healthz` endpoints."""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from urllib.parse import urlsplit

from prometheus_client import CONTENT_TYPE_LATEST

from worktrack.config import (
    APP_SLUG,
    DEFAULT_METRICS_HOST,
    DEFAULT_METRICS_PORT,
    DEFAULT_METRICS_SHUTDOWN_TIMEOUT,
)
from worktrack.telemetry.metrics import TelemetryRecorder

JSON_CONTENT_TYPE = "application/json; charset=utf-8"
METRICS_ROUTE = "/metrics"
HEALTHZ_ROUTE = "/healthz"
DEFAULT_SHUTDOWN_TIMEOUT_SECONDS: float = DEFAULT_METRICS_SHUTDOWN_TIMEOUT


class _TelemetryHttpServer(ThreadingHTTPServer):
    """`ThreadingHTTPServer` subclass carrying an attached `TelemetryRecorder`."""

    def __init__(
        self,
        server_address: tuple[str, int],
        recorder: TelemetryRecorder,
    ) -> None:
        """Bind server socket and attach `recorder` for request handlers."""
        self.recorder = recorder
        super().__init__(server_address, _TelemetryRequestHandler)


class _TelemetryRequestHandler(BaseHTTPRequestHandler):
    """HTTP GET handler serving `/metrics` and `/healthz`."""

    server: _TelemetryHttpServer

    def do_GET(self) -> None:
        """Handle HTTP GET requests for `/metrics` and `/healthz`."""
        route = urlsplit(self.path).path
        recorder = self.server.recorder
        if route == METRICS_ROUTE:
            self._write_response(HTTPStatus.OK, CONTENT_TYPE_LATEST, recorder.render_metrics())
        elif route == HEALTHZ_ROUTE:
            body = json.dumps(
                {"status": "ok", "namespace": APP_SLUG, "mode": recorder.mode}
            ).encode("utf-8")
            self._write_response(HTTPStatus.OK, JSON_CONTENT_TYPE, body)
        else:
            body = json.dumps({"status": "not_found"}).encode("utf-8")
            self._write_response(HTTPStatus.NOT_FOUND, JSON_CONTENT_TYPE, body)

    def log_message(self, format: str, *args: object) -> None:
        """Suppress noisy default stderr access logs during health probes."""
        return

    def _write_response(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        """Send status code, headers, and body bytes to the HTTP client."""
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MetricsHttpServer:
    """Threaded HTTP server serving Prometheus `/metrics` and `/healthz`."""

    def __init__(
        self,
        recorder: TelemetryRecorder,
        *,
        host: str = DEFAULT_METRICS_HOST,
        port: int = DEFAULT_METRICS_PORT,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        """Create an HTTP exposition server bound to `host` and `port`.

        Args:
            recorder: `TelemetryRecorder` rendering Prometheus exposition.
            host: Bind hostname or IP address.
            port: Bind TCP port (`0` selects an OS-assigned ephemeral port).
            shutdown_timeout: Thread join timeout in seconds on shutdown.
        """
        self._httpd = _TelemetryHttpServer((host, port), recorder)
        self._shutdown_timeout = shutdown_timeout
        self._thread: threading.Thread | None = None

    @property
    def bound_host(self) -> str:
        """Return actual bound IP/hostname string."""
        address = self._httpd.server_address[0]
        return address.decode("utf-8") if isinstance(address, bytes) else str(address)

    @property
    def bound_port(self) -> int:
        """Return actual bound TCP port number."""
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        """Return base `http://<host>:<port>` URL for local probes."""
        return f"http://{self.bound_host}:{self.bound_port}"

    def start_background(self) -> None:
        """Start serving HTTP requests on a daemon background thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="worktrack-metrics-http",
            daemon=True,
        )
        self._thread.start()

    def serve_forever(self) -> None:
        """Serve HTTP requests synchronously in the foreground until interrupted."""
        try:
            self._httpd.serve_forever()
        finally:
            self._httpd.server_close()

    def shutdown(self) -> None:
        """Stop background HTTP server and release listening socket."""
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join(timeout=self._shutdown_timeout)
            self._thread = None
        self._httpd.server_close()

    def __enter__(self) -> MetricsHttpServer:
        """Enter context manager by starting background HTTP server."""
        self.start_background()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit context manager by shutting down background HTTP server."""
        self.shutdown()
