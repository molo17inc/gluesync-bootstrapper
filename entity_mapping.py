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


class ColumnNames:
    """The column names of one table, to tell which of them a name in the YAML spells.

    Names that differ by case only are different columns (Cosmos DB's ``id`` next to a document field ``ID``): a
    name is the column spelt exactly so. Only a name no column is spelt as falls back to the one column that
    spells it case-insensitively (a SQL source or target may fold case), and is no column at all when several do.
    """

    def __init__(self, names: Iterable[Any]):
        self._exact = {str(name) for name in names}
        self._folded: Dict[str, List[str]] = {}
        for name in sorted(self._exact):
            self._folded.setdefault(name.lower(), []).append(name)

    def resolve(self, name: Any) -> Optional[str]:
        """The spelling of the column ``name`` stands for, None for no column or an ambiguous one."""
        name = str(name)
        if name in self._exact:
            return name
        folded = self._folded.get(name.lower(), [])
        return folded[0] if len(folded) == 1 else None

    def key(self, name: Any) -> str:
        """What identifies the column ``name`` stands for: its spelling, else the name folded, so a name no
        column matches still compares case-insensitively with the others."""
        return self.resolve(name) or str(name).lower()


def find_by_name(items: Iterable[Any], name: Any, get=lambda item: item.get("name")) -> Optional[Any]:
    """The item named ``name``: the first spelt exactly so, else the only one spelt so case-insensitively.

    None when no item is, or when several spellings differ from ``name`` by case only (:class:`ColumnNames`).
    """
    wanted = str(name)
    lowered = wanted.lower()
    folded: List[Any] = []
    for item in items:
        spelt = str(get(item))
        if spelt == wanted:
            return item
        if spelt.lower() == lowered:
            folded.append(item)
    return folded[0] if len({str(get(item)) for item in folded}) == 1 else None


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------

def _read_side(entry: Mapping[str, Any], side: str, scalar_key: str, list_key: str) -> List[Any]:
    """One side of a stored matrix entry, by CoreHub's decoding rules (see :func:`read_entry`)."""
    scalar = entry.get(scalar_key)
    ids = entry.get(list_key)
    if ids is None:
        if scalar is None:
            raise EntityMappingError(f"the matrix entry {dict(entry)} has no {side} column ({scalar_key} or {list_key})")
        return [scalar] if scalar != 0 else []
    if not isinstance(ids, list):
        raise EntityMappingError(f"{list_key} must be a list of column ids, not {ids!r}")
    if scalar is not None and list(ids) != ([scalar] if scalar != 0 else []):
        raise EntityMappingError(f"{scalar_key} {scalar} disagrees with {list_key} {ids}; send one shape")
    return list(ids)


def read_entry(entry: Mapping[str, Any]) -> Entry:
    """``(source_table_id, target_table_id, source_ids, target_ids)`` of a stored matrix entry.

    CoreHub's decoding rules, side by side: the list wins, and a scalar next to it must agree
    (``[v]`` with ``v``, ``[]`` with ``0``); a scalar alone gives ``[]`` for ``0`` and ``[v]``
    otherwise; a ``null`` list or scalar counts as absent, and a side with neither is refused.
    """
    source_ids, target_ids = (_read_side(entry, *side) for side in _SIDES)
    return entry.get("sourceTableObjectId"), entry.get("targetTableObjectId"), source_ids, target_ids


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


def _ordered(ids: Iterable[Any], by_id: Mapping[Any, Mapping[str, Any]]) -> List[Any]:
    return sorted(ids, key=lambda i: _order_key(by_id[i]))


def _pair_entries(source_table_id, target_table_id, relations, where) -> List[Entry]:
    """The relations as single pairs, for a CoreHub older than Gluesync 2.3 (see :func:`build_entries`)."""
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


def _check_target_ids(target_columns, where) -> None:
    """Refuse two target columns of different names that share an id: a relation names columns by id, so it could not
    tell them apart (a source column fanned out onto several target columns must not give them its id)."""
    named: Dict[Any, Any] = {}
    for column in target_columns or []:
        if not isinstance(column, Mapping) or column.get("id") is None:
            continue
        first = named.setdefault(column["id"], column.get("name"))
        if str(first) != str(column.get("name")):
            raise EntityMappingError(
                f"target columns {first} and {column.get('name')}{where} share the id {column['id']}; every target "
                f"column needs its own id"
            )


