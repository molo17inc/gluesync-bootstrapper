"""Options of a backup import that change nothing unless asked for (feat/simple-backup-import).

* :class:`Remap` renames schemas, tables, entities, groups and agents of a backup YAML in memory, before any
  request, matching the names it is given case-insensitively (``--remap remap.yaml``, ``--schema-map`` ...).
* :class:`ImportOptions` carries ``--dry-run``, the pre-write discovery validation, the offline (no discovery)
  mode and ``--on-existing``; :func:`current` is the options of the run in progress, set by the CLI, main.py or
  the Automator with :func:`use`.
* :class:`ImportReport` is what a run would do or did: the per-table plan of a dry run, the validation issues,
  the entities saved without discovery and the existing entities skipped or updated.

Every default reproduces the behaviour from before these options.

remap.yaml format (every section optional, names matched case-insensitively)::

    schemas:            # source schema renames: the YAML section OLD is used for the source schema NEW
      SCH01: SCH01_QA
    targetSchemas:      # target schema renames (the ``target:`` of each schema section)
      TGT01: TGT01_TEST
    tables:             # source table renames, ``TABLE`` or ``SCHEMA.TABLE`` (old schema name)
      SCH01.TAB001: TAB001_V2
    targetTables:       # target table renames, ``TABLE`` or ``TARGETSCHEMA.TABLE`` (old names)
      TAB002: TAB002_TEST
    entityPrefix: "TEST_"   # added to every entity name
    entitySuffix: ""
    groups:             # group renames (``groups:`` section, table ``groupId`` and group schedules)
      nightly: nightly_test
    agents:             # agent renames by agentTag or agentId (Automator agents-config.yaml)
      mssql-ct: mssql-ct-test
"""

from __future__ import annotations

import contextlib
import copy
import difflib
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

import yaml

ON_EXISTING_CHOICES = ("skip", "update", "fail")


_SRC_DST = "SRC=DST"

def _folded(mapping) -> Dict[str, str]:
    return {str(k).casefold(): str(v) for k, v in (mapping or {}).items()}


def parse_pairs(values: Optional[Iterable[str]], flag: str) -> Dict[str, str]:
    """``['A=B', ...]`` of a repeatable flag as a dict; a value without ``=`` is an error naming the flag."""
    pairs = {}
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"{flag} expects SRC=DST, got {value!r}")
        src, dst = value.split("=", 1)
        if not src.strip() or not dst.strip():
            raise ValueError(f"{flag} expects SRC=DST, got {value!r}")
        pairs[src.strip()] = dst.strip()
    return pairs


def _has_custom_entry(custom, entry) -> bool:
    """Whether a custom key (``name`` or ``name@@...``) names whitelisted table ``entry``, case-insensitively."""
    return any(k.split("@@")[0].casefold() == str(entry).casefold() for k in custom)


