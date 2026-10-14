#!/usr/bin/env python3
"""
Backup and restore of the Core Hub user groups (with their members and their Query Studio
data-access policies) and of the data-access policies of each user.

Core Hub is faked by a small in-memory router patched over corehub.fetch_core_hub.
"""

import io
import logging
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import requests
import yaml

logging.disable(logging.CRITICAL)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import automator_app.corehub as corehub  # noqa: E402

CONNECTION = {"base_url": "http://hub", "use_ssl": False, "skip_verify": False}
POLICY_PATH = "/query-studio/data-access"

ANALYSTS_POLICY = {
    "userId": "group:g1",
    "connectionId": "conn-1",
    "mode": "RESTRICTED",
    "rules": [{"schemaName": "sales", "tableName": None, "effect": "ALLOW"}],
    "piiUnmaskGrants": [{"schemaName": "sales", "tableName": "customers", "columnName": "email"}],
    "updatedBy": "admin",
    "updatedAt": 1,
    "groupId": "g1",
}
ALICE_POLICY = {
    "userId": "u-alice",
    "connectionId": "conn-2",
    "mode": "OPEN",
    "rules": [{"schemaName": "hr", "tableName": "salaries", "effect": "DENY"}],
    "piiUnmaskGrants": [],
    "updatedBy": "admin",
    "updatedAt": 2,
}


SOURCE_CONNECTIONS = [
    {"connectionId": "conn-1", "name": "sales-db"},
    {"connectionId": "conn-2", "name": "hr-db"},
]


def _not_found(path):
    response = requests.Response()
    response.status_code = 404
    cause = requests.HTTPError("404 Client Error: Not Found", response=response)
    try:
        raise RuntimeError(f"CoreHub request GET {path} failed: {cause}") from cause
    except RuntimeError as exc:
        return exc


class FakeHub:
    """Answers the few Core Hub routes these flows use and records every write."""

    def __init__(self, users, groups=None, policies=None, supports_groups=True, connections=None):
        self.users = users
        self.groups = groups or []
        self.policies = policies or {}
        self.supports_groups = supports_groups
        self.connections = SOURCE_CONNECTIONS if connections is None else connections
        self.writes = []

    def __call__(self, path, method="GET", token=None, body=None, **kwargs):
        if method != "GET":
            self.writes.append((method, path, body))
        if path.startswith("/user-groups") and not self.supports_groups:
            raise _not_found(path)
        if path == "/users":
            return self.users
        if path == "/connections":
            if isinstance(self.connections, Exception):
                raise self.connections
            return self.connections
        if path == "/user-groups" and method == "GET":
            return self.groups
        if path == "/user-groups" and method == "POST":
            created = {"id": f"new-{len(self.groups) + 1}", **body}
            self.groups.append(created)
            return created
        if path.startswith(POLICY_PATH) and method == "GET":
            principal = path[len(POLICY_PATH) + 1:]
            if principal not in self.policies and principal.startswith("users/") and self.policies.get("unsupported"):
                raise _not_found(path)
            return {"items": self.policies.get(principal, [])}
        return {}


def _export(hub):
    with mock.patch.object(corehub, "fetch_core_hub", side_effect=hub), mock.patch.object(corehub, "configure_core_hub"):
        return (
            corehub.export_user_groups(token="tok", **CONNECTION),
            corehub.export_data_access_policies(token="tok", **CONNECTION),
        )


def _source_hub():
    return FakeHub(
        users=[
            {"id": "u-alice", "username": "alice"},
            {"id": "u-bob", "username": "bob"},
        ],
        groups=[
            {"id": "g1", "name": "Analysts", "description": "Reads sales", "memberIds": ["u-alice", "u-bob"]},
            {"id": "g2", "name": "Auditors", "memberIds": []},
        ],
        policies={"groups/g1": [ANALYSTS_POLICY], "users/u-alice": [ALICE_POLICY]},
    )


