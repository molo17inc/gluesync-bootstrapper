"""A fake CoreHub for the bootstrapper payload tests.

Runs ``create_all_entities.create_entities`` against an in-memory discovery and captures every
``PUT /pipelines/{id}/config/entities`` body, so a test can compare payloads without a running
CoreHub. Discovery is described per agent as ``{(schema, table): [column, ...]}``; a table that is
not described discovers no column, as a missing table does.
"""

import contextlib
import copy
import io
import re
import sys
from pathlib import Path
from unittest import mock

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SOURCE_AGENT = "source-agent"
TARGET_AGENT = "target-agent"


def column(name, col_id, data_type="varchar", is_pk=False, nullable=True, **extra):
    """A discovered column as the discovery route returns it."""
    col = {
        "id": col_id,
        "position": extra.pop("position", col_id),
        "name": name,
        "dataType": data_type,
        "isPK": is_pk,
        "isNullable": nullable,
        "charMaxLength": 255 if data_type.lower() in {"varchar", "nvarchar"} else 0,
        "numPrec": 0,
        "numScale": 0,
    }
    col.update(extra)
    return col


def node_info(category="RDBMS"):
    return {"category": category, "dataTypesMatrix": []}


class _Response:
    def __init__(self, status_code, text, body=None):
        self.status_code = status_code
        self.text = text
        self.body = body

    def json(self):
        return self.body if self.body is not None else {"message": self.text}


def _refusal(path, reason, status_code=400, method="PUT", body=None):
    """The error commons.fetch_core_hub raises for an HTTP error: a RuntimeError caused by requests' HTTPError."""
    import requests

    cause = requests.exceptions.HTTPError(f"{status_code} Client Error", response=_Response(status_code, reason, body))
    try:
        raise cause
    except requests.exceptions.HTTPError as error:
        try:
            raise RuntimeError(f"CoreHub request {method} {path} failed: {error} | Response: {reason}") from error
        except RuntimeError as wrapped:
            return wrapped


def gs_function_source(file_name, *function_names):
    """A ``{fileName, code}`` source of a function collection with one ``@GsFunction`` method per name."""
    methods = "\n".join(
        f'    @GsFunction("{name}")\n    public static String {name}(String value) {{ return value; }}\n'
        for name in function_names
    )
    class_name = file_name.rsplit("/", 1)[-1].removesuffix(".java")
    return {"fileName": file_name, "code": f"public class {class_name} {{\n{methods}}}\n"}


