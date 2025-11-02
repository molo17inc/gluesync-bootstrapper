# This program is part of Gluesync.
#
# Automator is dual-licensed under the following licenses:
#
# 1. GNU General Public License (GPL) Version 3
#    You may use, modify, and distribute this software under the terms of the GPL v3.
#    See the LICENSE-GPL file or <http://www.gnu.org/licenses/gpl-3.0.html> for details.
#    This option is available at no cost, but any derivative works must also be licensed under GPL v3.
#
# 2. MOLO17 Commercial License
#    Alternatively, you may use this software under the MOLO17 Commercial License,
#    which includes a warranty and permits proprietary use. Contact MOLO17 at info@molo17.com
#    for licensing terms and conditions.
#
# You must choose one of these licenses to use this software. Using this software implies
# acceptance of one of these licenses. See the accompanying LICENSE files or contact
# MOLO17 for more information.
#
# Copyright (C) 2025 MOLO17. All rights reserved.

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
