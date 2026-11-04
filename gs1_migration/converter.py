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

"""Converts a GS1 (Gluesync 1.x) configuration into Gluesync 2 entity drafts.

sql-to-nosql Query and QueryDataModeling entities become Query entities, nosql-to-sql DataModeling entities
document models. Pure: it never persists, never calls Core Hub, and never logs or echoes the configuration.
Secret-looking keys are removed before anything is read.

A Query becomes a root-only model, a QueryDataModeling (QDM) a root with one MANY array per level-2 entry,
linked to the root by the ``primaryTableKeys`` labels every GS1 level projects. Document ids keep GS1's: QDM
ids from the ``primaryTableKeys`` labels sorted by label (``entitiesKeyMapping`` ignored, as GS1 did), a Query
with an ``entitiesKeyMapping`` entry from its fields in their order under ``GS1_DROP_NULLS``, any other Query
from the key describe proposes (GS1 used the FROM table's primary key). Converted entities omit empty arrays
as GS1 did, write GS1's ``type`` and ``scope`` as static fields, and do not delete until the customer signs off
the reported counts.

An entity is not converted (bucket UNSUPPORTED) when a structure check refuses it, or GS1 did something
Gluesync 2 does not: an unsupported id separator, a TTL touch, or an entity kind the converter does not take.

The structure checks (a statement that depends on the moment it runs, a key that is not a plain column, a
document model that does not hold together) are Core Hub's: they parse the level SQL. :func:`convert` takes
them as ``structure_check``; without it no structure check runs and every draft the converter itself accepts
is returned. :func:`gs1_import.corehub_structure_check` asks Core Hub.
"""

import re
from typing import Any, Callable, Dict, List, Optional

from . import issue_codes
from . import model as m
from .json_values import as_bool, content, int_member, kotlin_list, obj, optional_string, string, strings, utf16_sort_key
from .nosql_to_sql import CouchbaseSettings, NoSqlToSqlDraft, Refused
from .secret_stripper import strip
from .setting_notes import setting_notes

REPOSITORIES = ("SQL_TO_NOSQL", "NOSQL_TO_SQL", "NOSQL_TO_SQL_SYBASE")
BUCKETS = ("FLAT", "MULTI_LEVEL", "DOCUMENT_SPLIT", "DROPPED", "UNSUPPORTED")

_TYPE_FIELD = "type"
_SCOPE_FIELD = "scope"
_DEFAULT_KEYSPACE = "_default"
# The Couchbase string type: GS1 converts only to Couchbase.
_STRING_TYPE = "string"
# 1 % of the entity's roots, never fewer than 100 deletes per window.
_MIN_DELETES_PER_WINDOW = 100
# How Core Hub words a key that is a plain column of another table than the level's root key: these three parts, in
# this order, each after at least one character, on one line.
_JOINED_TABLE_KEY = (" is a column of ", ", not of ", ", the table of the level's root key")
_LINE_BREAK = re.compile(r"\r\n|[\n\r\u0085\u2028\u2029]")

StructureCheck = Callable[[Dict[str, Any]], List[Dict[str, Any]]]
"""Checks one draft and returns Core Hub's structure issues (dicts as Core Hub writes them).

The argument says what to check: ``{"entity": name, "kind": "sql", "model": ..., "keysProposed": bool}`` for a
Query draft, or ``{"entity": name, "kind": "document", "draft": entity}`` for a document split.
"""


class ConversionError(RuntimeError):
    """The configuration could not be converted; the message never carries configuration values."""


def conversion_request(config: Dict[str, Any], pipeline_id: str, source_agent_id: str, target_agent_id: str,
                       root_counts: Optional[Dict[str, int]] = None, repository: str = "SQL_TO_NOSQL") -> Dict[str, Any]:
    """The request Core Hub's converter took: the configuration, the pipeline and its two agents."""
    if repository not in REPOSITORIES:
        raise ValueError(f"repository must be one of {', '.join(REPOSITORIES)}")
    return {
        "config": config,
        "pipelineId": pipeline_id,
        "sourceAgentId": source_agent_id,
        "targetAgentId": target_agent_id,
        "rootCounts": dict(root_counts or {}),
        "repository": repository,
    }


def convert(request: Dict[str, Any], structure_check: Optional[StructureCheck] = None) -> Dict[str, Any]:
    """The drafts, one per converted entity, what became of every GS1 entity, and the keys stripped."""
    try:
        return _convert(request, structure_check)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConversionError(f"The GS1 configuration could not be converted ({type(exc).__name__})") from None


