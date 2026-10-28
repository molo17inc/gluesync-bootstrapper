"""Environment for live e2e. Secrets and hostnames come only from env vars.

Designed to run on Marco's Mac against Core Hub at https://localhost (self-signed)
and DBs on localhost ports. Samples in committed files use 192.0.2.x / localhost only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import urlparse


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value


def _truthy(name: str, default: str = "0") -> bool:
    return (_get(name) or default).strip().lower() in ("1", "true", "yes", "on")


def _password_from(direct_var: str, env_name_var: str, *fallbacks: str) -> Optional[str]:
    """Read a password: prefer DIRECT value, else value of the variable named by ENV_NAME_VAR, else fallbacks."""
    direct = _get(direct_var)
    if direct:
        return direct
    named = _get(env_name_var)
    if named:
        return _get(named)
    for fb in fallbacks:
        value = _get(fb)
        if value:
            return value
    return None


@dataclass(frozen=True)
class HubEnv:
    url: str
    username: str
    password: str
    skip_verify: bool
    password_env_name: str  # env var name import_backup should read (--password-env)

    @property
    def host(self) -> str:
        return (urlparse(self.url).hostname or "").lower()

    @property
    def use_ssl(self) -> bool:
        return self.url.strip().lower().startswith("https://")


@dataclass(frozen=True)
class DbEnv:
    """One target dialect for direct CREATE TABLE verification. Skip if incomplete."""
    dialect: str  # mssql | mysql | postgres
    host: str
    port: int
    database: str
    username: str
    password: str
    schema: str
    agent_tag: str  # Core Hub agentTag to override for this dialect


@dataclass(frozen=True)
class AgentEnv:
    """Source/target agent tags and the env var names that hold their passwords."""
    source_tag: str
    target_tag: str
    source_password_env: str
    target_password_env: str


def hub_configured() -> bool:
    url = _get("GS_E2E_HUB_URL") or _get("GS_COREHUB_URL")
    password = _password_from(
        "GS_E2E_HUB_PASSWORD", "GS_E2E_HUB_PASSWORD_ENV",
        "GS_COREHUB_PASSWORD",
    )
    return bool(url and password)


def load_hub() -> HubEnv:
    url = _get("GS_E2E_HUB_URL") or _get("GS_COREHUB_URL")
    if not url:
        raise RuntimeError("GS_E2E_HUB_URL (or GS_COREHUB_URL) is required for live e2e")
    # Default password env name for CLI --password-env (Mac: export GS_COREHUB_PASSWORD=...)
    password_env_name = (
        _get("GS_E2E_HUB_PASSWORD_ENV")
        or ("GS_E2E_HUB_PASSWORD" if _get("GS_E2E_HUB_PASSWORD") else None)
        or "GS_COREHUB_PASSWORD"
    )
    password = _password_from(
        "GS_E2E_HUB_PASSWORD", "GS_E2E_HUB_PASSWORD_ENV",
        "GS_COREHUB_PASSWORD",
    )
    if not password:
        raise RuntimeError(
            "Set GS_E2E_HUB_PASSWORD, or GS_E2E_HUB_PASSWORD_ENV pointing at a var, or GS_COREHUB_PASSWORD"
        )
    username = _get("GS_E2E_HUB_USERNAME") or _get("GS_COREHUB_USERNAME") or "admin"
    # Self-signed localhost: default skip verify ON for e2e
    skip = _truthy("GS_E2E_SKIP_VERIFY", default="1") or _truthy("GS_COREHUB_SKIP_VERIFY", default="0")
    return HubEnv(
        url=url.rstrip("/"),
        username=username,
        password=password,
        skip_verify=skip,
        password_env_name=password_env_name if _get(password_env_name) else (
            "GS_E2E_HUB_PASSWORD" if _get("GS_E2E_HUB_PASSWORD") else "GS_COREHUB_PASSWORD"
        ),
    )


def load_agents() -> AgentEnv:
    """Agent tags and password env-var *names* (values never written to files)."""
    source_tag = _get("GS_E2E_SOURCE_AGENT_TAG") or "ibm-iseries"
    target_tag = _get("GS_E2E_TARGET_AGENT_TAG") or "mssql-ct"
    # Env var NAMES that hold passwords (import_backup / agent_overrides convention)
    source_pw_env = _get("GS_E2E_SOURCE_PASSWORD_ENV") or (
        "GS_IMPORT_AGENT_PASSWORD_" + source_tag.upper().replace("-", "_").replace(".", "_")
    )
    target_pw_env = _get("GS_E2E_TARGET_PASSWORD_ENV") or (
        "GS_IMPORT_AGENT_PASSWORD_" + target_tag.upper().replace("-", "_").replace(".", "_")
    )
    return AgentEnv(
        source_tag=source_tag,
        target_tag=target_tag,
        source_password_env=source_pw_env,
        target_password_env=target_pw_env,
    )


def ensure_agent_passwords_in_environ(agents: Optional[AgentEnv] = None) -> Dict[str, str]:
    """Copy dialect/source passwords into the GS_IMPORT_AGENT_PASSWORD_* names import expects.

    Returns the env mapping applied (names only in keys; values are secrets — caller sets os.environ).
    """
    agents = agents or load_agents()
    applied: Dict[str, str] = {}
    # Prefer explicit import password env; else dialect password; else GS_E2E_SOURCE/TARGET_PASSWORD
    source_pw = _get(agents.source_password_env) or _get("GS_E2E_SOURCE_PASSWORD")
    target_pw = _get(agents.target_password_env) or _get("GS_E2E_TARGET_PASSWORD")
    # Dialect fallbacks for target
    if not target_pw:
        for dialect in ("MSSQL", "MYSQL", "POSTGRES"):
            target_pw = _get(f"GS_E2E_{dialect}_PASSWORD")
            if target_pw:
                break
    if source_pw:
        applied[agents.source_password_env] = source_pw
    if target_pw:
        applied[agents.target_password_env] = target_pw
    return applied


def load_db(dialect: str) -> Optional[DbEnv]:
    """Read GS_E2E_<DIALECT>_* vars. Host may be localhost on Mac. dialect: mssql|mysql|postgres."""
    prefix = f"GS_E2E_{dialect.upper()}_"
    host = _get(prefix + "HOST")
    password = _password_from(prefix + "PASSWORD", prefix + "PASSWORD_ENV")
    if not host or not password:
        return None
    defaults = {
        "mssql": (1433, "master", "dbo", "mssql-ct"),
        "mysql": (3306, "mysql", "", "mysql-cdc"),
        "postgres": (15432, "postgres", "public", "postgresql-cdc"),
    }
    port_d, db_d, sch_d, tag_d = defaults[dialect]
    agent_tag = _get(prefix + "AGENT_TAG") or _get("GS_E2E_TARGET_AGENT_TAG") or tag_d
    return DbEnv(
        dialect=dialect,
        host=host,
        port=int(_get(prefix + "PORT") or port_d),
        database=_get(prefix + "DATABASE") or db_d,
        username=_get(prefix + "USERNAME") or ("sa" if dialect == "mssql" else dialect),
        password=password,
        schema=_get(prefix + "SCHEMA") or sch_d,
        agent_tag=agent_tag,
    )


def agent_connect_host(probe_host: Optional[str] = None) -> str:
    """Host agents/Core Hub (in Docker) should use to reach DBs on the Mac.

    GS_E2E_AGENT_CONNECT_HOST wins. Loopback probe hosts (127.0.0.1 / localhost) are never
    used for agent credentials — agents run in Docker and must use host.docker.internal.
    """
    explicit = _get("GS_E2E_AGENT_CONNECT_HOST")
    if explicit:
        return explicit
    loopback = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
    for candidate in (_get("GS_E2E_TARGET_HOST"), probe_host):
        if candidate and candidate.strip().lower() not in loopback:
            return candidate
    return "host.docker.internal"


def _dialect_for_agent_tag(tag: str) -> str:
    """Map Core Hub agentTag to env dialect key (mssql|mysql|postgres)."""
    t = (tag or "").lower()
    if "mysql" in t:
        return "mysql"
    if "postgres" in t or "pgsql" in t:
        return "postgres"
    return "mssql"


def overrides_dict_for(agents: Optional[AgentEnv] = None, dialect: Optional[str] = None,
                       target_tag: Optional[str] = None) -> dict:
    """Build agent-overrides JSON-serializable dict from env (hosts/ports only; passwordEnv names).

    Keys are always ``agents.source_tag`` / target tag (must match agents-config in the ZIP).
    Connection hosts use ``agent_connect_host`` so Docker agents reach Mac-published DB ports.
    Target dialect is inferred from the target agent tag when ``dialect`` is omitted, so a
    default pair like mssql-ct → mysql-cdc does not put MSSQL ports under the mysql-cdc key.
    """
    agents = agents or load_agents()
    tgt = target_tag or agents.target_tag
    dialect = dialect or _dialect_for_agent_tag(tgt)
    db = load_db(dialect)
    # Prefer dialect DB connection when present; else generic GS_E2E_TARGET_* defaults
    probe_host = (db.host if db else None) or "127.0.0.1"
    host = agent_connect_host(probe_host)
    port = (db.port if db else None) or int(_get("GS_E2E_TARGET_PORT") or {
        "mssql": 1433, "mysql": 3306, "postgres": 15432,
    }.get(dialect, 1433))
    database = (db.database if db else None) or _get("GS_E2E_TARGET_DATABASE") or "demo"
    username = (db.username if db else None) or _get("GS_E2E_TARGET_USERNAME") or (
        "gluesync" if dialect != "mssql" else "sa"
    )
    # Target password env must match the tag key we emit (dialect may swap target tag)
    target_pw_env = (
        "GS_IMPORT_AGENT_PASSWORD_" + tgt.upper().replace("-", "_").replace(".", "_")
        if target_tag else agents.target_password_env
    )
    # Source always from MSSQL (or explicit GS_E2E_SOURCE_*) in this live layout
    source_db_env = load_db("mssql")
    source_host = agent_connect_host(
        _get("GS_E2E_SOURCE_HOST") or (source_db_env.host if source_db_env else None) or probe_host
    )
    source_port = int(
        _get("GS_E2E_SOURCE_PORT")
        or (str(source_db_env.port) if source_db_env else None)
        or _get("GS_E2E_MSSQL_PORT")
        or "1433"
    )
    source_user = (
        _get("GS_E2E_SOURCE_USERNAME")
        or (source_db_env.username if source_db_env else None)
        or _get("GS_E2E_MSSQL_USERNAME")
        or "gluesync"
    )
    source_db = (
        _get("GS_E2E_SOURCE_DATABASE")
        or (source_db_env.database if source_db_env else None)
        or _get("GS_E2E_MSSQL_DATABASE")
        or "demo"
    )
    target_entry = {
        "host": host,
        "port": port,
        "database": database,
        "username": username,
        "passwordEnv": target_pw_env,
    }
    # MySQL JDBC (mysql-cdc) on Core Hub 2.3 needs allowPublicKeyRetrieval for non-SSL local DBs.
    if dialect == "mysql" or "mysql" in tgt.lower():
        target_entry["customHostCredentials"] = {"allowPublicKeyRetrieval": True}
    return {
        agents.source_tag: {
            "host": source_host,
            "port": source_port,
            "database": source_db,
            "username": source_user,
            "passwordEnv": agents.source_password_env,
        },
        tgt: target_entry,
    }


def agents_ready() -> bool:
    """Whether live create-table / full-import should exercise in-hub agents.

    On Core Hub 2.3.1 trial, agents run *inside* the hub (no separate docker pull): they are
    created per pipeline via GET /pipelines/{id}/agents/add. Set GS_E2E_AGENTS_READY=1 for
    that layout (or any hub where agent tags are available). Set =0 to force-skip.
    Default: False so accidental live runs without confirmed agent tags do not hang on
    credential PUT.
    """
    flag = (_get("GS_E2E_AGENTS_READY") or "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return False


# Synthetic identifiers only (RFC 5737 / localhost). Never put real client hosts here.
SAMPLE_HOST = "192.0.2.10"
SAMPLE_HUB = "https://localhost"
SYNTH_PIPELINE_PREFIX = "e2e_synth_"
SYNTH_SCHEMA = "E2E_TGT"
SYNTH_TABLE = "E2E_TAB_001"
