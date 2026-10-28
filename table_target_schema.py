"""Per-table target schemas in a TABLE_LIST YAML.

A schema entry has one ``target:``. A table whose target lives in another schema carries its own
``targetSchema:`` (written by export_template_from_corehub.py since this change). Before, the exporter filed
every table under the target schema of the *first* table it met, so such tables were silently recreated in the
wrong schema on import.

:func:`split_by_target_schema` turns one schema entry into one copy per target schema; create_all_entities.main
runs a pass per copy. A YAML without ``targetSchema:`` gives a single pass identical to before.
"""
import copy
import logging
import os
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

TABLE_TARGET_SCHEMA_KEY = "targetSchema"
_SNAPSHOT_COMMAND = re.compile(r"(pre|post)SnapshotCommand", re.IGNORECASE)
# One identifier: [bracketed, spaces and ]] allowed], "quoted", `backticked` or bare.
_IDENT = r'(?:\[(?:[^\]]|\]\])+\]|"(?:[^"]|"")+"|`[^`]+`|[A-Za-z_#@][\w$#@]*)'
# A table reference after the keyword introducing it: a.b, a.b.c (db.schema.table), a..c (db, default schema).
_TABLE_REF = re.compile(r'\b(?:FROM|INTO|UPDATE|JOIN|TABLE|USING)\s+(' + _IDENT + r'(?:\s*\.\s*(?:' + _IDENT
                        + r')?)*)', re.IGNORECASE)
_PART = re.compile(r'\s*(' + _IDENT + r')?\s*(\.|$)')
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_STRINGS = re.compile(r"'(?:[^']|'')*'")


class _Ambiguous(Exception):
    """A command whose schema cannot be read safely."""


def _unquote(ident):
    if ident.startswith('['):
        return ident[1:-1].replace(']]', ']')
    if ident.startswith('"'):
        return ident[1:-1].replace('""', '"')
    if ident.startswith('`'):
        return ident[1:-1]
    return ident


def _split_parts(ref):
    parts, pos = [], 0
    while pos < len(ref):
        m = _PART.match(ref, pos)
        if not m or m.end() == pos:
            raise _Ambiguous(ref)
        parts.append(_unquote(m.group(1)) if m.group(1) else None)
        pos = m.end()
    return parts


def _schemas_named(command, strict=False):
    """The schemas the table references of a SQL command name (comments and string literals ignored):
    ``S.T`` -> S, ``DB.S.T`` -> S (three-part names), quoted/bracketed names with spaces included. Unqualified
    names name none. ``strict``: _Ambiguous for a reference whose schema cannot be told (``DB..T``, the default
    schema; four or more parts, a linked server)."""
    text = _STRINGS.sub("''", _COMMENTS.sub(" ", str(command)))
    found = []
    for m in _TABLE_REF.finditer(text):
        try:
            parts = _split_parts(m.group(1))
        except _Ambiguous:
            if strict:
                raise
            continue
        if len(parts) == 2 and parts[0]:
            found.append(parts[0])
        elif len(parts) == 3 and parts[1]:
            found.append(parts[1])
        elif len(parts) > 1 and strict:
            raise _Ambiguous(m.group(1))
    return found


def _schemas_root(yaml_config):
    return yaml_config.get('schemas', yaml_config) if isinstance(yaml_config, dict) else {}


def _schema_entry(yaml_config, source_schema):
    root = _schemas_root(yaml_config)
    entry = root.get(source_schema) if isinstance(root, dict) else None
    return entry if isinstance(entry, dict) else None


def table_target_schemas(yaml_config, source_schema, default_target) -> Dict[str, str]:
    """``{yaml table key: target schema}`` for every table of ``source_schema`` (custom and whitelist)."""
    entry = _schema_entry(yaml_config, source_schema)
    if entry is None:
        return {}
    schema_target = entry.get('target') or default_target
    tables = entry.get('tables') or {}
    custom = tables.get('custom') or {}
    result = {}
    for key in list(tables.get('whitelist') or []) + list(custom):
        cfg = custom.get(key) if isinstance(custom, dict) else None
        override = cfg.get(TABLE_TARGET_SCHEMA_KEY) if isinstance(cfg, dict) else None
        result[str(key)] = str(override) if override else schema_target
    return result