def _check_side(side, ids, by_id, index, owner, described, where) -> None:
    """M5 and M7 on one side of relation ``index``: every id a column of the entity, listed once, in one relation."""
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        raise EntityMappingError(
            f"the column mapping{where} ({described}) names the {side} column id(s) "
            f"{', '.join(str(i) for i in unknown)}, which are no {side} column of the entity"
        )
    if len(set(ids)) != len(ids):
        raise EntityMappingError(f"the column mapping{where} ({described}) names a {side} column more than once")
    for column_id in ids:
        if owner.setdefault((side, column_id), index) != index:
            raise EntityMappingError(
                f"{side} column {by_id[column_id].get('name')}{where} is related twice; a column belongs to one "
                f"relation (N:1: list its sources in one row; 1:N: one row per target with the same source)"
            )


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
        return _pair_entries(source_table_id, target_table_id, relations, where)

    sources_by_id = _columns_by_id(source_columns)
    targets_by_id = _columns_by_id(target_columns)
    _check_target_ids(target_columns, where)
    owner: Dict[Tuple[str, Any], int] = {}
    linked: List[Tuple[List[Any], List[Any]]] = []
    for index, relation in enumerate(relations):
        sources, targets = list(relation["sources"]), list(relation["targets"])
        described = f"{_names(sources, sources_by_id) or 'no source'} -> {_names(targets, targets_by_id) or 'no target'}"
        _check_side("source", sources, sources_by_id, index, owner, described, where)
        _check_side("target", targets, targets_by_id, index, owner, described, where)
        if len(sources) > 1 and len(targets) > 1:
            raise EntityMappingError(
                f"the column mapping{where} relates the source columns {_names(sources, sources_by_id)} to the target "
                f"columns {_names(targets, targets_by_id)} (M:N), which is not supported"
            )
        if sources and targets:
            linked.append((_ordered(sources, sources_by_id), _ordered(targets, targets_by_id)))

    linked.sort(key=lambda pair: _order_key(sources_by_id[pair[0][0]]))
    linked_sources = {i for sources, _ in linked for i in sources}
    linked_targets = {i for _, targets in linked for i in targets}
    return (
        [(source_table_id, target_table_id, sources, targets) for sources, targets in linked]
        + [(source_table_id, target_table_id, [i], [])
           for i in _ordered((i for i in sources_by_id if i not in linked_sources), sources_by_id)]
        + [(source_table_id, target_table_id, [], [i])
           for i in _ordered((i for i in targets_by_id if i not in linked_targets), targets_by_id)]
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


# ---------------------------------------------------------------------------
# YAML ``columns:``
# ---------------------------------------------------------------------------

def _is_whitelist(rows: Sequence[Any]) -> bool:
    """Whitelist rows ``{name, sourceName, type}``, decided on the first row as the bootstrapper always has."""
    first = rows[0]
    return isinstance(first, Mapping) and "name" in first and "type" in first


def _is_mapping(rows: Sequence[Any]) -> bool:
    """Mapping rows (explicit ``{source, target}`` or shorthand ``{SRC: tgt}``), decided on the first row."""
    first = rows[0]
    return isinstance(first, Mapping) and ("source" in first or "name" not in first)


def is_column_mappings(columns_list):
    """
    Return True if the columns list contains column mappings (not column definitions).

    Detects both:
    - Export format:  [{source: "col", target: "COL", type: ..., ...}]
    - User format:    [{col_name: "target_name"}, ...]  e.g. [{id: "ID"}]

    Column *definitions* (not mappings) have a 'name' key.
    """
    if not columns_list:
        return False
    first = columns_list[0]
    if not isinstance(first, dict):
        return False
    # Export format: has explicit 'source' key
    if "source" in first:
        return True
    # User format: no 'name' key (column definitions always have 'name')
    return "name" not in first


def _names_of(value: Any) -> List[str]:
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _relation(sources, targets, row) -> Dict[str, Any]:
    return {"sources": list(sources), "targets": list(targets), "explicit": True, "row": row}


def _whitelist_relations(rows: Sequence[Any]) -> List[Dict[str, Any]]:
    return [
        _relation([str(row.get("sourceName") or row.get("name"))], [str(row["name"])], row)
        for row in rows
        if isinstance(row, Mapping) and row.get("name") is not None
    ]


def _explicit_targets(sources: List[str], target: Any, where: str) -> List[str]:
    """The targets of an explicit row: a list (``[]`` is 1:null), the row's target, or the source name."""
    if isinstance(target, list):
        if len(target) > 1:
            raise EntityMappingError(
                f"the column row{where} for {', '.join(sources)} lists the targets {', '.join(map(str, target))}: "
                f"write one row per target with the same source"
            )
        return [str(t) for t in target]
    if target is not None:
        return [str(target)]
    if len(sources) > 1:
        raise EntityMappingError(
            f"the column row{where} with the sources {', '.join(sources)} names no target: several sources "
            f"feed one target column (N:1), which the row has to name"
        )
    return sources


def _is_produced_row(row: Mapping[str, Any]) -> bool:
    """A column fed by no source, ``{target, expression | type, ...}``, as the export writes it."""
    return "target" in row and "name" not in row and ("expression" in row or "type" in row)


def _mapping_row_relations(row: Mapping[str, Any], where: str) -> List[Dict[str, Any]]:
    """The relations of one mapping row: explicit, a column fed by no source, or shorthand pairs."""
    if "source" in row:
        source = row.get("source")
        if source is None or source == []:
            return []
        sources = _names_of(source)
        return [_relation(sources, _explicit_targets(sources, row.get("target"), where), row)]
    if _is_produced_row(row):
        return [_relation([], [str(row["target"])], row)] if row.get("target") is not None else []
    return [
        _relation([str(key)], [value if isinstance(value, str) else str(value)], row)
        for key, value in row.items()
        if value is not None and not isinstance(value, (dict, list, bool))
    ]


def yaml_relations(rows: Optional[Sequence[Any]], label: str = "") -> Optional[List[Dict[str, Any]]]:
    """The relations a YAML ``columns:`` list states, one per row and pair, by column name.

    ``None`` when the list states no mapping (absent, empty, or neither of the two forms below), so
    every discovered column is related by name. The form is decided on the first row, as before:

    - whitelist ``{name, sourceName, type}``: ``sourceName`` (``name`` when absent) onto ``name``;
    - mapping rows, each one of:
      - explicit ``{source, target, ...}``: an absent or ``null`` target is the source name; a list
        ``source`` is N:1 and needs a target; ``target: []`` is 1:null; a ``null`` source states
        nothing;
      - a column fed by no source (null:1) ``{target, expression | type, ...}``, as the export writes
        a column produced by a field function or the UDF;
      - shorthand ``{SRC: tgt, ...}``: one 1:1 relation per scalar item (GSSD-1355: any key is a
        column name here, ``id`` included).

    Relations are returned as stated, a source repeated over rows included; :func:`group_relations`
    turns them into the relations of a Gluesync 2.3 entity.
    """
    if not rows or not isinstance(rows, list):
        return None
    if _is_whitelist(rows):
        return _whitelist_relations(rows)
    if not _is_mapping(rows):
        return None
    where = f" of {label}" if label else ""
    return [relation for row in rows if isinstance(row, Mapping) for relation in _mapping_row_relations(row, where)]


def _claim_targets(target_owner, relation, targets, sources_text, where, source_key, target_key) -> None:
    """Make ``relation`` the one relation of each of ``targets``, refusing a target another row names."""
    for target in targets:
        owner = target_owner.get(target_key(target))
        if owner is None:
            target_owner[target_key(target)] = relation
            continue
        if owner is relation:
            raise EntityMappingError(f"the column row {sources_text} -> {target}{where} is listed twice")
        known = {source_key(o) for o in owner["sources"]}
        all_sources = owner["sources"] + [s for s in relation["sources"] if source_key(s) not in known]
        raise EntityMappingError(
            f"target column {target}{where} is named by several rows ({', '.join(owner['sources']) or 'no source'}; "
            f"{sources_text or 'no source'}); a column belongs to one relation: for several sources onto one target "
            f"write one row with source: [{', '.join(all_sources)}] and a custom field function, or the entity's UDF"
        )


def _check_one_to_n(existing, source, targets, sources_text, where, target_key) -> None:
    """A row on the single source of an earlier relation: it may only add targets to a 1:1 or 1:N relation."""
    if len(existing["sources"]) > 1:
        raise EntityMappingError(
            f"source column {source}{where} is in the N:1 row of {', '.join(existing['targets'])} and in "
            f"another row: merged they would relate several sources to several targets (M:N), which is not supported"
        )
    if not existing["targets"] or not targets:
        raise EntityMappingError(
            f"source column {source}{where} is unlinked (target: []) in one row and linked in another; keep one of the two"
        )
    if any(target_key(t) in {target_key(e) for e in existing["targets"]} for t in targets):
        raise EntityMappingError(f"the column row {sources_text} -> {', '.join(targets)}{where} is listed twice")


def group_relations(relations: Sequence[Mapping[str, Any]], label: str = "",
                    source_names: Optional[ColumnNames] = None,
                    target_names: Optional[ColumnNames] = None) -> List[Dict[str, Any]]:
    """The relations of a Gluesync 2.3 entity from the YAML relations.

    Rows that share a single source are one 1:N relation. Refused, naming ``label`` and the columns: a
    row listed twice, a target named by several rows (the N:1 form lists its sources in one row), a
    source of an N:1 row named by another row (M:N), a source both unlinked (``target: []``) and
    linked. ``source_names`` and ``target_names`` are the columns of the entity: names that differ by case only
    are different columns when both exist, and only a name no column is spelt as is compared case-insensitively.
    Without them every name is.
    """
    where = f" of {label}" if label else ""
    source_key = (source_names or ColumnNames(())).key
    target_key = (target_names or ColumnNames(())).key
    grouped: List[Dict[str, Any]] = []
    by_source: Dict[str, Dict[str, Any]] = {}
    target_owner: Dict[str, Dict[str, Any]] = {}
    for relation in relations:
        sources, targets = list(relation["sources"]), list(relation["targets"])
        sources_text = ", ".join(sources)
        if len(sources) == 1 and source_key(sources[0]) in by_source:
            existing = by_source[source_key(sources[0])]
            _check_one_to_n(existing, sources[0], targets, sources_text, where, target_key)
            _claim_targets(target_owner, existing, targets, sources_text, where, source_key, target_key)
            existing["targets"].extend(targets)
            continue
        named = next((source for source in sources if source_key(source) in by_source), None)
        if named is not None:
            raise EntityMappingError(
                f"source column {named}{where} is named by the N:1 row of {', '.join(targets) or 'no target'} and by "
                f"another row: merged they would relate several sources to several targets (M:N), which is not supported"
            )
        merged = {"sources": sources, "targets": [], "explicit": relation.get("explicit", True), "row": relation.get("row")}
        _claim_targets(target_owner, merged, targets, sources_text, where, source_key, target_key)
        merged["targets"].extend(targets)
        by_source.update((source_key(source), merged) for source in sources)
        grouped.append(merged)
    return grouped


def grouped_only_forms(relations: Optional[Sequence[Mapping[str, Any]]], table_config: Optional[Mapping[str, Any]],
                       source_names: Optional[ColumnNames] = None) -> List[str]:
    """The YAML forms of a table that only a Gluesync 2.3 CoreHub can store, one description each.

    ``source_names`` are the source columns of the table (see :func:`group_relations`).
    """
    source_key = (source_names or ColumnNames(())).key
    forms: List[str] = []
    seen_sources: Dict[str, int] = {}
    for relation in relations or []:
        sources, targets = relation["sources"], relation["targets"]
        if len(sources) > 1:
            forms.append(f"source: [{', '.join(sources)}] (several sources onto {', '.join(targets)})")
        if sources and not targets:
            forms.append(f"target: [] on source {', '.join(sources)} (read, never written)")
        if len(sources) == 1:
            key = source_key(sources[0])
            seen_sources[key] = seen_sources.get(key, 0) + 1
            if seen_sources[key] == 2:
                forms.append(f"source {sources[0]} repeated over several rows (one source onto several targets)")
    if table_config and table_config.get("targetKeys"):
        forms.append("targetKeys")
    return forms


# ---------------------------------------------------------------------------
# Keys (R12, the key rules B1-B7 of the backend plan)
# ---------------------------------------------------------------------------

def _canonical(names: Iterable[str], candidates: Iterable[str]) -> Dict[str, str]:
    """``{name.lower(): spelling}`` of ``names`` as ``candidates`` spell them (the first match wins)."""
    by_lower: Dict[str, str] = {}
    for candidate in candidates:
        by_lower.setdefault(str(candidate).lower(), str(candidate))
    return {str(name).lower(): by_lower.get(str(name).lower(), str(name)) for name in names}


def _joined(names: Iterable[str]) -> str:
    return ", ".join(names)


def drop_forbidden_key_links(
    relations: Sequence[Mapping[str, Any]],
    *,
    physical_source: Iterable[str],
    physical_target: Iterable[str],
    label: str = "",
) -> Tuple[List[Dict[str, Any]], List[str], List[str]]:
    """The relations without the links R12.8 forbids, the target columns they fed, and a warning each.

    While the target table has its own primary key, a column of the source primary key feeds only
    target key columns (B3). A link the bootstrapper guessed by matching names is dropped, so the
    source key is read and never written (1:null), as the MCP draft does (backend OP-26 A, BOP-4 A);
    one a YAML row states is refused. Returns ``(relations, dropped_targets, warnings)``: a target
    no relation feeds any more leaves the entity, or it would be a checked column nothing writes.
    """
    physical_source_lower = {str(n).lower() for n in physical_source}
    physical_target_lower = {str(n).lower() for n in physical_target}
    where = f" of {label}" if label else ""
    if not physical_target_lower:
        return [dict(r, sources=list(r["sources"]), targets=list(r["targets"])) for r in relations], [], []

    kept: List[Dict[str, Any]] = []
    dropped: List[str] = []
    warnings: List[str] = []
    target_key_text = _joined(sorted(physical_target, key=str.lower))
    for relation in relations:
        sources, targets = list(relation["sources"]), list(relation["targets"])
        keyed_sources = [s for s in sources if s.lower() in physical_source_lower]
        forbidden = [t for t in targets if t.lower() not in physical_target_lower] if keyed_sources else []
        if forbidden and relation.get("explicit", True):
            raise EntityMappingError(
                f"R12.8: the YAML links the source key {_joined(keyed_sources)}{where} to the target column(s) "
                f"{_joined(forbidden)}, which are not keys, while the target's primary key is {target_key_text}. A source "
                f"key feeds only a target key: link it to {target_key_text}, or unlink it with target: []"
            )
        if forbidden:
            for target in forbidden:
                warnings.append(
                    f"R12.8: source key {_joined(keyed_sources)}{where} is not linked to target column {target}, which is "
                    f"not a key while the target's primary key is {target_key_text}: it is read but never written"
                )
            dropped.extend(forbidden)
            targets = [t for t in targets if t not in forbidden]
        kept.append(dict(relation, sources=sources, targets=targets))
    still_fed = {t.lower() for r in kept for t in r["targets"]}
    return kept, [t for t in dropped if t.lower() not in still_fed], warnings


def _k2b_keys(relations, ph_s, ph_t) -> set:
    """On a source without a primary key, the single source of each relation onto the target primary key (K2b)."""
    if ph_s or not ph_t:
        return set()
    return {
        relation["sources"][0].lower()
        for relation in relations
        if len(relation["sources"]) == 1 and any(t.lower() in ph_t for t in relation["targets"])
    }


def _check_k2b_keys(requested, k2b, spelling, where) -> None:
    """B6 and B7 (K2b, K9): YAML keys on a source keyed by the target primary key are exactly the K2b keys."""
    missing, manual = k2b - requested, requested - k2b
    if not requested or not (missing or manual):
        return
    problems = []
    if missing:
        problems.append(
            f"K2b: {_joined(spelling[k] for k in sorted(missing))} must be a key, being the only source of "
            f"a target primary key column while the source table has no primary key"
        )
    if manual:
        problems.append(
            f"K9: {_joined(spelling[k] for k in sorted(manual))} cannot be a key next to the keys the "
            f"target primary key gives the source ({_joined(spelling[k] for k in sorted(k2b))}): the source table "
            f"has no primary key, so its keys are the source columns feeding the target primary key and no other"
        )
    # K9: the rule itself is unchanged on purpose (Core Hub 2.3.1 accepts no extra manual keys here).
    raise EntityMappingError(
        "; ".join(problems) + f"{where}. Leave keys: out to key the entity on {_joined(spelling[k] for k in sorted(k2b))}"
    )


def _source_keys(requested, ph_s, k2b, physical_source, spelling, where, source_is_nosql, strict=False,
                 warnings=None) -> set:
    """Step 1 of :func:`derive_keys`: the source keys, lower-cased.

    YAML keys that contain the whole source primary key and more are kept as they are, with a warning (Core Hub
    decides), unless ``strict``: they still identify each row. Keys that miss a primary key column are refused."""
    if ph_s:
        if requested and requested > ph_s and not strict:
            if warnings is not None:
                warnings.append(
                    f"R12.1: the keys{where} ({_joined(spelling[k] for k in sorted(requested))}) contain the source "
                    f"table's primary key ({_joined(sorted(physical_source))}) and more: kept as the YAML gives them, "
                    f"Core Hub decides (--strict-validate refuses them)")
            return requested
        if requested and requested != ph_s:
            raise EntityMappingError(
                f"R12.1: the keys{where} ({_joined(spelling[k] for k in sorted(requested))}) are not the source "
                f"table's primary key ({_joined(sorted(physical_source))}); leave keys: out, or name exactly that key"
            )
        return ph_s
    if k2b:
        _check_k2b_keys(requested, k2b, spelling, where)
        return k2b
    if not requested:
        hint = (
            " A NoSQL source declares no primary key: name the key column(s) in keys:, usually _id (the document id)."
            if source_is_nosql else " Name the key column(s) in keys:."
        )
        raise EntityMappingError(f"the source table{where} has no key: it has no primary key and keys: names none.{hint}")
    return requested


def _primary_target_keys(ph_t, requested, target_names, fed, produced, physical_target, spelling, where) -> set:
    """B2 and BOP-13: a target with a primary key is keyed on it, each of its columns fed or produced."""
    if requested and requested != ph_t:
        raise EntityMappingError(
            f"R12.2: targetKeys{where} ({_joined(spelling[k] for k in sorted(requested))}) is not the "
            f"target table's physical primary key as discovery reports it ({_joined(sorted(physical_target))}): "
            f"with a primary key on the target, targetKeys must name exactly its columns; leave targetKeys out, or "
            f"set it to exactly {_joined(sorted(physical_target))}"
        )
    in_entity = {str(n).lower() for n in target_names}
    for key in sorted(ph_t):
        if key not in in_entity or (key not in fed and key not in produced):
            raise EntityMappingError(
                f"target primary key {spelling[key]}{where} is fed by no source column: a new entity needs a "
                f"value for it (an identity or a default is not used), so add a row {{target: {spelling[key]}, "
                f"expression: ...}} with a field function that produces it, link a source column to it, or give the "
                f"entity a UDF"
            )
    return ph_t


def _check_single_non_key_sources(relations, source_keys, requested, where) -> None:
    """B5 reverse: with no primary key on either table, a target key fed by one source makes that source a key."""
    for relation in relations:
        if len(relation["sources"]) != 1 or relation["sources"][0].lower() in source_keys:
            continue
        keyed = [t for t in relation["targets"] if t.lower() in requested]
        if keyed:
            raise EntityMappingError(
                f"R12.6: targetKeys{where} names {_joined(keyed)}, fed by {relation['sources'][0]} alone, which "
                f"is not a key: with no primary key on either table the single source of a target key is a "
                f"key; add {relation['sources'][0]} to keys:"
            )


def _check_k9(requested, inherited, spelling, where, target_key_known, strict, warnings) -> None:
    """K9 on targetKeys naming keys next to the inherited ones: refused when enforced (see
    :func:`_inherited_target_keys`), else a warning."""
    extra = _joined(spelling[k] for k in sorted(requested - inherited))
    kept = _joined(spelling[k] for k in sorted(inherited))
    if target_key_known:
        raise EntityMappingError(
            f"K9: targetKeys{where} names {extra} next to the keys the target inherits from the source primary "
            f"key ({kept}), and discovery reports no primary key on the target table; leave targetKeys out (or "
            f"set it to exactly those inherited keys) in this table's YAML entry, or give the target table a "
            f"primary key that targetKeys then names exactly")
    if strict:
        raise EntityMappingError(
            f"K9: targetKeys{where} names {extra} next to the keys the target inherits from the source primary "
            f"key ({kept}); the target's primary key could not be read and --strict-validate is set")
    if warnings is not None:
        warnings.append(
            f"K9 not checked{where}: targetKeys names {extra} next to the keys inherited from the source primary "
            f"key ({kept}); the target's primary key could not be read (no discovery, agent unreachable or table "
            f"missing), so the keys are sent as the YAML names them and Core Hub decides (valid when the "
            f"target's primary key is exactly the targetKeys)")


def _inherited_target_keys(relations, source_keys, ph_s, requested, spelling, where,
                           target_key_known=True, strict=False, warnings=None) -> set:
    """B4, B5 and B7 on a target without a primary key: the targets of the source keys, plus ``targetKeys:``.

    K9 (no target key next to the keys inherited from a source primary key) is a Core Hub rule for a target table
    with NO physical primary key (Core Hub 2.3.1.1, GluesyncMainRoutesOrchestrator). It is enforced here only when
    ``target_key_known`` (discovery read the target table and it has no primary key) or ``strict``; otherwise the
    target may have a primary key the bootstrapper cannot see (no discovery, agent unreachable, table missing):
    the keys are kept as the YAML names them, with a warning, and Core Hub decides."""
    inherited = {
        t.lower() for relation in relations
        if any(s.lower() in source_keys for s in relation["sources"])
        for t in relation["targets"]
    }
    if ph_s and requested - inherited:
        _check_k9(requested, inherited, spelling, where, target_key_known, strict, warnings)
    if not ph_s:
        _check_single_non_key_sources(relations, source_keys, requested, where)
    return inherited | requested


def _refuse_unknown(names, spelling, message) -> None:
    """Refuse the ``names`` no column is spelt as: ``message`` is formatted with the unknown names."""
    unknown = [n for n in names if str(n).lower() not in spelling]
    if unknown:
        raise EntityMappingError(message.format(_joined(map(str, unknown))))


def _spelt(order, keys, spelling) -> List[str]:
    """``keys`` (lower-cased) spelt as ``order`` spells them, in that order."""
    return [n for n in dict.fromkeys(order) if n.lower() in keys and spelling[n.lower()] == n]


def derive_keys(
    *,
    relations: Sequence[Mapping[str, Any]],
    source_names: Sequence[str],
    target_names: Sequence[str],
    physical_source: Iterable[str],
    physical_target: Iterable[str],
    yaml_keys: Sequence[str],
    yaml_target_keys: Sequence[str],
    produced_targets: Iterable[str],
    label: str = "",
    source_is_nosql: bool = False,
    target_key_known: bool = True,
    strict: bool = False,
    warnings: Optional[List[str]] = None,
) -> Tuple[List[str], List[str]]:
    """The source and target key columns of a Gluesync 2.3 entity with a SQL target, by name.

    ``relations`` are by column name, after :func:`drop_forbidden_key_links`; ``source_names`` the
    source columns a key may name (discovery and the entity), ``target_names`` the target columns of
    the entity; ``physical_*`` the primary key discovery reports (empty for a table without one, a
    table that does not exist yet, or a side whose key flags are no primary key); ``produced_targets``
    the target columns a field function or the entity's UDF writes. As the MCP draft derives them:

    1. Source keys: the source primary key (B1); else, on a target with a primary key, the single
       source of each relation onto it (K2b, B6); else the YAML ``keys:``. YAML keys that differ from
       the first two are refused with the rule (BOP-3 A); a source with no key at all is refused, with
       ``_id`` named for a NoSQL source (BOP-6 A).
    2. Target keys: the target primary key (B2), each of its columns fed by a relation or produced
       (BOP-13 A); else the targets of the relations with a source key (B4, B5) plus ``targetKeys:``,
       none of them next to keys inherited from a source primary key (B7), and none fed by a single
       source that is not a key (B5). A target left without a key is refused, as CoreHub refuses every
       agent entity without keys.

    ``target_key_known`` is False when discovery could not read the target table (it may then have a primary
    key nobody saw): K9 is then only a warning appended to ``warnings``, unless ``strict``.

    Returns ``(source_keys, target_keys)`` spelt as the columns are, in column order.
    """
    where = f" of {label}" if label else ""
    source_order = list(source_names) + [str(n) for n in physical_source]
    target_order = list(target_names) + [str(n) for n in physical_target]
    physical_source = source_order[len(source_names):]
    physical_target = target_order[len(target_names):]
    ph_s = {n.lower() for n in physical_source}
    ph_t = {n.lower() for n in physical_target}
    source_spelling = _canonical(source_order, source_order)
    target_spelling = _canonical(target_order, target_order)

    _refuse_unknown(yaml_keys, source_spelling, f"the keys{where} name {{}}, which are not columns of the source table")
    requested = {str(k).lower() for k in yaml_keys}
    source_keys = _source_keys(requested, ph_s, _k2b_keys(relations, ph_s, ph_t), physical_source,
                               source_spelling, where, source_is_nosql, strict, warnings)

    _refuse_unknown(yaml_target_keys, target_spelling,
                    f"targetKeys{where} names {{}}, which are not target columns of the entity")
    target_requested = {str(k).lower() for k in yaml_target_keys}
    if ph_t:
        fed = {t.lower() for r in relations if r["sources"] for t in r["targets"]}
        produced = {str(n).lower() for n in produced_targets}
        target_keys = _primary_target_keys(ph_t, target_requested, target_names, fed, produced, physical_target,
                                           target_spelling, where)
    else:
        target_keys = _inherited_target_keys(relations, source_keys, ph_s, target_requested, target_spelling, where,
                                             target_key_known, strict, warnings)
    if not target_keys:
        raise EntityMappingError(
            f"the target table{where} has no key: no source key column feeds a target column and targetKeys names none. "
            f"Map a source key column onto a target column, or name the target key column(s) in targetKeys:"
        )
    return _spelt(source_order, source_keys, source_spelling), _spelt(target_order, target_keys, target_spelling)


def _source_rows(source, relation, row_of) -> List[Dict[str, Any]]:
    """The rows one source column gives at its place (see :func:`yaml_rows`)."""
    if relation is None:
        row = row_of(source, None)
        row["target"] = []
        return [row]
    if len(relation["sources"]) == 1:
        return [row_of(source, target) for target in relation["targets"]]
    if source != relation["sources"][0]:
        return []
    target = relation["targets"][0]
    return [{"source": list(relation["sources"]), "target": target, "type": row_of(source, target).get("type")}]


def yaml_rows(relations: Mapping[str, List[Any]], source_order: Sequence[str], row_of) -> List[Dict[str, Any]]:
    """The YAML ``columns:`` rows of the relations :func:`relations_of` read, in source order.

    ``row_of(source_name, target_name)`` builds the metadata row of one source column onto one target
    column (either may be None). Each source column gives, at its place: a 1:1 or 1:N row per target;
    for the first source of an N:1 relation one row with the list of its sources (the per-source
    metadata left out); ``target: []`` when it feeds nothing (1:null). Then one ``{target, type}`` row
    per target no source feeds (with the length, precision and scale ``row_of`` gives it), where the export attaches
    the field function that produces it.
    """
    by_source = {source: relation for relation in relations["relations"] for source in relation["sources"]}
    rows = [row for source in source_order for row in _source_rows(source, by_source.get(source), row_of)]
    for target in relations["unlinked_targets"]:
        produced = row_of(None, target)
        target_type = produced.get("type")
        dimensions = {key: value for key, value in produced.items() if key not in ("source", "target", "type")}
        rows.append({"target": target, "type": target_type, **dimensions} if target_type is not None
                    else {"target": target, **dimensions})
    return rows