class ExportTests(unittest.TestCase):
    def test_groups_keep_their_members_by_username_and_their_policies_without_audit_fields(self):
        groups, _ = _export(_source_hub())

        analysts, auditors = groups["user_groups"]
        self.assertEqual("Analysts", analysts["name"])
        self.assertEqual("Reads sales", analysts["description"])
        self.assertEqual(
            [{"id": "u-alice", "username": "alice"}, {"id": "u-bob", "username": "bob"}],
            analysts["members"],
        )
        self.assertEqual(
            [{
                "connectionId": "conn-1",
                "connectionName": "sales-db",
                "mode": "RESTRICTED",
                "rules": ANALYSTS_POLICY["rules"],
                "piiUnmaskGrants": ANALYSTS_POLICY["piiUnmaskGrants"],
            }],
            analysts["policies"],
        )
        self.assertEqual([], auditors["members"])
        self.assertEqual([], auditors["policies"])

    def test_only_users_with_policies_are_exported(self):
        _, policies = _export(_source_hub())

        self.assertEqual(
            [{
                "userId": "u-alice",
                "username": "alice",
                "policies": [{
                    "connectionId": "conn-2",
                    "connectionName": "hr-db",
                    "mode": "OPEN",
                    "rules": ALICE_POLICY["rules"],
                    "piiUnmaskGrants": [],
                }],
            }],
            policies["user_policies"],
        )

    def test_a_core_hub_without_user_groups_is_written_down_as_unsupported(self):
        groups, _ = _export(FakeHub(users=[], supports_groups=False))

        self.assertIn("unsupported", groups["user_groups"])

    def test_a_core_hub_without_data_access_policies_is_written_down_as_unsupported(self):
        hub = FakeHub(users=[{"id": "u-alice", "username": "alice"}], policies={"unsupported": True})

        _, policies = _export(hub)

        self.assertIn("unsupported", policies["user_policies"])

    def test_a_failed_listing_is_written_down_as_an_error(self):
        def failing(path, **kwargs):
            raise RuntimeError("CoreHub request GET /user-groups failed: 500 Server Error")

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=failing), mock.patch.object(corehub, "configure_core_hub"):
            groups = corehub.export_user_groups(token="tok", **CONNECTION)
            policies = corehub.export_data_access_policies(token="tok", **CONNECTION)

        self.assertIn("500", groups["user_groups"]["error"])
        self.assertIn("500", policies["user_policies"]["error"])

    def test_one_user_whose_policies_fail_does_not_stop_the_others(self):
        hub = _source_hub()
        original = hub.__call__

        def flaky(path, **kwargs):
            if path == f"{POLICY_PATH}/users/u-bob":
                raise RuntimeError("CoreHub request failed: 500")
            return original(path, **kwargs)

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=flaky), mock.patch.object(corehub, "configure_core_hub"):
            policies = corehub.export_data_access_policies(token="tok", **CONNECTION)

        self.assertEqual(["alice"], [entry["username"] for entry in policies["user_policies"]])
        self.assertEqual(1, len(policies["errors"]))