def _target_pass_config(yaml_config, source_schema, target, keys, first):
    """A copy of ``yaml_config`` holding only the tables ``keys`` of ``source_schema``, targeting ``target``;
    only the ``first`` pass keeps the schedules. None when no table is left."""
    copied = copy.deepcopy(yaml_config)
    new_entry = _schema_entry(copied, source_schema)
    new_entry['target'] = target
    tables = new_entry.setdefault('tables', {})
    if tables.get('whitelist') is not None:
        tables['whitelist'] = [k for k in tables['whitelist'] if str(k) in keys]
    custom = tables.get('custom') or {}
    tables['custom'] = {k: v for k, v in custom.items() if str(k) in keys}
    if not first:
        new_entry.pop('schedules', None)
        new_entry.pop('group_schedules', None)
    if not tables['custom'] and not tables.get('whitelist'):
        return None
    return copied


def split_by_target_schema(yaml_config, source_schema, default_target) -> List[Tuple[str, Any]]:
    """``[(target_schema, yaml_config)]``: one pass per target schema; ``[(default, yaml_config)]`` without overrides.

    The first pass keeps the schema's own target and its schema/group schedules; later passes carry only their
    tables (their schedules are left to the first pass, so nothing is created twice).
    """
    entry = _schema_entry(yaml_config, source_schema)
    if entry is None:
        return [(default_target, yaml_config)]
    schema_target = entry.get('target') or default_target
    by_table = table_target_schemas(yaml_config, source_schema, default_target)
    # Schema names compare casefolded (as Core Hub's SQL Server/most targets do): "tgt01" next to the schema's
    # "TGT01" is the same pass, under the schema's spelling (else the first spelling met).
    spelling = {str(schema_target).casefold(): schema_target}
    for t in by_table.values():
        spelling.setdefault(str(t).casefold(), t)
    folded_target = str(schema_target).casefold()
    others = sorted({spelling[str(t).casefold()] for t in by_table.values()
                     if str(t).casefold() != folded_target}, key=str)
    if not others:
        return [(schema_target, yaml_config)]
    passes = []
    for index, target in enumerate([schema_target] + others):
        keys = {k for k, t in by_table.items() if str(t).casefold() == str(target).casefold()}
        copied = _target_pass_config(yaml_config, source_schema, target, keys, first=index == 0)
        if copied is not None:
            passes.append((target, copied))
    logger.info(f"Source schema {source_schema}: tables target {len(passes)} schemas "
                f"({', '.join(t for t, _ in passes)}): one entity pass per target schema")
    return passes


def _snapshot_commands(node, path=""):
    if isinstance(node, dict):
        yield from _dict_snapshot_commands(node, path)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _snapshot_commands(value, f"{path}[{i}]")


def _dict_snapshot_commands(node, path):
    for key, value in node.items():
        here = f"{path}.{key}" if path else str(key)
        if _SNAPSHOT_COMMAND.search(str(key)) and isinstance(value, str):
            yield here, value
        else:
            yield from _snapshot_commands(value, here)


def snapshot_command_warnings(yaml_config, source_schema, default_target) -> List[str]:
    """A warning per pre/postSnapshotCommand of a table naming a schema other than the table's target schema."""
    entry = _schema_entry(yaml_config, source_schema)
    if entry is None:
        return []
    by_table = table_target_schemas(yaml_config, source_schema, default_target)
    custom = (entry.get('tables') or {}).get('custom') or {}
    warnings = []
    for key in sorted(custom, key=str):
        target = by_table.get(str(key))
        if not target:
            continue
        for where, command in _snapshot_commands(custom[key]):
            named = sorted({name for name in _schemas_named(command)
                            if name.casefold() != str(target).casefold()})
            if named:
                warnings.append(f"{source_schema}.{key}: {where} names schema {', '.join(named)} but the table's "
                                f"target schema is {target} (check the command or set {TABLE_TARGET_SCHEMA_KEY}:)")
    return warnings


