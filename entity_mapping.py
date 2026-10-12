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


def _names_of(value: Any) -> List[str]:
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _relation(sources, targets, row) -> Dict[str, Any]:
    return {"sources": list(sources), "targets": list(targets), "explicit": True, "row": row}


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
    where = f" of {label}" if label else ""
    relations: List[Dict[str, Any]] = []

    if _is_whitelist(rows):
        for row in rows:
            if isinstance(row, Mapping) and row.get("name") is not None:
                relations.append(_relation([str(row.get("sourceName") or row.get("name"))], [str(row["name"])], row))
        return relations
    if not _is_mapping(rows):
        return None

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if "source" in row:
            source = row.get("source")
            if source is None or source == []:
                continue
            sources = _names_of(source)
            target = row.get("target")
            if isinstance(target, list):
                if len(target) > 1:
                    raise EntityMappingError(
                        f"the column row{where} for {', '.join(sources)} lists the targets {', '.join(map(str, target))}: "
                        f"write one row per target with the same source"
                    )
                targets = [str(t) for t in target]
            elif target is None:
                if len(sources) > 1:
                    raise EntityMappingError(
                        f"the column row{where} with the sources {', '.join(sources)} names no target: several sources "
                        f"feed one target column (N:1), which the row has to name"
                    )
                targets = sources
            else:
                targets = [str(target)]
            relations.append(_relation(sources, targets, row))
            continue
        if "target" in row and "name" not in row and ("expression" in row or "type" in row):
            if row.get("target") is not None:
                relations.append(_relation([], [str(row["target"])], row))
            continue
        for key, value in row.items():
            if value is None or isinstance(value, (dict, list, bool)):
                continue
            relations.append(_relation([str(key)], [value if isinstance(value, str) else str(value)], row))
    return relations


def group_relations(relations: Sequence[Mapping[str, Any]], label: str = "") -> List[Dict[str, Any]]:
    """The relations of a Gluesync 2.3 entity from the YAML relations, names compared case-insensitively.

    Rows that share a single source are one 1:N relation. Refused, naming ``label`` and the columns: a
    row listed twice, a target named by several rows (the N:1 form lists its sources in one row), a
    source of an N:1 row named by another row (M:N), a source both unlinked (``target: []``) and
    linked.
    """
    where = f" of {label}" if label else ""
    grouped: List[Dict[str, Any]] = []
    by_source: Dict[str, Dict[str, Any]] = {}
    target_owner: Dict[str, Dict[str, Any]] = {}

    def claim_targets(relation, targets, sources_text):
        for target in targets:
            owner = target_owner.get(target.lower())
            if owner is None:
                target_owner[target.lower()] = relation
                continue
            if owner is relation:
                raise EntityMappingError(f"the column row {sources_text} -> {target}{where} is listed twice")
            all_sources = owner["sources"] + [s for s in relation["sources"] if s.lower() not in {o.lower() for o in owner["sources"]}]
            raise EntityMappingError(
                f"target column {target}{where} is named by several rows ({', '.join(owner['sources']) or 'no source'}; "
                f"{sources_text or 'no source'}); a column belongs to one relation: for several sources onto one target "
                f"write one row with source: [{', '.join(all_sources)}] and a custom field function, or the entity's UDF"
            )

    for relation in relations:
        sources, targets = list(relation["sources"]), list(relation["targets"])
        sources_text = ", ".join(sources)
        if len(sources) == 1 and sources[0].lower() in by_source:
            existing = by_source[sources[0].lower()]
            if len(existing["sources"]) > 1:
                raise EntityMappingError(
                    f"source column {sources[0]}{where} is in the N:1 row of {', '.join(existing['targets'])} and in "
                    f"another row: merged they would relate several sources to several targets (M:N), which is not supported"
                )
            if not existing["targets"] or not targets:
                raise EntityMappingError(
                    f"source column {sources[0]}{where} is unlinked (target: []) in one row and linked in another; "
                    f"keep one of the two"
                )
            if any(t.lower() in {e.lower() for e in existing["targets"]} for t in targets):
                raise EntityMappingError(f"the column row {sources_text} -> {', '.join(targets)}{where} is listed twice")
            claim_targets(existing, targets, sources_text)
            existing["targets"].extend(targets)
            continue
        for source in sources:
            existing = by_source.get(source.lower())
            if existing is not None:
                raise EntityMappingError(
                    f"source column {source}{where} is named by the N:1 row of {', '.join(targets) or 'no target'} and by "
                    f"another row: merged they would relate several sources to several targets (M:N), which is not supported"
                )
        merged = {"sources": sources, "targets": [], "explicit": relation.get("explicit", True), "row": relation.get("row")}
        claim_targets(merged, targets, sources_text)
        merged["targets"].extend(targets)
        for source in sources:
            by_source[source.lower()] = merged
        grouped.append(merged)
    return grouped