@dataclass
class Remap:
    """Renames applied to a backup YAML; every lookup is case-insensitive."""

    schemas: Dict[str, str] = field(default_factory=dict)
    target_schemas: Dict[str, str] = field(default_factory=dict)
    tables: Dict[str, str] = field(default_factory=dict)
    target_tables: Dict[str, str] = field(default_factory=dict)
    entity_prefix: str = ""
    entity_suffix: str = ""
    groups: Dict[str, str] = field(default_factory=dict)
    agents: Dict[str, str] = field(default_factory=dict)

    _SECTIONS = {"schemas": "schemas", "targetSchemas": "target_schemas", "tables": "tables",
                 "targetTables": "target_tables", "groups": "groups", "agents": "agents"}

    def __post_init__(self):
        for attr in ("schemas", "target_schemas", "tables", "target_tables", "groups", "agents"):
            setattr(self, attr, _folded(getattr(self, attr)))

    @classmethod
    def from_mapping(cls, data: Dict[str, Any]) -> "Remap":
        if not isinstance(data, dict):
            raise ValueError("remap file must be a mapping")
        unknown = set(data) - set(cls._SECTIONS) - {"entityPrefix", "entitySuffix"}
        if unknown:
            raise ValueError(f"remap file: unknown section(s) {', '.join(sorted(unknown))}")
        kwargs = {}
        for key, attr in cls._SECTIONS.items():
            value = data.get(key) or {}
            if not isinstance(value, dict):
                raise ValueError(f"remap file: '{key}' must be a mapping of OLD: NEW")
            kwargs[attr] = value
        return cls(entity_prefix=str(data.get("entityPrefix") or ""),
                   entity_suffix=str(data.get("entitySuffix") or ""), **kwargs)

    @classmethod
    def load(cls, path: str) -> "Remap":
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_mapping(yaml.safe_load(handle) or {})

    def merged(self, other: "Remap") -> "Remap":
        """This remap with ``other``'s entries on top (CLI flags over the remap file)."""
        return Remap(
            schemas={**self.schemas, **other.schemas},
            target_schemas={**self.target_schemas, **other.target_schemas},
            tables={**self.tables, **other.tables},
            target_tables={**self.target_tables, **other.target_tables},
            entity_prefix=other.entity_prefix or self.entity_prefix,
            entity_suffix=other.entity_suffix or self.entity_suffix,
            groups={**self.groups, **other.groups},
            agents={**self.agents, **other.agents},
        )

    def __bool__(self):
        return any((self.schemas, self.target_schemas, self.tables, self.target_tables, self.entity_prefix,
                    self.entity_suffix, self.groups, self.agents))

    # -- single names -------------------------------------------------------------------------------------
    @staticmethod
    def _lookup(mapping, name):
        if name is None:
            return None
        return mapping.get(str(name).casefold(), name)

    def schema(self, name):
        return self._lookup(self.schemas, name)

    def target_schema(self, name):
        return self._lookup(self.target_schemas, name)

    def group(self, name):
        return self._lookup(self.groups, name)

    def agent(self, name):
        return self._lookup(self.agents, name)

    @staticmethod
    def _qualified(mapping, schema, table):
        if table is None:
            return None
        key = f"{schema}.{table}".casefold()
        if schema is not None and key in mapping:
            return mapping[key]
        return mapping.get(str(table).casefold(), table)

    def table(self, old_schema, name):
        return self._qualified(self.tables, old_schema, name)

    def target_table(self, old_target_schema, name):
        return self._qualified(self.target_tables, old_target_schema, name)

    def entity_name(self, name):
        if not name or not (self.entity_prefix or self.entity_suffix):
            return name
        return f"{self.entity_prefix}{name}{self.entity_suffix}"

    # -- the YAML ---------------------------------------------------------------------------------------
    def apply(self, yaml_config):
        """A remapped deep copy of a backup YAML (``schemas:`` layout or top-level schema sections).

        A renamed source table keeps its target table: the old name is written as the custom ``name`` when the
        YAML did not give one, so only the source side moves.
        """
        if not self or not isinstance(yaml_config, dict):
            return yaml_config
        config = copy.deepcopy(yaml_config)
        nested = isinstance(config.get("schemas"), dict)
        root = config["schemas"] if nested else config
        renamed = {}
        for schema_name, schema_config in root.items():
            if not isinstance(schema_config, dict) or (not nested and schema_name in ("groups", "exportMetadata")):
                renamed[schema_name] = schema_config
                continue
            renamed[self.schema(schema_name)] = self._apply_schema(schema_name, schema_config)
        if nested:
            config["schemas"] = renamed
        else:
            config.clear()
            config.update(renamed)
        groups = config.get("groups")
        if isinstance(groups, dict) and self.groups:
            config["groups"] = {self.group(k): v for k, v in groups.items()}
        return config

    def _rename_listings(self, schema_name, tables):
        for listing in ("whitelist", "blacklist"):
            entries = tables.get(listing)
            if isinstance(entries, list):
                tables[listing] = [self.table(schema_name, e) if isinstance(e, str) else e for e in entries]

    def _add_renamed_whitelist_entries(self, schema_name, old_target, tables, custom, new_custom):
        """Whitelisted tables without a custom entry: renaming their source or target needs one carrying ``name``."""
        for entry in yaml_config_whitelist(tables):
            new_source = self.table(schema_name, entry)
            new_target = self.target_table(old_target, entry)
            renamed = new_source != entry or new_target != entry
            if renamed and not _has_custom_entry(custom, entry):
                new_custom[new_source] = {"name": new_target}

    def _apply_schema(self, schema_name, schema_config):
        old_target = schema_config.get("target")
        if old_target is not None:
            schema_config["target"] = self.target_schema(old_target)
        tables = schema_config.get("tables")
        if not isinstance(tables, dict):
            return schema_config
        custom = tables.get("custom") or {}
        new_custom = {}
        for key, table in custom.items():
            new_custom[self._custom_key(schema_name, key, table)] = self._apply_table(schema_name, old_target,
                                                                                      key, table)
        self._rename_listings(schema_name, tables)
        self._add_renamed_whitelist_entries(schema_name, old_target, tables, custom, new_custom)
        if custom or new_custom:
            tables["custom"] = new_custom
        group_schedules = schema_config.get("group_schedules")
        if isinstance(group_schedules, dict) and self.groups:
            schema_config["group_schedules"] = {self.group(k): v for k, v in group_schedules.items()}
        return schema_config

    def _custom_key(self, schema_name, key, table):
        base, sep, suffix = str(key).partition("@@")
        if isinstance(table, dict) and table.get("sourceTableName"):
            return key  # the key is a label; sourceTableName is the source table
        return f"{self.table(schema_name, base)}{sep}{suffix}"

    def _apply_table(self, schema_name, old_target, key, table):
        if not isinstance(table, dict):
            return table
        base = str(key).split("@@")[0]
        source_name = table.get("sourceTableName") or base
        if table.get("sourceTableName"):
            table["sourceTableName"] = self.table(schema_name, table["sourceTableName"])
        if table.get("targetSchema"):
            old_target = table["targetSchema"]
            table["targetSchema"] = self.target_schema(old_target)
        old_target_name = table.get("name", source_name)
        new_target_name = self.target_table(old_target, old_target_name)
        if new_target_name != old_target_name or "name" in table or self.table(schema_name, source_name) != source_name:
            table["name"] = new_target_name
        for group_key in ("groupId", "group"):
            if group_key in table and self.groups and isinstance(table[group_key], str):
                table[group_key] = self.group(table[group_key])
        return table