def _convert(request: Dict[str, Any], structure_check: Optional[StructureCheck]) -> Dict[str, Any]:
    stripped, paths = strip(obj(request.get("config")))
    config = _Gs1Config(stripped)
    conversions = [_convert_entity(request, config, name, body, structure_check) for name, body in config.entities]
    return {
        "entities": [c["entity"] for c in conversions if c["entity"] is not None],
        "perEntity": [{"gs1Name": c["name"], "bucket": c["bucket"], "issues": c["issues"]} for c in conversions],
        "strippedKeys": paths,
    }


def _conversion(name: str, bucket: str, issues: List[Dict[str, Any]], entity: Optional[Dict[str, Any]]):
    return {"name": name, "bucket": bucket, "issues": issues, "entity": entity}


def _unsupported(name: str, issue: Dict[str, Any]):
    return _conversion(name, "UNSUPPORTED", [issue], None)


def _has_error(issues: List[Dict[str, Any]]) -> bool:
    return any(issue.get("severity") == "ERROR" for issue in issues)


def _convert_entity(request, config, name, body, structure_check):
    if name in config.entities_to_apply_ttl:
        return _unsupported(name, issue_codes.converter_error(
            "GS1_TTL_TOUCH_NOT_SUPPORTED", f"{name} is listed in couchbase.entitiesToApplyTtl"))
    repository = request.get("repository", "SQL_TO_NOSQL")
    if repository != "SQL_TO_NOSQL":
        return _convert_nosql_to_sql(request, config, name, body, structure_check)
    key_mapping = config.key_mappings.get(name)
    if "query" in body and _is_primitive_member(body["query"]):
        draft = _query_draft(name, body, key_mapping)
    elif isinstance(body.get("queryDataModeling"), dict):
        draft = _qdm_draft(name, body["queryDataModeling"], key_mapping)
    else:
        return _unsupported(name, issue_codes.converter_error("GS1_ENTITY_KIND_NOT_SUPPORTED", _kind_message(name, body)))
    if draft.refusal is not None:
        return _unsupported(name, draft.refusal)
    model = draft.model
    keys_proposed = not model["root"]["key"]
    structure = _structure(structure_check, {"entity": name, "kind": "sql", "model": model, "keysProposed": keys_proposed})
    issues = [_with_migration_hint(issue, model["root"]["name"]) for issue in structure + draft.warnings]
    if _has_error(structure):
        return _conversion(name, "UNSUPPORTED", issues, None)
    options = dict(model["options"], emptyArray="OMIT", maxDeletesPerWindow=_max_deletes(request.get("rootCounts", {}).get(name)))
    entity = m.entity(name, [
        m.query_entity(name, request["sourceAgentId"], {"root": model["root"], "options": options}),
        _target_draft(request, config, name, body, draft.key_mapping),
    ])
    bucket = "FLAT" if not model["root"]["children"] else "MULTI_LEVEL"
    return _conversion(name, bucket, issues, entity)


def _convert_nosql_to_sql(request, config, name, body, structure_check):
    """A nosql-to-sql DataModeling entity drafted as a document split; a table or mapping entity is not converted."""
    if not isinstance(body.get("dataModeling"), list):
        kind = "mapping" if "mapping" in body else "table"
        return _unsupported(name, issue_codes.converter_error(
            "GS1_ENTITY_KIND_NOT_SUPPORTED", f"{name} is a {kind} entity: only DataModeling entities convert"))
    sybase = request.get("repository") == "NOSQL_TO_SQL_SYBASE"
    try:
        source, target, warnings = NoSqlToSqlDraft(name, body, config.nosql_to_sql, sybase).draft(
            request["sourceAgentId"], request["targetAgentId"])
    except Refused as refused:
        return _unsupported(name, issue_codes.converter_error("GS1_ENTITY_KIND_NOT_SUPPORTED", str(refused)))
    draft = m.entity(name, [source, target])
    structure = _structure(structure_check, {"entity": name, "kind": "document", "draft": draft})
    issues = structure + warnings + config.setting_notes()
    if _has_error(structure):
        return _conversion(name, "UNSUPPORTED", issues, None)
    return _conversion(name, "DOCUMENT_SPLIT", issues, draft)


def _structure(structure_check: Optional[StructureCheck], subject: Dict[str, Any]) -> List[Dict[str, Any]]:
    return list(structure_check(subject)) if structure_check is not None else []


def _is_primitive_member(value: Any) -> bool:
    return content(value) is not None


# ---------- source models ----------

class _Draft:
    def __init__(self, model=None, key_mapping=None, warnings=None, refusal=None):
        self.model = model
        self.key_mapping = key_mapping if key_mapping is not None else m.key_mapping("", "", [])
        self.warnings = warnings or []
        self.refusal = refusal


