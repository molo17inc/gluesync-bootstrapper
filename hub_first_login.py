# This program is part of Gluesync.
#
# Bootstrapper is dual-licensed under the following licenses:
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

"""Complete the first access of a fresh Core Hub: admin/admin -> the password the caller wants.

This is the ``hub-first-login`` one-shot the demo kits run before the bootstrapper. On Linux the kits inline it as a
``python -c`` script in the bootstrapper image; the Windows image has no Python (it carries one PyInstaller exe), so
the same logic lives here and is reached with ``gluesync-bootstrapper.exe hub-first-login``.

The behaviour is the one of the inline script:

* wait for Core Hub to answer ``POST /authentication/login`` (admin/admin), up to ``HUB_LOGIN_ATTEMPTS`` tries
  ``HUB_LOGIN_INTERVAL`` seconds apart;
* ``changeRequired`` in the answer: ``POST /authentication/reset-password`` to ``HUB_ADMIN_PASSWORD``;
* otherwise the login with ``HUB_ADMIN_PASSWORD`` must work (the first access was already completed).

Exit codes: 0 done (or already done), 1 Core Hub answered something unexpected, 2 Core Hub never answered.
"""

import argparse
import os
import sys
import time

import requests
import urllib3

DEFAULT_CORE_HUB_URL = "http://gluesync-core-hub:1717"
DEFAULT_ATTEMPTS = 90
DEFAULT_INTERVAL = 2.0
DEFAULT_REQUEST_TIMEOUT = 10.0
DEFAULT_ADMIN_USER = "admin"
INITIAL_ADMIN_PASSWORD = "admin"

EXIT_OK = 0
EXIT_UNEXPECTED_RESPONSE = 1
EXIT_UNREACHABLE = 2


def _env_flag(name, default=False):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "t", "y", "yes")


def _env_number(name, default, cast):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw.strip())
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="gluesync-bootstrapper hub-first-login",
        description=(
            "Complete the first access of a fresh Core Hub: log in as admin/admin and, when Core Hub asks for it, "
            "set the admin password to HUB_ADMIN_PASSWORD."
        ),
        epilog=(
            "Environment: CORE_HUB_URL (default %s), HUB_ADMIN_PASSWORD (required; DEFAULT_PASSWORD is used when it "
            "is not set), HUB_LOGIN_ATTEMPTS, HUB_LOGIN_INTERVAL, SSL_SKIP_VERIFY. Exit codes: 0 done, 1 unexpected "
            "Core Hub response, 2 Core Hub not reachable." % DEFAULT_CORE_HUB_URL
        ),
    )
    parser.add_argument("--attempts", type=int, help="connection attempts before giving up (default 90)")
    parser.add_argument("--interval", type=float, help="seconds between two attempts (default 2)")
    parser.add_argument("--timeout", type=float, help="seconds of a single request (default 10)")
    return parser


def _login(session, url, password, timeout):
    return session.post(
        url + "/authentication/login",
        json={"username": DEFAULT_ADMIN_USER, "password": password},
        timeout=timeout,
    )


def _wait_for_core_hub(session, url, attempts, interval, timeout):
    """Return the answer of the first login that reaches Core Hub, or the last connection error."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return _login(session, url, INITIAL_ADMIN_PASSWORD, timeout), None
        except requests.RequestException as error:
            last_error = error
            print(f"attempt {attempt}/{attempts}: hub not ready yet", flush=True)
            if attempt < attempts:
                time.sleep(interval)
    return None, last_error


def _body(response):
    """The JSON object of an answer, or an empty one when Core Hub did not send JSON."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _describe(error):
    text = str(error).strip().splitlines()[0] if str(error).strip() else ""
    if len(text) > 240:
        text = text[:237] + "..."
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def run(url, new_password, attempts, interval, timeout, verify=True):
    """Run the first login against ``url``; returns the process exit code."""
    with requests.Session() as session:
        session.verify = verify
        response, error = _wait_for_core_hub(session, url, attempts, interval, timeout)
        if response is None:
            print(
                f"ERROR: Core Hub not reachable at {url} after {attempts} attempt(s): {_describe(error)}",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_UNREACHABLE

        body = _body(response)
        if response.status_code == 200 and body.get("changeRequired") and body.get("apiToken"):
            result = session.post(
                url + "/authentication/reset-password",
                headers={"Authorization": "Bearer " + body["apiToken"]},
                json={"oldPassword": INITIAL_ADMIN_PASSWORD, "newPassword": new_password},
                timeout=timeout,
            )
            print("first access completed:", result.status_code, result.text, flush=True)
            if not result.ok:
                print(f"ERROR: Core Hub refused the password change (HTTP {result.status_code})", file=sys.stderr)
                return EXIT_UNEXPECTED_RESPONSE
            return EXIT_OK

        try:
            known = _login(session, url, new_password, timeout)
        except requests.RequestException as login_error:
            print(f"ERROR: Core Hub stopped answering at {url}: {_describe(login_error)}", file=sys.stderr)
            return EXIT_UNREACHABLE
        if known.status_code == 200:
            print("admin password already set", flush=True)
            return EXIT_OK

        print(f"ERROR: unexpected login response {response.status_code}: {response.text}", file=sys.stderr)
        return EXIT_UNEXPECTED_RESPONSE


def main(argv=None):
    args = build_parser().parse_args(argv)
    url = (os.environ.get("CORE_HUB_URL") or DEFAULT_CORE_HUB_URL).strip().rstrip("/")
    new_password = os.environ.get("HUB_ADMIN_PASSWORD") or os.environ.get("DEFAULT_PASSWORD")
    if not new_password:
        print("ERROR: HUB_ADMIN_PASSWORD is not set: it is the admin password to set on Core Hub", file=sys.stderr)
        return EXIT_UNEXPECTED_RESPONSE

    attempts = args.attempts if args.attempts is not None else _env_number("HUB_LOGIN_ATTEMPTS", DEFAULT_ATTEMPTS, int)
    interval = args.interval if args.interval is not None else _env_number("HUB_LOGIN_INTERVAL", DEFAULT_INTERVAL, float)
    timeout = args.timeout if args.timeout is not None else DEFAULT_REQUEST_TIMEOUT
    if attempts < 1:
        print("ERROR: attempts must be at least 1", file=sys.stderr)
        return EXIT_UNEXPECTED_RESPONSE

    verify = not _env_flag("SSL_SKIP_VERIFY")
    if not verify:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return run(url, new_password, attempts, interval, timeout, verify=verify)


if __name__ == "__main__":
    sys.exit(main())