def yaml_config_whitelist(tables_section) -> List[str]:
    whitelist = tables_section.get("whitelist") if isinstance(tables_section, dict) else None
    return [e for e in whitelist or [] if isinstance(e, str)]


def remap_from_args(args) -> Remap:
    """The remap of the CLI flags: ``--remap FILE`` with ``--schema-map`` ... on top."""
    base = Remap.load(args.remap) if getattr(args, "remap", None) else Remap()
    flags = Remap(
        schemas=parse_pairs(getattr(args, "schema_map", None), "--schema-map"),
        target_schemas=parse_pairs(getattr(args, "target_schema_map", None), "--target-schema-map"),
        tables=parse_pairs(getattr(args, "table_map", None), "--table-map"),
        target_tables=parse_pairs(getattr(args, "target_table_map", None), "--target-table-map"),
        entity_prefix=getattr(args, "entity_prefix", None) or "",
        entity_suffix=getattr(args, "entity_suffix", None) or "",
        groups=parse_pairs(getattr(args, "group_map", None), "--group-map"),
        agents=parse_pairs(getattr(args, "agent_map", None), "--agent-map"),
    )
    return base.merged(flags)


def add_cli_arguments(parser, *, entity_options=True):
    """The flags of this module on an argparse parser (create_all_entities.py, main.py, dbmoto converter)."""
    group = parser.add_argument_group("backup import")
    group.add_argument("--remap", metavar="FILE", help="remap.yaml with schema/table/entity/group/agent renames")
    group.add_argument("--schema-map", action="append", metavar=_SRC_DST, help="rename a source schema (repeatable)")
    group.add_argument("--target-schema-map", action="append", metavar=_SRC_DST,
                       help="rename a target schema (repeatable)")
    group.add_argument("--table-map", action="append", metavar="[SCHEMA.]SRC=DST",
                       help="rename a source table (repeatable)")
    group.add_argument("--target-table-map", action="append", metavar="[SCHEMA.]SRC=DST",
                       help="rename a target table (repeatable)")
    group.add_argument("--entity-prefix", help="prefix added to every entity name")
    group.add_argument("--entity-suffix", help="suffix added to every entity name")
    group.add_argument("--group-map", action="append", metavar=_SRC_DST, help="rename a group (repeatable)")
    group.add_argument("--agent-map", action="append", metavar=_SRC_DST,
                       help="rename an agent tag/id (repeatable)")
    if not entity_options:
        return group
    group.add_argument("--dry-run", action="store_true",
                       help="only GET requests: print the per-table plan, write nothing, exit 1 if anything would fail")
    group.add_argument("--no-validate", action="store_true",
                       help="skip the pre-write check of the YAML's schemas/tables/columns against discovery")
    group.add_argument("--strict-validate", action="store_true",
                       help="stop before any write when that check finds an issue (default: report and go on)")
    group.add_argument("--allow-offline-entities", action="store_true",
                       help="when discovery fails, build entities from the YAML's own columns/keys/types")
    group.add_argument("--allow-missing-tables", action="store_true",
                       help="import the entities of YAML tables missing from the source (or a source that cannot be "
                            "reached), built from the YAML's columns/keys/types; source tables are never created. "
                            "Core Hub acceptance of such entities is UNVERIFIED")
    group.add_argument("--no-discovery", action="store_true",
                       help="never call discovery: build every entity from the YAML (implies offline entities)")
    group.add_argument("--on-existing", choices=ON_EXISTING_CHOICES, default=None,
                       help="what to do with entities already in the pipeline (default: send them as before)")
    group.add_argument("--corehub-version", metavar="X.Y.Z",
                       help="Core Hub version to assume when GET /version is unreadable (env COREHUB_VERSION)")
    return group


