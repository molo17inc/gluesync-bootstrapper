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

"""A GS1 nosql-to-sql DataModeling entity drafted as a Gluesync 2 document model.

The level-1 table is the root, a NESTED table a ONE child under its ``parentField``, a further level-1 table an
extension of the root (``path = ""``), a level-2 entry a MANY array and a level-3 entry an array of the
level-2 array named by ``superLevelName``. The ``join.on`` relations become the parent links (the child
receives the parent's value, as GS1 copied it). A FLATTENED table keeps its key and link fields on its level
and drafts every other field under one MERGED level whose path is to confirm: GS1 merged every sub-object of
the item, and the configuration does not name them.

A map, a list or an array of the document is written to a text column as GS1's JSON text (``GS1_JSON``). Two
items of an array with one key write one row, the last item's, as GS1 did (``LAST_WINS``), which also lets a
level keyed by its parent's key alone stand: one row per parent.

GS1 never deleted SQL rows: every level reports its deletes (``REPORT_ONLY``) and keeps the children of an
absent array until the customer signs off the reported counts. A level whose key lacks the root key deletes
through its parent chain (``CHAIN``). A GS1 TTL or ``skipExpiration`` turns expiry into ``IGNORE``.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import model as m
from .issue_codes import warning
from .json_values import as_bool, as_int, content, kotlin_list, obj, optional_string

_DEFAULT_KEYSPACE = "_default"
_CUSTOM_FORMAT = "CUSTOM_FORMAT"
_TEMPORAL_TYPES = {"LOCAL_DATE", "LOCAL_DATE_TIME", "LOCAL_TIME", "OFFSET_DATE_TIME"}
# The path of the MERGED level a FLATTENED table's fields are drafted under: the sub-object is not known.
_PATH_TO_CONFIRM = "pathToConfirm"
# The SQL type of a drafted column: GS1 configurations carry none, describe of the target gives it.
_TYPE_TO_DESCRIBE = "VARCHAR"
_TABLE_TYPES = ("SIMPLE", "NESTED", "FLATTENED")


def _write_policy() -> Dict[str, Any]:
    # GS1 never deleted SQL rows; nothing is deleted until the customer signs off the reported counts.
    return m.write_policy(deletes="REPORT_ONLY", absent_array="KEEP_CHILDREN")


@dataclass
class CouchbaseSettings:
    """The ``couchbase`` block of a GS1 nosql-to-sql configuration, and the top-level settings it reads."""

    use_collections: bool
    ttl_seconds: int
    skip_expiration: bool
    entities_keys: Dict[str, List[str]]
    # Per entity, a field name and the name of its GS1 DataType.
    entities_data_types: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # GS1 read the dateFormat only when convertDateAs is CUSTOM_FORMAT.
    convert_date_as: Optional[str] = None
    date_format: Optional[str] = None
    # copySourceEntitiesChanges: false keeps GS1 from copying the changes, the entity is snapshot only.
    copy_changes: bool = True


class Refused(Exception):
    """The entity is not converted; the message says why."""


class _Table:
    """One GS1 table of a level. ``fields`` and ``keys`` are read as document field (the label) -> column."""

    def __init__(self, raw: Dict[str, Any], sybase: bool):
        self.table = _content_or_empty(raw, "table")
        self.alias = _content_or_empty(raw, "alias")
        declared = optional_string(raw, "tableType")
        self.type = declared if declared in _TABLE_TYPES else "SIMPLE"
        self.parent_field = optional_string(raw, "parentField")
        self.from_table = bool(as_bool(raw["fromTable"])) if "fromTable" in raw else False
        self.fields = _labels(raw.get("fields"), sybase)
        self.keys = _labels(raw.get("keys"), sybase)
        on = obj(raw.get("join")).get("on")
        self.join_on = [text for text in (content(item) for item in on) if text is not None] if isinstance(on, list) else []
        schema, _, name = self.table.partition(".") if "." in self.table else ("", "", self.table)
        self.target = m.table_with_id(schema, name)
        self.target_key = (self.target["id"], schema, name)

    def column_of(self, label: str) -> str:
        if label in self.fields:
            return self.fields[label]
        return self.keys.get(label, label)


def _content_or_empty(raw: Dict[str, Any], name: str) -> str:
    text = optional_string(raw, name)
    return "" if text is None else text


def _labels(element: Any, sybase: bool) -> Dict[str, str]:
    labels: Dict[str, str] = {}
    for key, value in obj(element).items():
        text = content(value)
        if text is None:
            continue
        if sybase:
            labels[text] = key
        else:
            labels[key] = text
    return labels


class _Entry:
    """One ``dataModeling`` entry: a level number, the array (or root) name and its tables."""

    def __init__(self, raw: Dict[str, Any], sybase: bool):
        level = as_int(raw["level"]) if "level" in raw else None
        self.level = 0 if level is None else level
        self.name = _content_or_empty(raw, "name")
        self.super_level_name = optional_string(raw, "superLevelName")
        tables = raw.get("tables")
        self.tables = [_Table(t, sybase) for t in tables if isinstance(t, dict)] if isinstance(tables, list) else []

    def main_table(self) -> Optional[_Table]:
        """The table the level's own fields go to: the ``fromTable`` one, else the first that is not NESTED."""
        for table in self.tables:
            if table.from_table:
                return table
        for table in self.tables:
            if table.type != "NESTED":
                return table
        return None

    def first_table(self) -> _Table:
        if not self.tables:
            raise ValueError(f"Level '{self.name}' has no table")
        return self.tables[0]


