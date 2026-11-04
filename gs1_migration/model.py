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

"""The Gluesync 2 entity JSON the GS1 converter drafts, as ``PUT /config/entities`` takes it.

Every builder returns the object with its members in the order Core Hub writes them and with Core Hub's
defaults, so a draft reads the same as the one Core Hub's own converter answered. Ids of tables and columns
are the first 8 bytes of the SHA-256 of their name, as Core Hub computes them.
"""

import hashlib
from typing import Any, Dict, List, Optional

DEFAULT_SEPARATOR = "::"
CUSTOM_KEYS_SEPARATOR = ["::", "-", "_", "/", ""]
MODEL_SCHEMA = "#gs_model"

DOCUMENT_VALUE_TYPES = [
    "STRING", "INT", "FLOAT", "DOUBLE", "LONG", "BIG_DECIMAL", "BOOLEAN",
    "LOCAL_DATE", "LOCAL_DATE_TIME", "LOCAL_TIME", "OFFSET_DATE_TIME", "BINARY",
]


def id64(text: str) -> int:
    """Core Hub's 64-bit id of a name: the first 8 bytes of its SHA-256, signed big-endian."""
    digest = hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def table_or_object_id(schema_or_scope: str, name_or_collection: str) -> int:
    return id64(f"{schema_or_scope}.{name_or_collection}")


def column_id(label: str) -> int:
    return id64(label)


def level_table_id(level_name: str) -> int:
    return table_or_object_id(MODEL_SCHEMA, level_name)


def column(col_id: int, table_id: int, name: str, position: int, data_type: str, is_pk: bool,
           nullable: bool = False) -> Dict[str, Any]:
    return {
        "id": col_id,
        "tableId": table_id,
        "name": name,
        "position": position,
        "dataType": data_type,
        "isPK": is_pk,
        "isIdentity": False,
        "isNullable": nullable,
    }


def container_column(col_id: int, table_id: int, name: str, kind: str) -> Dict[str, Any]:
    """The column of the entity object holding a child level: ``gs:array`` or ``gs:object``."""
    return column(col_id, table_id, name, 0, kind, False, nullable=True)


def write_policy(deletes: str = "ON", absent_array: str = "DELETE_CHILDREN",
                 delete_scope: str = "ROOT_KEY") -> Dict[str, Any]:
    return {"deletes": deletes, "absentArray": absent_array, "jsonColumns": [], "deleteScope": delete_scope}


