#!/usr/bin/env python3
"""The function collection of a pipeline travels with its backup and is restored before its entities.

Found restoring backup_Ontario on another Core Hub: the entity upsert failed with "column RECEIVED_DATE is composed
by 'convertDate', which the function collection does not export". The function collection (Gluesync 2.3.0.0+) is a
set of Java sources scoped to the pipeline; the backup held the YAML and the UDF sources only, so a restore could not
recreate it and the target agent refused every entity whose ``CustomFn`` names one of its functions.

Covered here:
- export: the collection is written under ``function-collection/`` (``pipeline_<id>/`` in a multi-pipeline ZIP), a 404
  writes nothing, any other failure is not silent;
- restore: publish, poll, install, before any UDF is compiled and any entity is saved; an identical collection is only
  installed; a running publication (409) is waited for; a refusal stops the restore of that pipeline and says why;
- the pre-flight check of the entities names the table, the column and the function that is not exported;
- a Core Hub before 2.3 is unaffected.
"""

import io
import logging
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import yaml

logging.disable(logging.CRITICAL)

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
for _path in (str(TESTS_DIR), str(PROJECT_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import commons  # noqa: E402
import function_collection  # noqa: E402
from fake_corehub import (  # noqa: E402
    SOURCE_AGENT, TARGET_AGENT, FakeCoreHub, FakeFunctionCollections, _refusal, gs_function_source, patched,
    run_create_entities,
)
from test_bootstrapper_payload_golden import SOURCE_TABLES, TARGET_TABLES, _source_list  # noqa: E402

V23 = "2.3.0.0 - build test"
V22 = "2.2.11.5 - build test"

DATE_FUNCTIONS = gs_function_source("DateFunctions.java", "convertDate")
HELPERS = gs_function_source("util/Helpers.java", "trimAll")

CUSTOM_FN_COLUMNS = """        name: customers
        columns:
          - ID: id
          - source: NAME
            target: name
            expression:
              type: CustomFn
              functionName: convertDate
              arguments:
                - {type: SourceColumnArg, name: NAME}
          - EMAIL: email_address"""


def table(columns, name="CUSTOMERS"):
    return f"""
src:
  target: tgt
  tables:
    whitelist: [{name}]
    custom:
      {name}:
{columns}
"""


def routed_fetch(collections, *, version=V23, events=None, extra=None):
    """A ``fetch_core_hub``: ``/version``, the function collection routes of each pipeline, anything else via ``extra``.

    ``collections`` maps a pipeline id to its :class:`FakeFunctionCollections`; every call lands in ``events``.
    """
    events = events if events is not None else []
    expected = []

    def fetch(path, method="GET", token=None, body=None, **kwargs):
        if path == "/version":
            return version
        found = re.match(r"/pipelines/([^/]+)/config/function-collection", path)
        if found:
            expected.append((method, path.split("function-collection", 1)[1] or "/",
                             kwargs.get("expected_error_statuses")))
            collection = collections[found.group(1)]
            collection.log = events
            return collection.fetch(path, method, token, body)
        events.append((method, path))
        return extra(path, method, body) if extra else {}

    fetch.events = events
    fetch.expected = expected
    return fetch


def methods(events):
    """The ``(method, suffix)`` of the function collection calls in ``events``."""
    root = FakeFunctionCollections.ROOT
    return [(m, p.split(root, 1)[1] or "/") for m, p in events if root in p]


def timeline(events):
    """``events`` as ``COLLECTION method suffix`` for the function collection routes, ``ENTITIES`` for an entity upsert,
    and the rest of the ``("RUN" | "UDF", ...)`` markers as they are; anything else is left out."""
    root = FakeFunctionCollections.ROOT
    steps = []
    for kind, path in events:
        if root in path:
            steps.append(f"COLLECTION {kind} {path.split(root, 1)[1] or '/'}")
        elif kind in ("RUN", "UDF"):
            steps.append(f"{kind} {path}")
        elif kind == "PUT" and path.endswith("/config/entities"):
            steps.append("ENTITIES")
        elif kind == "PUT" and re.fullmatch(r"/pipelines/[^/]+", path):
            steps.append("COMPLETED")
    return steps


class FunctionCollectionTestCase(unittest.TestCase):
    def setUp(self):
        import create_user_defined_functions

        commons.reset_corehub_version_cache()
        sleep = mock.patch.object(function_collection.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        # An import or an upload extracts into a session directory and points UDF_PATH at it: keep both out of the
        # Automator workspace of the machine and out of the other tests.
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        for patcher in (mock.patch.dict(os.environ, {"GLUESYNC_AUTOMATOR_WORKDIR": workspace.name}),
                        mock.patch.object(create_user_defined_functions, "UDF_PATH",
                                          create_user_defined_functions.UDF_PATH)):
            patcher.start()
            self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# Restore: publish, poll, install
# ---------------------------------------------------------------------------

class RestoreTests(FunctionCollectionTestCase):
    SOURCES = [dict(DATE_FUNCTIONS), dict(HELPERS)]

    def restore(self, collection, *, version=V23, sources=None):
        fetch = routed_fetch({"pipe": collection}, version=version)
        result = function_collection.restore_function_collection(
            "tok", "pipe", self.SOURCES if sources is None else sources, fetch=fetch)
        return result, collection

    def test_publishes_polls_the_job_and_installs(self):
        result, collection = self.restore(FakeFunctionCollections(polls=2))

        self.assertEqual(collection.requests, [
            ("GET", "/"), ("PUT", "/"),
            ("GET", "/publications/job-1"), ("GET", "/publications/job-1"), ("GET", "/publications/job-1"),
            ("POST", "/install"),
        ])
        self.assertEqual(collection.puts, [{"sources": self.SOURCES}])
        self.assertEqual(sorted(result["exportedFunctions"]), ["convertDate", "trimAll"])
        self.assertTrue(result["published"])
        self.assertTrue(collection.installed)
        self.assertEqual(self.sleep.call_count, 3)

    def test_an_identical_collection_is_installed_without_publishing(self):
        collection = FakeFunctionCollections(self.SOURCES, installed=False)

        result, _ = self.restore(collection)

        self.assertEqual(collection.requests, [("GET", "/"), ("POST", "/install")])
        self.assertEqual(collection.puts, [])
        self.assertFalse(result["published"])
        self.assertEqual(sorted(result["exportedFunctions"]), ["convertDate", "trimAll"])

    def test_an_identical_installed_collection_is_already_installed(self):
        collection = FakeFunctionCollections(self.SOURCES, installed=True)

        result, _ = self.restore(collection)

        self.assertEqual(collection.requests, [("GET", "/"), ("POST", "/install")])
        self.assertFalse(result["published"])
        self.assertEqual(sorted(result["exportedFunctions"]), ["convertDate", "trimAll"])

    def test_other_sources_replace_the_published_collection(self):
        collection = FakeFunctionCollections([gs_function_source("Old.java", "legacy")])

        result, _ = self.restore(collection)

        self.assertEqual(collection.puts, [{"sources": self.SOURCES}])
        self.assertEqual(sorted(result["exportedFunctions"]), ["convertDate", "trimAll"])

    def test_a_running_publication_is_waited_for_and_then_the_collection_is_published(self):
        collection = FakeFunctionCollections(busy=1, polls=1)

        result, _ = self.restore(collection)

        self.assertEqual(collection.requests, [
            ("GET", "/"), ("PUT", "/"),                                                      # 409: another job runs
            ("GET", "/publications/job-1"), ("GET", "/publications/job-1"),                  # it is waited for
            ("PUT", "/"), ("GET", "/publications/job-1"), ("GET", "/publications/job-1"),    # ours
            ("POST", "/install"),
        ])
        self.assertEqual(len(collection.puts), 2)
        self.assertTrue(result["published"])

    def test_a_refused_collection_names_every_finding_and_is_not_installed(self):
        collection = FakeFunctionCollections(rejection={
            "collectionErrors": [{"file": "DateFunctions.java", "line": 12, "column": 5, "message": "cannot find symbol"}],
            "consumerFailures": [{"consumer": "UDF_ORDERS", "language": "Java",
                                  "errors": [{"message": "incompatible types"}]}],
            "fieldFunctionFailures": [{"entityId": "e1", "entityName": "ONTARIO.IVTSTR", "columnName": "RECEIVED_DATE",
                                       "findings": ["convertDate no longer takes a String"]}],
            "warnings": [], "unverifiedConsumers": [],
        })

        with self.assertRaises(function_collection.FunctionCollectionError) as raised:
            self.restore(collection)

        message = str(raised.exception)
        self.assertIn("pipe", message)
        self.assertIn("DateFunctions.java:12:5: cannot find symbol", message)
        self.assertIn("UDF UDF_ORDERS no longer compiles: incompatible types", message)
        self.assertIn("entity ONTARIO.IVTSTR column RECEIVED_DATE: convertDate no longer takes a String", message)
        self.assertNotIn(("POST", "/install"), collection.requests)

    def test_a_publication_that_cannot_be_carried_out_says_why(self):
        with self.assertRaises(function_collection.FunctionCollectionError) as raised:
            self.restore(FakeFunctionCollections(failure="no JDK on the host"))

        self.assertIn("no JDK on the host", str(raised.exception))

    def test_a_publication_that_never_ends_times_out(self):
        with mock.patch.dict(os.environ, {"FUNCTION_COLLECTION_PUBLICATION_TIMEOUT_SECONDS": "0.05"}):
            with self.assertRaises(function_collection.FunctionCollectionError) as raised:
                self.restore(FakeFunctionCollections(polls=10 ** 9))

        self.assertIn("did not finish in time", str(raised.exception))

    def test_the_poll_interval_is_configurable(self):
        with mock.patch.dict(os.environ, {"FUNCTION_COLLECTION_POLL_SECONDS": "0.25"}):
            self.restore(FakeFunctionCollections(polls=1))

        self.sleep.assert_called_with(0.25)

    def test_a_pipeline_that_cannot_be_read_stops_the_restore(self):
        collection = FakeFunctionCollections(get_error="boom")

        with self.assertRaises(function_collection.FunctionCollectionError) as raised:
            self.restore(collection)

        self.assertIn("Could not read the function collection of pipeline pipe", str(raised.exception))
        self.assertEqual(collection.puts, [])

    def test_a_core_hub_before_2_3_is_left_alone(self):
        collection = FakeFunctionCollections()

        result, _ = self.restore(collection, version=V22)

        self.assertIsNone(result)
        self.assertEqual(collection.requests, [])

    def test_nothing_to_restore_makes_no_request(self):
        collection = FakeFunctionCollections()

        result, _ = self.restore(collection, sources=[])

        self.assertIsNone(result)
        self.assertEqual(collection.requests, [])


# ---------------------------------------------------------------------------
# The folder in a backup
# ---------------------------------------------------------------------------

class BackupFolderTests(unittest.TestCase):
    def zip_of(self, members):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for name, text in members.items():
                zf.writestr(name, text)
        return zipfile.ZipFile(io.BytesIO(buffer.getvalue()))

    def test_files_keep_their_pipeline_folder_and_package_directories(self):
        zf = self.zip_of({
            "function-collection/DateFunctions.java": "class A {}",
            "pipeline_aaaa1111/function-collection/util/Helpers.java": "class B {}",
            "pipeline_aaaa1111/udf-src/Other.java": "class C {}",
            "backup_x.yaml": "x: 1",
        })
        with tempfile.TemporaryDirectory() as root:
            written = function_collection.extract_function_collection_members(zf, Path(root))

            self.assertEqual(written, 2)
            self.assertEqual((Path(root) / "function-collection" / "DateFunctions.java").read_text(), "class A {}")
            self.assertEqual(
                (Path(root) / "pipeline_aaaa1111" / "function-collection" / "util" / "Helpers.java").read_text(),
                "class B {}")
            self.assertFalse((Path(root) / "pipeline_aaaa1111" / "udf-src").exists())

    def test_a_member_cannot_leave_the_folder(self):
        zf = self.zip_of({
            "function-collection/../../escape.java": "x",
            "pipeline_a/function-collection/..\\..\\escape2.java": "x",
            "__MACOSX/function-collection/._DateFunctions.java": "x",
        })
        with tempfile.TemporaryDirectory() as root:
            function_collection.extract_function_collection_members(zf, Path(root))

            self.assertFalse((Path(root).parent / "escape.java").exists())
            found = sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file())
            self.assertTrue(all(name.startswith(("function-collection", "pipeline_a")) for name in found), found)
            self.assertFalse(any("__MACOSX" in name or "._" in name for name in found), found)

    def test_the_folder_of_the_pipeline_the_yaml_came_from_comes_first(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("pipeline_aaaa1111", "pipeline_bbbb2222"):
                (Path(root) / name / "function-collection").mkdir(parents=True)

            find = function_collection.find_function_collection_dir
            self.assertEqual(find([root], "bbbb2222"), Path(root) / "pipeline_bbbb2222" / "function-collection")
            self.assertIsNone(find([root]), "two pipelines and no way to tell which one the YAML is of")
            self.assertIsNone(find([root], "cccc3333"))

    def test_a_single_pipeline_folder_is_the_one_without_an_id(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "pipeline_aaaa1111" / "function-collection").mkdir(parents=True)

            self.assertEqual(function_collection.find_function_collection_dir([root, None]),
                             Path(root) / "pipeline_aaaa1111" / "function-collection")

    def test_the_sources_read_back_as_published(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "function-collection"
            (folder / "util").mkdir(parents=True)
            (folder / "DateFunctions.java").write_text("class A {}", encoding="utf-8")
            (folder / "util" / "Helpers.java").write_text("class B {}", encoding="utf-8")
            (folder / ".DS_Store").write_text("junk", encoding="utf-8")

            self.assertEqual(function_collection.read_function_collection_dir(folder), [
                {"fileName": "DateFunctions.java", "code": "class A {}"},
                {"fileName": "util/Helpers.java", "code": "class B {}"},
            ])


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _entity(name="DRIVERS", schema="demo"):
    return {
        "entityId": f"ent-{name}", "entityName": f"{schema}.{name}", "groupId": "",
        "agentEntities": [
            {"type": "SingleTable", "agentId": "src-agent", "entityType": {"type": "Source"},
             "entityObject": {"id": "101", "schema": schema, "collection": name},
             "table": {"id": "101", "name": name, "schema": schema},
             "columns": [{"id": 1, "name": "ID", "dataType": "int", "isPK": True}],
             "keys": [{"id": 1, "name": "ID"}]},
            {"type": "SingleTable", "agentId": "tgt-agent",
             "entityType": {"type": "Target", "unchangedDataFilterType": "ENTIRE_ROW",
                            "allowedOperations": ["INSERT", "UPDATE", "DELETE"]},
             "entityObject": {"id": "101", "schema": "public", "collection": name},
             "table": {"id": "101", "name": name, "schema": "public"},
             "columns": [{"id": 1, "name": "ID", "dataType": "int", "isPK": True}],
             "keys": [{"id": 1, "name": "ID"}]},
        ],
    }


AGENTS = [
    {"agentType": "SOURCE", "agentTag": "mssql", "agentId": "src-agent",
     "hostCredentials": {"connectionName": "src", "host": "1.2.3.4", "port": 1433, "password": "p"}},
    {"agentType": "TARGET", "agentTag": "mysql", "agentId": "tgt-agent",
     "hostCredentials": {"connectionName": "tgt", "host": "5.6.7.8", "port": 3306, "password": "p"}},
]


class ExportTests(FunctionCollectionTestCase):
    def export(self, function, collections, **kwargs):
        import automator_app.corehub as corehub

        fetch = routed_fetch(collections, extra=lambda path, method, body: (
            {"pipelineId": "pipe", "name": "TestPipe"} if re.fullmatch(r"/pipelines/[^/]+", path) else {}))
        entities = [_entity()]
        with mock.patch.object(corehub, "fetch_pipeline_entities", return_value=entities), \
             mock.patch.object(corehub, "build_entities_maps", return_value={"ent-DRIVERS": entities[0]}), \
             mock.patch.object(corehub, "fetch_groups_map", return_value=({}, {}, {})), \
             mock.patch.object(corehub, "fetch_pipeline_jobs", return_value=[]), \
             mock.patch.object(corehub, "infer_agent_schema_types", return_value=("SQL", "SQL")), \
             mock.patch.object(corehub, "enrich_null_column_types_from_discovery", return_value=0), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS", "mysql": "RDBMS"}), \
             mock.patch.object(corehub, "get_pipeline_agents", return_value=AGENTS), \
             mock.patch.object(corehub, "list_pipelines",
                               return_value=[{"id": pid, "name": f"Pipe {pid}"} for pid in collections]), \
             mock.patch.object(corehub, "fetch_core_hub", side_effect=fetch):
            return getattr(corehub, function)(token="tok", base_url="http://test", use_ssl=False,
                                              skip_verify=False, **kwargs)

    def test_full_backup_writes_the_collection_next_to_the_yaml(self):
        zip_bytes = self.export("export_pipeline_full_backup", {"pipe": FakeFunctionCollections([DATE_FUNCTIONS, HELPERS])},
                                pipeline_id="pipe")

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            self.assertIn("pipeline_pipe/function-collection/DateFunctions.java", names)
            self.assertIn("pipeline_pipe/function-collection/util/Helpers.java", names)
            self.assertEqual(zf.read("pipeline_pipe/function-collection/DateFunctions.java").decode("utf-8"),
                             DATE_FUNCTIONS["code"])
            self.assertTrue(any(n.startswith("pipeline_pipe/backup_") and n.endswith(".yaml") for n in names))

    def test_full_backup_without_a_collection_writes_none_and_succeeds(self):
        zip_bytes = self.export("export_pipeline_full_backup", {"pipe": FakeFunctionCollections()}, pipeline_id="pipe")

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            self.assertFalse(any("function-collection" in n for n in zf.namelist()))
            self.assertTrue(any(n.endswith(".yaml") for n in zf.namelist()))

    def test_full_backup_fails_when_the_collection_cannot_be_read(self):
        with self.assertRaises(function_collection.FunctionCollectionError) as raised:
            self.export("export_pipeline_full_backup", {"pipe": FakeFunctionCollections(get_error="boom")},
                        pipeline_id="pipe")

        self.assertIn("Could not read the function collection of pipeline pipe", str(raised.exception))

    def test_entity_only_export_with_a_collection_is_a_zip_even_without_udfs(self):
        payload, media_type, filename = self.export(
            "export_pipeline_entities_package", {"pipe": FakeFunctionCollections([DATE_FUNCTIONS])}, pipeline_id="pipe")

        self.assertEqual(media_type, "application/zip")
        self.assertTrue(filename.endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            self.assertIn("function-collection/DateFunctions.java", zf.namelist())
            self.assertTrue(any(n.endswith(".yaml") for n in zf.namelist()))

    def test_entity_only_export_without_a_collection_stays_plain_yaml(self):
        payload, media_type, filename = self.export(
            "export_pipeline_entities_package", {"pipe": FakeFunctionCollections()}, pipeline_id="pipe")

        self.assertEqual(media_type, "application/x-yaml")
        self.assertTrue(filename.endswith(".yaml"))
        self.assertIn("demo", yaml.safe_load(payload.decode("utf-8")))

    def test_all_pipelines_backup_keeps_each_collection_with_its_pipeline(self):
        zip_bytes = self.export("export_all_pipelines_yaml", {
            "aaaa1111": FakeFunctionCollections([DATE_FUNCTIONS]),
            "bbbb2222": FakeFunctionCollections([HELPERS]),
            "cccc3333": FakeFunctionCollections(),
        })

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            self.assertIn("pipeline_aaaa1111/function-collection/DateFunctions.java", names)
            self.assertIn("pipeline_bbbb2222/function-collection/util/Helpers.java", names)
            self.assertFalse(any(n.startswith("pipeline_cccc3333/") for n in names))
            self.assertNotIn("function-collection-errors.txt", names)

    def test_all_pipelines_backup_lists_a_collection_it_could_not_read(self):
        zip_bytes = self.export("export_all_pipelines_yaml", {
            "aaaa1111": FakeFunctionCollections(get_error="boom"),
            "bbbb2222": FakeFunctionCollections([HELPERS]),
        })

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            note = zf.read("function-collection-errors.txt").decode("utf-8")
            self.assertIn("pipeline_bbbb2222/function-collection/util/Helpers.java", names)
            self.assertIn("Pipe aaaa1111 (aaaa1111)", note)
            self.assertIn("boom", note)
            self.assertEqual(len([n for n in names if n.endswith(".yaml") and "agents-config" not in n]), 2)


# ---------------------------------------------------------------------------
# Restore: the order of the requests
# ---------------------------------------------------------------------------

AGENTS_CONFIG = {
    "agents": [
        {"agentType": "SOURCE", "agentTag": "mssql-cdc", "agentId": "src-1", "hostCredentials": {"host": "1.1.1.1"},
         "customHostCredentials": {}, "specificConfiguration": {}},
        {"agentType": "TARGET", "agentTag": "mariadb-cdc", "agentId": "tgt-1", "hostCredentials": {"host": "2.2.2.2"},
         "customHostCredentials": {}, "specificConfiguration": {}},
        {"agentType": "SOURCE", "agentTag": "mssql-cdc", "agentId": "src-2", "hostCredentials": {"host": "3.3.3.3"},
         "customHostCredentials": {}, "specificConfiguration": {}},
        {"agentType": "TARGET", "agentTag": "mariadb-cdc", "agentId": "tgt-2", "hostCredentials": {"host": "4.4.4.4"},
         "customHostCredentials": {}, "specificConfiguration": {}},
    ],
    "pipelines": [
        {"pipelineId": "aaaa1111", "pipelineName": "First",
         "agents": [{"agentType": "SOURCE", "agentTag": "mssql-cdc", "agentId": "src-1"},
                    {"agentType": "TARGET", "agentTag": "mariadb-cdc", "agentId": "tgt-1"}]},
        {"pipelineId": "bbbb2222", "pipelineName": "Second",
         "agents": [{"agentType": "SOURCE", "agentTag": "mssql-cdc", "agentId": "src-2"},
                    {"agentType": "TARGET", "agentTag": "mariadb-cdc", "agentId": "tgt-2"}]},
    ],
}


def pipeline_yaml(pipeline_id, name):
    return yaml.safe_dump({
        "exportMetadata": {"pipelineId": pipeline_id, "pipelineName": name},
        "dbo": {"target": "public", "sourceType": "SQL", "targetType": "SQL",
                "tables": {"whitelist": [], "custom": {}}},
    }, sort_keys=False)


class ImportOrderTests(FunctionCollectionTestCase):
    """An Export All / full backup import publishes the collection before its UDFs and its entities."""

    @classmethod
    def setUpClass(cls):
        import automator_app.app as app_module
        from automator_app.state import state
        from fastapi.testclient import TestClient

        cls.app_module = app_module
        cls.state = state
        cls.client = TestClient(app_module.create_app())

    def setUp(self):
        super().setUp()
        import automator_app.corehub as corehub

        self.state.set_auth("tok", "http://test", use_ssl=False, skip_verify=False)
        self.addCleanup(self.state.clear_auth)
        self.events = []
        self.collections = {}
        self.created = 0

        def import_config_only(**kwargs):
            self.created += 1
            return {"pipelineId": f"new-{self.created}", "pipelineName": kwargs["config"].get("pipelineName")}

        def run_create_entities_(**kwargs):
            self.events.append(("RUN", kwargs["pipeline_id"]))
            self.assertIs(kwargs["restore_function_collection"], False)
            return {"success": True}

        self.fetch = routed_fetch(self.collections, events=self.events)
        for patcher in (
            mock.patch.object(corehub, "import_pipeline_config_only", side_effect=import_config_only),
            mock.patch.object(corehub, "run_create_entities", side_effect=run_create_entities_),
            mock.patch.object(corehub, "fetch_core_hub", side_effect=self.fetch),
            mock.patch.object(self.app_module.create_user_defined_functions, "main",
                              side_effect=lambda pipeline_id, *a, **k: self.events.append(("UDF", pipeline_id))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def post(self, members):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("agents-config.yaml", yaml.safe_dump(AGENTS_CONFIG, sort_keys=False))
            for name, text in members.items():
                zf.writestr(name, text)
        response = self.client.post("/api/import/all", files={"file": ("backup.zip", buffer.getvalue(), "application/zip")})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def backup(self, **collections):
        members = {"backup_First_aaaa1111_20260610_112407.yaml": pipeline_yaml("aaaa1111", "First"),
                   "udf-tgt-1/UdfOrders.java": "class UdfOrders {}"}
        for pipeline_id, sources in collections.items():
            for source in sources:
                members[f"pipeline_{pipeline_id}/function-collection/{source['fileName']}"] = source["code"]
        return members

    def test_the_collection_is_installed_before_the_udfs_and_the_entities(self):
        self.collections["new-1"] = FakeFunctionCollections(polls=1)

        result = self.post(self.backup(aaaa1111=[DATE_FUNCTIONS, HELPERS]))

        self.assertIsNone(result.get("logs"), result)
        self.assertEqual(timeline(self.events), [
            "COLLECTION GET /", "COLLECTION PUT /", "COLLECTION GET /publications/job-1",
            "COLLECTION GET /publications/job-1", "COLLECTION POST /install",
            "RUN new-1", "UDF new-1", "COMPLETED",
        ])
        self.assertEqual(self.collections["new-1"].puts, [{"sources": [DATE_FUNCTIONS, HELPERS]}])
        self.assertTrue(self.collections["new-1"].installed)

    def test_each_pipeline_gets_its_own_collection(self):
        self.collections.update({"new-1": FakeFunctionCollections(), "new-2": FakeFunctionCollections()})
        members = self.backup(aaaa1111=[DATE_FUNCTIONS], bbbb2222=[HELPERS])
        members["backup_Second_bbbb2222_20260610_112414.yaml"] = pipeline_yaml("bbbb2222", "Second")

        self.post(members)

        self.assertEqual(self.collections["new-1"].puts, [{"sources": [DATE_FUNCTIONS]}])
        self.assertEqual(self.collections["new-2"].puts, [{"sources": [HELPERS]}])

    def test_a_collection_the_pipeline_already_has_is_only_installed(self):
        self.collections["new-1"] = FakeFunctionCollections([DATE_FUNCTIONS], installed=False)

        self.post(self.backup(aaaa1111=[DATE_FUNCTIONS]))

        self.assertEqual(self.collections["new-1"].puts, [])
        self.assertEqual(methods(self.events), [("GET", "/"), ("POST", "/install")])
        self.assertIn(("RUN", "new-1"), self.events)

    def test_a_running_publication_is_waited_for(self):
        self.collections["new-1"] = FakeFunctionCollections(busy=1)

        result = self.post(self.backup(aaaa1111=[DATE_FUNCTIONS]))

        self.assertIsNone(result.get("logs"), result)
        self.assertEqual(len(self.collections["new-1"].puts), 2)
        self.assertEqual(timeline(self.events), [
            "COLLECTION GET /", "COLLECTION PUT /", "COLLECTION GET /publications/job-1", "COLLECTION PUT /",
            "COLLECTION POST /install", "RUN new-1", "UDF new-1", "COMPLETED",
        ])

    def test_a_refused_collection_stops_the_restore_of_the_pipeline_and_says_why(self):
        self.collections["new-1"] = FakeFunctionCollections(rejection={
            "collectionErrors": [{"file": "DateFunctions.java", "line": 3, "message": "cannot find symbol"}],
            "consumerFailures": [], "warnings": [], "unverifiedConsumers": []})

        result = self.post(self.backup(aaaa1111=[DATE_FUNCTIONS]))

        text = " ".join([result["message"]] + (result.get("logs") or []))
        self.assertIn("function collection not restored", text)
        self.assertIn("DateFunctions.java:3: cannot find symbol", text)
        self.assertNotIn(("RUN", "new-1"), self.events)
        self.assertNotIn(("UDF", "new-1"), self.events)
        self.assertNotIn(("PUT", "/pipelines/new-1"), self.events, "a pipeline without entities is not marked complete")

    def test_a_backup_without_a_collection_makes_no_function_collection_request(self):
        result = self.post(self.backup())

        self.assertIsNone(result.get("logs"), result)
        self.assertEqual(methods(self.events), [])
        self.assertIn(("RUN", "new-1"), self.events)

    def test_the_collection_of_a_zip_run_on_its_own_is_extracted_next_to_the_yaml(self):
        import create_all_entities

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("backup_First_aaaa1111_20260610_112407.yaml", pipeline_yaml("aaaa1111", "First"))
            zf.writestr("function-collection/DateFunctions.java", DATE_FUNCTIONS["code"])

        yaml_path = self.app_module._store_uploaded_zip(buffer.getvalue())

        config = yaml.safe_load(yaml_path.read_text())
        self.assertEqual(create_all_entities._function_collection_sources(str(yaml_path), config),
                         [{"fileName": "DateFunctions.java", "code": DATE_FUNCTIONS["code"]}])


class DuplicatePipelineTests(FunctionCollectionTestCase):
    def test_the_collection_is_copied_before_the_entities_are_cloned(self):
        import automator_app.corehub as corehub

        collections = {"original-pipe": FakeFunctionCollections([DATE_FUNCTIONS, HELPERS]),
                       "cloned-pipe": FakeFunctionCollections()}

        def extra(path, method, body):
            if path == "/pipelines/original-pipe/config":
                return {"agents": [{"agentType": "SOURCE", "agentTag": "mssql", "agentId": "src1"},
                                   {"agentType": "TARGET", "agentTag": "mysql", "agentId": "tgt1"}]}
            if path == "/pipelines" and method == "GET":
                return []
            if path == "/pipelines" and method == "POST":
                return {"pipelineId": "cloned-pipe", "name": body.get("name")}
            if "/agents/add" in path:
                return {"agentId": "new-agent-1"}
            return {"ok": True}

        fetch = routed_fetch(collections, extra=extra)

        def run_create(**kwargs):
            fetch.events.append(("RUN", kwargs["pipeline_id"]))
            self.assertIs(kwargs["restore_function_collection"], False)
            return {"success": True, "result": {"errors": []}}

        with mock.patch.object(corehub, "fetch_core_hub", side_effect=fetch), \
             mock.patch.object(corehub, "fetch_pipeline_entities", return_value=[]), \
             mock.patch.object(corehub, "export_pipeline_yaml", return_value="schemas:\n  demo:\n    target: public\n"), \
             mock.patch.object(corehub, "run_create_entities", side_effect=run_create), \
             mock.patch.object(corehub, "_load_agent_type_catalog", return_value={"mssql": "RDBMS", "mysql": "RDBMS"}):
            corehub.duplicate_pipeline(
                token="tok", base_url="http://test", pipeline_id="original-pipe", new_pipeline_name="ClonedPipe",
                source_agent_tag="mssql", target_agent_tag="mysql", source_agent_password="src-pass",
                target_agent_password="tgt-pass", clone_entities=True, use_ssl=False, skip_verify=False)

        self.assertEqual(collections["cloned-pipe"].puts, [{"sources": [DATE_FUNCTIONS, HELPERS]}])
        self.assertTrue(collections["cloned-pipe"].installed)
        steps = timeline(fetch.events)
        self.assertLess(steps.index("COLLECTION POST /install"), steps.index("RUN cloned-pipe"))


# ---------------------------------------------------------------------------
# Restore through create_all_entities
# ---------------------------------------------------------------------------

class CreateEntitiesRestoreTests(FunctionCollectionTestCase):
    """``create_all_entities.main`` (Automator run, bootstrapper, round trip) restores the collection first."""

    YAML = table("""        name: customers
        customProperties:
          target:
            udf:
              - {name: UdfCustomers, type: Java}
        columns:
          - ID: id
          - source: NAME
            target: name
            expression:
              type: CustomFn
              functionName: convertDate
              arguments:
                - {type: SourceColumnArg, name: NAME}
          - EMAIL: email_address""")

    def setUp(self):
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        (self.root / "function-collection").mkdir()
        (self.root / "function-collection" / "DateFunctions.java").write_text(DATE_FUNCTIONS["code"], encoding="utf-8")
        self.yaml_path = self.root / "backup.yaml"
        self.yaml_path.write_text(self.YAML, encoding="utf-8")

    def main(self, hub, **kwargs):
        import create_all_entities
        import create_user_defined_functions

        agents = [{"agentType": "SOURCE", "agentId": SOURCE_AGENT, "agentTag": "mssql"},
                  {"agentType": "TARGET", "agentId": TARGET_AGENT, "agentTag": "mysql"}]
        with patched(hub, udf_handler=lambda *a, **k: hub.events.append(("UDF", a[0]))), \
             mock.patch.object(create_all_entities, "get_pipeline_config", return_value={}), \
             mock.patch.object(create_all_entities, "get_pipeline_agents", return_value=agents), \
             mock.patch.object(create_all_entities, "lockfile_complete"), \
             mock.patch.object(create_all_entities, "lockfile_failure"), \
             mock.patch.object(create_user_defined_functions, "UDF_PATH", str(self.root)):
            return create_all_entities.main("pipeline", "src", "tgt", "RDBMS", "RDBMS", str(self.yaml_path), "token",
                                            True, 50, **kwargs)

    def hub(self, collection=None):
        return FakeCoreHub(SOURCE_TABLES, TARGET_TABLES, version=V23, function_collection=collection or
                           FakeFunctionCollections(polls=1))

    def test_the_collection_is_published_and_installed_before_the_udf_and_the_entity(self):
        hub = self.hub()

        result = self.main(hub)

        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(timeline(hub.events), [
            "COLLECTION GET /", "COLLECTION PUT /", "COLLECTION GET /publications/job-1",
            "COLLECTION GET /publications/job-1", "COLLECTION POST /install",
            "COLLECTION GET /",  # the pre-flight check reads the installed collection
            "UDF CUSTOMERS", "ENTITIES",
        ])
        self.assertEqual(hub.function_collection.puts, [{"sources": [DATE_FUNCTIONS]}])

    def test_a_refused_collection_creates_no_udf_and_no_entity(self):
        hub = self.hub(FakeFunctionCollections(rejection={
            "collectionErrors": [{"file": "DateFunctions.java", "line": 3, "message": "cannot find symbol"}],
            "consumerFailures": [], "warnings": [], "unverifiedConsumers": []}))

        result = self.main(hub)

        self.assertEqual(result["total"], 0)
        self.assertIn("DateFunctions.java:3: cannot find symbol", " ".join(result["errors"]))
        self.assertEqual(hub.puts, [])
        self.assertFalse([e for e in hub.events if e[0] == "UDF"])

    def test_a_caller_that_has_restored_it_does_not_publish_again(self):
        hub = self.hub(FakeFunctionCollections([DATE_FUNCTIONS]))

        result = self.main(hub, restore_function_collection=False)

        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(hub.function_collection.puts, [])
        self.assertEqual(timeline(hub.events), ["COLLECTION GET /", "UDF CUSTOMERS", "ENTITIES"])

    def test_function_collection_path_names_the_sources_of_a_standalone_run(self):
        (self.root / "function-collection" / "DateFunctions.java").unlink()
        (self.root / "function-collection").rmdir()
        elsewhere = self.root / "kit-sources"
        elsewhere.mkdir()
        (elsewhere / "DateFunctions.java").write_text(DATE_FUNCTIONS["code"], encoding="utf-8")
        hub = self.hub()

        with mock.patch.dict(os.environ, {"FUNCTION_COLLECTION_PATH": str(elsewhere)}):
            result = self.main(hub)

        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(hub.function_collection.puts, [{"sources": [DATE_FUNCTIONS]}])

    def test_a_backup_without_a_collection_publishes_nothing(self):
        (self.root / "function-collection" / "DateFunctions.java").unlink()
        (self.root / "function-collection").rmdir()
        hub = self.hub(FakeFunctionCollections.exporting("convertDate"))

        result = self.main(hub)

        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(hub.function_collection.puts, [])

    def test_a_core_hub_before_2_3_is_not_asked_for_a_collection(self):
        hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES, version=V22, function_collection=FakeFunctionCollections())

        result = self.main(hub)

        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(hub.function_collection.requests, [])


# ---------------------------------------------------------------------------
# Pre-flight check of the entities
# ---------------------------------------------------------------------------

def run(columns, collection, *, version=V23, tables=("CUSTOMERS",), skip_errors=True, yaml_text=None):
    hub = FakeCoreHub(SOURCE_TABLES, TARGET_TABLES, version=version, function_collection=collection)
    result = run_create_entities(hub, yaml_text or table(columns), _source_list(*tables), skip_errors=skip_errors)
    return hub, result


class PreflightTests(FunctionCollectionTestCase):
    def test_a_function_the_collection_does_not_export_is_refused_before_any_request(self):
        hub, result = run(CUSTOM_FN_COLUMNS, FakeFunctionCollections.exporting("trimAll"))

        self.assertEqual(hub.puts, [])
        self.assertEqual((result["successful"], result["failed"]), (0, 1))
        message = " ".join(result["errors"])
        self.assertIn("Table CUSTOMERS", message)
        self.assertIn("column name is composed by the function convertDate", message)
        self.assertIn("installed on the target pipeline pipeline does not export it (it exports trimAll)", message)
        self.assertIn("Restore or publish the function collection on the target pipeline first", message)
        self.assertIn("older than this release", message)

    def test_a_pipeline_without_a_collection_is_warned_about_and_the_entity_is_saved(self):
        # CoreHub skips the check while no collection is installed: one may be published after the entity.
        import create_all_entities

        logging.disable(logging.NOTSET)
        self.addCleanup(logging.disable, logging.CRITICAL)
        with self.assertLogs(create_all_entities.logger, level="WARNING") as logs:
            hub, result = run(CUSTOM_FN_COLUMNS, FakeFunctionCollections())

        self.assertEqual((result["successful"], result["failed"]), (1, 0), result)
        self.assertEqual(len(hub.puts), 1)
        warning = " ".join(logs.output)
        self.assertIn("Table CUSTOMERS", warning)
        self.assertIn("column name is composed by the function convertDate", warning)
        self.assertIn("has no function collection installed", warning)
        self.assertIn("restored or published", warning)
        self.assertIn("older than this release", warning)

    def test_a_collection_published_but_not_installed_is_the_same_as_none(self):
        hub, result = run(CUSTOM_FN_COLUMNS, FakeFunctionCollections([DATE_FUNCTIONS], installed=False))

        self.assertEqual((result["successful"], result["failed"]), (1, 0), result)
        self.assertEqual(len(hub.puts), 1)

    def test_an_exported_function_is_saved(self):
        hub, result = run(CUSTOM_FN_COLUMNS, FakeFunctionCollections.exporting("convertDate"))

        self.assertEqual((result["successful"], result["failed"]), (1, 0), result)
        self.assertEqual(len(hub.puts), 1)

    def test_only_the_table_with_the_unbound_function_is_refused(self):
        yaml_text = f"""
src:
  target: tgt
  tables:
    whitelist: [CUSTOMERS, PARTS]
    custom:
      CUSTOMERS:
{CUSTOM_FN_COLUMNS}
      PARTS: {{}}
"""
        hub, result = run(None, FakeFunctionCollections.exporting("trimAll"), tables=("CUSTOMERS", "PARTS"),
                          yaml_text=yaml_text)

        self.assertEqual((result["successful"], result["failed"]), (1, 1), result)
        self.assertEqual([e["entityName"] for put in hub.puts for e in put["entities"]], ["src.PARTS"])
        self.assertEqual(len(result["errors"]), 1)

    def test_every_column_and_function_is_named(self):
        columns = """        name: customers
        columns:
          - ID: id
          - source: NAME
            target: name
            expression: {type: CustomFn, functionName: convertDate, arguments: [{type: SourceColumnArg, name: NAME}]}
          - source: EMAIL
            target: email_address
            expression: {type: CustomFn, functionName: maskEmail, arguments: [{type: SourceColumnArg, name: EMAIL}]}"""

        _, result = run(columns, FakeFunctionCollections.exporting("convertDate"))

        message = " ".join(result["errors"])
        self.assertIn("column email_address is composed by the function maskEmail", message)
        self.assertNotIn("column name is composed", message)

    def test_a_refusal_raises_when_errors_are_not_skipped(self):
        from entity_mapping import EntityMappingError

        with self.assertRaises(EntityMappingError) as raised:
            run(CUSTOM_FN_COLUMNS, FakeFunctionCollections.exporting("trimAll"), skip_errors=False)

        self.assertIn("function collection", str(raised.exception))

    def test_a_yaml_without_custom_functions_does_not_read_the_collection(self):
        hub, result = run("""        name: customers
        columns:
          - ID: id
          - NAME: name""", FakeFunctionCollections())

        self.assertEqual((result["successful"], result["failed"]), (1, 0), result)
        self.assertEqual(hub.function_collection.requests, [])

    def test_a_core_hub_before_2_3_is_unaffected(self):
        hub, result = run(CUSTOM_FN_COLUMNS, FakeFunctionCollections(), version=V22)

        self.assertEqual((result["successful"], result["failed"]), (1, 0), result)
        self.assertEqual(hub.function_collection.requests, [])

    def test_an_unreadable_collection_leaves_the_check_to_core_hub(self):
        hub, result = run(CUSTOM_FN_COLUMNS, FakeFunctionCollections(get_error="boom"))

        self.assertEqual((result["successful"], result["failed"]), (1, 0), result)

    def test_a_function_name_nested_in_an_argument_is_checked(self):
        columns = """        name: customers
        columns:
          - ID: id
          - source: NAME
            target: name
            expression:
              type: CustomFn
              functionName: convertDate
              arguments:
                - {type: CustomFn, functionName: trimAll, arguments: [{type: SourceColumnArg, name: NAME}]}"""

        _, result = run(columns, FakeFunctionCollections.exporting("convertDate"))

        self.assertIn("the function trimAll", " ".join(result["errors"]))


class RoundTripTests(FunctionCollectionTestCase):
    """The Automator round trip restores the function collection with the YAML and compares it."""

    @staticmethod
    def side(pipeline_id, collection):
        return {"pipelineId": pipeline_id, "pipeline": {"name": "kit"}, "agents": [], "groups": [], "jobs": [],
                "entities": [{"entityName": "s.T", "agentEntities": []}], "idMap": {}, "export": "a: 1\n",
                "functionCollection": collection}

    def run_round_trip(self, original, restored):
        import json

        import automator_roundtrip as rt

        sides = {"orig": self.side("orig", original), "new": self.side("new", restored)}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(rt, "authenticate", return_value="tok"), \
                mock.patch.object(rt, "snapshot_pipeline", side_effect=lambda _t, pid: {
                    k: v for k, v in sides[pid].items() if k != "export"}), \
                mock.patch.object(rt, "render_pipeline_export", side_effect=lambda _t, pid: sides[pid]["export"]), \
                mock.patch.object(rt, "restore_export", return_value=("new", "")) as restore:
            code = rt.run("orig", Path(tmp), "/nonexistent", 60, "kit")
            report = json.loads((Path(tmp) / "report.json").read_text())
            exported = {str(p.relative_to(tmp)): p.read_text() for p in Path(tmp).rglob("*.java")}
        return code, report, exported, restore

    def test_the_exported_sources_are_given_to_the_restore_and_compared(self):
        sources = {"DateFunctions.java": DATE_FUNCTIONS["code"], "util/Helpers.java": HELPERS["code"]}

        code, report, exported, restore = self.run_round_trip(sources, dict(sources))

        self.assertEqual(code, 0, report)
        self.assertEqual(report["checks"][-1]["name"], "restored-function-collection-matches")
        self.assertEqual(restore.call_args.kwargs["function_collection_dir"].name, "original-function-collection")
        self.assertEqual(exported["original-function-collection/DateFunctions.java"], DATE_FUNCTIONS["code"])
        self.assertEqual(exported["original-function-collection/util/Helpers.java"], HELPERS["code"])

    def test_a_source_lost_or_changed_by_the_restore_fails(self):
        sources = {"DateFunctions.java": "class A {\n}\n", "Helpers.java": "class B {}\n"}
        restored = {"DateFunctions.java": "class A {\n  int x;\n}\n"}

        code, report, _, _ = self.run_round_trip(sources, restored)

        check = report["checks"][-1]
        self.assertEqual(code, 1)
        self.assertFalse(check["passed"])
        self.assertIn("missing after restore: Helpers.java", check["message"])
        self.assertIn("configured differently: DateFunctions.java", check["message"])

    def test_a_pipeline_without_a_collection_adds_no_check_and_restores_none(self):
        code, report, exported, restore = self.run_round_trip({}, {})

        self.assertEqual(code, 0, report)
        self.assertNotIn("restored-function-collection-matches", [c["name"] for c in report["checks"]])
        self.assertIsNone(restore.call_args.kwargs["function_collection_dir"])
        self.assertEqual(exported, {})

    def test_the_restore_process_is_pointed_at_the_sources(self):
        import automator_roundtrip as rt

        seen = {}

        def run_main(cmd, cwd, env, **kwargs):
            seen["path"] = env.get("FUNCTION_COLLECTION_PATH")
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"FUNCTION_COLLECTION_PATH": "/someone/elses"}), \
                mock.patch.object(rt.subprocess, "run", side_effect=run_main):
            rt.restore_export(Path(tmp) / "e.yaml", "p", Path(tmp), 60, function_collection_dir=Path(tmp) / "src")
            self.assertEqual(seen["path"], str(Path(tmp) / "src"))
            rt.restore_export(Path(tmp) / "e.yaml", "p", Path(tmp), 60)
            self.assertIsNone(seen["path"])


class NotFoundAndErrorsTests(FunctionCollectionTestCase):
    def test_http_status_is_read_through_the_chained_errors(self):
        self.assertEqual(function_collection.http_status(_refusal("/p", "x", 404, method="GET")), 404)
        self.assertEqual(function_collection.http_status(RuntimeError("no response")), None)

    def test_a_404_is_no_collection_and_other_errors_are_not(self):
        fake = FakeFunctionCollections()
        fetch = routed_fetch({"pipe": fake})
        self.assertIsNone(function_collection.fetch_function_collection("tok", "pipe", fetch=fetch))

        broken = routed_fetch({"pipe": FakeFunctionCollections(get_error="boom")})
        with self.assertRaises(function_collection.FunctionCollectionError):
            function_collection.fetch_function_collection("tok", "pipe", fetch=broken)

    def test_the_expected_statuses_are_declared_so_they_are_not_logged_as_errors(self):
        fetch = routed_fetch({"pipe": FakeFunctionCollections(busy=1)})
        function_collection.restore_function_collection("tok", "pipe", [DATE_FUNCTIONS], fetch=fetch)

        by_call = {(method, suffix): statuses for method, suffix, statuses in fetch.expected}
        self.assertEqual(by_call[("GET", "/")], {404})
        self.assertEqual(by_call[("PUT", "/")], {409})

    def test_file_names_stay_under_their_folder_and_are_not_lost(self):
        collection = FakeFunctionCollections([
            {"fileName": "../../etc/Evil.java", "code": "a"},
            {"fileName": "util/Helpers.java", "code": "b"},
            {"fileName": "Helpers.java", "code": "c"},
            {"fileName": "Helpers.java", "code": "d"},
        ])
        files = function_collection.function_collection_files("tok", "pipe", fetch=routed_fetch({"pipe": collection}))

        self.assertEqual(files, [("etc/Evil.java", "a"), ("util/Helpers.java", "b"), ("Helpers.java", "c"),
                                 ("Helpers_2.java", "d")])


if __name__ == "__main__":
    unittest.main()
