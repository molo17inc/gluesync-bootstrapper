"""
Per-agent data type matrices transcribed from the official GlueSync Kotlin source.

Each entry maps an agent internalName (from agents.json) to a list of matrix entries with:
  - "gluesyncDataType": canonical GlueSync type
  - "defaultType":      default native type for that agent when used as target
  - "supportedTypes":   source types (lowercase) that map to this gluesync type

Use get_matrix_for_agent(agent_tag) to look up the matrix for a given agent.
Use map_source_type_to_target(source_type, source_tag, target_tag) to map a type end-to-end.
"""

# ---------------------------------------------------------------------------
# MySQL / MariaDB
# ---------------------------------------------------------------------------
_MYSQL = [
    {"gluesyncDataType": "SHORT",           "defaultType": "smallint",   "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "int",        "supportedTypes": ["tinyint", "mediumint", "int", "year"]},
    {"gluesyncDataType": "LONG",            "defaultType": "bigint",     "supportedTypes": ["bigint"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "float",      "supportedTypes": ["float"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "double",     "supportedTypes": ["double"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "decimal",    "supportedTypes": ["decimal", "numeric"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "boolean",    "supportedTypes": ["bool", "boolean"]},
    {"gluesyncDataType": "STRING",          "defaultType": "varchar",    "supportedTypes": ["char", "varchar", "text", "tinytext", "mediumtext", "longtext", "enum", "set", "json", "geometry"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "varbinary",  "supportedTypes": ["bit", "binary", "varbinary", "blob", "tinyblob", "mediumblob", "longblob"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",       "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "time",       "supportedTypes": ["time"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "datetime",   "supportedTypes": ["datetime", "timestamp"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "datetime",   "supportedTypes": ["datetime", "timestamp"]},
]

# ---------------------------------------------------------------------------
# MS SQL Server
# ---------------------------------------------------------------------------
_MSSQL = [
    {"gluesyncDataType": "STRING",          "defaultType": "varchar",        "supportedTypes": ["char", "varchar", "text", "nchar", "nvarchar", "ntext", "json", "geography", "geometry", "xml", "uniqueidentifier"]},
    {"gluesyncDataType": "SHORT",           "defaultType": "smallint",       "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "int",            "supportedTypes": ["tinyint", "int"]},
    {"gluesyncDataType": "LONG",            "defaultType": "bigint",         "supportedTypes": ["bigint"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "real",           "supportedTypes": ["real"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "float",          "supportedTypes": ["float"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "decimal",        "supportedTypes": ["decimal", "numeric", "money", "smallmoney"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "bit",            "supportedTypes": ["bit"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",           "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "time",           "supportedTypes": ["time"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "datetime",       "supportedTypes": ["smalldatetime", "datetime2", "datetime"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "datetimeoffset", "supportedTypes": ["datetimeoffset"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "binary",         "supportedTypes": ["binary", "varbinary", "image", "timestamp"]},
]

# ---------------------------------------------------------------------------
# IBM AS/400 (IBM i)
# ---------------------------------------------------------------------------
_AS400 = [
    {"gluesyncDataType": "SHORT",           "defaultType": "smallint",           "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "integer",            "supportedTypes": ["integer"]},
    {"gluesyncDataType": "LONG",            "defaultType": "bigint",             "supportedTypes": ["bigint"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "decimal",            "supportedTypes": ["numeric", "decimal", "decfloat"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "double precision",   "supportedTypes": ["double precision", "double"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "real",               "supportedTypes": ["real", "single precision"]},
    {"gluesyncDataType": "STRING",          "defaultType": "character varying",  "supportedTypes": ["character", "character varying", "national character", "national character varying", "graphic", "xml"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",               "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "time",               "supportedTypes": ["time"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "timestamp",          "supportedTypes": ["timestamp"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "binary large object","supportedTypes": ["character large object", "character for bit data", "character varying for bit data", "binary", "binary varying", "binary large object", "national character large object"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "boolean",            "supportedTypes": ["boolean"]},
]

# ---------------------------------------------------------------------------
# IBM Db2 for LUW
# ---------------------------------------------------------------------------
_DB2_LUW = [
    {"gluesyncDataType": "SHORT",           "defaultType": "smallint",  "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "integer",   "supportedTypes": ["integer", "int"]},
    {"gluesyncDataType": "LONG",            "defaultType": "bigint",    "supportedTypes": ["bigint"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "boolean",   "supportedTypes": ["boolean"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "decimal",   "supportedTypes": ["decimal", "numeric"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "real",      "supportedTypes": ["real"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "double",    "supportedTypes": ["double", "float"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",      "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "time",      "supportedTypes": ["time"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "timestamp", "supportedTypes": ["timestamp"]},
    {"gluesyncDataType": "STRING",          "defaultType": "varchar",   "supportedTypes": ["varchar", "char", "character", "clob", "graphic", "vargraphic", "dbclob", "xml"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "blob",      "supportedTypes": ["blob", "binary", "varbinary", "rowid"]},
]

# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
_POSTGRESQL = [
    {"gluesyncDataType": "SHORT",           "defaultType": "smallint",         "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "integer",          "supportedTypes": ["integer", "int"]},
    {"gluesyncDataType": "LONG",            "defaultType": "bigint",           "supportedTypes": ["bigint", "serial", "smallserial", "bigserial"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "boolean",          "supportedTypes": ["boolean", "bool"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "decimal",          "supportedTypes": ["decimal", "numeric"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "float",            "supportedTypes": ["float", "real"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "double precision", "supportedTypes": ["double precision"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",             "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "timestamp",        "supportedTypes": ["timestamp"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "timestamptz",      "supportedTypes": ["timestamptz"]},
    {"gluesyncDataType": "OFFSET_TIME",     "defaultType": "time",             "supportedTypes": ["time", "timetz"]},
    {"gluesyncDataType": "STRING",          "defaultType": "varchar",          "supportedTypes": ["varchar", "char", "text", "citext", "enum", "json", "jsonb", "uuid", "inet", "geometry", "geography", "box2d", "vector", "string"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "bytea",            "supportedTypes": ["bytea", "bit"]},
]

# ---------------------------------------------------------------------------
# CockroachDB  (PostgreSQL wire-compatible)
# ---------------------------------------------------------------------------
_COCKROACHDB = [
    {"gluesyncDataType": "SHORT",           "defaultType": "smallint",         "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "integer",          "supportedTypes": ["integer", "int"]},
    {"gluesyncDataType": "LONG",            "defaultType": "bigint",           "supportedTypes": ["bigint", "serial", "smallserial", "bigserial"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "boolean",          "supportedTypes": ["boolean", "bool"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "decimal",          "supportedTypes": ["decimal", "numeric"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "float",            "supportedTypes": ["float", "real"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "double precision", "supportedTypes": ["double precision"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",             "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "timestamp",        "supportedTypes": ["timestamp"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "timestamptz",      "supportedTypes": ["timestamptz"]},
    {"gluesyncDataType": "OFFSET_TIME",     "defaultType": "time",             "supportedTypes": ["time", "timetz"]},
    {"gluesyncDataType": "STRING",          "defaultType": "varchar",          "supportedTypes": ["varchar", "char", "text", "citext", "enum", "json", "jsonb", "uuid", "inet", "geometry", "geography", "box2d", "vector", "string"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "bytes",            "supportedTypes": ["bytes", "bit"]},
]

# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------
_ORACLE = [
    {"gluesyncDataType": "SHORT",           "defaultType": "number",                   "supportedTypes": ["number", "smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "number",                   "supportedTypes": ["integer", "int", "number"]},
    {"gluesyncDataType": "LONG",            "defaultType": "number",                   "supportedTypes": ["number"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "number",                   "supportedTypes": ["number", "decimal", "numeric", "float"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "float",                    "supportedTypes": ["float", "binary_float"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "float",                    "supportedTypes": ["float", "binary_double"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "number",                   "supportedTypes": ["number"]},
    {"gluesyncDataType": "STRING",          "defaultType": "varchar2",                 "supportedTypes": ["varchar2", "varchar", "char", "nchar", "nvarchar2", "clob", "nclob", "xmltype"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",                     "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "timestamp",                "supportedTypes": ["timestamp", "date"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "timestamp with time zone", "supportedTypes": ["timestamp with time zone", "timestamp with local time zone"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "raw",                      "supportedTypes": ["raw", "long raw", "blob", "bfile"]},
]

# ---------------------------------------------------------------------------
# SAP Sybase ASE
# ---------------------------------------------------------------------------
_SYBASE = [
    {"gluesyncDataType": "SHORT",           "defaultType": "smallint",  "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "int",       "supportedTypes": ["tinyint", "int"]},
    {"gluesyncDataType": "LONG",            "defaultType": "bigint",    "supportedTypes": ["bigint"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "decimal",   "supportedTypes": ["decimal", "numeric", "money", "smallmoney"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "real",      "supportedTypes": ["real"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "float",     "supportedTypes": ["float", "double precision"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "bit",       "supportedTypes": ["bit"]},
    {"gluesyncDataType": "STRING",          "defaultType": "varchar",   "supportedTypes": ["char", "varchar", "nchar", "nvarchar", "text", "unitext", "unichar", "univarchar"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",      "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "time",      "supportedTypes": ["time"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "datetime",  "supportedTypes": ["datetime", "smalldatetime", "bigdatetime"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "binary",    "supportedTypes": ["binary", "varbinary", "image"]},
]

# ---------------------------------------------------------------------------
# Vertica
# ---------------------------------------------------------------------------
_VERTICA = [
    {"gluesyncDataType": "STRING",          "defaultType": "varchar",                 "supportedTypes": ["varchar", "char", "geometry"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "varbinary",               "supportedTypes": ["varbinary"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "timestamp with time zone","supportedTypes": ["timestamp with time zone"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "timestamp",               "supportedTypes": ["timestamp"]},
    {"gluesyncDataType": "OFFSET_TIME",     "defaultType": "time with timezone",      "supportedTypes": ["time with timezone"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "time",                    "supportedTypes": ["time"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",                    "supportedTypes": ["date"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "numeric",                 "supportedTypes": ["numeric"]},
    {"gluesyncDataType": "INT",             "defaultType": "int",                     "supportedTypes": ["int", "tinyint", "smallint"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "float",                   "supportedTypes": ["float"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "boolean",                 "supportedTypes": ["boolean"]},
    {"gluesyncDataType": "LONG",            "defaultType": "bigint",                  "supportedTypes": ["bigint"]},
]

# ---------------------------------------------------------------------------
# SingleStore
# ---------------------------------------------------------------------------
_SINGLESTORE = [
    {"gluesyncDataType": "SHORT",           "defaultType": "smallint",       "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "int",            "supportedTypes": ["tinyint", "int"]},
    {"gluesyncDataType": "LONG",            "defaultType": "bigint",         "supportedTypes": ["bigint"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "float",          "supportedTypes": ["float"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "decimal",        "supportedTypes": ["decimal", "numeric", "money", "smallmoney", "real"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "bit",            "supportedTypes": ["bit"]},
    {"gluesyncDataType": "STRING",          "defaultType": "varchar",        "supportedTypes": ["varchar", "char", "text", "nchar", "nvarchar", "ntext", "json", "xml"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "varbinary",      "supportedTypes": ["binary", "varbinary", "image", "geography", "geometry"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",           "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "time",           "supportedTypes": ["time"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "datetimeoffset", "supportedTypes": ["datetimeoffset"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "datetime",       "supportedTypes": ["datetime", "datetime2", "smalldatetime", "timestamp"]},
]

# ---------------------------------------------------------------------------
# Google BigQuery
# ---------------------------------------------------------------------------
_BIGQUERY = [
    {"gluesyncDataType": "STRING",          "defaultType": "STRING",    "supportedTypes": ["string", "json"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "BOOLEAN",   "supportedTypes": ["boolean"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "BYTES",     "supportedTypes": ["bytes"]},
    {"gluesyncDataType": "SHORT",           "defaultType": "INT64",     "supportedTypes": ["int64"]},
    {"gluesyncDataType": "INT",             "defaultType": "INT64",     "supportedTypes": ["int64"]},
    {"gluesyncDataType": "LONG",            "defaultType": "INT64",     "supportedTypes": ["int64"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "FLOAT64",   "supportedTypes": ["float64"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "FLOAT64",   "supportedTypes": ["float64"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "NUMERIC",   "supportedTypes": ["numeric", "bignumeric"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "DATE",      "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "TIME",      "supportedTypes": ["time"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "TIMESTAMP", "supportedTypes": ["timestamp", "datetime"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "TIMESTAMP", "supportedTypes": ["timestamp"]},
    {"gluesyncDataType": "MAP",             "defaultType": "STRUCT",    "supportedTypes": ["struct"]},
    {"gluesyncDataType": "ARRAY",           "defaultType": "ARRAY",     "supportedTypes": ["array"]},
]

# ---------------------------------------------------------------------------
# ScyllaDB  (also used for Cassandra-compatible agents)
# ---------------------------------------------------------------------------
_SCYLLADB = [
    {"gluesyncDataType": "STRING",          "defaultType": "text",      "supportedTypes": ["text", "inet", "varchar", "uuid", "timeuuid"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "boolean",   "supportedTypes": ["boolean"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "blob",      "supportedTypes": ["blob"]},
    {"gluesyncDataType": "SHORT",           "defaultType": "smallint",  "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "int",       "supportedTypes": ["int", "tinyint", "varint"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "float",     "supportedTypes": ["float"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "double",    "supportedTypes": ["double"]},
    {"gluesyncDataType": "LONG",            "defaultType": "bigint",    "supportedTypes": ["bigint"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "decimal",   "supportedTypes": ["decimal"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",      "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "time",      "supportedTypes": ["time"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "timestamp", "supportedTypes": ["timestamp"]},
]

# ---------------------------------------------------------------------------
# GridGain
# ---------------------------------------------------------------------------
_GRIDGAIN = [
    {"gluesyncDataType": "STRING",          "defaultType": "varchar",   "supportedTypes": ["varchar", "char"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "varbinary", "supportedTypes": ["varbinary"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "timestamp", "supportedTypes": ["timestamp"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "time",      "supportedTypes": ["time"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",      "supportedTypes": ["date"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "numeric",   "supportedTypes": ["numeric"]},
    {"gluesyncDataType": "SHORT",           "defaultType": "smallint",  "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "int",       "supportedTypes": ["int"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "float",     "supportedTypes": ["float"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "boolean",   "supportedTypes": ["boolean"]},
    {"gluesyncDataType": "LONG",            "defaultType": "bigint",    "supportedTypes": ["bigint"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "double",    "supportedTypes": ["double"]},
]

# ---------------------------------------------------------------------------
# Couchbase
# ---------------------------------------------------------------------------
_COUCHBASE = [
    {"gluesyncDataType": "STRING",          "defaultType": "string",           "supportedTypes": ["string"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "boolean",          "supportedTypes": ["boolean"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "binary",           "supportedTypes": ["binary"]},
    {"gluesyncDataType": "INT",             "defaultType": "int",              "supportedTypes": ["int"]},
    {"gluesyncDataType": "LONG",            "defaultType": "long",             "supportedTypes": ["long", "number"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "float",            "supportedTypes": ["float"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "double",           "supportedTypes": ["double"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "big_decimal",      "supportedTypes": ["big_decimal"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",             "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "time",             "supportedTypes": ["time"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "date_time",        "supportedTypes": ["date_time"]},
    {"gluesyncDataType": "MAP",             "defaultType": "object",           "supportedTypes": ["object"]},
    {"gluesyncDataType": "ARRAY",           "defaultType": "array",            "supportedTypes": ["array"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "offset_date_time", "supportedTypes": ["offset_date_time"]},
]

# ---------------------------------------------------------------------------
# Aerospike
# ---------------------------------------------------------------------------
_AEROSPIKE = [
    {"gluesyncDataType": "STRING",      "defaultType": "string",     "supportedTypes": ["string"]},
    {"gluesyncDataType": "BOOLEAN",     "defaultType": "boolean",    "supportedTypes": ["boolean"]},
    {"gluesyncDataType": "INT",         "defaultType": "int",        "supportedTypes": ["int"]},
    {"gluesyncDataType": "LONG",        "defaultType": "long",       "supportedTypes": ["long"]},
    {"gluesyncDataType": "DOUBLE",      "defaultType": "double",     "supportedTypes": ["double"]},
    {"gluesyncDataType": "FLOAT",       "defaultType": "float",      "supportedTypes": ["float"]},
    {"gluesyncDataType": "BIG_DECIMAL", "defaultType": "decimal",    "supportedTypes": ["decimal"]},
    {"gluesyncDataType": "BYTE_ARRAY",  "defaultType": "byte_array", "supportedTypes": ["byte_array"]},
    {"gluesyncDataType": "ARRAY",       "defaultType": "any_array",  "supportedTypes": ["any_array", "number_array", "string_array", "binary_array"]},
    {"gluesyncDataType": "MAP",         "defaultType": "object",     "supportedTypes": ["object"]},
]

# ---------------------------------------------------------------------------
# Kafka
# ---------------------------------------------------------------------------
_KAFKA = [
    {"gluesyncDataType": "STRING",          "defaultType": "string",      "supportedTypes": ["string"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "boolean",     "supportedTypes": ["boolean"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "binary",      "supportedTypes": ["binary"]},
    {"gluesyncDataType": "SHORT",           "defaultType": "smallint",    "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "int",         "supportedTypes": ["int"]},
    {"gluesyncDataType": "LONG",            "defaultType": "long",        "supportedTypes": ["long"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "float",       "supportedTypes": ["float"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "double",      "supportedTypes": ["double"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "big_decimal", "supportedTypes": ["big_decimal"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",        "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "time",        "supportedTypes": ["time"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "date_time",   "supportedTypes": ["date_time"]},
    {"gluesyncDataType": "ARRAY",           "defaultType": "array",       "supportedTypes": ["array"]},
    {"gluesyncDataType": "MAP",             "defaultType": "map",         "supportedTypes": ["map"]},
]

# ---------------------------------------------------------------------------
# File-based agents: AWS S3 and GCP Storage share the same type space
# ---------------------------------------------------------------------------
_FILE_STORAGE = [
    {"gluesyncDataType": "STRING",          "defaultType": "string",           "supportedTypes": ["string"]},
    {"gluesyncDataType": "BOOLEAN",         "defaultType": "boolean",          "supportedTypes": ["boolean"]},
    {"gluesyncDataType": "BYTE_ARRAY",      "defaultType": "binary",           "supportedTypes": ["binary"]},
    {"gluesyncDataType": "SHORT",           "defaultType": "int",              "supportedTypes": ["smallint"]},
    {"gluesyncDataType": "INT",             "defaultType": "int",              "supportedTypes": ["int"]},
    {"gluesyncDataType": "LONG",            "defaultType": "long",             "supportedTypes": ["long"]},
    {"gluesyncDataType": "FLOAT",           "defaultType": "float",            "supportedTypes": ["float"]},
    {"gluesyncDataType": "DOUBLE",          "defaultType": "double",           "supportedTypes": ["double"]},
    {"gluesyncDataType": "BIG_DECIMAL",     "defaultType": "big_decimal",      "supportedTypes": ["big_decimal"]},
    {"gluesyncDataType": "LOCAL_DATE",      "defaultType": "date",             "supportedTypes": ["date"]},
    {"gluesyncDataType": "LOCAL_TIME",      "defaultType": "time",             "supportedTypes": ["time"]},
    {"gluesyncDataType": "LOCAL_DATE_TIME", "defaultType": "date_time",        "supportedTypes": ["date_time"]},
    {"gluesyncDataType": "OFFSET_DATE_TIME","defaultType": "offset_date_time", "supportedTypes": ["timestamp", "offset_date_time"]},
    {"gluesyncDataType": "OFFSET_TIME",     "defaultType": "offset_time",      "supportedTypes": ["offset_time"]},
    {"gluesyncDataType": "ARRAY",           "defaultType": "array",            "supportedTypes": ["array"]},
    {"gluesyncDataType": "MAP",             "defaultType": "map",              "supportedTypes": ["map"]},
]

# ---------------------------------------------------------------------------
# Master registry  —  key: agent internalName (lowercase, matches agents.json)
# ---------------------------------------------------------------------------
AGENT_MATRICES: dict = {
    # MySQL family
    "mysql-cdc":                  _MYSQL,
    "mysql-triggers":             _MYSQL,
    "mariadb-cdc":                _MYSQL,
    "mariadb-triggers":           _MYSQL,
    # MS SQL Server
    "mssql-cdc":                  _MSSQL,
    "mssql-triggers":             _MSSQL,
    "mssql-log":                  _MSSQL,
    # IBM AS/400
    "as400-journal":              _AS400,
    "as400-triggers":             _AS400,
    # IBM Db2 LUW
    "db2-luw-triggers":           _DB2_LUW,
    "db2-luw-log":                _DB2_LUW,
    # PostgreSQL family
    "postgresql-cdc":             _POSTGRESQL,
    "postgresql-triggers":        _POSTGRESQL,
    # CockroachDB
    "cockroachdb-cdc":            _COCKROACHDB,
    "cockroachdb-triggers":       _COCKROACHDB,
    # Oracle
    "oracle-logminer":            _ORACLE,
    "oracle-triggers":            _ORACLE,
    "oracle-xstream":             _ORACLE,
    # Sybase ASE
    "sybase-ase-triggers":        _SYBASE,
    # Vertica
    "vertica":                    _VERTICA,
    # SingleStore
    "singlestore":                _SINGLESTORE,
    "singlestore-cdc":            _SINGLESTORE,
    # Google BigQuery
    "bigquery":                   _BIGQUERY,
    "google-big-query":           _BIGQUERY,
    # ScyllaDB / Cassandra
    "scylladb":                   _SCYLLADB,
    "cassandra":                  _SCYLLADB,
    # GridGain
    "gridgain":                   _GRIDGAIN,
    # Couchbase
    "couchbase":                  _COUCHBASE,
    # Aerospike
    "aerospike":                  _AEROSPIKE,
    # Kafka
    "kafka":                      _KAFKA,
    # File storage
    "awss3":                      _FILE_STORAGE,
    "aws-s3":                     _FILE_STORAGE,
    "gcpstorage":                 _FILE_STORAGE,
    "gcp-storage":                _FILE_STORAGE,
}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def get_matrix_for_agent(agent_tag: str) -> list:
    """Return the matrix list for *agent_tag* (case-insensitive).

    Falls back to an empty list when the agent is unknown so callers can
    still fall back to the API-provided dataTypesMatrix.
    """
    return AGENT_MATRICES.get(str(agent_tag).lower(), [])


def _source_type_to_gs(source_type: str, source_matrix: list) -> str | None:
    """Resolve a native source type to a GlueSync canonical type name."""
    needle = source_type.lower().strip()
    for entry in source_matrix:
        if needle in [t.lower() for t in entry.get("supportedTypes", [])]:
            return entry["gluesyncDataType"]
    return None


def _gs_type_to_target(gs_type: str, target_matrix: list) -> str | None:
    """Resolve a GlueSync canonical type to the default native target type."""
    for entry in target_matrix:
        if entry.get("gluesyncDataType") == gs_type:
            return entry["defaultType"]
    return None


def map_source_type_to_target(source_type: str, source_tag: str, target_tag: str) -> str | None:
    """Map *source_type* from *source_tag* agent to the equivalent type on *target_tag* agent.

    Returns the mapped target type string, or None if either agent is unknown
    or the source type cannot be resolved.
    """
    source_matrix = get_matrix_for_agent(source_tag)
    target_matrix = get_matrix_for_agent(target_tag)
    if not source_matrix or not target_matrix:
        return None
    gs_type = _source_type_to_gs(source_type, source_matrix)
    if gs_type is None:
        return None
    return _gs_type_to_target(gs_type, target_matrix)
