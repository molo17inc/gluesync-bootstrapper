"""Fan-in: several YAML tables feeding one target table.

create_all_tables.handle_table_creation creates a target table from the first entity that reaches it and
returns "already exists" for the others, so columns only the later entities carry are never created and a
column fed with different types (numeric vs varchar(9)) keeps whichever came first. Nothing here ALTERs a
table: :func:`fan_in_warnings` reports the union of columns, type conflicts, and the union columns an existing
target table lacks, so a run (and its --dry-run) says so before entities fail at runtime.
"""
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

_CHAR_TYPES = ("char", "varchar", "nchar", "nvarchar", "character", "text", "string", "graphic", "vargraphic")


def _schemas(yaml_config) -> Dict[str, Any]:
    root = yaml_config.get("schemas", yaml_config) if isinstance(yaml_config, dict) else {}
    return {k: v for k, v in (root or {}).items() if isinstance(v, dict) and isinstance(v.get("tables"), dict)}


def type_signature(entry: Dict[str, Any]) -> Optional[str]:
    """``numeric(9,0)``, ``varchar(9)``, ``int``... of a YAML column entry; None when it names no type."""
    kind = entry.get("type")
    if not kind:
        return None
    kind = str(kind).strip().lower()
    if any(kind.startswith(c) for c in _CHAR_TYPES) and entry.get("dataLength"):
        return f"{kind}({entry['dataLength']})"
    if entry.get("numericPrecision"):
        return f"{kind}({entry['numericPrecision']},{entry.get('numericScale') or 0})"
    return kind


def target_feeders(yaml_config, default_target=None) -> Dict[Tuple[str, str], List[Tuple[str, str, Dict]]]:
    """``{(target schema, target table) casefolded: [(source schema, yaml key, table cfg)]}`` for every table."""
    feeders = defaultdict(list)
    for schema_name, entry in _schemas(yaml_config).items():
        schema_target = entry.get("target") or default_target or schema_name
        custom = entry["tables"].get("custom") or {}
        for key, cfg in custom.items():
            if not isinstance(cfg, dict):
                continue
            target_schema = cfg.get("targetSchema") or schema_target
            target_table = cfg.get("name") or str(cfg.get("sourceTableName") or key).split("@@")[0]
            feeders[(str(target_schema).casefold(), str(target_table).casefold())].append((schema_name, key, cfg))
    return feeders


def union_columns(feeds: Iterable[Tuple[str, str, Dict]]) -> Dict[str, Dict[str, List[str]]]:
    """``{target column casefolded: {type signature or '?': [feeder labels]}}`` of the feeders of one table."""
    union: Dict[str, Dict[str, List[str]]] = defaultdict(lambda: defaultdict(list))
    for schema_name, key, cfg in feeds:
        for entry in cfg.get("columns") or []:
            if not isinstance(entry, dict):
                continue
            target = entry.get("target") or entry.get("source")
            if not target:
                continue
            union[str(target).casefold()][type_signature(entry) or "?"].append(f"{schema_name}.{key}")
    return union


_NUMERIC = ("numeric", "decimal", "number")
# Integer types and the decimal digits they hold, narrowest first
_INTEGERS = {"tinyint": 3, "smallint": 5, "int": 10, "integer": 10, "bigint": 19}


def _is_char(base: str) -> bool:
    return any(base.startswith(c) for c in _CHAR_TYPES)


def _positive(value) -> Optional[int]:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if value > 0 or value == -1:
        return value
    return None


