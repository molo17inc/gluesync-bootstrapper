"""Command-line entrypoint for the Gluesync Automator."""

from __future__ import annotations

import argparse
import logging
import socket
import sys
import webbrowser

import uvicorn

from .app import create_app

LOGGER = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Gluesync Automator web UI")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host interface to bind (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on (default: 8080)",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Uvicorn log level (default: info)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload (development only)",
    )
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the Automator UI in the default browser after startup",
    )
    return parser.parse_args(argv)


def _is_port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    if _is_port_in_use(args.host, args.port):
        LOGGER.error("Port %s:%s is already in use", args.host, args.port)
        raise SystemExit(2)

    app = create_app()
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        reload=args.reload,
        lifespan="on",
    )
    server = uvicorn.Server(config)

    if args.open_browser:
        url = f"http://{args.host}:{args.port}"
        LOGGER.info("Opening browser at %s", url)
        webbrowser.open(url)

    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive use only
        LOGGER.info("Gluesync Automator interrupted by user")
        raise SystemExit(130) from None


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