def _env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _plan_row_line(row) -> str:
    """One line of the per-table plan of a dry run."""
    status = row.get("status", "ok")
    reason = f" reason: {row['reason']}" if row.get("reason") else ""
    return (f"  [{status.upper()}] {row.get('source')} -> {row.get('target')} "
            f"entity={row.get('entity')!r} keys={row.get('keys')} targetKeys={row.get('target_keys')} "
            f"exists={'yes' if row.get('exists') else 'no'}" + reason)


@dataclass
class ImportReport:
    """What a run would do (dry run) or did, beyond the counts create_entities returns."""

    plan: List[Dict[str, Any]] = field(default_factory=list)
    validation_issues: List[str] = field(default_factory=list)
    offline_entities: List[str] = field(default_factory=list)
    missing_source_tables: List[str] = field(default_factory=list)
    pending_target_tables: List[str] = field(default_factory=list)
    skipped_existing: List[str] = field(default_factory=list)
    updated_existing: List[str] = field(default_factory=list)
    blocked_writes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self):
        return {k: list(v) for k, v in self.__dict__.items()}

    def _dry_run_lines(self) -> list:
        lines = ["DRY RUN - nothing was written. Per-table plan:"]
        lines.extend(_plan_row_line(row) for row in self.plan)
        if not self.plan:
            lines.append("  (no entity)")
        lines.extend(f"  skipped write: {write}" for write in self.blocked_writes)
        return lines

    def render(self, dry_run=False) -> str:
        lines = self._dry_run_lines() if dry_run else []
        if self.validation_issues:
            lines.append(f"Validation against discovery: {len(self.validation_issues)} issue(s)")
            lines.extend(f"  - {issue}" for issue in self.validation_issues)
        if self.offline_entities:
            lines.append("Saved without discovery (built from the YAML; Core Hub acceptance not verified): "
                         + ", ".join(self.offline_entities))
        if self.missing_source_tables:
            lines.append("Source table missing, entity built from the YAML (--allow-missing-tables, UNVERIFIED): "
                         + ", ".join(self.missing_source_tables))
        if self.pending_target_tables:
            lines.append("Target table pending (could not be created now, create it before starting the pipeline): "
                         + ", ".join(self.pending_target_tables))
        if self.skipped_existing:
            lines.append("Skipped, already in the pipeline: " + ", ".join(self.skipped_existing))
        if self.updated_existing:
            lines.append("Updated with their existing entityId (not verified against a live Core Hub): "
                         + ", ".join(self.updated_existing))
        lines.extend(f"Warning: {w}" for w in self.warnings)
        return "\n".join(lines)