def _query_draft(name: str, body: Dict[str, Any], key_mapping: Optional[Dict[str, Any]]) -> _Draft:
    query = content(body["query"])
    if key_mapping is not None and key_mapping["separator"] not in m.CUSTOM_KEYS_SEPARATOR:
        return _Draft(refusal=issue_codes.converter_error(
            "GS1_SEPARATOR_NOT_SUPPORTED",
            f"The entitiesKeyMapping separator '{key_mapping['separator']}' of {name} is not one GS2 renders",
        ))
    root = m.model_level(name, key=key_mapping["fields"] if key_mapping is not None else [], sql=query)
    if key_mapping is None:
        return _Draft(
            model=m.data_model(root),
            key_mapping=_gs1_key_mapping(name, []),
            warnings=[issue_codes.warning(
                "GS1_ROOT_KEY_TO_CONFIRM",
                f"GS1 keyed the documents of {name} on the primary key of its FROM table: describe proposes it",
            )],
        )
    ids = m.key_mapping(key_mapping["prefix"], key_mapping["separator"],
                        [m.column_id(f) for f in key_mapping["fields"]], "GS1_DROP_NULLS")
    return _Draft(model=m.data_model(root), key_mapping=ids)


def _qdm_draft(name: str, qdm: Dict[str, Any], key_mapping: Optional[Dict[str, Any]]) -> _Draft:
    root_key = strings(qdm, "primaryTableKeys")
    raw_levels = qdm.get("levels")
    levels = [level for level in raw_levels if isinstance(level, dict)] if isinstance(raw_levels, list) else []
    numbers = [int_member(level, "level") for level in levels]
    roots = [level for level, number in zip(levels, numbers) if number == 1]
    if len(roots) != 1 or any(number is None or not 1 <= number <= 2 for number in numbers):
        return _Draft(refusal=issue_codes.converter_error(
            "GS1_ENTITY_KIND_NOT_SUPPORTED",
            f"{name} has {len(roots)} level-1 entries and levels {kotlin_list(numbers)}: a QDM entity has one level 1 "
            "and level-2 arrays",
        ))
    children = []
    for level, number in zip(levels, numbers):
        if number != 2:
            continue
        item_key = root_key + [k for k in strings(level, "levelKeys") if k not in root_key]
        children.append(m.model_level(
            string(level, "name"),
            path=string(level, "name"),
            key=item_key,
            parent_link=[m.key_link(k, k) for k in root_key],
            order_by=[m.order_term(k) for k in item_key[len(root_key):]],
            sql=string(level, "query"),
        ))
    root_level = roots[0]
    root = m.model_level(string(root_level, "name"), key=root_key, sql=string(root_level, "query"), children=children)
    not_carried = []
    if "primaryTable" in qdm:
        not_carried.append("primaryTable, which GS1 ignored")
    if key_mapping is not None:
        not_carried.append("the entitiesKeyMapping entry, which GS1 ignored for a QDM entity")
    warnings = [issue_codes.warning("GS1_SETTING_NOT_CARRIED", f"{name}: {text}, is not carried over") for text in not_carried]
    return _Draft(model=m.data_model(root), key_mapping=_gs1_key_mapping(name, sorted(root_key, key=utf16_sort_key)),
                  warnings=warnings)


def _gs1_key_mapping(name: str, keys: List[str]) -> Dict[str, Any]:
    """GS1's id of a QDM entity, and of a Query without entitiesKeyMapping: ``name::`` and the keys sorted by label."""
    return m.key_mapping(name, m.DEFAULT_SEPARATOR, [m.column_id(k) for k in keys], "GS1")


# ---------- target ----------

def _target_draft(request, config, name, body, key_mapping):
    """The collection GS1 wrote, its static ``type`` and ``scope`` fields, and no DELETE until signed off."""
    type_name = optional_string(body, "type")
    type_name = name if type_name is None else type_name
    scope = optional_string(body, "scope") or ""
    # Without collections the agent writes the default collection and stamps its type key with the object's collection.
    target_object = m.entity_object(scope, type_name) if config.use_collections else m.entity_object(_DEFAULT_KEYSPACE, type_name)
    statics = [(_TYPE_FIELD, type_name), (_SCOPE_FIELD, scope)]
    columns = [
        m.column(m.column_id(field), target_object["id"], field, index + 1, _STRING_TYPE, False)
        for index, (field, _) in enumerate(statics)
    ]
    functions = [m.static_string(col["id"], target_object["id"], value) for col, (_, value) in zip(columns, statics)]
    return m.nosql_entity(
        name,
        request["targetAgentId"],
        m.target_type(["INSERT", "UPDATE"], field_functions=functions),
        target_object,
        key_mapping,
        columns,
    )


