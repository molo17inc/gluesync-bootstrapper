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

"""What becomes of every setting of a GS1 nosql-to-sql configuration that the converted entities do not carry.

One note (``GS1_SETTING_ELSEWHERE``) per group of settings the configuration sets, naming where the setting
goes on Gluesync 2, an agent or an entity setting, or why Gluesync 2 does not need it. A setting the model
carries (``entitiesKeys``, ``entitiesDataTypes``, ``convertDateAs`` and ``dateFormat``, ``useCollections``,
the TTL settings) gets no note, and neither does a connection secret, which the stripper removed unread. A
setting no rule knows is named as one Gluesync 2 has no counterpart for.
"""

from typing import Any, Dict, List, Tuple

from . import issue_codes
from .json_values import content, json_text, take_utf16

_MAX_VALUE_TEXT = 60
_BLOCKS = {"couchbase", "mssql", "sourceEntities"}

# The settings the converted model carries, or the converter reports where it handles them.
_CARRIED = {
    "": {"sourceEntities", "entitiesKeyMapping", "convertDateAs", "dateFormat"},
    "couchbase.": {"useCollections", "ttlSeconds", "skipExpiration", "entitiesToApplyTtl", "entitiesKeys", "entitiesDataTypes"},
}

_TOP_LEVEL: List[Tuple[List[str], str]] = [
    (["sourceHost", "sourcePort", "sourceName", "sourceUsername"],
     "the connection of the source is set on the source agent, not on the entity"),
    (["targetHost", "targetPort", "targetName", "targetUsername"],
     "the connection of the target is set on the target agent, not on the entity"),
    (["sourceChangeRetention"], "the Couchbase agent's 'Source change retention in hours' (sourceChangeRetention)"),
    (["copySourceEntitiesAtStartup"],
     "GS2 takes the snapshot when the pipeline starts: start the pipeline without a snapshot to skip it"),
    (["copySourceEntitiesChanges"],
     "false makes the converted entity snapshot only (its source's snapshotOnly), true is what GS2 does by default"),
    (["maxMigrationItemsCountPerIteration"],
     "GS2 pages the snapshot by size, not by documents: the source entity's maxSnapshotPageKbSize"),
    (["maxItemsCountPerTransaction", "maxTransactionCountPerIteration"],
     "GS2 sizes a CDC batch on the source entity: maxFetchItemsCountPerIteration and maxTransactionMessageKbSize"),
    (["pollingIntervalMilliseconds"],
     "the source entity's pollingIntervalMilliseconds, which the DCP source does not use: it is pushed the changes"),
    ([
        "numberOfThreadPerEntityForMigration",
        "usePaginatedMigration",
        "numberOfThreadPerEntity",
        "enableEntitiesBuffer",
        "entitiesBufferReleaseMilliseconds",
        "nodesNumber",
        "currentNodeNumber",
        "maxConnectionsCount",
        "statusEndpointPort",
        "syncBufferCapacity",
    ], "GS1 internals: Core Hub schedules and buffers the work, GS2 does not need them"),
    (["metricsLogDelay", "machineStatsUpdateDelay"],
     "Core Hub publishes the metrics and the machine statistics, GS2 does not need them"),
    (["ignoreDocumentWithNullKeys"],
     "GS1 skipped only the rows whose key was NULL, in the log, and wrote the rest of the document; GS2 fails a document "
     "holding a NULL key (NULL_ITEM_KEY) and the target entity's insertConflictStrategy decides: SKIP skips the document"),
]

_COUCHBASE: List[Tuple[List[str], str]] = [
    (["timeoutSeconds"], "the Couchbase agent's 'Connection Timeout' (connectTimeout), in seconds"),
    (["queryTimeoutSeconds"], "the Couchbase agent's 'Query Timeout' (queryTimeout), in seconds"),
    (["kvTimeoutSeconds"], "the Couchbase agent's 'Key-Value Timeout' (kvTimeout), in seconds"),
    (["kvDurableTimeoutSeconds"], "the Couchbase agent's 'Key-Value Durable Timeout' (kvDurableTimeout), in seconds"),
    (["kvScanTimeoutSeconds"],
     "GS2 reads the snapshot and the changes through DCP and N1QL: there is no key-value scan to time"),
    (["customTypeKey"],
     "the Couchbase agent's \"Custom 'type' field name\" (customTypeFieldName), which tells the entities of one bucket apart"),
    (["indexReplicaCount"], "the Couchbase agent's 'Index Replica Count' (indexReplicaCount)"),
    (["indexesBuildTimeoutMilliseconds"],
     "the Couchbase agent's 'Indexes Build Timeout Seconds' (indexesBuildTimeoutSeconds), in seconds"),
    (["directReadDocumentMode"],
     "GS2 reads the changes through DCP, which delivers the whole document: there is nothing to turn on"),
    (["eventing", "pollingMilliseconds", "disableAutomaticMigrationIndexesCreation"],
     "GS1 captured the changes with an Eventing function, GS2 reads them through DCP: no Eventing function, no polling and no "
     "helper index to create"),
]

_MSSQL: List[Tuple[List[str], str]] = [
    (["loginTimeout"], "GS2 has no login timeout setting on the SQL Server agent: it connects with the JDBC driver's default"),
]


def setting_notes(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """One WARNING issue per setting group the configuration sets and no converted model carries."""
    notes = _notes(config, _TOP_LEVEL, "") + _block_notes(config, "couchbase", _COUCHBASE) + _block_notes(config, "mssql", _MSSQL)
    return [issue_codes.warning("GS1_SETTING_ELSEWHERE", message, location=location) for location, message in notes]


def _block_notes(config: Dict[str, Any], block: str, rules: List[Tuple[List[str], str]]) -> List[Tuple[str, str]]:
    settings = config.get(block)
    return _notes(settings, rules, f"{block}.") if isinstance(settings, dict) else []


def _notes(settings: Dict[str, Any], rules: List[Tuple[List[str], str]], prefix: str) -> List[Tuple[str, str]]:
    known = {key for keys, _ in rules for key in keys} | _CARRIED.get(prefix, set())
    named = []
    for keys, where in rules:
        present = [key for key in keys if key in settings]
        if present:
            values = ", ".join(f"{prefix}{key} = {_text(settings[key])}" for key in present)
            named.append((f"{prefix}{present[0]}", f"GS1 {values}: {where}"))
    unknown = [
        (f"{prefix}{key}",
         f"GS1 {prefix}{key} = {_text(value)}: GS2 has no entity or agent setting for it, it is not carried over")
        for key, value in settings.items()
        if key not in known and (prefix or key not in _BLOCKS)
    ]
    return named + unknown


def _text(value: Any) -> str:
    text = content(value)
    return text if text is not None else take_utf16(json_text(value), _MAX_VALUE_TEXT)
