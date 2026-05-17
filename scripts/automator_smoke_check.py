#!/usr/bin/env python3
"""Wait for the Automator smoke server to come up and validate its endpoints."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Optional


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the Automator smoke server")
    parser.add_argument(
        "--base-url",
        required=True,
        help="Base URL for the running Automator server, e.g. http://127.0.0.1:18080",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="Maximum time to wait for the server, in seconds",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Delay between health checks, in seconds",
    )
    return parser.parse_args(argv)


def _healthz_body_is_ok(body: str) -> bool:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    deadline = time.time() + args.timeout
    last_error: Optional[Exception] = None

    health_url = f"{args.base_url}/api/healthz"
    root_url = f"{args.base_url}/"

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=2) as response:
                body = response.read().decode("utf-8")
                if response.status == 200 and _healthz_body_is_ok(body):
                    break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(args.interval)
    else:
        print(f"Timed out waiting for {health_url}: {last_error}", file=sys.stderr)
        return 1

    with urllib.request.urlopen(root_url, timeout=2) as response:
        if response.status != 200:
            print(f"Root endpoint returned HTTP {response.status}", file=sys.stderr)
            return 1
        if "text/html" not in response.headers.get_content_type():
            print("Root endpoint did not return HTML", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
