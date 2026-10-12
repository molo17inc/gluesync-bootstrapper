"""The column mapping of an entity: matrix entries, relations and their YAML form.

CoreHub stores which source columns feed which target columns on the target entity type, in
``columnsMappingMatrix``. Gluesync 2.3 writes each entry as two lists of column ids, so one entry
holds a whole relation::

    {sourceTableObjectId, targetTableObjectId, sourceColumnIds: [2, 3], targetColumnIds: [12]}  # N:1

Every earlier release decodes only one pair per entry (``sourceColumnId``, ``targetColumnId``,
``0`` for "no column"). This module reads both shapes, builds the entries a save needs from
relations and encodes them for the CoreHub in use. It is shared by create_all_entities.py and
export_template_from_corehub.py: pure functions over dicts, no I/O.

A relation is ``{"sources": [...], "targets": [...], "explicit": bool}``: one source and one target
is 1:1, several sources N:1, several targets 1:N, no target 1:null (read, never written), no source
null:1 (produced by a field function or the UDF). ``explicit`` tells a relation the YAML states from
one the bootstrapper guessed by matching names. An entry is the tuple
``(source_table_id, target_table_id, source_ids, target_ids)``.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

Entry = Tuple[Any, Any, List[Any], List[Any]]

_SIDES = (("source", "sourceColumnId", "sourceColumnIds"), ("target", "targetColumnId", "targetColumnIds"))


class EntityMappingError(ValueError):
    """A column mapping CoreHub would refuse, found before any request; the message names the columns."""


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------

def read_entry(entry: Mapping[str, Any]) -> Entry:
    """``(source_table_id, target_table_id, source_ids, target_ids)`` of a stored matrix entry.

    CoreHub's decoding rules, side by side: the list wins, and a scalar next to it must agree
    (``[v]`` with ``v``, ``[]`` with ``0``); a scalar alone gives ``[]`` for ``0`` and ``[v]``
    otherwise; a ``null`` list or scalar counts as absent, and a side with neither is refused.
    """
    sides = []
    for side, scalar_key, list_key in _SIDES:
        scalar = entry.get(scalar_key)
        ids = entry.get(list_key)
        if ids is not None:
            if not isinstance(ids, list):
                raise EntityMappingError(f"{list_key} must be a list of column ids, not {ids!r}")
            if scalar is not None and list(ids) != ([scalar] if scalar != 0 else []):
                raise EntityMappingError(f"{scalar_key} {scalar} disagrees with {list_key} {ids}; send one shape")
            sides.append(list(ids))
        elif scalar is not None:
            sides.append([scalar] if scalar != 0 else [])
        else:
            raise EntityMappingError(f"the matrix entry {dict(entry)} has no {side} column ({scalar_key} or {list_key})")
    return entry.get("sourceTableObjectId"), entry.get("targetTableObjectId"), sides[0], sides[1]


def encode_entries(entries: Sequence[Entry], grouped: bool) -> List[Dict[str, Any]]:
    """The ``columnsMappingMatrix`` of ``entries`` for the CoreHub in use.

    ``grouped`` (Gluesync 2.3 and later) writes the two lists and no scalar, as CoreHub itself does.
    Otherwise each entry is the single pair an older CoreHub decodes, ``0`` for an empty side: the
    unlocked-schema pairing ``[]/[]`` is written ``(0, 0)`` as today, and an entry with one empty
    side (1:null, null:1) is not written, since an older runtime fails such a pair.
    """
    matrix = []
    for source_table_id, target_table_id, source_ids, target_ids in entries:
        if grouped:
            matrix.append({
                "sourceTableObjectId": source_table_id,
                "targetTableObjectId": target_table_id,
                "sourceColumnIds": list(source_ids),
                "targetColumnIds": list(target_ids),
            })
            continue
        if len(source_ids) > 1 or len(target_ids) > 1:
            raise EntityMappingError(
                f"the entry {list(source_ids)} -> {list(target_ids)} relates several columns, which a CoreHub older "
                f"than Gluesync 2.3 cannot store"
            )
        if bool(source_ids) != bool(target_ids):
            continue
        matrix.append({
            "sourceTableObjectId": source_table_id,
            "targetTableObjectId": target_table_id,
            "sourceColumnId": source_ids[0] if source_ids else 0,
            "targetColumnId": target_ids[0] if target_ids else 0,
        })
    return matrix


# ---------------------------------------------------------------------------
# Entries from relations
# ---------------------------------------------------------------------------

def _order_key(column: Mapping[str, Any]):
    """Ordinal position first (D4 of the backend plan), then id; a column without a position goes last."""
    position = column.get("position")
    has_position = isinstance(position, int) and not isinstance(position, bool) and position > 0
    column_id = column.get("id")
    return (0 if has_position else 1, position if has_position else 0, column_id if isinstance(column_id, int) else 0)


def _columns_by_id(columns: Iterable[Mapping[str, Any]]) -> Dict[Any, Mapping[str, Any]]:
    by_id: Dict[Any, Mapping[str, Any]] = {}
    for column in columns or []:
        if isinstance(column, Mapping) and column.get("id") is not None:
            by_id.setdefault(column["id"], column)
    return by_id


def _names(ids: Iterable[Any], by_id: Mapping[Any, Mapping[str, Any]]) -> str:
    return ", ".join(str(by_id[i].get("name")) if i in by_id else f"id {i}" for i in ids)


def build_entries(
    source_table_id: Any,
    target_table_id: Any,
    relations: Sequence[Mapping[str, Any]],
    source_columns: Sequence[Mapping[str, Any]],
    target_columns: Sequence[Mapping[str, Any]],
    *,
    grouped: bool,
    unlocked: bool = False,
    label: str = "",
) -> List[Entry]:
    """The matrix entries of one table pair, from relations over column ids.

    ``grouped`` (a Gluesync 2.3 CoreHub): every source and target column of the entity appears in
    exactly one entry (R7.4, as the field editor writes it): the relations with a source and a
    target first, ordered by their first source, then one ``[s]/[]`` per source with no target, then
    one ``[]/[t]`` per target with no source; ids inside an entry in ordinal order. Refused, naming
    ``label`` and the columns: an id that is no column of the entity (M5, the id ``0`` included), a
    column in two relations (M7), several sources and several targets (M:N), an id listed twice.

    Not grouped (an older CoreHub): the relations as given, one pair each, as the bootstrapper has
    always sent them; a relation an older CoreHub cannot hold is refused.

    ``unlocked``: the target table has an unlocked schema, so the entry only pairs the tables.
    """
    if unlocked:
        return [(source_table_id, target_table_id, [], [])]
    where = f" of {label}" if label else ""

    if not grouped:
        entries = []
        for relation in relations:
            sources, targets = list(relation["sources"]), list(relation["targets"])
            if len(sources) != 1 or len(targets) != 1:
                raise EntityMappingError(
                    f"the column mapping{where} relates {sources or 'no source'} to {targets or 'no target'}, which a "
                    f"CoreHub older than Gluesync 2.3 cannot store: only one source column onto one target column"
                )
            entries.append((source_table_id, target_table_id, sources, targets))
        return entries

    sources_by_id = _columns_by_id(source_columns)
    targets_by_id = _columns_by_id(target_columns)
    owner: Dict[Tuple[str, Any], int] = {}
    linked: List[Tuple[List[Any], List[Any]]] = []
    one_sided_sources, one_sided_targets = set(), set()

    for index, relation in enumerate(relations):
        sources, targets = list(relation["sources"]), list(relation["targets"])
        described = f"{_names(sources, sources_by_id) or 'no source'} -> {_names(targets, targets_by_id) or 'no target'}"
        for side, ids, by_id in (("source", sources, sources_by_id), ("target", targets, targets_by_id)):
            unknown = [i for i in ids if i not in by_id]
            if unknown:
                raise EntityMappingError(
                    f"the column mapping{where} ({described}) names the {side} column id(s) "
                    f"{', '.join(str(i) for i in unknown)}, which are no {side} column of the entity"
                )
            if len(set(ids)) != len(ids):
                raise EntityMappingError(f"the column mapping{where} ({described}) names a {side} column more than once")
            for column_id in ids:
                previous = owner.setdefault((side, column_id), index)
                if previous != index:
                    raise EntityMappingError(
                        f"{side} column {by_id[column_id].get('name')}{where} is related twice; a column belongs to one "
                        f"relation (N:1: list its sources in one row; 1:N: one row per target with the same source)"
                    )
        if len(sources) > 1 and len(targets) > 1:
            raise EntityMappingError(
                f"the column mapping{where} relates the source columns {_names(sources, sources_by_id)} to the target "
                f"columns {_names(targets, targets_by_id)} (M:N), which is not supported"
            )
        if sources and targets:
            linked.append((sorted(sources, key=lambda i: _order_key(sources_by_id[i])),
                           sorted(targets, key=lambda i: _order_key(targets_by_id[i]))))
        else:
            one_sided_sources.update(sources)
            one_sided_targets.update(targets)

    linked.sort(key=lambda pair: _order_key(sources_by_id[pair[0][0]]))
    linked_sources = {i for sources, _ in linked for i in sources}
    linked_targets = {i for _, targets in linked for i in targets}
    unlinked_sources = sorted((i for i in sources_by_id if i not in linked_sources), key=lambda i: _order_key(sources_by_id[i]))
    unlinked_targets = sorted((i for i in targets_by_id if i not in linked_targets), key=lambda i: _order_key(targets_by_id[i]))

    return (
        [(source_table_id, target_table_id, sources, targets) for sources, targets in linked]
        + [(source_table_id, target_table_id, [i], []) for i in unlinked_sources]
        + [(source_table_id, target_table_id, [], [i]) for i in unlinked_targets]
    )


# ---------------------------------------------------------------------------
# Relations from a stored matrix (export)
# ---------------------------------------------------------------------------

def relations_of(
    entries: Sequence[Entry],
    source_columns: Sequence[Mapping[str, Any]],
    target_columns: Sequence[Mapping[str, Any]],
    *,
    unlocked: bool = False,
) -> Dict[str, List[Any]]:
    """The relations a stored matrix holds between the columns of one table pair, by column name.

    Entries that share a column are one relation, so a legacy N:1 stored as two pairs reads as one
    N:1, as the field editor loads it. Returns ``relations`` (each ``{sources, targets}``, ordered by
    their first source, names in ordinal order), ``unlinked_sources`` (1:null: no entry or an entry
    with no target) and ``unlinked_targets`` (null:1), both in ordinal order. An unlocked table gives
    nothing: its entries only pair the tables. Ids that are no column of the entity are left out.
    """
    if unlocked:
        return {"relations": [], "unlinked_sources": [], "unlinked_targets": []}
    sources_by_id = _columns_by_id(source_columns)
    targets_by_id = _columns_by_id(target_columns)

    # Union-find over the columns that appear on both sides of some entry.
    parent: Dict[Tuple[str, Any], Tuple[str, Any]] = {}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for _, _, source_ids, target_ids in entries:
        nodes = [("s", i) for i in source_ids if i in sources_by_id] + [("t", i) for i in target_ids if i in targets_by_id]
        if not any(kind == "s" for kind, _ in nodes) or not any(kind == "t" for kind, _ in nodes):
            continue
        for node in nodes:
            parent.setdefault(node, node)
        root = find(nodes[0])
        for node in nodes[1:]:
            parent[find(node)] = root

    groups: Dict[Tuple[str, Any], List[Tuple[str, Any]]] = {}
    for node in parent:
        groups.setdefault(find(node), []).append(node)
    relations = []
    for members in groups.values():
        sources = sorted((i for kind, i in members if kind == "s"), key=lambda i: _order_key(sources_by_id[i]))
        targets = sorted((i for kind, i in members if kind == "t"), key=lambda i: _order_key(targets_by_id[i]))
        relations.append((sources, targets))
    relations.sort(key=lambda pair: _order_key(sources_by_id[pair[0][0]]))

    linked_sources = {i for sources, _ in relations for i in sources}
    linked_targets = {i for _, targets in relations for i in targets}
    return {
        "relations": [
            {"sources": [sources_by_id[i].get("name") for i in sources], "targets": [targets_by_id[i].get("name") for i in targets]}
            for sources, targets in relations
        ],
        "unlinked_sources": [
            sources_by_id[i].get("name")
            for i in sorted(sources_by_id, key=lambda i: _order_key(sources_by_id[i]))
            if i not in linked_sources
        ],
        "unlinked_targets": [
            targets_by_id[i].get("name")
            for i in sorted(targets_by_id, key=lambda i: _order_key(targets_by_id[i]))
            if i not in linked_targets
        ],
    }