def grouped_only_forms(relations: Optional[Sequence[Mapping[str, Any]]], table_config: Optional[Mapping[str, Any]]) -> List[str]:
    """The YAML forms of a table that only a Gluesync 2.3 CoreHub can store, one description each."""
    forms: List[str] = []
    seen_sources: Dict[str, int] = {}
    for relation in relations or []:
        sources, targets = relation["sources"], relation["targets"]
        if len(sources) > 1:
            forms.append(f"source: [{', '.join(sources)}] (several sources onto {', '.join(targets)})")
        if sources and not targets:
            forms.append(f"target: [] on source {', '.join(sources)} (read, never written)")
        if len(sources) == 1:
            key = sources[0].lower()
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
       source that is not a key (B5).

    Returns ``(source_keys, target_keys)`` spelt as the columns are, in column order.
    """
    where = f" of {label}" if label else ""
    physical_source = [str(n) for n in physical_source]
    physical_target = [str(n) for n in physical_target]
    ph_s = {n.lower() for n in physical_source}
    ph_t = {n.lower() for n in physical_target}
    source_spelling = _canonical(list(source_names) + physical_source, list(source_names) + physical_source)
    target_spelling = _canonical(list(target_names) + physical_target, list(target_names) + physical_target)
    produced = {str(n).lower() for n in produced_targets}

    unknown = [k for k in yaml_keys if str(k).lower() not in source_spelling]
    if unknown:
        raise EntityMappingError(
            f"the keys{where} name {_joined(map(str, unknown))}, which are not columns of the source table"
        )
    requested = {str(k).lower() for k in yaml_keys}

    # Single source of each relation onto the target primary key, on a source without one (K2b).
    k2b = set()
    if not ph_s and ph_t:
        for relation in relations:
            if len(relation["sources"]) == 1 and any(t.lower() in ph_t for t in relation["targets"]):
                k2b.add(relation["sources"][0].lower())

    if ph_s:
        if requested and requested != ph_s:
            raise EntityMappingError(
                f"R12.1: the keys{where} ({_joined(source_spelling[k] for k in sorted(requested))}) are not the source "
                f"table's primary key ({_joined(sorted(physical_source))}); leave keys: out, or name exactly that key"
            )
        source_keys = ph_s
    elif k2b:
        missing, manual = k2b - requested, (requested - k2b) if requested else set()
        if requested and (missing or manual):
            problems = []
            if missing:
                problems.append(
                    f"K2b: {_joined(source_spelling[k] for k in sorted(missing))} must be a key, being the only source of "
                    f"a target primary key column while the source table has no primary key"
                )
            if manual:
                problems.append(
                    f"K9: {_joined(source_spelling[k] for k in sorted(manual))} cannot be a key next to the keys the "
                    f"target primary key gives the source ({_joined(source_spelling[k] for k in sorted(k2b))})"
                )
            raise EntityMappingError(
                "; ".join(problems) + f"{where}. Leave keys: out to key the entity on "
                f"{_joined(source_spelling[k] for k in sorted(k2b))}"
            )
        source_keys = k2b
    else:
        source_keys = requested

    if not source_keys:
        hint = (
            " A NoSQL source declares no primary key: name the key column(s) in keys:, usually _id (the document id)."
            if source_is_nosql else " Name the key column(s) in keys:."
        )
        raise EntityMappingError(f"the source table{where} has no key: it has no primary key and keys: names none.{hint}")

    target_requested = {str(k).lower() for k in yaml_target_keys}
    unknown_targets = [k for k in yaml_target_keys if str(k).lower() not in target_spelling]
    if unknown_targets:
        raise EntityMappingError(f"targetKeys{where} names {_joined(map(str, unknown_targets))}, which are not target columns of the entity")

    fed = {t.lower() for r in relations if r["sources"] for t in r["targets"]}
    if ph_t:
        if target_requested and target_requested != ph_t:
            raise EntityMappingError(
                f"R12.2: targetKeys{where} ({_joined(target_spelling[k] for k in sorted(target_requested))}) is not the "
                f"target table's primary key ({_joined(sorted(physical_target))}); leave targetKeys out"
            )
        in_entity = {str(n).lower() for n in target_names}
        for key in sorted(ph_t):
            if key not in in_entity or (key not in fed and key not in produced):
                raise EntityMappingError(
                    f"target primary key {target_spelling[key]}{where} is fed by no source column: a new entity needs a "
                    f"value for it (an identity or a default is not used), so add a row {{target: {target_spelling[key]}, "
                    f"expression: ...}} with a field function that produces it, link a source column to it, or give the "
                    f"entity a UDF"
                )
        target_keys = ph_t
    else:
        inherited = set()
        for relation in relations:
            if any(s.lower() in source_keys for s in relation["sources"]):
                inherited.update(t.lower() for t in relation["targets"])
        if ph_s:
            manual = target_requested - inherited
            if manual:
                raise EntityMappingError(
                    f"K9: targetKeys{where} names {_joined(target_spelling[k] for k in sorted(manual))} next to the keys "
                    f"the target inherits from the source primary key ({_joined(target_spelling[k] for k in sorted(inherited))}); "
                    f"leave targetKeys out"
                )
        else:
            for relation in relations:
                if len(relation["sources"]) == 1 and relation["sources"][0].lower() not in source_keys:
                    keyed = [t for t in relation["targets"] if t.lower() in target_requested]
                    if keyed:
                        raise EntityMappingError(
                            f"R12.6: targetKeys{where} names {_joined(keyed)}, fed by {relation['sources'][0]} alone, which "
                            f"is not a key: with no primary key on either table the single source of a target key is a "
                            f"key; add {relation['sources'][0]} to keys:"
                        )
        target_keys = inherited | target_requested

    source_order = [n for n in dict.fromkeys(list(source_names) + physical_source)]
    target_order = [n for n in dict.fromkeys(list(target_names) + physical_target)]
    return (
        [n for n in source_order if n.lower() in source_keys and source_spelling[n.lower()] == n],
        [n for n in target_order if n.lower() in target_keys and target_spelling[n.lower()] == n],
    )
