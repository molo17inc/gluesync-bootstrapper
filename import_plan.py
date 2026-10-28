# This program is part of Gluesync.
#
# Bootstrapper is dual-licensed under the following licenses:
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

"""The import plan of a custom backup import: what of a backup ZIP to import, renamed how, and into what.

One plan drives the Automator (Analyze -> Customize -> Preview -> Import selected) and the CLI
(``import_backup --plan plan.json`` / ``--write-plan``). An empty plan (``{}``) is today's Import All: every
choice has the default the import had before plans existed.

A plan never holds a password. An agent's new password is ``passwordRef: "env:NAME"`` (CLI) or comes in the
separate in-memory ``agent_passwords`` of one Automator request; a plan with any password key is refused.

Plan JSON (every key optional)::

    {"version": 1,
     "globals":  {"notifications": true, "users": true, "accessPolicies": true},
     "defaults": {"onExistingEntity": "skip|update|fail", "onExistingPipeline": "suffix|fail",
                  "cleanupOnFailure": false, "createTables": true, "schedules": true, "groups": true,
                  "udfs": true, "functionCollection": true, "validate": true, "strictValidate": false},
     "pipelines": {"<backup pipeline id>": {
         "import": true, "name": "New name", "resumeInto": "<existing Core Hub pipeline id>",
         "agents": {"<agentTag or agentId>": {"host": "h", "port": 1433, "database": "d", "username": "u",
                                               "passwordRef": "env:NAME"}},
         "remap": {<import_options remap sections>},
         "tableTargetSchema": {"SCHEMA.TABLE": "TARGETSCHEMA"},
         "entities": {"include": ["SCHEMA.TABLE", ...], "exclude": [...],
                      "overrides": {"SCHEMA.TABLE": {"targetSchema": "S", "targetTable": "T", "entityName": "E"}}},
         "parts": {"udfs": true, "functionCollection": true, "schedules": true, "groups": true,
                   "createTables": true},
         "onExistingEntity": "skip", "onExistingPipeline": "suffix"}}}

Entities are named ``SOURCESCHEMA.KEY`` with the backup's own names (the YAML key, ``@@n`` included); a name
without ``@@`` also stands for its ``@@n`` variants. Selection and overrides use the backup's names; the remap
renames what is left afterwards.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import import_options

PLAN_VERSION = 1
GLOBALS = ("notifications", "users", "accessPolicies")
PARTS = ("udfs", "functionCollection", "schedules", "groups", "createTables")
_DEFAULT_KEYS = {"onExistingEntity", "onExistingPipeline", "cleanupOnFailure", "validate", "strictValidate", *PARTS}
_PIPELINE_KEYS = {"import", "name", "resumeInto", "agents", "remap", "tableTargetSchema", "entities", "parts",
                  "onExistingEntity", "onExistingPipeline"}
_AGENT_KEYS = {"host", "port", "database", "username", "passwordRef"}
_ENTITY_KEYS = {"include", "exclude", "overrides"}
_OVERRIDE_KEYS = {"targetSchema", "targetTable", "entityName"}
_PIPELINE_ON_EXISTING = ("suffix", "fail")
_PASSWORD_REF = re.compile(r"^env:([A-Za-z_]\w*)$", re.ASCII)
# A key naming any of these is a secret: a plan with one is refused (case and separators ignored).
_SECRET_PARTS = ("password", "passwd", "pwd", "secret", "token", "apikey", "privatekey")
_SECRET_EXEMPT = {"passwordref"}
# Env var names the Automator puts in-memory UI passwords under (a private mapping, never os.environ).
UI_PASSWORD_ENV_PREFIX = "GS_IMPORT_PLAN_PASSWORD_"


class PlanError(ValueError):
    """A plan that cannot be used; the message names the field, never a value that could be a secret."""


def _secret_key(key: Any) -> bool:
    folded = str(key).lower().replace("_", "").replace("-", "")
    return folded not in _SECRET_EXEMPT and any(part in folded for part in _SECRET_PARTS)


# Mappings whose keys are names from the backup or Core Hub (pipeline ids, agent refs, tables, schemas), not
# fields: a table called AUTH.PASSWORD_RESET is not a secret. Their values are still scanned.
_NAME_KEYED = (re.compile(r"^plan\.pipelines$"), re.compile(r"^plan\.pipelines\.[^.]+\.agents$"),
               re.compile(r"^plan\.pipelines\.[^.]+\.entities\.overrides$"),
               re.compile(r"^plan\.pipelines\.[^.]+\.tableTargetSchema$"),
               re.compile(r"^plan\.pipelines\.[^.]+\.remap\.[^.]+$"))


def _name_keyed(where: str) -> bool:
    return any(p.match(where) for p in _NAME_KEYED)


def _refuse_secrets(value: Any, where: str = "plan") -> None:
    """PlanError for a field that holds a password; only field names are checked, not names of tables,
    schemas, pipelines or agents used as keys (see _NAME_KEYED)."""
    if isinstance(value, dict):
        names_only = _name_keyed(where)
        for key, item in value.items():
            if not names_only and _secret_key(key):
                raise PlanError(f"{where}.{key}: a plan never holds a password or secret; use passwordRef "
                                "\"env:NAME\" (CLI) or the Automator's password fields")
            _refuse_secrets(item, f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _refuse_secrets(item, f"{where}[{index}]")


def _mapping(value: Any, where: str) -> Dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PlanError(f"{where} must be an object")
    return value


def _unknown(value: Dict[str, Any], allowed: Iterable[str], where: str) -> None:
    extra = sorted(set(value) - set(allowed))
    if extra:
        hint = import_options.suggest(extra[0], list(allowed))
        raise PlanError(f"{where}: unknown field(s) {', '.join(extra)}{hint}")


def _bool(value: Any, where: str) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    raise PlanError(f"{where} must be true or false")


def _choice(value: Any, choices: Tuple[str, ...], where: str) -> Optional[str]:
    if value is None:
        return None
    folded = str(value).strip().lower()
    if folded not in choices:
        raise PlanError(f"{where} must be one of {', '.join(choices)}")
    return folded


def _names(value: Any, where: str) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise PlanError(f"{where} must be a list of SCHEMA.TABLE names")
    return [v.strip() for v in value]


@dataclass
class PipelinePlan:
    """The choices for one backup pipeline (defaults: import it as Import All does)."""

    old_id: str
    import_: bool = True
    name: Optional[str] = None
    resume_into: Optional[str] = None
    agents: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    remap: import_options.Remap = field(default_factory=import_options.Remap)
    remap_raw: Dict[str, Any] = field(default_factory=dict)
    table_target_schema: Dict[str, str] = field(default_factory=dict)
    include: Optional[List[str]] = None
    exclude: List[str] = field(default_factory=list)
    overrides: Dict[str, Dict[str, str]] = field(default_factory=dict)
    parts: Dict[str, bool] = field(default_factory=dict)
    on_existing_entity: Optional[str] = None
    on_existing_pipeline: Optional[str] = None

    def changes_yaml(self) -> bool:
        return bool(self.include is not None or self.exclude or self.overrides or self.table_target_schema)


def _pipeline_agents(raw: Mapping[str, Any], where: str) -> Dict[str, Dict[str, Any]]:
    agents = {}
    for ref, entry in _mapping(raw.get("agents"), f"{where}.agents").items():
        entry = _mapping(entry, f"{where}.agents.{ref}")
        _unknown(entry, _AGENT_KEYS, f"{where}.agents.{ref}")
        password_ref = entry.get("passwordRef")
        if password_ref is not None and not _PASSWORD_REF.match(str(password_ref)):
            raise PlanError(f"{where}.agents.{ref}.passwordRef must be \"env:NAME\"")
        agents[str(ref)] = {k: v for k, v in entry.items() if v not in (None, "")}
    return agents


def _entity_overrides(entities: Mapping[str, Any], where: str) -> Dict[str, Dict[str, str]]:
    overrides = {}
    for key, entry in _mapping(entities.get("overrides"), f"{where}.entities.overrides").items():
        entry = _mapping(entry, f"{where}.entities.overrides.{key}")
        _unknown(entry, _OVERRIDE_KEYS, f"{where}.entities.overrides.{key}")
        overrides[str(key)] = {k: str(v) for k, v in entry.items() if v not in (None, "")}
    return overrides


def _optional_name(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip() or None


def _optional_text(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value).strip() or None


def _pipeline(old_id: str, raw: Any) -> PipelinePlan:
    where = f"pipelines.{old_id}"
    raw = _mapping(raw, where)
    _unknown(raw, _PIPELINE_KEYS, where)
    agents = _pipeline_agents(raw, where)
    remap_raw = _mapping(raw.get("remap"), f"{where}.remap")
    try:
        remap = import_options.Remap.from_mapping(remap_raw)
    except ValueError as exc:
        raise PlanError(f"{where}.remap: {exc}") from None
    tts = _mapping(raw.get("tableTargetSchema"), f"{where}.tableTargetSchema")
    for key, value in tts.items():
        if "." not in str(key) or not isinstance(value, str) or not value.strip():
            raise PlanError(f"{where}.tableTargetSchema: \"SCHEMA.TABLE\": \"TARGETSCHEMA\" expected")
    entities = _mapping(raw.get("entities"), f"{where}.entities")
    _unknown(entities, _ENTITY_KEYS, f"{where}.entities")
    overrides = _entity_overrides(entities, where)
    parts = _mapping(raw.get("parts"), f"{where}.parts")
    _unknown(parts, PARTS, f"{where}.parts")
    resume_into = raw.get("resumeInto")
    return PipelinePlan(
        old_id=str(old_id),
        import_=_bool(raw.get("import"), f"{where}.import") is not False,
        name=_optional_name(raw.get("name")),
        resume_into=_optional_text(resume_into),
        agents=agents,
        remap=remap,
        remap_raw=remap_raw,
        table_target_schema={str(k): str(v).strip() for k, v in tts.items()},
        include=_names(entities.get("include"), f"{where}.entities.include"),
        exclude=_names(entities.get("exclude"), f"{where}.entities.exclude") or [],
        overrides=overrides,
        parts={k: _bool(v, f"{where}.parts.{k}") for k, v in parts.items() if v is not None},
        on_existing_entity=_choice(raw.get("onExistingEntity"), import_options.ON_EXISTING_CHOICES,
                                   f"{where}.onExistingEntity"),
        on_existing_pipeline=_choice(raw.get("onExistingPipeline"), _PIPELINE_ON_EXISTING,
                                     f"{where}.onExistingPipeline"),
    )


@dataclass
class ImportPlan:
    """A loaded, shape-checked plan. ``ImportPlan()`` is the empty plan (Import All)."""

    globals: Dict[str, bool] = field(default_factory=dict)
    defaults: Dict[str, Any] = field(default_factory=dict)
    pipelines: Dict[str, PipelinePlan] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.globals or self.defaults or self.pipelines)

    def pipeline(self, old_id: Any) -> PipelinePlan:
        return self.pipelines.get(str(old_id)) or PipelinePlan(old_id=str(old_id))

    def global_enabled(self, name: str) -> bool:
        return self.globals.get(name, True) is not False

    def part(self, pipeline: PipelinePlan, name: str) -> bool:
        """Whether a part (udfs, schedules ...) of a pipeline is imported: pipeline, then defaults, then yes."""
        if name in pipeline.parts:
            return pipeline.parts[name]
        return self.defaults.get(name, True) is not False

    def on_existing_entity(self, pipeline: PipelinePlan) -> Optional[str]:
        return pipeline.on_existing_entity or self.defaults.get("onExistingEntity")

    def on_existing_pipeline(self, pipeline: PipelinePlan) -> Optional[str]:
        return pipeline.on_existing_pipeline or self.defaults.get("onExistingPipeline")

    def to_dict(self) -> Dict[str, Any]:
        """The plan as given (normalized JSON); it never holds a password."""
        return copy.deepcopy(self.raw)

    def selected_pipelines(self) -> Optional[List[str]]:
        return [k for k, p in self.pipelines.items() if p.import_]


def _parsed_json(source: Any) -> Any:
    """``source`` parsed when it is JSON text (str or bytes), as it is otherwise."""
    if not isinstance(source, (str, bytes)):
        return source
    try:
        return json.loads(source)
    except ValueError as exc:
        raise PlanError(f"plan is not valid JSON: {exc.msg} (line {exc.lineno})") from None


def _checked_default(key: str, value: Any) -> Any:
    if key == "onExistingEntity":
        return _choice(value, import_options.ON_EXISTING_CHOICES, f"defaults.{key}")
    if key == "onExistingPipeline":
        return _choice(value, _PIPELINE_ON_EXISTING, f"defaults.{key}")
    return _bool(value, f"defaults.{key}")


def load(source: Any) -> ImportPlan:
    """An :class:`ImportPlan` from JSON text, a mapping or None (the empty plan); PlanError when it is invalid."""
    if source is None or (isinstance(source, str) and not source.strip()):
        return ImportPlan()
    raw = _mapping(_parsed_json(source), "plan")
    _refuse_secrets(raw)
    _unknown(raw, {"version", "globals", "defaults", "pipelines"}, "plan")
    version = raw.get("version", PLAN_VERSION)
    if version != PLAN_VERSION:
        raise PlanError(f"plan.version {version!r} is not supported (expected {PLAN_VERSION})")
    globals_ = _mapping(raw.get("globals"), "globals")
    _unknown(globals_, GLOBALS, "globals")
    defaults = _mapping(raw.get("defaults"), "defaults")
    _unknown(defaults, _DEFAULT_KEYS, "defaults")
    checked = {key: _checked_default(key, value) for key, value in defaults.items() if value is not None}
    pipelines = {str(k): _pipeline(str(k), v) for k, v in _mapping(raw.get("pipelines"), "pipelines").items()}
    return ImportPlan(globals={k: _bool(v, f"globals.{k}") for k, v in globals_.items() if v is not None},
                      defaults=checked, pipelines=pipelines, raw=copy.deepcopy(raw))


# -- the backup YAML ------------------------------------------------------------------------------------------

def _schemas_root(yaml_config) -> Dict[str, Any]:
    if not isinstance(yaml_config, dict):
        return {}
    root = yaml_config.get("schemas") if isinstance(yaml_config.get("schemas"), dict) else yaml_config
    return {k: v for k, v in root.items() if isinstance(v, dict) and k not in ("groups", "exportMetadata")}


def entity_ids(yaml_config) -> List[Dict[str, Any]]:
    """Every table of a backup YAML: ``{"id": "SCHEMA.KEY", "schema", "key", "table", "listed"}``.

    ``listed`` is True for a whitelisted table without a custom entry."""
    out = []
    for schema, entry in _schemas_root(yaml_config).items():
        out.extend(_schema_entity_ids(schema, entry))
    return out


def _dict_or_empty(value) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _schema_entity_ids(schema, entry) -> List[Dict[str, Any]]:
    """The :func:`entity_ids` of one schema entry: its custom tables, then its whitelisted-only tables."""
    tables = _dict_or_empty(entry.get("tables"))
    custom = _dict_or_empty(tables.get("custom"))
    out = []
    for key, cfg in custom.items():
        table = _dict_or_empty(cfg).get("sourceTableName") or str(key).split("@@")[0]
        out.append({"id": f"{schema}.{key}", "schema": schema, "key": str(key), "table": table, "listed": False})
    for name in import_options.yaml_config_whitelist(tables):
        if not _custom_keys_for(custom, name):
            out.append({"id": f"{schema}.{name}", "schema": schema, "key": name, "table": name, "listed": True})
    return out


def _custom_keys_for(custom: Mapping[str, Any], name: str) -> List[str]:
    """The custom keys a whitelist name stands for: the exact key (``T@@2``), else every ``T@@n`` of ``T``."""
    exact = [k for k in custom if str(k).casefold() == str(name).casefold()]
    if exact or "@@" in str(name):
        return exact
    return [k for k in custom if str(k).split("@@")[0].casefold() == str(name).casefold()]


def _matches(name: str, entity_id: str) -> bool:
    """``SCHEMA.KEY`` matches its id exactly (casefolded); without ``@@`` it also matches the @@n variants."""
    name, entity_id = name.casefold(), entity_id.casefold()
    return entity_id == name or ("@@" not in name and entity_id.split("@@")[0] == name)


def check_names(yaml_config, pipeline: PipelinePlan) -> None:
    """PlanError for a selection, override or tableTargetSchema name the backup pipeline does not have."""
    ids = [e["id"] for e in entity_ids(yaml_config)]
    named = [("entities.include", n) for n in pipeline.include or []] + \
            [("entities.exclude", n) for n in pipeline.exclude] + \
            [("entities.overrides", n) for n in pipeline.overrides] + \
            [("tableTargetSchema", n) for n in pipeline.table_target_schema]
    for where, name in named:
        if not any(_matches(name, i) for i in ids):
            raise PlanError(f"pipelines.{pipeline.old_id}.{where}: no table {name!r} in the backup"
                            f"{import_options.suggest(name, ids)}")


def selected_ids(yaml_config, pipeline: PipelinePlan) -> List[str]:
    """The entity ids of the backup pipeline the plan imports."""
    out = []
    for entity in entity_ids(yaml_config):
        if pipeline.include is not None and not any(_matches(n, entity["id"]) for n in pipeline.include):
            continue
        if any(_matches(n, entity["id"]) for n in pipeline.exclude):
            continue
        out.append(entity["id"])
    return out


def _final_target(yaml_config, pipeline: PipelinePlan, entity: Dict[str, Any]) -> Tuple[str, str]:
    entry = _dict_or_empty(_schemas_root(yaml_config).get(entity["schema"]))
    custom = _dict_or_empty(_dict_or_empty(entry.get("tables")).get("custom"))
    cfg = _dict_or_empty(custom.get(entity["key"]))
    override = _override_for(pipeline.overrides, entity["id"]) or {}
    schema = (override.get("targetSchema") or _override_for(pipeline.table_target_schema, entity["id"])
              or cfg.get("targetSchema") or entry.get("target") or "")
    table = override.get("targetTable") or cfg.get("name") or str(entity["key"]).split("@@")[0]
    return str(schema), str(table)


def check_target_collisions(yaml_config, pipeline: PipelinePlan, old_id: Optional[str] = None) -> None:
    """PlanError for two overrides (or tableTargetSchema entries) differing only by case, where the first one
    would silently win, and for two selected tables the plan renames onto the same target table."""
    where = f"pipelines.{old_id or pipeline.old_id}"
    for section, mapping in (("entities.overrides", pipeline.overrides),
                             ("tableTargetSchema", pipeline.table_target_schema)):
        seen: Dict[str, str] = {}
        for key in mapping:
            other = seen.setdefault(str(key).casefold(), str(key))
            if other != str(key):
                raise PlanError(f"{where}.{section}: {other!r} and {key!r} differ only by case; keep one")
    selected = {i.casefold() for i in selected_ids(yaml_config, pipeline)}
    renamed: Dict[Tuple[str, str], str] = {}
    for entity in entity_ids(yaml_config):
        override = _override_for(pipeline.overrides, entity["id"]) or {}
        if entity["id"].casefold() not in selected or not override.get("targetTable"):
            continue
        schema, table = _final_target(yaml_config, pipeline, entity)
        other = renamed.setdefault((schema.casefold(), table.casefold()), entity["id"])
        if other != entity["id"]:
            raise PlanError(f"{where}.entities.overrides: {other!r} and {entity['id']!r} are both renamed to "
                            f"{schema}.{table}; give each its own targetTable")


def _override_for(mapping: Mapping[str, Any], entity_id: str):
    exact = [v for k, v in mapping.items() if k.casefold() == entity_id.casefold()]
    if exact:
        return exact[0]
    base = [v for k, v in mapping.items() if "@@" not in k and _matches(k, entity_id)]
    return base[0] if base else None


def _plan_keeps_everything(yaml_config, plan_flags: Tuple[bool, bool], pipeline: PipelinePlan,
                           selected: List[str]) -> bool:
    udfs, groups = plan_flags
    return bool(udfs and groups and not pipeline.overrides and not pipeline.table_target_schema
            and len(selected) == len(entity_ids(yaml_config)))


class _YamlPlanEditor:
    """Applies one pipeline plan to the schemas of a copied backup YAML (see :func:`apply_to_yaml`)."""

    def __init__(self, pipeline: PipelinePlan, keep: set, udfs: bool, groups: bool):
        self.pipeline = pipeline
        self.keep = keep
        self.udfs = udfs
        self.groups = groups
        self.notes: List[str] = []
        self.dropped_schemas: List[str] = []
        self.skipped = 0

    def _apply_overrides(self, cfg: Dict[str, Any], entity_id: str) -> None:
        target = _override_for(self.pipeline.table_target_schema, entity_id)
        if target:
            cfg["targetSchema"] = target
        override = _override_for(self.pipeline.overrides, entity_id) or {}
        for source_key, cfg_key in (("targetSchema", "targetSchema"), ("targetTable", "name"),
                                    ("entityName", "entityName")):
            if override.get(source_key):
                cfg[cfg_key] = override[source_key]

    def _strip_parts(self, cfg: Dict[str, Any], entity_id: str) -> None:
        if not self.udfs:
            target_props = (cfg.get("customProperties") or {}).get("target")
            if isinstance(target_props, dict) and target_props.pop("udf", None) is not None:
                self.notes.append(f"{entity_id}: UDF not imported (plan: udfs off)")
        if not self.groups:
            cfg.pop("groupId", None)
            cfg.pop("group", None)

    def _kept_custom(self, schema: str, custom: Mapping[str, Any]) -> Dict[str, Any]:
        kept_custom = {}
        for key, cfg in custom.items():
            entity_id = f"{schema}.{key}"
            if entity_id.casefold() not in self.keep:
                self.skipped += 1
                continue
            cfg = cfg if isinstance(cfg, dict) else {}
            self._apply_overrides(cfg, entity_id)
            self._strip_parts(cfg, entity_id)
            kept_custom[key] = cfg
        return kept_custom

    def _listed_extra(self, entity_id: str) -> Dict[str, Any]:
        override = _override_for(self.pipeline.overrides, entity_id) or {}
        target = _override_for(self.pipeline.table_target_schema, entity_id) or override.get("targetSchema")
        return {k: v for k, v in (("targetSchema", target), ("name", override.get("targetTable")),
                                  ("entityName", override.get("entityName"))) if v}

    def _kept_whitelist(self, schema: str, custom: Mapping[str, Any], kept_custom: Dict[str, Any],
                        whitelist: List[str]) -> List[str]:
        kept_white = []
        for name in whitelist:
            bases = _custom_keys_for(custom, name)
            if bases:
                if any(k in kept_custom for k in bases):
                    kept_white.append(name)
                continue
            entity_id = f"{schema}.{name}"
            if entity_id.casefold() not in self.keep:
                self.skipped += 1
                continue
            extra = self._listed_extra(entity_id)
            if extra:
                kept_custom[name] = extra
            kept_white.append(name)
        return kept_white

    def edit_schema(self, schema: str, entry: Dict[str, Any]) -> bool:
        """Edits one schema entry in place; True when nothing of it is selected (the schema goes away)."""
        tables = entry.get("tables") if isinstance(entry.get("tables"), dict) else None
        if tables is None:
            return False
        custom = tables.get("custom") if isinstance(tables.get("custom"), dict) else {}
        kept_custom = self._kept_custom(schema, custom)
        whitelist = import_options.yaml_config_whitelist(tables)
        kept_white = self._kept_whitelist(schema, custom, kept_custom, whitelist)
        if "custom" in tables or kept_custom:
            tables["custom"] = kept_custom
        if whitelist:
            tables["whitelist"] = kept_white
        if not self.groups:
            entry.pop("group_schedules", None)
        # An empty whitelist means "every table": a schema with nothing selected goes away.
        return not kept_custom and not kept_white and bool(custom or whitelist)

    def final_notes(self, config: Dict[str, Any]) -> List[str]:
        if not self.groups and "groups" in config:
            config.pop("groups", None)
            self.notes.append("groups not imported (plan: groups off)")
        if self.skipped:
            dropped = (f"; schema(s) {', '.join(self.dropped_schemas)} left out" if self.dropped_schemas else "")
            self.notes.append(f"{self.skipped} table(s) not selected by the plan" + dropped)
        return self.notes


def apply_to_yaml(yaml_config, plan: ImportPlan, pipeline: PipelinePlan) -> Tuple[Any, List[str]]:
    """A copy of one backup pipeline YAML with the plan's selection, overrides and skipped parts applied.

    The remap is NOT applied here (create_all_entities applies it once, through the run's options). Returns
    ``(yaml_config, notes)``; the input YAML is returned unchanged when the plan changes nothing in it."""
    udfs, groups = plan.part(pipeline, "udfs"), plan.part(pipeline, "groups")
    if not (pipeline.changes_yaml() or not udfs or not groups):
        return yaml_config, []
    check_names(yaml_config, pipeline)
    selected = selected_ids(yaml_config, pipeline)
    if _plan_keeps_everything(yaml_config, (udfs, groups), pipeline, selected):
        return yaml_config, []
    config = copy.deepcopy(yaml_config)
    editor = _YamlPlanEditor(pipeline, {i.casefold() for i in selected}, udfs, groups)
    root = config["schemas"] if isinstance(config.get("schemas"), dict) else config
    # _schemas_root builds a new dict, so deleting from ``root`` while iterating it is safe.
    for schema, entry in _schemas_root(config).items():
        if editor.edit_schema(schema, entry):
            del root[schema]
            editor.dropped_schemas.append(schema)
    return config, editor.final_notes(config)


# -- agents ---------------------------------------------------------------------------------------------------

def agent_overrides_for(pipeline: PipelinePlan, agent_passwords: Optional[Mapping[str, str]] = None,
                        env: Optional[Mapping[str, str]] = None) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    """``(overrides, env)`` for agent_overrides.apply: the plan's agent edits, with each new password under a
    private env name (never os.environ). ``agent_passwords``: ``{agentTag or agentId: password}`` of one UI
    request; ``env``: where ``passwordRef: "env:NAME"`` is read (os.environ for the CLI)."""
    overrides: Dict[str, Dict[str, Any]] = {}
    private_env: Dict[str, str] = {}
    for ref, entry in pipeline.agents.items():
        fields = {k: v for k, v in entry.items() if k != "passwordRef"}
        password_ref = entry.get("passwordRef")
        if password_ref:
            var = _PASSWORD_REF.match(str(password_ref)).group(1)
            value = (env or {}).get(var)
            if not value:
                raise PlanError(f"pipelines.{pipeline.old_id}.agents.{ref}.passwordRef: ${var} is not set")
            private_env[var] = value
            fields["passwordEnv"] = var
        overrides[ref] = fields
    _refuse_case_only_refs(pipeline, agent_passwords)
    for ref, value in (agent_passwords or {}).items():
        if not value:
            continue
        var = ui_password_env_name(ref)
        private_env[var] = value
        overrides.setdefault(str(ref), {})["passwordEnv"] = var
    return overrides, private_env


def ui_password_env_name(ref: Any) -> str:
    """The private variable an in-memory UI password of agent ``ref`` goes under. A digest of the exact ref keeps
    ``a-b`` / ``a_b`` / ``A-B`` apart (the readable part alone would collide)."""
    digest = hashlib.sha256(str(ref).encode("utf-8")).hexdigest()[:10]
    return f"{UI_PASSWORD_ENV_PREFIX}{re.sub(r'[^A-Za-z0-9]', '_', str(ref)).upper()}_{digest}"


def _refuse_case_only_refs(pipeline: "PipelinePlan", agent_passwords: Optional[Mapping[str, str]]) -> None:
    """Agent refs match case-insensitively, so ``Src`` and ``src`` are one agent: refuse both in one place."""
    for where, refs in (("agents", list(pipeline.agents)),
                        ("agent passwords", [r for r, v in (agent_passwords or {}).items() if v])):
        seen: Dict[str, str] = {}
        for ref in refs:
            other = seen.setdefault(str(ref).casefold(), str(ref))
            if other != str(ref):
                raise PlanError(f"pipelines.{pipeline.old_id}: {where} {other!r} and {ref!r} name the same agent "
                                "(agent refs ignore case); keep one")


def parse_agent_passwords(text: Optional[str]) -> Dict[str, Dict[str, str]]:
    """The ``agent_passwords`` form field: ``{"<backup pipeline id>": {"<agentTag|agentId>": "pw"}}``.

    PlanError on a bad shape; the message never holds a value."""
    if not text or not str(text).strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise PlanError("agent_passwords is not valid JSON") from None
    if not isinstance(data, dict):
        raise PlanError("agent_passwords must be an object")
    out = {}
    for pid, entries in data.items():
        if not isinstance(entries, dict) or not all(isinstance(v, str) for v in entries.values()):
            raise PlanError("agent_passwords must map pipeline id -> {agent: password}")
        out[str(pid)] = {str(k): v for k, v in entries.items() if v}
    return out


def secret_values(agent_passwords: Mapping[str, Mapping[str, str]]) -> List[str]:
    return [v for entries in agent_passwords.values() for v in entries.values() if v]


# -- the draft plan of an archive -----------------------------------------------------------------------------

def draft(index: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The plan JSON a backup's analysis starts from: every pipeline and table selected, defaults spelled out.

    ``index``: the analysed pipelines (``oldId``, ``name``, ``entities`` [ids]); importing the draft unchanged is
    the same as Import All."""
    pipelines = {}
    for item in index:
        pipelines[str(item["oldId"])] = {
            "import": True,
            "name": item.get("name"),
            "entities": {"include": [e["id"] for e in item.get("entities") or []]},
        }
    return {"version": PLAN_VERSION,
            "globals": dict.fromkeys(GLOBALS, True),
            "defaults": {},
            "pipelines": pipelines}


# -- logs -----------------------------------------------------------------------------------------------------

# A secret ``key: value`` / ``key=value`` pair of a log line: the key, then the first value form that matches
# right after it (a double-quoted string, a single-quoted string, else a bare token). Kept as small patterns
# applied in sequence (see _redact_key_values) instead of one large alternation.
_SECRET_KEY = re.compile(r"""(?ix)
    ["']?\b(?:password|passwd|pwd|secret|agent_passwords)\b["']?\s*[:=]\s*""")
_SECRET_VALUE_FORMS = (
    re.compile(r'"(?:[^"\\]|\\.)*"'),
    re.compile(r"'(?:[^'\\]|\\.)*'"),
    re.compile(r"[^\s,;}\]]+"),
)
_REDACTED = "***"


def _secret_value_end(text: str, pos: int) -> Optional[int]:
    """The end of the secret value starting at ``pos``, or None when no value form matches there."""
    for form in _SECRET_VALUE_FORMS:
        match = form.match(text, pos)
        if match:
            return match.end()
    return None


def _redact_key_values(text: str) -> str:
    """Every ``password: value`` (and the other secret keys) of ``text`` with the value replaced by ``***``."""
    out: List[str] = []
    done = 0
    search_from = 0
    while True:
        key = _SECRET_KEY.search(text, search_from)
        if key is None:
            break
        value_end = _secret_value_end(text, key.end())
        if value_end is None:
            search_from = key.start() + 1
            continue
        out.append(text[done:key.end()])
        out.append(_REDACTED)
        done = search_from = value_end
    out.append(text[done:])
    return "".join(out)


class SecretRegistry:
    """The passwords of the imports in progress: their exact values are hidden from every log record."""

    def __init__(self):
        self._values: Dict[int, List[str]] = {}

    def add(self, owner: int, values: Iterable[str]) -> None:
        self._values[owner] = [v for v in values if v and len(v) >= 3]

    def discard(self, owner: int) -> None:
        self._values.pop(owner, None)

    def redact(self, text: str) -> str:
        text = _redact_key_values(text)
        # A snapshot: another import thread may add or discard its values meanwhile.
        snapshot = tuple(self._values.values())
        for values in snapshot:
            for value in values:
                if value in text:
                    text = text.replace(value, _REDACTED)
        return text


SECRETS = SecretRegistry()
