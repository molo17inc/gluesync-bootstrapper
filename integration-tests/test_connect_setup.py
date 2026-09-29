#!/usr/bin/env python3
"""Connect portable bundle and setup call-order tests."""

import io
import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import connect_portable as connect


ADMIN = "admin@example.com"
MQTT = "tcp://mosquitto:1883"


def bundle():
    return {
        "kind": connect.BUNDLE_KIND,
        "organization": {
            "name": "Acme",
            "slug": "acme",
            "billingEmail": "billing@acme.example",
            "taxId": "IT123",
            "address": {
                "line1": "1 Road",
                "city": "Rome",
                "postalCode": "00100",
                "country": "IT",
            },
            "defaultTimezone": "Europe/Rome",
            "settings": {"advisory": {"enabled": False}},
        },
        "members": [
            {"role": "owner", "user": {"email": ADMIN, "fullName": "Kit Admin"}},
        ],
        "sites": [
            {
                "id": "old-site-hint",
                "name": "Plant",
                "description": "line",
                "tags": ["prod"],
                "proxyIpAllowlist": ["10.0.0.0/8"],
            }
        ],
    }


class FakeAPI:
    def __init__(self, handlers):
        self.handlers = list(handlers)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body, headers.get("Authorization"), headers.get("Cookie")))
        if not self.handlers:
            raise AssertionError(f"unexpected {method} {url}")
        expected, status, payload = self.handlers.pop(0)
        path = url.split("://", 1)[-1]
        path = path[path.find("/") :]
        if expected != (method, path):
            raise AssertionError(f"expected {expected} got {(method, path)}")
        return status, payload


class AllowListTests(unittest.TestCase):
    def test_secrets_and_urls_are_rejected(self):
        document = bundle()
        document["organization"]["password"] = "secret"
        with self.assertRaises(connect.ConnectError):
            connect.normalize_bundle(document)

        document = bundle()
        document["sites"][0]["enrollmentToken"] = "plain"
        with self.assertRaises(connect.ConnectError):
            connect.normalize_bundle(document)

        document = bundle()
        document["connectUrl"] = "tcp://broker:1883"
        with self.assertRaises(connect.ConnectError):
            connect.normalize_bundle(document)

        document = bundle()
        document["sites"][0]["tags"] = ["gsj_not_a_token"]
        with self.assertRaises(connect.ConnectError):
            connect.normalize_bundle(document)

    def test_export_keeps_only_portable_fields(self):
        org = {
            "id": "org_1",
            "name": "Acme",
            "slug": "acme",
            "billingEmail": "billing@acme.example",
            "taxId": None,
            "address": {"line1": "1 Road", "city": "Rome", "postalCode": "00100", "country": "IT"},
            "defaultTimezone": "Europe/Rome",
            "settings": {"advisory": {"enabled": False}},
            "status": "active",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z",
            "passwordHash": "nope",
            "connectUrl": "tcp://old:1883",
        }
        members = [
            {
                "id": "mem_1",
                "orgId": "org_1",
                "userId": "user_1",
                "role": "owner",
                "user": {
                    "email": ADMIN,
                    "fullName": "Kit Admin",
                    "idpSubject": "password:admin",
                    "avatarUrl": "https://cdn.example/a.png",
                    "disabled": False,
                    "passwordHash": "$2b$secret",
                },
            }
        ]
        sites = [
            {
                "id": "site_live",
                "name": "Plant",
                "description": "line",
                "tags": ["prod"],
                "proxyIpAllowlist": ["10.1.0.0/16"],
                "status": "online",
                "lastSeenAt": "2026-01-01T00:00:00Z",
                "agent": {"hostname": "hub"},
                "health": {"ok": True},
                "expiresAt": None,
                "supportTicketId": "T1",
                "isConnected": True,
            }
        ]
        exported = connect.bundle_from_api(org, members, sites)
        blob = json.dumps(exported)
        for forbidden in (
            "passwordHash",
            "idpSubject",
            "avatarUrl",
            "disabled",
            "enrollmentToken",
            "isConnected",
            "supportTicketId",
            "connectUrl",
            "tcp://",
            "https://",
        ):
            self.assertNotIn(forbidden, blob)
        self.assertEqual(exported["organization"]["slug"], "acme")
        self.assertEqual(exported["members"][0]["user"]["email"], ADMIN)
        self.assertEqual(exported["members"][0]["role"], "owner")
        self.assertEqual(exported["sites"][0]["name"], "Plant")
        self.assertEqual(exported["sites"][0]["id"], "site_live")
        self.assertNotIn("agent", exported["sites"][0])
        self.assertNotIn("health", exported["sites"][0])