def merge_types(specs: List[Tuple[str, Any, Any, Any]]):
    """The widest type that holds every ``(base type, length, precision, scale)`` of ``specs``.

    Returns ``(spec, None)``, or ``(None, reason)`` when no type is clearly safe for all of them. Safe merges: one
    character family (widest length; ``n``-types when any is one; ``varchar`` when lengths or kinds differ; -1 =
    max wins), numeric/decimal (widest integer digits and scale), integers (widest), integer with numeric (a
    numeric with enough digits), and identical types. Anything else (numeric vs varchar, numeric vs datetime2...)
    is a conflict: a cast would change or lose data."""
    norm = [(str(b or "").strip().lower(), _positive(l), _positive(p), _positive(s) or 0) for b, l, p, s in specs]
    norm = [n for n in norm if n[0]]
    if not norm:
        return None, "no type"
    bases = {n[0] for n in norm}
    if len(bases) == 1 and len({n[1:] for n in norm}) == 1:
        return norm[0], None
    if all(_is_char(b) for b in bases):
        return _merge_char_types(norm, bases)
    if all(b in _NUMERIC or b in _INTEGERS for b in bases):
        return _merge_numeric_types(norm, bases)
    if all(b in _APPROXIMATE for b in bases) and len(bases) > 1:
        return _merge_approximate_types(norm)
    if len(bases) == 1:
        # One type, different parameters (float(24)/float(53), money/datetime2 precisions...): the widest of each.
        return _widest_params(norm), None
    return None, f"incompatible types {' / '.join(sorted(bases))}"


_APPROXIMATE = {"real": 24, "float": 53, "double": 53, "double precision": 53}


def _widest_params(norm):
    def widest(values):
        values = [v for v in values if v is not None]
        return -1 if -1 in values else (max(values) if values else None)
    return (norm[0][0], widest([n[1] for n in norm]), widest([n[2] for n in norm]), max(n[3] for n in norm))


def _merge_approximate_types(norm):
    """real / float / double: the floating type with the most mantissa bits of them."""
    def bits(n):
        return n[2] if n[0] == "float" and n[2] else _APPROXIMATE[n[0]]
    widest = max(norm, key=bits)
    return (widest[0], None, widest[2] if widest[0] == "float" else None, 0), None


def _merge_char_types(norm, bases):
    """merge_types for one character family."""
    lengths = [n[1] for n in norm if n[1] is not None]
    if -1 in lengths:
        length = -1
    else:
        length = max(lengths) if lengths else None
    if len(bases) == 1:
        return (norm[0][0], length, None, 0), None
    if any(b in ("text", "ntext") for b in bases):
        return None, f"cannot merge {' / '.join(sorted(bases))}"
    unicode = any(b.startswith("n") for b in bases)
    return (("nvarchar" if unicode else "varchar"), length, None, 0), None


def _merge_numeric_types(norm, bases):
    """merge_types for integers and numeric/decimal types."""
    if all(b in _INTEGERS for b in bases):
        return (max(bases, key=lambda b: _INTEGERS[b]), None, None, 0), None
    int_digits, scale = 0, 0
    for base, _l, prec, sc in norm:
        if base in _INTEGERS:
            int_digits = max(int_digits, _INTEGERS[base])
        elif prec is None:
            return None, f"{base} without a precision"
        else:
            int_digits, scale = max(int_digits, prec - sc), max(scale, sc)
    if int_digits + scale > 38:
        return None, f"needs {int_digits + scale} digits, over 38"
    numeric = next(b for b in (n[0] for n in norm) if b in _NUMERIC)
    return (numeric, None, int_digits + scale, scale), None


def describe(spec) -> str:
    base, length, prec, scale = spec
    if length:
        return f"{base}({'max' if length == -1 else length})"
    if prec:
        return f"{base}({prec},{scale or 0})"
    return base


def _row_spec(entry: Dict[str, Any]):
    """The target ``(base, length, precision, scale)`` a YAML row declares (target* fields first); None for no type."""
    def first(*keys):
        return next((entry.get(k) for k in keys if entry.get(k) not in (None, "")), None)
    base = first("targetDataType", "type")
    if not base:
        return None
    return (str(base).split("(")[0], first("targetDataLength", "dataLength"),
            first("targetNumericPrecision", "numericPrecision"), first("targetNumericScale", "numericScale"))