def model_level(name: str, *, path: str = "", cardinality: str = "MANY", embedding: str = "NESTED",
                columns: Optional[List[Dict[str, Any]]] = None, key: Optional[List[str]] = None,
                parent_link: Optional[List[Dict[str, str]]] = None, order_by: Optional[List[Dict[str, Any]]] = None,
                sql: Optional[str] = None, policy: Optional[Dict[str, Any]] = None,
                children: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    level = {
        "name": name,
        "path": path,
        "cardinality": cardinality,
        "embedding": embedding,
        "ownership": "OWNED",
        "columns": list(columns or []),
        "key": list(key or []),
        "parentLink": list(parent_link or []),
        "orderBy": list(order_by or []),
    }
    if sql is not None:
        level["source"] = {"type": "Sql", "query": sql}
    level["writePolicy"] = dict(policy) if policy is not None else write_policy()
    level["children"] = list(children or [])
    return level


def key_link(child_column: str, parent_column: str) -> Dict[str, str]:
    return {"childColumn": child_column, "parentColumn": parent_column}


def order_term(col: str) -> Dict[str, Any]:
    return {"column": col, "descending": False}


def model_options(*, empty_array: str = "EMPTY", max_deletes_per_window: int = 10000, on_expiry: str = "DELETE",
                  duplicate_item_keys: str = "FAIL_DOCUMENT", structured_value_text: str = "AS_DELIVERED",
                  field_value_types: Optional[Dict[str, str]] = None,
                  date_format: Optional[str] = None) -> Dict[str, Any]:
    options = {
        "emptyArray": empty_array,
        "maxItemsPerLevel": 10000,
        "maxDeletesPerWindow": max_deletes_per_window,
        "readConsistency": "SNAPSHOT_IF_AVAILABLE",
        "queryTimeoutSeconds": 300,
        "onExpiry": on_expiry,
        "duplicateItemKeys": duplicate_item_keys,
        "structuredValueText": structured_value_text,
        "fieldValueTypes": dict(field_value_types or {}),
    }
    if date_format is not None:
        options["dateFormat"] = date_format
    return options


def data_model(root: Dict[str, Any], options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"root": root, "options": options if options is not None else model_options()}


def source_type(snapshot_only: bool = False) -> Dict[str, Any]:
    return {
        "type": "Source",
        "maxFetchItemsCountPerIteration": 10000,
        "maxTransactionMessageKbSize": 4096,
        "pollingIntervalMilliseconds": 500,
        "unchangedDataFilterType": "DISABLED",
        "snapshotOnly": snapshot_only,
        "resumableSnapshot": True,
    }


def target_type(allowed_operations: List[str], *, mapping_matrix: Optional[List[Dict[str, Any]]] = None,
                field_functions: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {
        "type": "Target",
        "allowedOperations": list(allowed_operations),
        "snapshotWritingConcurrency": 2,
        "columnsMappingMatrix": list(mapping_matrix or []),
        "tablesWithUnlockedSchema": [],
        "tablesWithUnlockedDataTypes": [],
        "useBulkOperationsDuringCDC": False,
        "useBulkOperationsWhileSnapshot": False,
        "fieldFunctions": list(field_functions or []),
        "distinctCaseInsensitiveKeyData": False,
        "insertConflictStrategy": "UPSERT",
        "changedColumnsPolicy": "DISABLED",
        "nullValuePolicy": "WRITE_NULL",
        "absentFieldPolicy": "WRITE_NULL",
        "columnWritePolicies": [],
    }


def static_string(col_id: int, table_or_object: int, value: str) -> Dict[str, Any]:
    """A field function writing a constant text to a column."""
    return {
        "columnId": col_id,
        "tableOrObjectId": table_or_object,
        "expression": {"type": "StaticStr", "staticValue": value, "isTechnicalField": True, "requireUserInput": True},
    }


def mapping_matrix_entry(source_table: int, target_table: int, source_column: int, target_column: int) -> Dict[str, Any]:
    return {
        "sourceTableObjectId": source_table,
        "targetTableObjectId": target_table,
        "sourceColumnIds": [source_column],
        "targetColumnIds": [target_column],
    }


def entity_object(scope: str, collection: str) -> Dict[str, Any]:
    return {"id": table_or_object_id(scope, collection), "scope": scope, "collection": collection}


def table_with_id(schema: str, name: str) -> Dict[str, Any]:
    return {"id": table_or_object_id(schema, name), "schema": schema, "name": name, "type": "TABLE"}


def key_mapping(prefix: str, separator: str, keys: List[int], value_rendering: str = "TRIMMED") -> Dict[str, Any]:
    return {"prefix": prefix, "suffix": "", "separator": separator, "keys": list(keys), "valueRendering": value_rendering}


def _agent_entity(kind: str, entity_name: str, entity_type: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    return {
        "type": kind,
        "entityId": "",
        "entityName": entity_name,
        "agentEntityId": "",
        "entityType": entity_type,
        "agentId": agent_id,
        "orderIndex": 0,
        "customProperties": {},
        "tablesProperties": {},
    }


def query_entity(entity_name: str, agent_id: str, model: Dict[str, Any], snapshot_only: bool = False) -> Dict[str, Any]:
    query = _agent_entity("Query", entity_name, source_type(snapshot_only), agent_id)
    query["model"] = model
    query["capturedTables"] = []
    return query


def nosql_entity(entity_name: str, agent_id: str, entity_type: Dict[str, Any], entity_obj: Dict[str, Any],
                 keys: Dict[str, Any], columns: List[Dict[str, Any]],
                 document_model: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    nosql = _agent_entity("NoSqlEntity", entity_name, entity_type, agent_id)
    nosql["entityObject"] = entity_obj
    nosql["keyMapping"] = keys
    nosql["columns"] = columns
    if document_model is not None:
        nosql["documentModel"] = document_model
    return nosql


def multi_table_entity(entity_name: str, agent_id: str, entity_type: Dict[str, Any], tables: List[Dict[str, Any]],
                       columns: List[Any]) -> Dict[str, Any]:
    """``columns`` alternates a table and its columns, as Core Hub writes a map keyed by table."""
    multi_table = _agent_entity("MultiTable", entity_name, entity_type, agent_id)
    multi_table["tables"] = tables
    multi_table["columns"] = columns
    return multi_table


def entity(entity_name: str, agent_entities: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"entityId": "", "entityName": entity_name, "agentEntities": agent_entities, "groupId": "_default", "orderIndex": 0}


def issue(code: str, severity: str, message: str, hint: str, level: Optional[str] = None,
          location: Optional[str] = None) -> Dict[str, Any]:
    """An issue as describe, validate and the converter report it; a null level or location is left out."""
    out: Dict[str, Any] = {"code": code, "severity": severity}
    if level is not None:
        out["level"] = level
    if location is not None:
        out["location"] = location
    out["message"] = message
    out["hint"] = hint
    return out
