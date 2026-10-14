"""A fake CoreHub for the bootstrapper payload tests.

Runs ``create_all_entities.create_entities`` against an in-memory discovery and captures every
``PUT /pipelines/{id}/config/entities`` body, so a test can compare payloads without a running
CoreHub. Discovery is described per agent as ``{(schema, table): [column, ...]}``; a table that is
not described discovers no column, as a missing table does.
"""

import contextlib
import copy
import io
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
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {"message": self.text}


def _refusal(path, reason, status_code=400):
    """The error commons.fetch_core_hub raises for an HTTP error: a RuntimeError caused by requests' HTTPError."""
    import requests

    cause = requests.exceptions.HTTPError(f"{status_code} Client Error", response=_Response(status_code, reason))
    try:
        raise cause
    except requests.exceptions.HTTPError as error:
        try:
            raise RuntimeError(f"CoreHub request PUT {path} failed: {error} | Response: {reason}") from error
        except RuntimeError as wrapped:
            return wrapped


class FakeCoreHub:
    """In-memory discovery plus a capture of the entity upserts.

    ``version`` is what ``GET /version`` answers (``None`` for a CoreHub that cannot tell);
    ``refuse`` decides, per PUT body, whether CoreHub answers it with an error.
    """

    def __init__(self, source_tables, target_tables, *, version="2.2.11.5 - build test",
                 source_category="RDBMS", target_category="RDBMS", refuse=None):
        self.source_tables = source_tables
        self.target_tables = target_tables
        self.version = version
        self.source_category = source_category
        self.target_category = target_category
        self.refuse = refuse
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
        if path.endswith("/config/entities") and method == "PUT":
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


def run_create_entities(hub, yaml_text, source_tables, *, source_schema="src", target_schema="tgt",
                        source_type="RDBMS", target_type="RDBMS", skip_errors=True, chunk_size=50, udf_handler=None):
    """Run ``create_entities`` on ``yaml_text`` against ``hub``; returns its result.

    ``udf_handler`` stands in for ``handle_udf_function_definition`` (a no-op by default).
    """
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
