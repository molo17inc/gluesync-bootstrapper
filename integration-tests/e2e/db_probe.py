"""Optional direct DB checks for create-table-if-missing. Drivers are optional; skip if missing/incomplete env."""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

from .env import DbEnv


def _connect(db: DbEnv):
    if db.dialect == "mssql":
        import pymssql
        return pymssql.connect(server=db.host, port=db.port, user=db.username, password=db.password,
                               database=db.database)
    if db.dialect in ("mysql", "mariadb"):
        import pymysql
        return pymysql.connect(host=db.host, port=db.port, user=db.username, password=db.password,
                               database=db.database)
    if db.dialect == "postgres":
        import psycopg2
        return psycopg2.connect(host=db.host, port=db.port, user=db.username, password=db.password,
                                dbname=db.database)
    raise ValueError(f"unknown dialect {db.dialect}")


def table_exists(db: DbEnv, schema: str, table: str) -> bool:
    conn = _connect(db)
    try:
        cur = conn.cursor()
        if db.dialect == "mssql":
            cur.execute(
                "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (schema, table),
            )
        elif db.dialect in ("mysql", "mariadb"):
            cur.execute(
                "SELECT 1 FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                (schema or db.database, table),
            )
        else:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
                (schema or "public", table),
            )
        return cur.fetchone() is not None
    finally:
        conn.close()


def column_names(db: DbEnv, schema: str, table: str) -> Sequence[str]:
    conn = _connect(db)
    try:
        cur = conn.cursor()
        if db.dialect == "mssql":
            cur.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                (schema, table),
            )
        elif db.dialect in ("mysql", "mariadb"):
            cur.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
                (schema or db.database, table),
            )
        else:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                (schema or "public", table),
            )
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def drop_table_if_exists(db: DbEnv, schema: str, table: str) -> None:
    """Cleanup helper for synthetic e2e tables only. Caller must pass synth names."""
    if not table.upper().startswith("E2E_") and not table.upper().startswith("SYNTH"):
        raise RuntimeError(f"refusing to drop non-synthetic table {schema}.{table}")
    conn = _connect(db)
    try:
        cur = conn.cursor()
        if db.dialect == "mssql":
            cur.execute(f"IF OBJECT_ID(N'[{schema}].[{table}]', N'U') IS NOT NULL DROP TABLE [{schema}].[{table}]")
        elif db.dialect in ("mysql", "mariadb"):
            ident_schema = f"`{schema}`." if schema else ""
            cur.execute(f"DROP TABLE IF EXISTS {ident_schema}`{table}`")
        else:
            cur.execute(f'DROP TABLE IF EXISTS "{schema}"."{table}"')
        conn.commit()
    finally:
        conn.close()


def available(db: Optional[DbEnv]) -> Tuple[bool, str]:
    if db is None:
        return False, "env incomplete"
    try:
        __import__({"mssql": "pymssql", "mysql": "pymysql", "mariadb": "pymysql", "postgres": "psycopg2"}[db.dialect])
    except ImportError as exc:
        return False, f"driver missing: {exc}"
    try:
        conn = _connect(db)
        conn.close()
        return True, "ok"
    except Exception as exc:  # pylint: disable=broad-except
        return False, str(exc)


def ensure_schema(db: DbEnv, schema: str) -> None:
    """Create schema if missing (synthetic e2e schemas only)."""
    if not schema or not schema.upper().startswith("E2E"):
        raise RuntimeError(f"refusing to create non-synthetic schema {schema!r}")
    conn = _connect(db)
    try:
        cur = conn.cursor()
        if db.dialect == "mssql":
            # Bracket-quote schema; only called for E2E* names
            cur.execute(
                "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = %s) "
                f"EXEC(N'CREATE SCHEMA [{schema}]')",
                (schema,),
            )
        elif db.dialect in ("mysql", "mariadb"):
            # MySQL "schema" == database; CREATE DATABASE if missing
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{schema}`")
        else:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        conn.commit()
    finally:
        conn.close()


def ensure_source_table(db: DbEnv, schema: str, table: str) -> None:
    """Create a minimal ID/NAME source table for create-table e2e (synth names only)."""
    if not table.upper().startswith("E2E_") and not table.upper().startswith("SYNTH"):
        raise RuntimeError(f"refusing to create non-synthetic table {schema}.{table}")
    ensure_schema(db, schema)
    if table_exists(db, schema, table):
        return
    conn = _connect(db)
    try:
        cur = conn.cursor()
        if db.dialect == "mssql":
            cur.execute(
                f"CREATE TABLE [{schema}].[{table}] ("
                f"[ID] INT NOT NULL PRIMARY KEY, [NAME] VARCHAR(64) NULL)"
            )
        elif db.dialect in ("mysql", "mariadb"):
            sch = schema or db.database
            cur.execute(
                f"CREATE TABLE `{sch}`.`{table}` ("
                f"`ID` INT NOT NULL PRIMARY KEY, `NAME` VARCHAR(64) NULL)"
            )
        else:
            cur.execute(
                f'CREATE TABLE "{schema}"."{table}" ('
                f'"ID" INTEGER PRIMARY KEY, "NAME" VARCHAR(64))'
            )
        conn.commit()
    finally:
        conn.close()
