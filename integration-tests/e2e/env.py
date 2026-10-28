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
    # Normalize aliases
    if dialect == "pgsql":
        dialect = "postgres"
    if dialect == "mariadb":
        # probe via mysql driver; env still under MARIADB_* when load_db("mariadb")
        pass
    defaults = {
        "mssql": (1433, "demo", "dbo", "mssql-ct"),
        "mysql": (3306, "demo", "", "mysql-cdc"),
        "mariadb": (3307, "demo", "", "mariadb-cdc"),
        "postgres": (15432, "demo", "public", "postgresql-cdc"),
        "oracle": (1521, "XEPDB1", "", "oracle-triggers"),
        "db2": (50000, "DEMO", "", "db2-luw-cdc"),
        "vertica": (5433, "demo", "public", "vertica"),
    }
    if dialect not in defaults:
        return None
    port_d, db_d, sch_d, tag_d = defaults[dialect]
    agent_tag = _get(prefix + "AGENT_TAG") or _get("GS_E2E_TARGET_AGENT_TAG") or tag_d
    return DbEnv(
        dialect=dialect,
        host=host,
        port=int(_get(prefix + "PORT") or port_d),
        database=_get(prefix + "DATABASE") or db_d,
        username=_get(prefix + "USERNAME") or _get(prefix + "USER") or ("gluesync" if dialect != "mssql" else "gluesync"),
        password=password,
        schema=_get(prefix + "SCHEMA") or sch_d,
        agent_tag=agent_tag,
    )


def agent_connect_host(probe_host: Optional[str] = None) -> str:
    """Host agents/Core Hub (in Docker) should use to reach DBs on the Mac.

    Non-loopback kit hosts (e.g. GS_E2E_KAFKA_HOST=kafka after core-hub joins the
    kafka compose network) win so advertised broker DNS resolves. Loopback probe
    hosts are never used — fall through to GS_E2E_AGENT_CONNECT_HOST /
    host.docker.internal.
    """
    loopback = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
    if probe_host and probe_host.strip().lower() not in loopback:
        return probe_host.strip()
    explicit = _get("GS_E2E_AGENT_CONNECT_HOST")
    if explicit:
        return explicit
    for candidate in (_get("GS_E2E_TARGET_HOST"),):
        if candidate and candidate.strip().lower() not in loopback:
            return candidate.strip()
    return "host.docker.internal"