class FakeFunctionCollections:
    """The function collection routes of a CoreHub for one pipeline (``/config/function-collection``).

    ``published`` holds the sources of the published version (None: nothing published, GET answers 404); a
    publication exports the ``@GsFunction`` methods of the sources it is given. ``busy`` publications refuse the next
    PUTs with a 409 and the running job; ``polls`` is how many reads of a job answer Running before it ends;
    ``rejection`` (or ``failure``) makes publications end refused. ``requests`` records ``(method, path)`` of every
    call, in order, and ``log`` receives the same entries when given, to tell the order from other routes' calls.
    """

    ROOT = "/config/function-collection"

    def __init__(self, sources=None, *, installed=True, busy=0, polls=0, rejection=None, failure=None, log=None,
                 get_error=None):
        self.published = copy.deepcopy(sources)
        self.installed = installed and sources is not None
        self.busy = busy
        self.polls = polls
        self.rejection = rejection
        self.failure = failure
        self.get_error = get_error
        self.requests = []
        self.log = log if log is not None else self.requests
        self.puts = []
        self.version = 1 if sources is not None else 0
        self._job_reads = 0

    @classmethod
    def exporting(cls, *function_names, **options):
        """A published, installed collection whose one source exports ``function_names``."""
        return cls([gs_function_source("Functions.java", *function_names)], **options)

    @staticmethod
    def handles(path):
        return FakeFunctionCollections.ROOT in path

    def _exported(self, sources):
        return [
            {"name": name, "description": "", "className": "Functions", "methodName": name,
             "parameterTypes": ["String"], "returnType": "String"}
            for source in sources or [] for name in re.findall(r'@GsFunction\("(\w+)"\)', source["code"])
        ]

    def _job(self, state="Running", *, published=None):
        job = {"jobId": "job-1", "pipelineId": "pipeline", "state": state}
        if state == "Completed":
            if self.rejection is not None:
                job["rejection"] = copy.deepcopy(self.rejection)
            elif published is not None:
                job["published"] = published
        if state == "Failed" or (state == "Completed" and self.failure):
            job["failure"] = self.failure or "publication failed"
        return job

    def _finished(self):
        if self.failure:
            return self._job("Failed")
        if self.rejection is not None:
            return self._job("Completed")
        return self._job("Completed", published={
            "version": f"v{self.version}", "exportedFunctions": self._exported(self.published),
            "warnings": [], "unverifiedConsumers": [], "requiresPipelineRestart": True})

    def fetch(self, path, method="GET", token=None, body=None, **kwargs):
        suffix = path.split(self.ROOT, 1)[1]
        self.requests.append((method, suffix or "/"))
        if self.log is not self.requests:
            self.log.append((method, path))
        if method == "GET" and not suffix:
            if self.get_error:
                raise _refusal(path, self.get_error, 500, method="GET")
            if self.published is None:
                raise _refusal(path, "No function collection published for this pipeline", 404, method="GET")
            return {
                "version": f"v{self.version}", "installedVersion": f"v{self.version}" if self.installed else None,
                "sources": copy.deepcopy(self.published),
                "exportedFunctions": self._exported(self.published) if self.installed else [],
                "invocationStats": {},
            }
        if method == "PUT" and not suffix:
            return self._put(path, body)
        if method == "GET" and suffix.startswith("/publications/"):
            self._job_reads += 1
            return self._job() if self._job_reads <= self.polls else self._finished()
        if method == "POST" and suffix == "/install":
            return self._install()
        raise AssertionError(f"unexpected function collection call {method} {path}")

    def _put(self, path, body):
        self.puts.append(copy.deepcopy(body))
        if self.busy:
            self.busy -= 1
            raise _refusal(path, "publication already running", 409, body=self._job())
        self._job_reads = 0
        if self.rejection is None and not self.failure:
            self.published = copy.deepcopy(body["sources"])
            self.version += 1
            self.installed = False
        return self._job() if self.polls else self._finished()

    def _install(self):
        if self.published is None:
            return {"result": "NothingPublished", "exportedFunctions": [], "restartedEntities": []}
        result = "AlreadyInstalled" if self.installed else "Installed"
        self.installed = True
        return {"result": result, "version": f"v{self.version}", "exportedFunctions": self._exported(self.published),
                "restartedEntities": []}