INFER_ENV = "GS_IMPORT_INFER_TARGET_SCHEMA"


def infer_enabled() -> bool:
    """Whether :func:`apply_inferred_target_schemas` runs: on unless GS_IMPORT_INFER_TARGET_SCHEMA is false/0/no/off
    (``--no-infer-target-schema``)."""
    return str(os.getenv(INFER_ENV, "true")).strip().lower() not in ("false", "0", "no", "off")


def inferred_target_schema(cfg, schema_target) -> Any:
    """The schema every pre/postSnapshotCommand of a table names, when they all name that one schema and it is
    not ``schema_target``; None otherwise (no command, a command naming no or several schemas, or disagreement)."""
    if not isinstance(cfg, dict) or cfg.get(TABLE_TARGET_SCHEMA_KEY):
        return None
    named = {}
    commands = list(_snapshot_commands(cfg))
    if not commands:
        return None
    for _where, command in commands:
        try:
            found = {name.casefold(): name for name in _schemas_named(command, strict=True)}
        except _Ambiguous:
            return None
        if len(found) != 1:
            return None
        named.update(found)
    if len(named) != 1:
        return None
    (folded, spelled), = named.items()
    return spelled if folded != str(schema_target).casefold() else None


def _inferred_changes(schema_name, entry, default_target):
    """``(schema, table key, inferred schema, schema target)`` of each custom table of a schema with one."""
    if not isinstance(entry, dict):
        return []
    schema_target = entry.get('target') or default_target or schema_name
    custom = (entry.get('tables') or {}).get('custom') or {}
    if not isinstance(custom, dict):
        return []
    changes = []
    for key, cfg in custom.items():
        inferred = inferred_target_schema(cfg, schema_target)
        if inferred:
            changes.append((schema_name, key, inferred, schema_target))
    return changes


def apply_inferred_target_schemas(yaml_config, default_target=None, enabled=None):
    """``yaml_config`` with a ``targetSchema:`` on each table whose snapshot commands all name one other schema.

    A table without ``targetSchema:`` whose pre/postSnapshotCommand(s) all qualify their tables with one schema
    other than the table's target (``DELETE FROM [TGT02].[TAB050]`` under target TGT01) was exported by
    an exporter that filed it under the wrong schema: its target is that schema. An explicit ``targetSchema:``
    always wins. Returns ``(yaml_config, notes)``; the input is not modified (a copy is returned when a table
    changes). Off with GS_IMPORT_INFER_TARGET_SCHEMA=false."""
    enabled = infer_enabled() if enabled is None else enabled
    root = _schemas_root(yaml_config)
    if not enabled or not isinstance(root, dict):
        return yaml_config, []
    changes = [change for schema_name, entry in root.items()
               for change in _inferred_changes(schema_name, entry, default_target)]
    if not changes:
        return yaml_config, []
    copied = copy.deepcopy(yaml_config)
    copied_root = _schemas_root(copied)
    notes = []
    for schema_name, key, inferred, schema_target in changes:
        copied_root[schema_name]['tables']['custom'][key][TABLE_TARGET_SCHEMA_KEY] = inferred
        note = (f"{schema_name}.{key}: targetSchema {inferred} inferred from its snapshot command(s) (the schema "
                f"target is {schema_target}; set {TABLE_TARGET_SCHEMA_KEY}: to override, {INFER_ENV}=false to "
                f"disable)")
        logger.info(note)
        notes.append(note)
    return copied, notes


def merge_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The results of several passes as one: counts summed, errors and lists concatenated, flags or-ed."""
    if len(results) == 1:
        return results[0]
    merged: Dict[str, Any] = {"successful": 0, "failed": 0, "total": 0, "errors": []}
    for result in results:
        for key, value in (result or {}).items():
            if key in ("successful", "failed", "total"):
                merged[key] += value or 0
            elif isinstance(value, list):
                merged.setdefault(key, []).extend(value)
            elif isinstance(value, bool):
                merged[key] = merged.get(key, False) or value
            else:
                merged.setdefault(key, value)
    return merged