def _others(tables: List[_Table], main: _Table) -> List[_Table]:
    return [table for table in tables if table is not main]


class NoSqlToSqlDraft:
    """Drafts one DataModeling entity; :meth:`draft` returns the source and target entities and the warnings."""

    def __init__(self, name: str, body: Dict[str, Any], settings: CouchbaseSettings, sybase: bool):
        self.name = name
        self.body = body
        self.settings = settings
        self.sybase = sybase
        self.warnings: List[Dict[str, Any]] = []
        self.target_columns: Dict[Tuple[Any, ...], Tuple[Dict[str, Any], List[Dict[str, Any]]]] = {}
        self.matrix: List[Dict[str, Any]] = []
        self.entity_object = self._entity_object()
        self.entity_columns: List[Dict[str, Any]] = []

    def draft(self, source_agent_id: str, target_agent_id: str):
        entries = [_Entry(raw, self.sybase) for raw in self.body["dataModeling"] if isinstance(raw, dict)]
        roots = [entry for entry in entries if entry.level == 1]
        if len(roots) != 1:
            raise Refused(f"{self.name} has {len(roots)} level-1 entries: a DataModeling entity has one")
        root_entry = roots[0]
        root_table = root_entry.main_table()
        if root_table is None:
            raise Refused(f"{self.name} has no level-1 table to write the document root to")
        for entry in entries:
            if entry.level > 3:
                self.warnings.append(warning(
                    "GS1_SETTING_NOT_CARRIED", f"{self.name}: level {entry.level} '{entry.name}', which GS1 skipped"))
        entity_keys = self.settings.entities_keys.get(self.name)
        root_key = entity_keys if entity_keys is not None else list(root_table.keys)
        arrays = [self._array_level(entry, entries, root_key) for entry in entries if entry.level == 2]
        root = self._level(root_entry.name, "", "MANY", root_table, _others(root_entry.tables, root_table), [],
                           is_root=True, key=root_key)
        root["children"] = root["children"] + arrays
        root = self._chain_scoped(root, None, True)
        objects = [child["path"] for child in root["children"] if child["cardinality"] == "ONE" and child["path"]]
        for array in arrays:
            self._container(array["path"], "gs:array")
        for path in objects:
            self._container(path, "gs:object")
        for label in root_key:
            self._entity_column(label)
        # The DCP source decomposes the root key from the document id: those are the id columns.
        for col in self.entity_columns:
            if col["name"] in root_key:
                col["isPK"] = True
        self.warnings.append(warning(
            "GS1_DOCUMENT_ID_TO_CONFIRM",
            f"{self.name}: the draft lays the root key {kotlin_list(root_key)} out in the document id with no prefix "
            f"and '{m.DEFAULT_SEPARATOR}'",
        ))
        expires = self.settings.ttl_seconds > 0 or self.settings.skip_expiration
        value_types = self._value_types()
        date_format = self.settings.date_format if self._date_format_applies(value_types) else None
        source = m.nosql_entity(
            self.name,
            source_agent_id,
            m.source_type(snapshot_only=not self.settings.copy_changes),
            self.entity_object,
            m.key_mapping("", m.DEFAULT_SEPARATOR, [m.column_id(label) for label in root_key]),
            list(self.entity_columns),
            document_model=m.data_model(root, m.model_options(
                on_expiry="IGNORE" if expires else "DELETE",
                duplicate_item_keys="LAST_WINS",
                structured_value_text="GS1_JSON",
                field_value_types=value_types,
                date_format=date_format,
            )),
        )
        columns: List[Any] = []
        for table, table_columns in self.target_columns.values():
            columns.extend([table, list(table_columns)])
        target = m.multi_table_entity(
            self.name,
            target_agent_id,
            m.target_type(["INSERT", "UPDATE", "DELETE", "TRUNCATE"], mapping_matrix=list(self.matrix)),
            [table for table, _ in self.target_columns.values()],
            columns,
        )
        return source, target, list(self.warnings)

    def _value_types(self) -> Dict[str, str]:
        """The ``entitiesDataTypes`` of the entity; a name GS1 does not know made GS1 fail, so it is reported."""
        declared = self.settings.entities_data_types.get(self.name, {})
        known = {name: type_name for name, type_name in declared.items() if type_name in m.DOCUMENT_VALUE_TYPES}
        for field_name, type_name in declared.items():
            if type_name not in m.DOCUMENT_VALUE_TYPES:
                self.warnings.append(warning(
                    "GS1_SETTING_NOT_CARRIED",
                    f"{self.name}: couchbase.entitiesDataTypes.{self.name}.{field_name} is '{type_name}', which is no GS1 "
                    f"DataType ({', '.join(m.DOCUMENT_VALUE_TYPES)}): GS1 failed on it, the field is not typed",
                ))
        return known

    def _date_format_applies(self, types: Dict[str, str]) -> bool:
        """GS1 parsed a typed date with ``dateFormat`` only when ``convertDateAs`` was CUSTOM_FORMAT."""
        if self.settings.convert_date_as != _CUSTOM_FORMAT or not any(t in _TEMPORAL_TYPES for t in types.values()):
            return False
        if self.settings.date_format is None or not self.settings.date_format.strip():
            self.warnings.append(warning(
                "GS1_SETTING_NOT_CARRIED",
                f"{self.name}: convertDateAs is CUSTOM_FORMAT and no dateFormat is set: GS1 kept the text of its typed "
                "dates, so GS2 reads ISO-8601",
            ))
            return False
        return True

    def _array_level(self, entry: _Entry, entries: List[_Entry], root_key: List[str]) -> Dict[str, Any]:
        """A level-2 entry: an array of the root, with the level-3 entries naming it as their super level."""
        table = entry.main_table() or entry.first_table()
        level = self._level(entry.name, entry.name, "MANY", table, _others(entry.tables, table), root_key)
        grandchildren = []
        for child in entries:
            if child.level == 3 and child.super_level_name == entry.name:
                child_table = child.main_table() or child.first_table()
                grandchildren.append(self._level(
                    child.name, child.name, "MANY", child_table, _others(child.tables, child_table), level["key"]))
        level["children"] = level["children"] + grandchildren
        return level

    def _chain_scoped(self, level: Dict[str, Any], parent: Optional[Dict[str, Any]], parent_holds_root_key: bool):
        """A NESTED level whose key does not hold the root key deletes its stale items through the parent chain."""
        if level["embedding"] != "NESTED":
            return level
        if parent is None:
            holds_root_key = True
        else:
            linked = [
                next((link["childColumn"] for link in level["parentLink"] if link["parentColumn"] == label), None)
                for label in parent["key"]
            ]
            holds_root_key = parent_holds_root_key and all(c is not None and c in level["key"] for c in linked)
        if not holds_root_key:
            level["writePolicy"] = dict(level["writePolicy"], deleteScope="CHAIN")
        level["children"] = [self._chain_scoped(child, level, holds_root_key) for child in level["children"]]
        return level

    def _level(self, level_name: str, path: str, cardinality: str, table: _Table, others: List[_Table],
               parent_key: List[str], is_root: bool = False, key: Optional[List[str]] = None) -> Dict[str, Any]:
        """The level written to ``table``, its NESTED tables as ONE children, its other tables as extensions."""
        if key is None:
            key = list(table.keys)
        source_table_id = self.entity_object["id"] if is_root else m.level_table_id(level_name)
        parent_link = [] if is_root else self._parent_link(table, parent_key)
        labels = list(dict.fromkeys(list(table.fields) + list(table.keys)))
        level_key = key if key else [link["childColumn"] for link in parent_link]
        if table.type == "FLATTENED":
            direct, merged = self._flattened(table, labels, level_key + [link["childColumn"] for link in parent_link])
        else:
            direct, merged = labels, {}
        for label in direct:
            self._map(source_table_id, label, table, table.column_of(label), label in level_key)
        if is_root:
            for label in direct:
                self._entity_column(label)
        merged_levels = []
        for sub_object, fields in merged.items():
            for label in fields:
                self._map(source_table_id, f"{sub_object}.{label}", table, table.column_of(label), False)
            if is_root:
                self._container(sub_object, "gs:object")
            merged_name = f"{level_name}_{sub_object}"
            merged_levels.append(m.model_level(
                merged_name,
                path=sub_object,
                cardinality="ONE",
                embedding="MERGED",
                columns=[self._level_column(merged_name, label) for label in fields],
                policy=_write_policy(),
            ))
        children = []
        for other in others:
            nested = other.type == "NESTED" and other.parent_field is not None
            child_path = other.parent_field if nested else ""
            children.append(self._level(other.target["name"], child_path, "ONE", other, [], level_key))
        return m.model_level(
            level_name,
            path=path,
            cardinality=cardinality,
            columns=[] if is_root else [self._level_column(level_name, label) for label in direct],
            key=level_key,
            parent_link=parent_link,
            policy=_write_policy(),
            children=merged_levels + children,
        )

    def _parent_link(self, table: _Table, parent_key: List[str]) -> List[Dict[str, str]]:
        """The ``join.on`` relations read as GS1 read them, else the parent key labels the table maps itself."""
        if not table.join_on:
            return [m.key_link(label, label) for label in parent_key if label in table.fields]
        links = []
        for relation in table.join_on:
            sides = relation.split("=")
            if len(sides) < 2 or any("." not in side for side in sides[:2]):
                self.warnings.append(warning(
                    "GS1_SETTING_NOT_CARRIED",
                    f"{self.name}: join relation '{relation}' of {table.table}, which GS1 failed to parse "
                    "(expected 'alias.field = alias.field')",
                ))
                continue
            parent, child = (side.split(".")[1].strip() for side in sorted(sides[:2], key=lambda s: f"{table.alias}." in s))
            links.append(m.key_link(child, parent))
        return links

    def _flattened(self, table: _Table, labels: List[str], own: List[str]):
        """A FLATTENED item keeps its key and link fields; every other field goes under a MERGED level to place."""
        direct = [label for label in labels if label in own]
        merged = [label for label in labels if label not in own]
        if not merged:
            return direct, {}
        self.warnings.append(warning(
            "GS1_FLATTENED_FIELD_TO_PLACE",
            f"{self.name}: FLATTENED fields {kotlin_list(merged)} of {table.table} are drafted under the MERGED path "
            f"'{_PATH_TO_CONFIRM}': set the path of their sub-object, and move the item's own fields back to the level",
        ))
        return direct, {_PATH_TO_CONFIRM: merged}

    def _map(self, source_table_id: int, label: str, table: _Table, column_name: str, is_key: bool) -> None:
        target_table, columns = self.target_columns.setdefault(table.target_key, (table.target, []))
        target = next((c for c in columns if c["name"] == column_name), None)
        if target is None:
            target = m.column(
                m.column_id(f"{target_table['schema']}.{target_table['name']}.{column_name}"),
                target_table["id"],
                column_name,
                len(columns) + 1,
                _TYPE_TO_DESCRIBE,
                is_key,
            )
            columns.append(target)
        self.matrix.append(m.mapping_matrix_entry(source_table_id, target_table["id"], m.column_id(label), target["id"]))

    def _entity_column(self, label: str) -> None:
        if all(c["name"] != label for c in self.entity_columns):
            self.entity_columns.append(m.column(
                m.column_id(label), self.entity_object["id"], label, len(self.entity_columns) + 1, _TYPE_TO_DESCRIBE, False))

    def _container(self, path: str, kind: str) -> None:
        if all(c["name"] != path for c in self.entity_columns):
            self.entity_columns.append(m.container_column(m.column_id(path), self.entity_object["id"], path, kind))

    @staticmethod
    def _level_column(level_name: str, label: str) -> Dict[str, Any]:
        return m.column(m.column_id(label), m.level_table_id(level_name), label, 0, _TYPE_TO_DESCRIBE, False)

    def _entity_object(self) -> Dict[str, Any]:
        """The collection GS1 read: the entity's ``type`` under its ``scope``, else its type in the default scope."""
        type_name = optional_string(self.body, "type")
        type_name = self.name if type_name is None else type_name
        scope = optional_string(self.body, "scope") or ""
        if self.settings.use_collections:
            return m.entity_object(scope, type_name)
        self.warnings.append(warning(
            "GS1_SETTING_NOT_CARRIED",
            f"{self.name}: GS1 read the default collection and kept the documents whose type is '{type_name}'; the source "
            "agent needs scopes and collections off, with GS1's type field as its type key",
        ))
        return m.entity_object(_DEFAULT_KEYSPACE, type_name)