class RestoreTests(unittest.TestCase):
    def _restore(self, hub, groups=None, policies=None):
        with mock.patch.object(corehub, "fetch_core_hub", side_effect=hub), mock.patch.object(corehub, "configure_core_hub"):
            results = {}
            if groups is not None:
                results["groups"] = corehub.import_user_groups(token="tok", groups_data=groups, **CONNECTION)
            if policies is not None:
                results["policies"] = corehub.import_data_access_policies(token="tok", policies_data=policies, **CONNECTION)
            return results

    def test_a_backup_restores_onto_a_core_hub_where_the_users_have_other_ids(self):
        groups, policies = _export(_source_hub())
        target = FakeHub(users=[{"id": "t-alice", "username": "alice"}, {"id": "t-bob", "username": "bob"}])

        results = self._restore(target, groups, policies)

        self.assertEqual(
            ("POST", "/user-groups", {"name": "Analysts", "description": "Reads sales", "memberIds": ["t-alice", "t-bob"]}),
            target.writes[0],
        )
        self.assertIn(
            ("PUT", f"{POLICY_PATH}/groups/new-1/connections/conn-1",
             {"mode": "RESTRICTED", "rules": ANALYSTS_POLICY["rules"], "piiUnmaskGrants": ANALYSTS_POLICY["piiUnmaskGrants"]}),
            target.writes,
        )
        self.assertIn(
            ("PUT", f"{POLICY_PATH}/users/t-alice/connections/conn-2",
             {"mode": "OPEN", "rules": ALICE_POLICY["rules"], "piiUnmaskGrants": []}),
            target.writes,
        )
        self.assertEqual("restored", results["groups"]["status"])
        self.assertEqual(2, results["groups"]["created"])
        self.assertEqual(1, results["groups"]["policies"])
        self.assertEqual("2 created, 0 updated, 1 policies", results["groups"]["summary"])
        self.assertEqual({"status": "restored", "summary": "1 users, 1 policies", "users": 1, "policies": 1}, results["policies"])

    def test_an_existing_group_is_matched_by_name_and_gets_the_backup_members(self):
        groups, _ = _export(_source_hub())
        target = FakeHub(
            users=[{"id": "u-alice", "username": "alice"}],
            groups=[{"id": "t-g", "name": "analysts", "memberIds": []}],
        )

        result = self._restore(target, groups)["groups"]

        self.assertIn(("PUT", "/user-groups/t-g", {"description": "Reads sales"}), target.writes)
        self.assertIn(("PUT", "/user-groups/t-g/members", {"memberIds": ["u-alice"]}), target.writes)
        self.assertEqual(1, result["updated"])
        self.assertEqual(["Analysts: member bob not found, left out"], result["warnings"])

    def test_a_user_missing_on_the_target_is_reported_and_skipped(self):
        _, policies = _export(_source_hub())
        target = FakeHub(users=[])

        result = self._restore(target, policies=policies)["policies"]

        self.assertEqual([], target.writes)
        self.assertEqual(["user alice not found, policies left out"], result["warnings"])

    def test_a_member_kept_only_by_id_is_found_by_the_same_id(self):
        groups = {"user_groups": [{"name": "Ops", "members": [{"id": "u-x", "username": None}], "policies": []}]}
        target = FakeHub(users=[{"id": "u-x", "username": "oidc-user"}])

        self._restore(target, groups)

        self.assertEqual(("POST", "/user-groups", {"name": "Ops", "description": None, "memberIds": ["u-x"]}), target.writes[0])

    def test_a_failed_write_is_reported_and_the_next_group_still_restored(self):
        groups, _ = _export(_source_hub())
        target = FakeHub(users=[{"id": "u-alice", "username": "alice"}])
        original = target.__call__

        def refusing(path, method="GET", **kwargs):
            if method == "POST" and kwargs.get("body", {}).get("name") == "Analysts":
                raise RuntimeError("CoreHub request POST /user-groups failed: 409")
            return original(path, method=method, **kwargs)

        result = self._restore(refusing, groups)["groups"]

        self.assertEqual(1, result["created"])
        self.assertEqual(1, len(result["errors"]))
        self.assertIn("Analysts", result["errors"][0])

    def test_a_created_group_without_an_id_is_reported(self):
        groups = {"user_groups": [{"name": "Ops", "members": [], "policies": []}]}
        target = FakeHub(users=[])

        def no_id(path, method="GET", **kwargs):
            return [] if method == "POST" else target(path, method=method, **kwargs)

        result = self._restore(no_id, groups)["groups"]

        self.assertEqual(["Ops: Core Hub did not answer the id of the created group"], result["errors"])

    def test_a_failed_policy_write_is_reported(self):
        _, policies = _export(_source_hub())
        target = FakeHub(users=[{"id": "u-alice", "username": "alice"}])
        original = target.__call__

        def refusing(path, method="GET", **kwargs):
            if method == "PUT":
                raise RuntimeError("CoreHub request PUT failed: 400")
            return original(path, method=method, **kwargs)

        result = self._restore(refusing, policies=policies)["policies"]

        self.assertEqual(0, result["policies"])
        self.assertIn("user alice on conn-2", result["errors"][0])

    def test_sections_that_were_not_exported_are_skipped(self):
        hub = FakeHub(users=[])
        results = [
            self._restore(hub, groups={})["groups"],
            self._restore(hub, groups={"user_groups": {"unsupported": "old hub"}})["groups"],
            self._restore(hub, groups={"user_groups": {"error": "boom"}})["groups"],
            self._restore(hub, policies={"user_policies": "nonsense"})["policies"],
        ]

        self.assertEqual(["skipped"] * 4, [result["status"] for result in results])
        self.assertEqual("not exported: old hub", results[1]["reason"])
        self.assertEqual([], hub.writes)

    def test_a_core_hub_without_user_groups_skips_the_groups(self):
        groups, _ = _export(_source_hub())

        result = self._restore(FakeHub(users=[], supports_groups=False), groups)["groups"]

        self.assertEqual({"status": "skipped", "reason": "this Core Hub has no user groups"}, result)

    def test_group_policies_exported_with_an_error_are_reported(self):
        groups = {"user_groups": [{"name": "Ops", "members": [], "policies": {"error": "boom"}}]}

        result = self._restore(FakeHub(users=[]), groups)["groups"]

        self.assertEqual(["group Ops: policies exported with error"], result["errors"])

    def test_a_policy_follows_its_connection_to_the_new_id_the_pipelines_import_gave_it(self):
        groups, policies = _export(_source_hub())
        target = FakeHub(
            users=[{"id": "u-alice", "username": "alice"}],
            connections=[{"connectionId": "new-sales", "name": "sales-db"}, {"connectionId": "new-hr", "name": "hr-db"}],
        )

        results = self._restore(target, groups, policies)

        puts = [path for method, path, _ in target.writes if method == "PUT" and path.startswith(POLICY_PATH)]
        self.assertEqual(
            [f"{POLICY_PATH}/groups/new-1/connections/new-sales", f"{POLICY_PATH}/users/u-alice/connections/new-hr"],
            puts,
        )
        self.assertNotIn("skipped_policies", results["groups"])

    def test_a_policy_whose_connection_is_missing_is_skipped_and_reported(self):
        groups, policies = _export(_source_hub())
        target = FakeHub(users=[{"id": "u-alice", "username": "alice"}], connections=[])

        results = self._restore(target, groups, policies)

        self.assertEqual([], [write for write in target.writes if write[1].startswith(POLICY_PATH)])
        self.assertEqual(
            ["group Analysts on sales-db: connection not found on this Core Hub"],
            results["groups"]["skipped_policies"],
        )
        self.assertEqual(["user alice on hr-db: connection not found on this Core Hub"], results["policies"]["skipped_policies"])
        self.assertIn("1 policies skipped (connection not found; import the pipelines, then restore again)", results["policies"]["summary"])
        self.assertEqual(0, results["policies"]["policies"])

    def test_two_connections_with_the_backup_name_are_ambiguous_and_skip_the_policy(self):
        _, policies = _export(_source_hub())
        target = FakeHub(
            users=[{"id": "u-alice", "username": "alice"}],
            connections=[{"connectionId": "a", "name": "hr-db"}, {"connectionId": "b", "name": "hr-db"}],
        )

        result = self._restore(target, policies=policies)["policies"]

        self.assertEqual(1, len(result["skipped_policies"]))
        self.assertEqual([], target.writes)

    def test_connections_that_cannot_be_listed_skip_every_policy_and_say_why(self):
        _, policies = _export(_source_hub())
        target = FakeHub(users=[{"id": "u-alice", "username": "alice"}], connections=RuntimeError("403 Forbidden"))

        result = self._restore(target, policies=policies)["policies"]

        self.assertEqual([], target.writes)
        self.assertEqual(1, len(result["skipped_policies"]))
        self.assertIn("could not be listed: 403 Forbidden", result["warnings"][0])

    def test_a_backup_without_connection_names_keeps_the_ids_this_core_hub_has(self):
        policies = {"user_policies": [{"userId": "u-alice", "username": "alice",
                                       "policies": [{"connectionId": "conn-2", "mode": "OPEN"}]}]}
        target = FakeHub(users=[{"id": "u-alice", "username": "alice"}])

        self._restore(target, policies=policies)

        self.assertEqual([("PUT", f"{POLICY_PATH}/users/u-alice/connections/conn-2", {"mode": "OPEN"})], target.writes)