class SetupOrderTests(unittest.TestCase):
    def test_fresh_setup_call_order_and_single_enrollment(self):
        fake = FakeAPI(
            [
                (("POST", "/api/auth/password/login"), 200, {"token": "session", "session": {"userId": "u1"}}),
                (("GET", "/api/organizations"), 200, []),
                (("POST", "/api/organizations"), 201, {"id": "org_new", "slug": "acme", "name": "Acme"}),
                (("PATCH", "/api/organizations/org_new"), 200, {"id": "org_new"}),
                (
                    ("GET", "/api/organizations/org_new/members"),
                    200,
                    [{"id": "mem_1", "role": "owner", "user": {"email": ADMIN, "fullName": "Kit Admin"}}],
                ),
                (("PATCH", "/api/organizations/org_new/members/mem_1"), 200, {"id": "mem_1", "role": "owner"}),
                (("GET", "/api/organizations/org_new/sites"), 200, []),
                (("POST", "/api/organizations/org_new/sites"), 201, {"id": "site_new", "name": "Plant"}),
                (
                    ("PATCH", "/api/organizations/org_new/sites/site_new/proxy-allowlist"),
                    200,
                    {"id": "site_new"},
                ),
                (
                    ("POST", "/api/organizations/org_new/sites/enrollment-tokens"),
                    201,
                    {"token": "enroll-once", "tokenId": "et1", "expiresAt": "2026-01-01T01:00:00Z"},
                ),
                (("POST", "/connect/configure"), 200, {"configured": True}),
            ]
        )
        out = io.StringIO()
        args = type(
            "Args",
            (),
            {
                "connect_import": None,
                "connect_setup": True,
                "connect_export": None,
                "connect_relay": True,
                "connect_jdbc_token": False,
                "connect_mcp_token": False,
            },
        )()
        # Drive setup_bundle directly so the order is asserted without env login twice.
        client = connect.ConnectClient("http://connect-api:3000", bearer="session", transport=fake)
        hub = connect.ConnectClient(
            "http://gluesync-core-hub:1717",
            bearer="hub-token",
            cookie_token="hub-token",
            transport=fake,
        )
        # The login call is not part of setup_bundle. Prepend was for a full CLI test below.
        fake.handlers = fake.handlers[1:]
        fake.calls.clear()
        connect.setup_bundle(
            client,
            bundle(),
            mqtt_url=MQTT,
            admin_email=ADMIN,
            hub=hub,
            relay=True,
            stdout=out,
        )
        paths = [(call[0], call[1].split("://", 1)[-1].split("/", 1)[-1]) for call in fake.calls]
        paths = [(method, "/" + path) for method, path in paths]
        self.assertEqual(
            paths,
            [
                ("GET", "/api/organizations"),
                ("POST", "/api/organizations"),
                ("PATCH", "/api/organizations/org_new"),
                ("GET", "/api/organizations/org_new/members"),
                ("PATCH", "/api/organizations/org_new/members/mem_1"),
                ("GET", "/api/organizations/org_new/sites"),
                ("POST", "/api/organizations/org_new/sites"),
                ("PATCH", "/api/organizations/org_new/sites/site_new/proxy-allowlist"),
                ("POST", "/api/organizations/org_new/sites/enrollment-tokens"),
                ("POST", "/connect/configure"),
            ],
        )
        enrollment_calls = [call for call in fake.calls if call[1].endswith("/enrollment-tokens")]
        self.assertEqual(len(enrollment_calls), 1)
        self.assertEqual(enrollment_calls[0][2]["ttlMinutes"], 60)
        self.assertEqual(enrollment_calls[0][2]["siteId"], "site_new")
        configure = fake.calls[-1][2]
        self.assertEqual(configure["connectUrl"], MQTT)
        self.assertEqual(configure["enrollmentToken"], "enroll-once")
        self.assertEqual(configure["siteName"], "Plant")
        self.assertEqual(fake.calls[-1][4], "gs-auth=hub-token")
        printed = out.getvalue()
        self.assertEqual(printed.count("GLUESYNC_CONNECT_TOKEN="), 1)
        self.assertIn("GLUESYNC_CONNECT_TOKEN=enroll-once\n", printed)
        self.assertIn(f"GLUESYNC_CONNECT_URL={MQTT}\n", printed)
        self.assertIn("GLUESYNC_JDBC_CONNECT_RELAY_ENABLED=true\n", printed)
        self.assertNotIn("enroll-once", json.dumps(connect.normalize_bundle(bundle())))

    def test_existing_org_site_and_member_are_patched_not_posted(self):
        fake = FakeAPI(
            [
                (
                    ("GET", "/api/organizations"),
                    200,
                    [{"id": "org_old", "slug": "acme", "name": "Old"}],
                ),
                (("PATCH", "/api/organizations/org_old"), 200, {"id": "org_old"}),
                (
                    ("GET", "/api/organizations/org_old/members"),
                    200,
                    [{"id": "mem_9", "role": "viewer", "user": {"email": ADMIN, "fullName": "Kit Admin"}}],
                ),
                (("PATCH", "/api/organizations/org_old/members/mem_9"), 200, {"id": "mem_9", "role": "owner"}),
                (
                    ("GET", "/api/organizations/org_old/sites"),
                    200,
                    [{"id": "site_old", "name": "Plant"}],
                ),
                (("PATCH", "/api/organizations/org_old/sites/site_old"), 200, {"id": "site_old"}),
                (
                    ("PATCH", "/api/organizations/org_old/sites/site_old/proxy-allowlist"),
                    200,
                    {"id": "site_old"},
                ),
                (
                    ("POST", "/api/organizations/org_old/sites/enrollment-tokens"),
                    201,
                    {"token": "enroll-once", "tokenId": "et1", "expiresAt": "2026-01-01T01:00:00Z"},
                ),
            ]
        )
        client = connect.ConnectClient("http://connect-api:3000", bearer="session", transport=fake)
        connect.setup_bundle(
            client,
            bundle(),
            mqtt_url=MQTT,
            admin_email=ADMIN,
            stdout=io.StringIO(),
        )
        methods_and_paths = [(call[0], call[1]) for call in fake.calls]
        self.assertFalse(any(method == "POST" and path.endswith("/api/organizations") for method, path in methods_and_paths))
        self.assertFalse(any(path.endswith("/members/direct") for _, path in methods_and_paths))
        self.assertFalse(any(path.endswith("/sites") and method == "POST" for method, path in methods_and_paths))
        self.assertTrue(any(path.endswith("/members/mem_9") and method == "PATCH" for method, path in methods_and_paths))
        self.assertEqual(sum(1 for _, path in methods_and_paths if path.endswith("/enrollment-tokens")), 1)

    def test_create_conflict_is_not_retried_as_overwrite(self):
        fake = FakeAPI(
            [
                (("GET", "/api/organizations"), 200, []),
                (
                    ("POST", "/api/organizations"),
                    409,
                    {"error": {"code": "conflict", "message": "slug taken"}},
                ),
            ]
        )
        client = connect.ConnectClient("http://connect-api:3000", bearer="session", transport=fake)
        with self.assertRaises(connect.ConnectConflict):
            connect.setup_bundle(
                client,
                bundle(),
                mqtt_url=MQTT,
                admin_email=ADMIN,
                stdout=io.StringIO(),
            )
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1][0], "POST")
        self.assertTrue(fake.calls[1][1].endswith("/api/organizations"))
        self.assertFalse(any(call[0] == "PATCH" for call in fake.calls))
        self.assertEqual(fake.handlers, [])

    def test_secret_bundle_makes_no_http_calls(self):
        fake = FakeAPI([])
        client = connect.ConnectClient("http://connect-api:3000", bearer="session", transport=fake)
        document = bundle()
        document["organization"]["bootstrapToken"] = "hash"
        with self.assertRaises(connect.ConnectError):
            connect.setup_bundle(client, document, mqtt_url=MQTT, admin_email=ADMIN)
        self.assertEqual(fake.calls, [])


class MainWiringTests(unittest.TestCase):
    def test_main_exposes_the_kit_command(self):
        source = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("'--connect-import'", source)
        self.assertIn("'--connect-setup'", source)
        self.assertIn("run_from_args", source)


if __name__ == "__main__":
    unittest.main()