# ---------- helpers ----------

def _kind_message(name: str, body: Dict[str, Any]) -> str:
    data_modeling = body.get("dataModeling")
    if isinstance(data_modeling, list):
        return f"{name} is a DataModeling entity ({len(data_modeling)} levels): converted from R5"
    if isinstance(data_modeling, dict):
        levels = data_modeling.get("levels")
        return f"{name} is a DataModeling entity ({len(levels) if isinstance(levels, list) else 0} levels): converted from R5"
    kind = "mapping" if "mapping" in body else "table"
    return f"{name} is a {kind} entity: only Query and QueryDataModeling convert"


def _max_deletes(root_count: Any) -> int:
    count = int(root_count) if root_count is not None else 0
    per_window = abs(count) // 100 * (1 if count >= 0 else -1)
    per_window = (per_window + 2 ** 31) % 2 ** 32 - 2 ** 31
    return max(_MIN_DELETES_PER_WINDOW, per_window)


def _names_a_joined_table(message: str) -> bool:
    """Whether a line of the message holds the parts of ``_JOINED_TABLE_KEY`` in order, as ``A.+B.+C`` matches."""
    for line in _LINE_BREAK.split(message):
        start, found = 0, True
        for index, part in enumerate(_JOINED_TABLE_KEY):
            position = line.find(part, start)
            if position < 0:
                found = False
                break
            start = position + len(part) + (1 if index < len(_JOINED_TABLE_KEY) - 1 else 0)
        if found:
            return True
    return False


def _with_migration_hint(issue: Dict[str, Any], root_level: str) -> Dict[str, Any]:
    """A child-level key of a table joined to the root key's table: the array is migrated as an entity of its own."""
    if (issue.get("code") == "KEY_NOT_PLAIN_COLUMN" and issue.get("level") != root_level
            and _names_a_joined_table(issue.get("message", ""))):
        level = issue.get("level")
        location = issue.get("location")
        return dict(issue, hint=(
            f"Move the array {'null' if level is None else level} into an entity of its own, keyed on the table that holds "
            f"{'null' if location is None else location}, or expose the root key on that table at the source: GS2 keys an "
            "array on the table its root key comes from (D6)."
        ))
    return issue


class _Gs1Config:
    """The parts of a stripped GS1 configuration the converter reads."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.entities = [(name, obj(body)) for name, body in obj(config.get("sourceEntities")).items()]
        self.key_mappings = {
            name: {"prefix": string(body, "prefix"), "separator": string(body, "separator"), "fields": strings(body, "fields")}
            for name, body in obj(config.get("entitiesKeyMapping")).items()
            if isinstance(body, dict)
        }
        couchbase = config.get("couchbase") if isinstance(config.get("couchbase"), dict) else None
        couchbase_or_empty = couchbase or {}
        self.use_collections = bool(as_bool(couchbase_or_empty["useCollections"])) if "useCollections" in couchbase_or_empty else False
        self.entities_to_apply_ttl = set(strings(couchbase_or_empty, "entitiesToApplyTtl"))
        self._setting_notes: Optional[List[Dict[str, Any]]] = None
        entities_keys = obj(couchbase_or_empty.get("entitiesKeys"))
        data_types = obj(couchbase_or_empty.get("entitiesDataTypes"))
        ttl = int_member(couchbase_or_empty, "ttlSeconds")
        self.nosql_to_sql = CouchbaseSettings(
            use_collections=self.use_collections,
            ttl_seconds=0 if ttl is None else ttl,
            skip_expiration=bool(as_bool(couchbase_or_empty["skipExpiration"])) if "skipExpiration" in couchbase_or_empty else False,
            entities_keys={name: strings(entities_keys, name) for name in entities_keys},
            entities_data_types={
                entity: {f: content(t) for f, t in obj(fields).items() if content(t) is not None}
                for entity, fields in data_types.items()
            },
            convert_date_as=optional_string(config, "convertDateAs"),
            date_format=optional_string(config, "dateFormat"),
            copy_changes=_copy_changes(config),
        )

    def setting_notes(self) -> List[Dict[str, Any]]:
        """The settings no converted entity carries, and where each goes on Gluesync 2 (computed once)."""
        if self._setting_notes is None:
            self._setting_notes = setting_notes(self.config)
        return [dict(note) for note in self._setting_notes]


def _copy_changes(config: Dict[str, Any]) -> bool:
    value = as_bool(config["copySourceEntitiesChanges"]) if "copySourceEntitiesChanges" in config else None
    return True if value is None else value