@dataclass
class ImportOptions:
    remap: Remap = field(default_factory=Remap)
    dry_run: bool = False
    validate: bool = True
    strict_validate: bool = False
    allow_offline_entities: bool = False
    no_discovery: bool = False
    allow_missing_tables: bool = False
    on_existing: Optional[str] = None
    corehub_version: Optional[str] = None
    report: ImportReport = field(default_factory=ImportReport)

    def __post_init__(self):
        if self.on_existing is not None and self.on_existing not in ON_EXISTING_CHOICES:
            raise ValueError(f"on_existing must be one of {ON_EXISTING_CHOICES}, got {self.on_existing!r}")

    @property
    def offline(self):
        return self.no_discovery or self.allow_offline_entities or self.allow_missing_tables

    @classmethod
    def from_args(cls, args) -> "ImportOptions":
        return cls(
            remap=remap_from_args(args),
            dry_run=bool(getattr(args, "dry_run", False)),
            validate=not getattr(args, "no_validate", False),
            strict_validate=bool(getattr(args, "strict_validate", False)),
            allow_offline_entities=bool(getattr(args, "allow_offline_entities", False)),
            no_discovery=bool(getattr(args, "no_discovery", False)),
            allow_missing_tables=bool(getattr(args, "allow_missing_tables", False)),
            on_existing=getattr(args, "on_existing", None),
            corehub_version=getattr(args, "corehub_version", None),
        )

    @classmethod
    def from_env(cls) -> "ImportOptions":
        """The options main.py hands to its create_all_entities subprocess through the environment."""
        remap_file = os.getenv("GS_IMPORT_REMAP")
        return cls(
            remap=Remap.load(remap_file) if remap_file else Remap(),
            dry_run=_env_flag("GS_IMPORT_DRY_RUN"),
            validate=not _env_flag("GS_IMPORT_NO_VALIDATE"),
            strict_validate=_env_flag("GS_IMPORT_STRICT_VALIDATE"),
            allow_offline_entities=_env_flag("GS_IMPORT_ALLOW_OFFLINE"),
            no_discovery=_env_flag("GS_IMPORT_NO_DISCOVERY"),
            allow_missing_tables=_env_flag("GS_IMPORT_ALLOW_MISSING_TABLES"),
            on_existing=os.getenv("GS_IMPORT_ON_EXISTING") or None,
            corehub_version=os.getenv("COREHUB_VERSION") or None,
        )


ENV_VARIABLES = ("GS_IMPORT_REMAP", "GS_IMPORT_DRY_RUN", "GS_IMPORT_NO_VALIDATE", "GS_IMPORT_STRICT_VALIDATE",
                 "GS_IMPORT_ALLOW_OFFLINE", "GS_IMPORT_NO_DISCOVERY", "GS_IMPORT_ALLOW_MISSING_TABLES",
                 "GS_IMPORT_ON_EXISTING", "COREHUB_VERSION")


def from_env_if_requested() -> Optional[ImportOptions]:
    """The environment's options when any GS_IMPORT_* / COREHUB_VERSION is set, else None (old behaviour)."""
    if any(os.getenv(name) for name in ENV_VARIABLES):
        return ImportOptions.from_env()
    return None


_CURRENT = ImportOptions()


def current() -> ImportOptions:
    return _CURRENT


@contextlib.contextmanager
def use(options: Optional[ImportOptions]):
    """Make ``options`` the options of the run in progress (the defaults again afterwards)."""
    global _CURRENT  # pylint: disable=global-statement
    previous = _CURRENT
    _CURRENT = options or ImportOptions()
    try:
        yield _CURRENT
    finally:
        _CURRENT = previous


class DryRunWriteBlocked(RuntimeError):
    """A write request reached the transport during a dry run (it was not sent)."""


def guard_write(method: str, path: str):
    """Refuse a non-GET request while a dry run is in progress (the last line of defence of ``--dry-run``)."""
    if _CURRENT.dry_run and str(method).upper() not in ("GET", "HEAD", "OPTIONS"):
        _CURRENT.report.blocked_writes.append(f"{method} {path}")
        raise DryRunWriteBlocked(f"dry run: {method} {path} not sent")


def suggest(name, candidates) -> str:
    """`` (did you mean X?)`` for the closest candidate: a case-only difference first, then a close spelling."""
    candidates = [c for c in candidates if c]
    for cand in candidates:
        if str(cand).casefold() == str(name).casefold():
            return f" (did you mean {cand!r}? names differ by case only)"
    close = difflib.get_close_matches(str(name).casefold(), [str(c).casefold() for c in candidates], n=1)
    if close:
        original = next(c for c in candidates if str(c).casefold() == close[0])
        return f" (did you mean {original!r}?)"
    return ""