def _column_specs(feeds) -> Dict[str, List[Tuple[str, Any, Any, Any]]]:
    """``{column casefolded: [type spec of each feeder row]}``."""
    specs = defaultdict(list)
    for _s, _k, cfg in feeds:
        for entry in cfg.get("columns") or []:
            if not isinstance(entry, dict) or not (entry.get("target") or entry.get("source")):
                continue
            spec = _row_spec(entry)
            if spec:
                specs[str(entry.get("target") or entry.get("source")).casefold()].append(spec)
    return specs


def type_conflicts(feeds) -> Dict[str, str]:
    """``{column casefolded: reason}`` for the union columns of ``feeds`` no type safely holds (see merge_types)."""
    specs = _column_specs(feeds)
    conflicts = {}
    for column, found in specs.items():
        merged, reason = merge_types(found)
        if merged is None:
            conflicts[column] = f"{reason} ({', '.join(sorted({describe(_norm(s)) for s in found}))})"
    return conflicts


def _norm(spec):
    b, l, p, s = spec
    return (str(b).lower(), _positive(l), _positive(p), _positive(s) or 0)


def peers_of(yaml_config, target_schema, target_table, default_target=None) -> List[Dict]:
    """The table cfgs of every YAML table that feeds ``target_schema.target_table`` (casefolded match)."""
    key = (str(target_schema).casefold(), str(target_table).casefold())
    return [cfg for _s, _k, cfg in target_feeders(yaml_config, default_target).get(key, [])]


def fan_in_warnings(yaml_config, source_schema, default_target=None,
                    existing_columns: Optional[Callable[[str, str], Optional[List[str]]]] = None) -> List[str]:
    """Warnings for each target table fed by 2+ YAML tables, one of which belongs to ``source_schema``.

    ``existing_columns(target_schema, target_table)`` gives the columns of an existing target table (None when
    the table does not exist or cannot be read).
    """
    warnings = []
    for feeds in (f for _key, f in sorted(target_feeders(yaml_config, default_target).items())):
        if len(feeds) < 2 or not any(s == source_schema for s, _k, _c in feeds):
            continue
        labels = ", ".join(f"{s}.{k}" for s, k, _c in feeds)
        spelled_schema, spelled_table = _spelled_target(yaml_config, feeds, default_target)
        prefix = f"fan-in {spelled_schema}.{spelled_table} (fed by {labels})"
        union = union_columns(feeds)
        warnings.extend(_type_conflict_warnings(prefix, union, type_conflicts(feeds)))
        if existing_columns is not None:
            warnings.extend(_missing_columns_warnings(prefix, union,
                                                      existing_columns(spelled_schema, spelled_table)))
    return warnings


def _spelled_target(yaml_config, feeds, default_target) -> Tuple[Any, str]:
    """The target schema and table of a fan-in as the first feeder spells them."""
    first_schema, first_key, first_cfg = feeds[0]
    spelled_schema = first_cfg.get("targetSchema") or _schemas(yaml_config)[first_schema].get("target") \
        or default_target or first_schema
    spelled_table = first_cfg.get("name") or str(first_key).split("@@")[0]
    return spelled_schema, spelled_table


def _type_conflict_warnings(prefix: str, union, conflicts: Dict[str, str]) -> List[str]:
    warnings = []
    for column, types in sorted(union.items()):
        known = {t: v for t, v in types.items() if t != "?"}
        if len(known) > 1 and column in conflicts:
            detail = "; ".join(f"{t} from {', '.join(v)}" for t, v in sorted(known.items()))
            warnings.append(f"{prefix}: column {column} has conflicting types that no "
                            f"type safely holds ({conflicts[column]}): {detail}; the table is created with the "
                            f"type of the first feeder created; set targetDataType/targetDataLength on the rows "
                            f"to choose one")
    return warnings


def _missing_columns_warnings(prefix: str, union, present: Optional[List[str]]) -> List[str]:
    if present is None:
        return []
    have = {str(c).casefold() for c in present}
    missing = sorted(c for c in union if c not in have)
    if not missing:
        return []
    return [f"{prefix}: the existing table lacks union column(s) "
            f"{', '.join(missing)}; they are not added (no ALTER is ever run): add them on the target, then resume"]
