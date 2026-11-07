"""Command-line entrypoint for rendering or serving Worktrack Prometheus metrics."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from worktrack.config import load_settings
from worktrack.telemetry.metrics import TelemetryRecorder
from worktrack.telemetry.server import MetricsHttpServer

EXIT_SUCCESS: int = 0


def build_argument_parser() -> argparse.ArgumentParser:
    """Build argument parser for `python -m worktrack.telemetry`."""
    parser = argparse.ArgumentParser(
        prog="python -m worktrack.telemetry",
        description="Render or serve Worktrack Prometheus metrics and /healthz.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start HTTP server exposing /metrics and /healthz.",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Optional bind host override (defaults to WORKTRACK_METRICS_HOST).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Optional bind TCP port override (defaults to WORKTRACK_METRICS_PORT).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute telemetry exposition CLI or start `/metrics` HTTP server."""
    args = build_argument_parser().parse_args(argv)
    settings = load_settings()
    recorder = TelemetryRecorder.from_settings(settings)

    if args.serve:
        host = settings.metrics_host if args.host is None else str(args.host)
        port = settings.metrics_port if args.port is None else int(args.port)
        server = MetricsHttpServer(
            recorder=recorder,
            host=host,
            port=port,
            shutdown_timeout=settings.metrics_shutdown_timeout,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return EXIT_SUCCESS
        return EXIT_SUCCESS

    sys.stdout.write(recorder.render_metrics_text())
    return EXIT_SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