class FakeCoreHub:
    """In-memory discovery plus a capture of the entity upserts.

    ``version`` is what ``GET /version`` answers (``None`` for a CoreHub that cannot tell);
    ``refuse`` decides, per PUT body, whether CoreHub answers it with an error. ``function_collection`` is the
    pipeline's collection (a :class:`FakeFunctionCollections`); by default it exports ``concat``, the function the
    payload fixtures bind, so those tests need not care about it.
    """

    def __init__(self, source_tables, target_tables, *, version="2.2.11.5 - build test",
                 source_category="RDBMS", target_category="RDBMS", refuse=None, function_collection=None):
        self.source_tables = source_tables
        self.target_tables = target_tables
        self.version = version
        self.source_category = source_category
        self.target_category = target_category
        self.refuse = refuse
        self.function_collection = function_collection or FakeFunctionCollections.exporting("concat")
        # Every function collection call and entity upsert, in order: ``(method, path)``.
        self.events = []
        self.function_collection.log = self.events
        self.puts = []
        self.saved = []

    def table_columns(self, token, pipeline_id, agent_id, schema, table):
        tables = self.source_tables if agent_id == SOURCE_AGENT else self.target_tables
        return {"columns": copy.deepcopy(tables.get((schema, table), []))}

    def agent_tables(self, token, pipeline_id, agent_id, schema):
        tables = self.source_tables if agent_id == SOURCE_AGENT else self.target_tables
        return [
            {"name": name, "schema": table_schema, "id": 9000 + idx}
            for idx, (table_schema, name) in enumerate(sorted(tables))
            if table_schema == schema
        ]

    def node_info(self, token, pipeline_id, agent_id):
        return node_info(self.source_category if agent_id == SOURCE_AGENT else self.target_category)

    def fetch(self, path, method="GET", token=None, body=None, **kwargs):
        if path == "/version":
            return self.version
        if FakeFunctionCollections.handles(path):
            return self.function_collection.fetch(path, method, token, body)
        if path.endswith("/config/entities") and method == "PUT":
            self.events.append((method, path))
            self.puts.append(copy.deepcopy(body))
            if self.refuse:
                reason = self.refuse(body)
                if reason:
                    raise _refusal(path, reason)
            self.saved.extend(copy.deepcopy(body.get("entities", [])))
            return {"ok": True}
        if path.endswith("/entities") and method == "GET":
            return [{"entity": {"entityId": f"entity-{idx}", **entity}} for idx, entity in enumerate(self.saved, 1)]
        return {}


@contextlib.contextmanager
def patched(hub, udf_handler=None):
    """Patch ``create_all_entities`` so its CoreHub calls, discovery and UDF handling go to ``hub``."""
    import commons
    import create_all_entities

    # Absent before the Gluesync 2.3 alignment, whose payloads the golden recordings come from.
    getattr(commons, "reset_corehub_version_cache", lambda: None)()

    patches = [
        mock.patch.object(create_all_entities, "CREATE_TABLE_IF_NOT_EXISTS", False),
        mock.patch.object(create_all_entities, "get_node_info", side_effect=hub.node_info),
        mock.patch.object(create_all_entities, "get_agent_tables", side_effect=hub.agent_tables),
        mock.patch.object(create_all_entities, "get_table_columns", side_effect=hub.table_columns),
        mock.patch.object(create_all_entities, "map_data_type", side_effect=lambda source_type, *a, **k: source_type),
        mock.patch.object(create_all_entities, "fetch_core_hub", side_effect=hub.fetch),
        mock.patch.object(create_all_entities, "handle_udf_function_definition",
                          side_effect=udf_handler or (lambda *a, **k: None)),
        mock.patch.object(create_all_entities, "create_group", side_effect=lambda token, pipeline_id, name, **k: f"group-{name}"),
        mock.patch.object(create_all_entities, "assign_entities_to_group", return_value=True),
        mock.patch.object(create_all_entities, "create_entity_schedules", return_value=None),
        mock.patch.object(create_all_entities, "create_group_schedules", return_value=None),
        mock.patch.object(create_all_entities, "create_pipeline_schedules", return_value=None),
    ]
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        yield


def run_create_entities(hub, yaml_text, source_tables, *, source_schema="src", target_schema="tgt",
                        source_type="RDBMS", target_type="RDBMS", skip_errors=True, chunk_size=50, udf_handler=None):
    """Run ``create_entities`` on ``yaml_text`` against ``hub``; returns its result.

    ``udf_handler`` stands in for ``handle_udf_function_definition`` (a no-op by default).
    """
    import create_all_entities

    with patched(hub, udf_handler):
        return create_all_entities.create_entities(
            token="token",
            pipeline_id="pipeline",
            source_schema=source_schema,
            target_schema=target_schema,
            tables=source_tables,
            source_agent_id=SOURCE_AGENT,
            target_agent_id=TARGET_AGENT,
            source_type=source_type,
            target_type=target_type,
            yaml_config=yaml.safe_load(yaml_text),
            skip_errors=skip_errors,
            chunk_size=chunk_size,
        )