def _dialect_for_agent_tag(tag: str) -> str:
    """Map Core Hub agentTag to env dialect / kit key for load_db / overrides."""
    t = (tag or "").lower()
    if "mysql" in t or "mariadb" in t:
        return "mysql"
    if "postgres" in t or "pgsql" in t or "cockroach" in t or "yugabyte" in t:
        return "postgres"
    if "oracle" in t:
        return "oracle"
    if "db2" in t:
        return "db2"
    if "vertica" in t:
        return "vertica"
    if "couch" in t:
        return "couchbase"
    if "mongo" in t:
        return "mongodb"
    if "dynamo" in t:
        return "dynamodb"
    if "kafka" in t:
        return "kafka"
    if "s3" in t or "awss3" in t:
        return "awss3"
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
    # Prefer kit-registry overrides when both tags map to configured kits
    try:
        src_kit = _kit_for_agent_tag(agents.source_tag)
        tgt_kit = _kit_for_agent_tag(tgt)
        if kit_configured(src_kit) and kit_configured(tgt_kit):
            return overrides_dict_for_pair(src_kit, tgt_kit, agents.source_tag, tgt)
        if tgt_kit == "pgsql" and kit_configured("postgres"):
            return overrides_dict_for_pair(src_kit, "pgsql", agents.source_tag, tgt)
    except Exception:
        pass
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
    # Source follows the source agent tag's dialect (or explicit GS_E2E_SOURCE_*); any source↔target pair.
    source_dialect = _dialect_for_agent_tag(agents.source_tag)
    source_db_env = load_db(source_dialect)
    source_defaults = {"mssql": (1433, "sa"), "mysql": (3306, "gluesync"), "postgres": (15432, "gluesync")}
    src_port_d, src_user_d = source_defaults.get(source_dialect, (1433, "gluesync"))
    source_host = agent_connect_host(
        _get("GS_E2E_SOURCE_HOST") or (source_db_env.host if source_db_env else None) or probe_host
    )
    source_port = int(
        _get("GS_E2E_SOURCE_PORT")
        or (str(source_db_env.port) if source_db_env else None)
        or _get(f"GS_E2E_{source_dialect.upper()}_PORT")
        or str(src_port_d)
    )
    source_user = (
        _get("GS_E2E_SOURCE_USERNAME")
        or (source_db_env.username if source_db_env else None)
        or _get(f"GS_E2E_{source_dialect.upper()}_USERNAME")
        or src_user_d
    )
    source_db = (
        _get("GS_E2E_SOURCE_DATABASE")
        or (source_db_env.database if source_db_env else None)
        or _get(f"GS_E2E_{source_dialect.upper()}_DATABASE")
        or "demo"
    )
    target_entry = {
        "host": host,
        "port": port,
        "database": database,
        "username": username,
        "passwordEnv": target_pw_env,
    }
    # Core Hub 2.3 JDBC: trustServerCertificate false needs a keystore; local kits have none.
    target_entry["trustServerCertificate"] = True
    # MySQL JDBC (mysql-cdc) on Core Hub 2.3 needs allowPublicKeyRetrieval for non-SSL local DBs.
    if dialect == "mysql" or "mysql" in tgt.lower():
        target_entry["customHostCredentials"] = {"allowPublicKeyRetrieval": True}
    source_entry = {
        "host": source_host,
        "port": source_port,
        "database": source_db,
        "username": source_user,
        "passwordEnv": agents.source_password_env,
        "trustServerCertificate": True,
    }
    if source_dialect == "mysql" or "mysql" in agents.source_tag.lower():
        source_entry["customHostCredentials"] = {"allowPublicKeyRetrieval": True}
    return {
        agents.source_tag: source_entry,
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




# ---------------------------------------------------------------------------
# Lab kit registry (generic): GS_E2E_<KIT>_* + agent tags. Missing kit => skip.
# Cassandra / Cosmos / Informix are intentionally absent (offline-only).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KitSpec:
    """One lab kit: env prefix, default agent tags, whether it can be source/target."""
    key: str                 # env prefix fragment, e.g. MSSQL, MYSQL, COUCHBASE
    category: str            # rdbms | nosql | streaming | object_store
    default_source_tag: str  # empty => cannot be source
    default_target_tag: str  # empty => cannot be target
    default_port: int
    default_database: str = "demo"
    default_username: str = "gluesync"
    probe_dialect: str = ""  # mssql|mysql|postgres for db_probe; empty = no direct probe


KIT_SPECS: Dict[str, KitSpec] = {
    "mssql": KitSpec("MSSQL", "rdbms", "mssql-ct", "mssql-cdc", 1433, probe_dialect="mssql"),
    "mssql_cdc": KitSpec("MSSQL_CDC", "rdbms", "mssql-cdc", "mssql-cdc", 1434, probe_dialect="mssql"),
    "mysql": KitSpec("MYSQL", "rdbms", "mysql-cdc", "mysql-cdc", 3306, probe_dialect="mysql"),
    "mariadb": KitSpec("MARIADB", "rdbms", "mariadb-cdc", "mariadb-cdc", 3307, probe_dialect="mysql"),
    "pgsql": KitSpec("PGSQL", "rdbms", "postgresql-cdc", "postgresql-cdc", 15432, probe_dialect="postgres"),
    "postgres": KitSpec("POSTGRES", "rdbms", "postgresql-cdc", "postgresql-cdc", 15432, probe_dialect="postgres"),
    "oracle": KitSpec("ORACLE", "rdbms", "oracle-triggers", "oracle-triggers", 1521, "XEPDB1", "GLUESYNCUSER"),
    "db2": KitSpec("DB2", "rdbms", "db2-luw-cdc", "db2-luw-triggers", 50000, "DEMO", "db2inst1"),
    "vertica": KitSpec("VERTICA", "rdbms", "", "vertica", 5433),  # target-only
    "couchbase": KitSpec("COUCHBASE", "nosql", "couchbase-dcp", "couchbase", 8091, "demo"),
    "mongodb": KitSpec("MONGODB", "nosql", "mongodb", "mongodb", 27017),
    "dynamodb": KitSpec("DYNAMODB", "nosql", "dynamodb", "dynamodb", 8000, default_username=""),
    "kafka": KitSpec("KAFKA", "streaming", "", "kafka", 9092, default_username=""),  # target-only
    "awss3": KitSpec("AWSS3", "object_store", "", "awss3", 9100),  # target-only
}

# Offline-only kits (no live lab): document for matrix skips
OFFLINE_ONLY_KITS = ("cassandra", "cosmos", "informix")


def _kit_env_prefix(kit: str) -> str:
    spec = KIT_SPECS.get(kit)
    return f"GS_E2E_{spec.key}_" if spec else f"GS_E2E_{kit.upper()}_"


def kit_configured(kit: str) -> bool:
    """True when enough GS_E2E_<KIT>_* is set to attempt live overrides for that kit."""
    if kit in OFFLINE_ONLY_KITS:
        return False
    spec = KIT_SPECS.get(kit)
    if not spec:
        return False
    prefix = _kit_env_prefix(kit)
    host = _get(prefix + "HOST")
    if not host:
        # postgres alias: accept PGSQL_* or POSTGRES_*
        if kit == "postgres":
            host = _get("GS_E2E_PGSQL_HOST")
        elif kit == "pgsql":
            host = _get("GS_E2E_POSTGRES_HOST")
    if not host:
        return False
    # Kafka / DynamoDB need no password
    if kit in ("kafka", "dynamodb"):
        return True
    pw = _password_from(prefix + "PASSWORD", prefix + "PASSWORD_ENV")
    if not pw and kit == "postgres":
        pw = _password_from("GS_E2E_PGSQL_PASSWORD", "GS_E2E_PGSQL_PASSWORD_ENV")
    if not pw and kit == "pgsql":
        pw = _password_from("GS_E2E_POSTGRES_PASSWORD", "GS_E2E_POSTGRES_PASSWORD_ENV")
    return bool(pw)


def available_kits(*, role: Optional[str] = None) -> list:
    """Kits with env configured. role=source|target filters by KitSpec capability."""
    out = []
    for key, spec in KIT_SPECS.items():
        if key == "postgres":
            continue  # alias of pgsql — prefer pgsql key in matrices
        if not kit_configured(key) and not (key == "pgsql" and kit_configured("postgres")):
            # pgsql may be configured only via POSTGRES_* 
            if key == "pgsql" and kit_configured("postgres"):
                pass
            elif not kit_configured(key):
                continue
        if role == "source" and not spec.default_source_tag:
            continue
        if role == "target" and not spec.default_target_tag:
            continue
        out.append(key)
    return out


def tag_for_kit(kit: str, role: str) -> str:
    """Resolve agent tag for kit+role from GS_E2E_<KIT>_{SOURCE,TARGET}_AGENT_TAG or defaults."""
    spec = KIT_SPECS[kit]
    role_u = "SOURCE" if role == "source" else "TARGET"
    explicit = _get(f"GS_E2E_{spec.key}_{role_u}_AGENT_TAG")
    if explicit:
        return explicit
    if role == "source":
        return _get(f"GS_E2E_{spec.key}_AGENT_TAG") or spec.default_source_tag
    return (
        _get(f"GS_E2E_{spec.key}_TARGET_AGENT_TAG")
        or _get(f"GS_E2E_{spec.key}_AGENT_TAG")
        or spec.default_target_tag
    )


def _kit_for_agent_tag(tag: str) -> str:
    """Best-effort kit key for an agentTag."""
    t = (tag or "").lower()
    mapping = [
        ("mariadb", "mariadb"),
        ("mysql", "mysql"),
        ("postgres", "pgsql"),
        ("pgsql", "pgsql"),
        ("oracle", "oracle"),
        ("db2", "db2"),
        ("vertica", "vertica"),
        ("couchbase", "couchbase"),
        ("mongo", "mongodb"),
        ("dynamo", "dynamodb"),
        ("kafka", "kafka"),
        ("awss3", "awss3"),
        ("s3", "awss3"),
        ("mssql-cdc", "mssql_cdc"),
        ("mssql", "mssql"),
    ]
    for needle, kit in mapping:
        if needle in t:
            return kit
    return "mssql"


def overrides_entry_for_kit(kit: str, agent_tag: str) -> dict:
    """One agent-overrides entry for a kit (host/port/db/user/passwordEnv + extras)."""
    spec = KIT_SPECS[kit]
    prefix = _kit_env_prefix(kit)
    # Allow POSTGRES_* to fill pgsql
    def g(suffix, default=None):
        v = _get(prefix + suffix)
        if v:
            return v
        if kit == "pgsql":
            return _get("GS_E2E_POSTGRES_" + suffix) or default
        if kit == "postgres":
            return _get("GS_E2E_PGSQL_" + suffix) or default
        return default

    host = agent_connect_host(g("HOST", "127.0.0.1"))
    port = int(g("PORT", str(spec.default_port)))
    database = g("DATABASE") or g("BUCKET") or spec.default_database
    username = g("USERNAME") or g("USER") or spec.default_username
    pw_env = "GS_IMPORT_AGENT_PASSWORD_" + agent_tag.upper().replace("-", "_").replace(".", "_")
    entry = {
        "host": host,
        "port": port,
        "database": database,
        "username": username,
        "passwordEnv": pw_env,
    }
    # Core Hub 2.3 JDBC agents: trustServerCertificate false requires an uploaded keystore
    # (Marco: "Keystore not found, please upload a keystore when using verify server certificate").
    # Local EnvBuilder kits have no keystore, so trust the server cert.
    _jdbc_tls_kits = {
        "mysql", "mariadb", "mssql", "mssql_cdc", "pgsql", "postgres", "oracle",
        "db2", "sap_hana", "sybase", "informix", "vertica",
    }
    if kit in _jdbc_tls_kits:
        entry["trustServerCertificate"] = True
    if kit in ("mysql", "mariadb") or "mysql" in agent_tag or "mariadb" in agent_tag:
        entry["customHostCredentials"] = {"allowPublicKeyRetrieval": True}
    if kit == "oracle":
        service = g("SERVICE") or g("DATABASE") or "XEPDB1"
        entry["database"] = service
        entry.setdefault("customHostCredentials", {})
        # agents often want serviceName
        entry["customHostCredentials"]["serviceName"] = service
    if kit == "couchbase":
        entry["database"] = g("BUCKET") or "demo"
    if kit == "mongodb":
        ch = entry.setdefault("customHostCredentials", {})
        if g("AUTH_DB"):
            ch["authDatabase"] = g("AUTH_DB")
        if g("REPLICA_SET"):
            ch["replicaSet"] = g("REPLICA_SET")
        # EnvBuilder RS member advertises "mongodb:27017"; agents reach the published
        # port via host.docker.internal. directConnection skips RS rediscovery.
        # Core Hub: without it PUT credentials times out
        # ("Coroutine \"jetty-call-handler\" timed out waiting for 10000 ms").
        ch["directConnection"] = True
    if kit == "awss3":
        # Agent builds endpointOverride as f"{host}:{port}" then URI.create — needs a scheme.
        # Core Hub: java.net.URISyntaxException: Expected scheme-specific part at index 21: host.docker.internal:
        scheme = (g("SCHEME") or "http").rstrip(":/")
        if not str(entry["host"]).startswith(("http://", "https://")):
            entry["host"] = f"{scheme}://{entry['host']}"
        ch = entry.setdefault("customHostCredentials", {})
        # AWSS3CustomHostCredentials.region (pathStyle is derived from !amazonaws host)
        ch["region"] = g("REGION") or "us-east-1"
        if g("BUCKET"):
            entry["database"] = g("BUCKET")
    if kit == "kafka":
        # Defaults are SASL_SSL; EnvBuilder kit is PLAINTEXT.
        # Also prefer docker DNS host when GS_E2E_KAFKA_HOST is a non-loopback name
        # (e.g. "kafka" after core-hub is attached to the kafka compose network) so
        # advertised listeners (OUTSIDE://kafka:9092) resolve.
        # Core Hub without PLAINTEXT/disableAuth: TimeoutException listTopics.
        entry["disableAuth"] = True
        entry.pop("passwordEnv", None)
        entry.pop("username", None)
        ch = entry.setdefault("customHostCredentials", {})
        ch["securityProtocol"] = g("SECURITY_PROTOCOL") or "PLAINTEXT"
    if kit == "dynamodb":
        # Agent uses host as AWS *region* and has no endpointOverride field
        # (DynamoDbCustomHostCredentials is empty). Local DynamoDB cannot be targeted;
        # host.docker.internal → UnknownHostException dynamodb.host.docker.internal.amazonaws.com.
        entry.pop("passwordEnv", None)
        if not username:
            entry.pop("username", None)
    return entry



def role_password_env(agent_tag: str, role: str) -> str:
    """Distinct passwordEnv for SOURCE vs TARGET when both agents share one agentTag.

    Tag-only ``GS_IMPORT_AGENT_PASSWORD_<TAG>`` would make ``check_password_vars`` refuse
    (same variable for two agents). Role-suffixed names keep connections independent even
    when the kit password value is identical (MySQL→MySQL).
    """
    base = "GS_IMPORT_AGENT_PASSWORD_" + agent_tag.upper().replace("-", "_").replace(".", "_")
    role_u = "SOURCE" if str(role).upper().startswith("S") else "TARGET"
    return f"{base}_{role_u}"


def overrides_dict_for_pair(source_kit: str, target_kit: str,
                            source_tag: Optional[str] = None,
                            target_tag: Optional[str] = None) -> dict:
    """Overrides for an arbitrary source_kit → target_kit live pair.

    When SOURCE and TARGET share one agentTag (MySQL→MySQL, mssql_cdc→mssql, …), key by
    the synthetic backup agentIds and give each a role-suffixed ``passwordEnv``. Tag-only
    keys would collapse connection fields and trip ``check_password_vars``.
    """
    src_tag = source_tag or tag_for_kit(source_kit, "source")
    tgt_tag = target_tag or tag_for_kit(target_kit, "target")
    src_entry = overrides_entry_for_kit(source_kit, src_tag)
    tgt_entry = overrides_entry_for_kit(target_kit, tgt_tag)
    if src_tag == tgt_tag:
        src_entry = {**src_entry, "passwordEnv": role_password_env(src_tag, "SOURCE")}
        tgt_entry = {**tgt_entry, "passwordEnv": role_password_env(tgt_tag, "TARGET")}
        return {
            SYNTH_SOURCE_AGENT_ID: src_entry,
            SYNTH_TARGET_AGENT_ID: tgt_entry,
        }
    return {
        src_tag: src_entry,
        tgt_tag: tgt_entry,
    }


def ensure_all_kit_passwords_in_environ() -> Dict[str, str]:
    """Copy every GS_E2E_<KIT>_PASSWORD into the matching GS_IMPORT_AGENT_PASSWORD_<TAG>."""
    applied: Dict[str, str] = {}
    for kit, spec in KIT_SPECS.items():
        if kit == "postgres":
            continue
        prefix = _kit_env_prefix(kit)
        pw = _password_from(prefix + "PASSWORD", prefix + "PASSWORD_ENV")
        if not pw and kit == "pgsql":
            pw = _password_from("GS_E2E_POSTGRES_PASSWORD", "GS_E2E_POSTGRES_PASSWORD_ENV")
        if not pw:
            continue
        for role in ("source", "target"):
            tag = tag_for_kit(kit, role) if (spec.default_source_tag if role == "source" else spec.default_target_tag) else ""
            if not tag:
                continue
            env_name = "GS_IMPORT_AGENT_PASSWORD_" + tag.upper().replace("-", "_").replace(".", "_")
            applied[env_name] = pw
            os.environ.setdefault(env_name, pw)
            # Same-tag SOURCE+TARGET pairs use role-suffixed passwordEnv (see overrides_dict_for_pair).
            role_env = role_password_env(tag, role)
            applied[role_env] = pw
            os.environ.setdefault(role_env, pw)
    return applied


# Synthetic identifiers only (RFC 5737 / localhost). Never put real client hosts here.
SAMPLE_HOST = "192.0.2.10"
# agentIds in fixtures/synthetic/synth1_agents-config.yaml (SOURCE / TARGET)
SYNTH_SOURCE_AGENT_ID = "a9e00001"
SYNTH_TARGET_AGENT_ID = "a9e00002"
SAMPLE_HUB = "https://localhost"
SYNTH_PIPELINE_PREFIX = "e2e_synth_"
SYNTH_SCHEMA = "E2E_TGT"
SYNTH_TABLE = "E2E_TAB_001"