class FullBackupTests(unittest.TestCase):
    def test_the_full_backup_round_trips_the_groups_and_the_policies(self):
        source = _source_hub()
        with mock.patch.object(corehub, "fetch_core_hub", side_effect=source), \
                mock.patch.object(corehub, "configure_core_hub"), \
                mock.patch.object(corehub, "export_all_pipelines_yaml", side_effect=RuntimeError("no pipelines")), \
                mock.patch.object(corehub.ai_settings, "export_ai_settings", side_effect=corehub.ai_settings.AiSettingsUnsupported("old")):
            archive = corehub.export_full_corehub_backup(token="tok", **CONNECTION)

        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            groups = yaml.safe_load(zf.read(corehub.USER_GROUPS_FILENAME))
            policies = yaml.safe_load(zf.read(corehub.DATA_ACCESS_POLICIES_FILENAME))
        self.assertEqual(["Analysts", "Auditors"], [group["name"] for group in groups["user_groups"]])
        self.assertEqual("alice", policies["user_policies"][0]["username"])

        target = FakeHub(users=[{"id": "u-alice", "username": "alice"}, {"id": "u-bob", "username": "bob"}])
        names = ("import_global_configs", "import_smtp_settings", "import_webhooks_config", "import_thresholds",
                 "import_schedules", "import_users", "import_oidc_config")
        with mock.patch.multiple(corehub, **{name: mock.Mock(return_value={}) for name in names}), \
                mock.patch.object(corehub, "fetch_core_hub", side_effect=target), \
                mock.patch.object(corehub, "configure_core_hub"):
            results = corehub.import_full_corehub_backup(token="tok", zip_bytes=archive, **CONNECTION)

        self.assertEqual("restored", results["user_groups"]["status"])
        self.assertEqual("restored", results["data_access_policies"]["status"])

    def test_a_failed_export_is_written_down_and_a_missing_file_skipped(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf, mock.patch.object(corehub, "logger"):
            corehub._write_section(zf, "user-groups.yaml", "user groups", mock.Mock(side_effect=RuntimeError("boom")))
        with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as zf:
            self.assertEqual({"error": "boom"}, yaml.safe_load(zf.read("user-groups.yaml")))

        restored = corehub.restore_safely("user-groups.yaml", lambda: {"status": "restored"})
        failed = corehub.restore_safely("user-groups.yaml", mock.Mock(side_effect=RuntimeError("down")))

        self.assertEqual({"status": "restored"}, restored)
        self.assertEqual({"status": "failed", "error": "down"}, failed)


def _full_backup_zip():
    source = _source_hub()
    with mock.patch.object(corehub, "fetch_core_hub", side_effect=source), \
            mock.patch.object(corehub, "configure_core_hub"), \
            mock.patch.object(corehub, "export_all_pipelines_yaml", side_effect=RuntimeError("no pipelines")), \
            mock.patch.object(corehub.ai_settings, "export_ai_settings", side_effect=corehub.ai_settings.AiSettingsUnsupported("old")):
        archive = corehub.export_full_corehub_backup(token="tok", **CONNECTION)
    # A full backup carries its pipelines under pipelines/, next to the global files.
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as source_zip, zipfile.ZipFile(buffer, "w") as zf:
        for name in source_zip.namelist():
            if not name.startswith("pipelines/"):
                zf.writestr(name, source_zip.read(name))
        zf.writestr("pipelines/agents-config.yaml", yaml.safe_dump({"agents": [{"agentType": "SOURCE", "agentTag": "mssql", "agentId": "a1"}]}))
    return buffer.getvalue()


class ImportAllTests(unittest.TestCase):
    """POST /api/import/all with a full backup ZIP: the access files are restored, never imported as pipelines."""

    @classmethod
    def setUpClass(cls):
        import automator_app.app as app_module
        from automator_app.state import state
        from fastapi.testclient import TestClient

        cls.app_module = app_module
        cls.state = state
        cls.client = TestClient(app_module.create_app())

    def setUp(self):
        self.state.set_auth("tok", "http://test", use_ssl=False, skip_verify=False)
        self.addCleanup(self.state.clear_auth)
        self.archive = _full_backup_zip()
        self.steps = []
        self.target = FakeHub(users=[{"id": "u-alice", "username": "alice"}, {"id": "u-bob", "username": "bob"}], connections=[])

        def import_pipeline(**kwargs):
            # The pipelines import recreates the connections, under new ids.
            self.steps.append("pipeline")
            self.target.connections = [{"connectionId": "new-sales", "name": "sales-db"}, {"connectionId": "new-hr", "name": "hr-db"}]
            return {"pipelineId": "p1", "pipelineName": kwargs["config"]["pipelineName"]}

        def hub(path, method="GET", **kwargs):
            if method == "POST" and path == "/user-groups":
                self.steps.append("group")
            return self.target(path, method=method, **kwargs)

        for name, kwargs in (
            ("fetch_core_hub", {"side_effect": hub}),
            ("configure_core_hub", {}),
            ("import_pipeline_config_only", {"side_effect": import_pipeline}),
            ("run_create_entities", {"return_value": {"success": True}}),
            ("import_users", {"return_value": {"status": "restored"}}),
            ("import_oidc_config", {"return_value": {"status": "restored"}}),
        ):
            patcher = mock.patch.object(corehub, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _post(self, route):
        response = self.client.post(route, files={"file": ("backup.zip", self.archive, "application/zip")})
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def test_the_access_files_are_not_taken_for_pipelines(self):
        for name in ("user-groups.yaml", "data-access-policies.yaml", "pipelines/user-groups.yaml"):
            self.assertIsNone(self.app_module._pipeline_yaml_stem(name), name)
        self.assertEqual("backup_x", self.app_module._pipeline_yaml_stem("pipelines/backup_x.yaml"))

        result = self._post("/api/import/all")

        self.assertNotIn("user-groups", result["message"])
        self.assertNotIn("data-access-policies", result["message"])
        self.assertNotIn("pipeline", self.steps)

    def test_import_all_restores_the_groups_and_policies_once_the_connections_exist(self):
        with zipfile.ZipFile(io.BytesIO(self.archive)) as zf:
            files = {name: zf.read(name) for name in zf.namelist()}
        files["pipelines/backup_Sales_p0_20260101_000000.yaml"] = yaml.safe_dump({"pipelineName": "Sales"}).encode()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        self.archive = buffer.getvalue()

        result = self._post("/api/import/all")

        self.assertEqual(["pipeline", "group", "group"], self.steps)
        puts = [path for method, path, _ in self.target.writes if method == "PUT" and path.startswith(POLICY_PATH)]
        self.assertEqual(
            [f"{POLICY_PATH}/groups/new-1/connections/new-sales", f"{POLICY_PATH}/users/u-alice/connections/new-hr"],
            puts,
        )
        self.assertNotIn("connection not found", " ".join(result.get("logs") or []))

    def test_import_all_reports_the_policies_it_had_to_skip(self):
        result = self._post("/api/import/all")

        logs = " ".join(result.get("logs") or [])
        self.assertIn("user groups: group Analysts on sales-db: connection not found on this Core Hub", logs)
        self.assertIn("data-access policies: user alice on hr-db: connection not found on this Core Hub", logs)
        self.assertEqual([], [write for write in self.target.writes if write[1].startswith(POLICY_PATH)])

    def test_a_failing_restore_is_reported_and_does_not_fail_the_import(self):
        with mock.patch.object(corehub, "import_user_groups", side_effect=RuntimeError("hub down")):
            result = self._post("/api/import/all")

        self.assertIn("user groups: hub down", " ".join(result.get("logs") or []))


if __name__ == "__main__":
    unittest.main()
