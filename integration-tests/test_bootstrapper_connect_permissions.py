#!/usr/bin/env python3
"""End-to-end Connect setup against a local permission server. No Core Hub."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import connect_portable as connect

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "admin-pass"
VIEWER_EMAIL = "viewer@example.com"
VIEWER_PASSWORD = "viewer-pass"
HUB_ADMIN = "hub-admin"
HUB_ADMIN_PASSWORD = "hub-admin-pass"
HUB_OPERATOR = "hub-operator"
HUB_OPERATOR_PASSWORD = "hub-operator-pass"
MQTT = "tcp://mosquitto:1883"
_INVALID = (
    "Invalid authorization token, please retry or make a new login "
    "in order to obtain a fresh token"
)
_CAPS = {
    "owner": frozenset({"site.read", "site.update", "site.enroll", "member.write", "org.write"}),
    "admin": frozenset({"site.read", "site.update", "site.enroll", "member.write", "org.write"}),
    "operator": frozenset({"site.read", "site.update"}),
    "viewer": frozenset({"site.read"}),
}


def _bearer(headers):
    raw = headers.get("Authorization") or headers.get("authorization") or ""
    if raw.startswith("Bearer "):
        token = raw[7:].strip()
        if token:
            return token
    cookie = headers.get("Cookie") or headers.get("cookie") or ""
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "gs-auth" and value:
            return value
    return None


def _denied(role):
    return 403, {"message": f"Your role ({role}) does not have permission to perform this action"}


def _unauthorized():
    return 401, {"notificationId": -1, "message": _INVALID, "exceptionMessage": None}


class PermissionState:
    """Tokens issued here keep the permission they were minted with."""

    def __init__(self):
        self.records = {}
        self.orgs = {}
        self.members = {}
        self.sites = {}
        self.enrollments = {}
        self.configure_calls = []
        self.org_writes = []
        self._n = 0
        self.connect_users = {
            ADMIN_EMAIL: {"password": ADMIN_PASSWORD, "role": "owner", "id": "user-admin", "fullName": "Kit Admin"},
            VIEWER_EMAIL: {"password": VIEWER_PASSWORD, "role": "viewer", "id": "user-viewer", "fullName": "Viewer"},
        }
        self.hub_users = {
            HUB_ADMIN: {"password": HUB_ADMIN_PASSWORD, "role": "SUPER_ADMIN"},
            HUB_OPERATOR: {"password": HUB_OPERATOR_PASSWORD, "role": "ADMIN"},
        }

    def mint(self, kind, role, email=None):
        self._n += 1
        token = f"{kind}-{role}-{self._n}"
        self.records[token] = {"kind": kind, "role": role, "email": email}
        return token

    def connect_record(self, headers, capability):
        token = _bearer(headers)
        record = self.records.get(token or "")
        if not record or record["kind"] != "connect":
            return None, _unauthorized()
        if capability not in _CAPS.get(record["role"], frozenset()):
            return None, _denied(record["role"])
        return record, None

    def connect_login(self, body):
        user = self.connect_users.get((body or {}).get("email"))
        if not user or user["password"] != (body or {}).get("password"):
            return _unauthorized()
        token = self.mint("connect", user["role"], user and (body or {}).get("email"))
        return 200, {"token": token, "session": {"userId": user["id"]}}

    def hub_login(self, body):
        user = self.hub_users.get((body or {}).get("username"))
        if not user or user["password"] != (body or {}).get("password"):
            return _unauthorized()
        token = self.mint("hub", user["role"])
        return 200, {"token": token, "changeRequired": False}

    def configure(self, headers, body):
        token = _bearer(headers)
        record = self.records.get(token or "")
        if not record or record["kind"] != "hub":
            self.configure_calls.append({"status": 401, "role": None, "auth": token, "enrollment": None})
            return _unauthorized()
        if record["role"] != "SUPER_ADMIN":
            enrollment = (body or {}).get("enrollmentToken")
            self.configure_calls.append(
                {"status": 403, "role": record["role"], "auth": token, "enrollment": enrollment}
            )
            return _denied(record["role"])
        payload = body or {}
        enrollment = payload.get("enrollmentToken") or ""
        connect_url = payload.get("connectUrl") or ""
        if not connect_url or not enrollment:
            return 400, {"message": "Missing connectUrl or enrollmentToken"}
        if enrollment not in self.enrollments:
            self.configure_calls.append(
                {"status": 400, "role": record["role"], "auth": token, "enrollment": enrollment}
            )
            return 400, {"message": "Unknown enrollment token"}
        self.configure_calls.append(
            {"status": 200, "role": record["role"], "auth": token, "enrollment": enrollment}
        )
        return 200, {"success": True}


def _org_collection(state, method, headers, body):
    record, error = state.connect_record(headers, "org.write" if method == "POST" else "site.read")
    if error:
        return error
    if method == "GET":
        return 200, list(state.orgs.values())
    if method != "POST":
        return 404, {"message": "not found"}
    slug = (body or {}).get("slug") or connect.slugify((body or {}).get("name") or "")
    state.org_writes.append(dict(body or {}))
    if any(org.get("slug") == slug for org in state.orgs.values()):
        return 409, {"error": {"code": "conflict", "message": "slug taken"}}
    org_id = f"org_{len(state.orgs) + 1}"
    user = state.connect_users[record["email"]]
    state.orgs[org_id] = {"id": org_id, "name": body.get("name"), "slug": slug, "billingEmail": body.get("billingEmail")}
    member_id = f"mem_{org_id}"
    state.members[org_id] = [
        {
            "id": member_id,
            "role": "owner",
            "user": {"email": record["email"], "fullName": user["fullName"]},
        }
    ]
    state.sites[org_id] = []
    return 201, {"id": org_id, "slug": slug, "name": body.get("name")}


def _patch_org(state, org_id, headers, body):
    _record, error = state.connect_record(headers, "org.write")
    if error:
        return error
    if org_id not in state.orgs:
        return 404, {"message": "not found"}
    state.org_writes.append(dict(body or {}))
    state.orgs[org_id].update({key: value for key, value in (body or {}).items() if key != "id"})
    return 200, {"id": org_id}


def _list_members(state, org_id, headers):
    _record, error = state.connect_record(headers, "site.read")
    if error:
        return error
    return 200, list(state.members.get(org_id, []))


def _patch_member(state, org_id, member_id, headers, body):
    _record, error = state.connect_record(headers, "member.write")
    if error:
        return error
    for member in state.members.get(org_id, []):
        if member["id"] == member_id:
            member["role"] = (body or {}).get("role", member["role"])
            return 200, {"id": member_id, "role": member["role"]}
    return 404, {"message": "not found"}


def _list_sites(state, org_id, headers):
    _record, error = state.connect_record(headers, "site.read")
    if error:
        return error
    return 200, list(state.sites.get(org_id, []))


def _create_site(state, org_id, headers, body):
    _record, error = state.connect_record(headers, "site.enroll")
    if error:
        return error
    site_id = f"site_{len(state.sites.get(org_id, [])) + 1}"
    site = {"id": site_id, "name": (body or {}).get("name")}
    state.sites.setdefault(org_id, []).append(site)
    return 201, site


def _patch_site(state, org_id, site_id, headers, body):
    _record, error = state.connect_record(headers, "site.update")
    if error:
        return error
    for site in state.sites.get(org_id, []):
        if site["id"] == site_id:
            site.update(body or {})
            site["id"] = site_id
            return 200, {"id": site_id}
    return 404, {"message": "not found"}


def _allowlist(state, org_id, site_id, headers, body):
    _record, error = state.connect_record(headers, "site.update")
    if error:
        return error
    return 200, {"id": site_id, "proxyIpAllowlist": (body or {}).get("proxyIpAllowlist")}


def _mint_enrollment(state, org_id, headers, body):
    record, error = state.connect_record(headers, "site.enroll")
    if error:
        return error
    if org_id not in state.orgs:
        return 404, {"message": "not found"}
    token = state.mint("enrollment", "mqtt", record["email"])
    state.enrollments[token] = {"siteId": (body or {}).get("siteId"), "ttlMinutes": (body or {}).get("ttlMinutes")}
    return 201, {"token": token, "tokenId": f"et-{len(state.enrollments)}", "expiresAt": "2026-01-01T01:00:00Z"}


def _child_marker(parts):
    if parts[-1] in ("proxy-allowlist", "enrollment-tokens"):
        return parts[-1]
    return parts[1]


def _org_child(state, method, parts, headers, body):
    org_id = parts[0]
    if len(parts) == 1 and method == "PATCH":
        return _patch_org(state, org_id, headers, body)
    routes = {
        (2, "GET", "members"): lambda: _list_members(state, org_id, headers),
        (3, "PATCH", "members"): lambda: _patch_member(state, org_id, parts[2], headers, body),
        (2, "GET", "sites"): lambda: _list_sites(state, org_id, headers),
        (2, "POST", "sites"): lambda: _create_site(state, org_id, headers, body),
        (3, "PATCH", "sites"): lambda: _patch_site(state, org_id, parts[2], headers, body),
        (4, "PATCH", "proxy-allowlist"): lambda: _allowlist(state, org_id, parts[2], headers, body),
        (3, "POST", "enrollment-tokens"): lambda: _mint_enrollment(state, org_id, headers, body),
    }
    handler = routes.get((len(parts), method, _child_marker(parts)))
    if handler is None:
        return 404, {"message": "not found"}
    return handler()


def dispatch(state, method, path, headers, body):
    path = path.split("?", 1)[0]
    if method == "POST" and path == "/api/auth/password/login":
        return state.connect_login(body)
    if method == "POST" and path == connect._HUB_LOGIN:
        return state.hub_login(body)
    if method == "POST" and path == connect._HUB_CONFIGURE:
        return state.configure(headers, body)
    if path == connect._API_ORGANIZATIONS:
        return _org_collection(state, method, headers, body)
    prefix = connect._API_ORGANIZATIONS + "/"
    if path.startswith(prefix):
        return _org_child(state, method, path[len(prefix):].split("/"), headers, body)
    return 404, {"message": "not found"}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _go(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw.decode()) if raw else None
        status, payload = dispatch(self.server.state, self.command, self.path, dict(self.headers), body)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._go()

    def do_POST(self):
        self._go()

    def do_PATCH(self):
        self._go()


class PermissionServer:
    def __init__(self):
        self.state = PermissionState()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.state = self.state
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _request(base, method, path, token=None, body=None, cookie=False):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token and cookie:
        request.add_header("Cookie", f"gs-auth={token}")
    elif token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        parsed = json.loads(raw) if raw else {}
        return exc.code, parsed


def _bundle():
    return {
        "kind": connect.BUNDLE_KIND,
        "organization": {
            "name": "Acme",
            "slug": "acme",
            "billingEmail": "billing@acme.example",
            "taxId": "IT123",
            "address": {"line1": "1 Road", "city": "Rome", "postalCode": "00100", "country": "IT"},
            "defaultTimezone": "Europe/Rome",
            "settings": {"advisory": {"enabled": False}},
        },
        "members": [{"role": "owner", "user": {"email": ADMIN_EMAIL, "fullName": "Kit Admin"}}],
        "sites": [
            {
                "name": "Plant",
                "description": "line",
                "tags": ["prod"],
                "proxyIpAllowlist": ["10.0.0.0/8"],
            }
        ],
    }


def _printed_token(stdout):
    lines = [line for line in stdout.splitlines() if line.startswith("GLUESYNC_CONNECT_TOKEN=")]
    if len(lines) != 1:
        raise AssertionError(f"expected one GLUESYNC_CONNECT_TOKEN line, saw {len(lines)}")
    return lines[0].split("=", 1)[1]


def _run_setup(base, bundle_path, extra):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("CORE", "CONNECT", "GLUESYNC", "GS_")):
            env.pop(key, None)
    env.update(
        {
            "CONNECT_API_URL": base,
            "CONNECT_ADMIN_EMAIL": ADMIN_EMAIL,
            "CONNECT_ADMIN_PASSWORD": ADMIN_PASSWORD,
            "GLUESYNC_CONNECT_URL": MQTT,
            "COREHUB_URL": base,
            "CORE_HUB_URL": base,
        }
    )
    env.update(extra)
    return subprocess.run(
        [sys.executable, "main.py", "--connect-import", bundle_path, "--connect-setup"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


class ConnectPermissionIntegrationTest(unittest.TestCase):
    def test_setup_checks_enrollment_and_hub_permissions(self):
        server = PermissionServer()
        base = server.start()
        try:
            operator, hub_admin = self._login_hub_roles(base)
            self._assert_unknown_token_rejected(server, base)
            with tempfile.TemporaryDirectory() as tmp:
                bundle_path = str(Path(tmp) / "connect.yaml")
                Path(bundle_path).write_text(yaml.safe_dump(_bundle()), encoding="utf-8")
                enrollment = self._assert_shared_token_rejected(server, base, bundle_path, operator)
                self._assert_enrollment_is_not_hub_auth(base, enrollment)
                self._assert_role_without_permission_rejected(server, base, operator, enrollment)
                self._assert_super_admin_accepted(server, base, hub_admin, enrollment)
                self._assert_enrollment_route_permissions(server, base)
                self._assert_login_setup_uses_super_admin(server, base, bundle_path, operator)
        finally:
            server.stop()


    def _login_hub_roles(self, base):
        _status, operator_login = _request(
            base, "POST", connect._HUB_LOGIN, body={"username": HUB_OPERATOR, "password": HUB_OPERATOR_PASSWORD}
        )
        _status, admin_login = _request(
            base, "POST", connect._HUB_LOGIN, body={"username": HUB_ADMIN, "password": HUB_ADMIN_PASSWORD}
        )
        return operator_login["token"], admin_login["token"]

    def _assert_unknown_token_rejected(self, server, base):
        missing, missing_body = _request(
            base,
            "POST",
            connect._HUB_CONFIGURE,
            token="not-a-hub-session",
            body={"connectUrl": MQTT, "enrollmentToken": "x", "siteName": "Plant"},
        )
        self.assertEqual(missing, 401)
        self.assertEqual(missing_body["message"], _INVALID)
        self.assertEqual(server.state.configure_calls[-1]["status"], 401)

    def _assert_shared_token_rejected(self, server, base, bundle_path, operator):
        denied = _run_setup(base, bundle_path, {"COREHUB_TOKEN": operator})
        self.assertNotEqual(denied.returncode, 0)
        call = server.state.configure_calls[-1]
        self.assertEqual(call["status"], 403)
        self.assertEqual(call["role"], "ADMIN")
        self.assertEqual(server.state.records[call["auth"]]["kind"], "hub")
        enrollment = _printed_token(denied.stdout)
        self.assertEqual(server.state.records[enrollment]["kind"], "enrollment")
        self.assertNotIn(enrollment, Path(bundle_path).read_text(encoding="utf-8"))
        self._assert_address_not_sent(server)
        return enrollment

    def _assert_address_not_sent(self, server):
        for written in server.state.org_writes:
            self.assertNotIn("address", written)
            self.assertNotIn("settings", written)

    def _configure(self, base, token, enrollment, cookie=False):
        return _request(
            base,
            "POST",
            connect._HUB_CONFIGURE,
            token=token,
            cookie=cookie,
            body={"connectUrl": MQTT, "enrollmentToken": enrollment, "siteName": "Plant"},
        )

    def _assert_enrollment_is_not_hub_auth(self, base, enrollment):
        stolen, stolen_body = self._configure(base, enrollment, enrollment)
        self.assertEqual(stolen, 401)
        self.assertEqual(stolen_body["message"], _INVALID)
        cookie_stolen, _cookie_body = self._configure(base, enrollment, enrollment, cookie=True)
        self.assertEqual(cookie_stolen, 401)

    def _assert_role_without_permission_rejected(self, server, base, operator, enrollment):
        lacking, lacking_body = self._configure(base, operator, enrollment)
        self.assertEqual(lacking, 403)
        self.assertIn("does not have permission", lacking_body["message"])
        self.assertIn("ADMIN", lacking_body["message"])
        self.assertEqual(server.state.configure_calls[-1]["role"], "ADMIN")

    def _assert_super_admin_accepted(self, server, base, hub_admin, enrollment):
        accepted, accepted_body = self._configure(base, hub_admin, enrollment)
        self.assertEqual(accepted, 200)
        self.assertTrue(accepted_body["success"])
        call = server.state.configure_calls[-1]
        self.assertEqual(call["auth"], hub_admin)
        self.assertEqual(call["enrollment"], enrollment)
        self.assertNotEqual(call["auth"], enrollment)
        self.assertEqual(server.state.records[hub_admin]["role"], "SUPER_ADMIN")

    def _assert_enrollment_route_permissions(self, server, base):
        org_id = next(iter(server.state.orgs))
        _viewer_status, viewer_login = _request(
            base, "POST", "/api/auth/password/login", body={"email": VIEWER_EMAIL, "password": VIEWER_PASSWORD}
        )
        viewer_mint, viewer_body = _request(
            base,
            "POST",
            f"{connect._API_ORGANIZATIONS}/{org_id}/sites/enrollment-tokens",
            token=viewer_login["token"],
            body={"siteId": "site_1", "ttlMinutes": 60},
        )
        self.assertEqual(viewer_mint, 403)
        self.assertIn("viewer", viewer_body["message"])
        _owner_status, owner_login = _request(
            base, "POST", "/api/auth/password/login", body={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
        minted, minted_body = _request(
            base,
            "POST",
            f"{connect._API_ORGANIZATIONS}/{org_id}/sites/enrollment-tokens",
            token=owner_login["token"],
            body={"siteId": "site_1", "ttlMinutes": 60},
        )
        self.assertEqual(minted, 201)
        self.assertEqual(server.state.records[minted_body["token"]]["kind"], "enrollment")
        replay, _replay_body = self._configure(base, minted_body["token"], minted_body["token"])
        self.assertEqual(replay, 401)

    def _assert_login_setup_uses_super_admin(self, server, base, bundle_path, operator):
        ready = _run_setup(
            base,
            bundle_path,
            {"COREHUB_USERNAME": HUB_ADMIN, "COREHUB_PASSWORD": HUB_ADMIN_PASSWORD, "COREHUB_TOKEN": operator},
        )
        self.assertEqual(ready.returncode, 0, ready.stderr)
        printed = _printed_token(ready.stdout)
        self.assertIn(f"GLUESYNC_CONNECT_URL={MQTT}", ready.stdout)
        self.assertEqual(server.state.records[printed]["kind"], "enrollment")
        self.assertNotIn(printed, Path(bundle_path).read_text(encoding="utf-8"))
        last = server.state.configure_calls[-1]
        self.assertEqual(last["status"], 200)
        self.assertEqual(last["enrollment"], printed)
        self.assertEqual(server.state.records[last["auth"]]["kind"], "hub")
        self.assertEqual(server.state.records[last["auth"]]["role"], "SUPER_ADMIN")
        self.assertNotEqual(last["auth"], printed)
        self.assertNotEqual(last["auth"], operator)
        posts = [item for item in server.state.org_writes if item.get("slug") == "acme"]
        self.assertEqual(len(posts), 1)

