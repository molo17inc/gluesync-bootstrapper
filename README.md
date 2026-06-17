# Copyright notice

This program is part of Gluesync.

Bootstrapper is dual-licensed under the following licenses:

1. GNU General Public License (GPL) Version 3
    You may use, modify, and distribute this software under the terms of the GPL v3.
    See the LICENSE-GPL file or <http://www.gnu.org/licenses/gpl-3.0.html> for details.
    This option is available at no cost, but any derivative works must also be licensed under GPL v3.

2. MOLO17 Commercial License
    Alternatively, you may use this software under the MOLO17 Commercial License,
    which includes a warranty and permits proprietary use. Contact MOLO17 at <info@molo17.com>
    for licensing terms and conditions.

You must choose one of these licenses to use this software. Using this software implies
acceptance of one of these licenses. See the accompanying LICENSE files or contact
MOLO17 for more information.

Copyright (C) 2025 MOLO17. All rights reserved.

# Gluesync Bootstrapper

Gluesync Bootstrapper is a configuration tool for setting up database schema mappings between source and target systems in an automated way, bypassing the need to manually configure entities in the Gluesync CoreHub Web UI. It allows you to define table structures, column mappings, and connection properties using YAML configuration files, enabling precise control over how data is transferred and transformed across different database systems.

## Features

- **Real-time data synchronization**: Between source and target databases
- **Transactions Auditing**: Automated end-to-end tracking of data movement (records processed, duration, metrics) into a dedicated audit table
- **Support for multiple database systems**: MS SQL Server, Couchbase, FTP/FTPS/SFTP/WebDAV/SMB/NFS file storage, etc.
- Configurable filtering and transformation rules
- Customizable document key generation
- Flexible polling intervals and batch processing
- TTL (Time To Live) management for target documents
- Type-safe column mapping and transformation
- Automated scheduling for entities and pipelines via Chronos integration

## Configuration

### Basic Structure

The configuration consists of two main parts:

1. Schema configuration (`table-list-template.yaml`)
2. Agent configuration (`config.json`)
3. Schedule configuration (integrated within the `table-list-template.yaml`)

### Schema Configuration

The schema configuration defines how tables should be synchronized and transformed. Example:

```yaml
dbo: # Source schema
  target: public # Target schema
  customProperties: # Custom properties for both source and target
    source: # Source custom properties
      maxItemsCountPerIteration: 1000
      pollingIntervalMilliseconds: 100
    target: # Target custom properties
      ttlValue: 10000000
  # Pipeline-level schedules (optional)
  schedules:
    - name: "Daily pipeline startup"
      description: "Start the entire pipeline every weekday morning"
      task_type: "pipeline_start"  # pipeline_start, pipeline_stop, pipeline_snapshot
      schedule:
        days_of_week: ["monday", "tuesday", "wednesday", "thursday", "friday"]
        hour: 7
        minute: 0
      enabled: true
  tables: # Tables to be synchronized
    whitelist: # Allowed tables to be synchronized, empty list means all tables
      - DRIVERS
      - VEHICLES
    blacklist: # Forbidden tables to be synchronized, empty list means no restrictions
      - TEST
```

#### Table Configuration

Each table can be configured with:

- Primary keys
- Custom document keys
- Column mappings with types
- Filtering rules for data synchronization
- Snapshot delete filtering rules
- Custom properties

Example table configuration with typed columns:

```yaml
DRIVERS:
  keys: 
    - ID
  documentKey:
    prefix: "TESTPREFIX"
    suffix: "TESTSUFFIX"
    separator: "-"
    keys: 
      - ID
      - LAST_NAME
  name: drivers
  columns:
    - FIRST_NAME: "FIRST_NAME"
    - LAST_NAME: "LAST_NAME"
    - AGE: "AGE"
    - EMAIL: "EMAIL"
  # Entity-level schedules (optional)
  schedules:
    - name: "Daily data snapshot"
      description: "Create snapshot of data daily at midnight"
      task_type: "entity_snapshot"  # entity_start, entity_stop, entity_snapshot
      cron_expression: "0 0 * * *"  # Every day at midnight
      enabled: true
  
  # User Defined Functions (UDF) configuration (optional)
  customProperties:
    target:
      udf:  # Define UDFs to be applied to this table
        - name: "UPPERCASE_NAMES"  # Name of the UDF function (must match the filename without extension)
          type: "Java"  # Currently supports Java or Kotlin
  
  # Filter configuration for data synchronization
  filter:
    clauses:
      - column: "STATUS"
        type: "string"
        operation: "Equal"
        value: "ACTIVE"
  
  # Snapshot delete filter - controls which records are deleted during snapshot
  snapshotDeleteFilter:
    clauses:
      - column: "STATUS"
        type: "string"
        operation: "NotEqual"
        value: "ARCHIVED"
  
  # Snapshot write method - controls how records are written during snapshot
  # UPSERT: Update existing records or insert new ones (default)
  # INSERT: Only insert new records, skip existing ones
  snapshotWriteMethod: "UPSERT"

  # Example with TTL and UDF combined
  customProperties:
    target:
      ttlValue: 20000000  # TTL in milliseconds
      udf:
        - name: "FORMAT_PHONE"
          type: "Kotlin"
```

The column configuration supports:

- Source column name as the key
- Target column name via the `name` property
- Column type via the `type` property
- Automatic type mapping between different database systems

### Entity Naming

Entities created in CoreHub from this YAML are named automatically:

- Single-table entities: `entityName = "<source_schema>.<tableKey>"`
- MultiTable (chained) entities: `entityName = "<source_schema>.<firstTableKeyInChain>"`

You do *not* declare `entityName` explicitly in the YAML.

- The mapping key under `tables.custom` (for example `DRIVERS:` or `VEHICLES:`) is the **source table name** and becomes the tail of the entity name.
- The optional `name:` field inside the table config controls the **target** table/collection name, not the entity name itself.

Example:

```yaml
dbo:
  target: dbo
  tables:
    custom:
      DRIVERS:
        name: drivers
```

Produces:

- Source table: `dbo.DRIVERS`
- Target table/collection: `dbo.drivers`
- CoreHub entity name: `dbo.DRIVERS`

### Groups and group metadata

At the top level you can define a `groups` section that documents group metadata:

```yaml
groups:
  sales:
    description: "Sales entities group"
    color: "#ffabc058"
```

- Keys under `groups` (for example `sales`) are **group names**.
- Table-level `groupId` fields (for example `groupId: "sales"`) tell the bootstrapper which group each entity belongs to.
- When creating **new** groups in CoreHub, `create_all_entities.py` uses the `groups` section (if present) to set the group `description` and `color`.
- If a group with the same name already exists in CoreHub, it is reused and its existing metadata is not overwritten.

The exporter scripts (`export_template_from_corehub.py`, `export_all_pipelines.py`) populate this section from CoreHub when generating backup YAML files.

## Snapshot Delete Filter

The `snapshotDeleteFilter` configuration controls which records are deleted from the target during snapshot synchronization operations. This is particularly useful when you want to preserve certain records in the target system that meet specific criteria, even if they're no longer present in the source.

### How It Works

During a snapshot operation, Gluesync normally deletes all records in the target that don't exist in the source. The `snapshotDeleteFilter` modifies this behavior by excluding records that match the filter criteria from deletion.

### Configuration

The filter is configured using a `clauses` array, where each clause specifies:

- `column`: The name of the column to filter on
- `type`: The data type of the column (string, number, boolean, date)
- `operation`: The comparison operation to perform
- `value`: The value to compare against

### Supported Operations

For string columns:

- `Equal`: Exact match
- `NotEqual`: Does not match
- `Contains`: Contains substring
- `NotContains`: Does not contain substring
- `StartsWith`: Starts with prefix
- `EndsWith`: Ends with suffix

For numeric columns:

- `Equal`: Equals
- `NotEqual`: Not equals
- `GreaterThan`: Greater than
- `GreaterThanOrEqual`: Greater than or equal
- `LessThan`: Less than
- `LessThanOrEqual`: Less than or equal

For boolean columns:

- `Equal`: True or False
- `NotEqual`: Opposite of the specified value

For date columns:

- `Equal`: Exact date match
- `NotEqual`: Not the specified date
- `Before`: Before the specified date
- `After`: After the specified date

### Examples

#### Example 1: Preserve Archived Records

Don't delete records marked as ARCHIVED:

```yaml
snapshotDeleteFilter:
  clauses:
    - column: "STATUS"
      type: "string"
      operation: "Equal"
      value: "ARCHIVED"
```

#### Example 2: Preserve Records Modified After a Date

Keep records modified after January 1, 2024:

```yaml
snapshotDeleteFilter:
  clauses:
    - column: "LAST_MODIFIED"
      type: "date"
      operation: "After"
      value: "2024-01-01"
```

#### Example 3: Multiple Conditions (AND logic)

Preserve records that are both archived AND have high priority:

```yaml
snapshotDeleteFilter:
  clauses:
    - column: "STATUS"
      type: "string"
      operation: "Equal"
      value: "ARCHIVED"
    - column: "PRIORITY"
      type: "number"
      operation: "GreaterThan"
      value: 5
```

### Use Cases

1. **Soft Deletes**: Preserve soft-deleted records in the target system
2. **Historical Data**: Keep historical records that no longer exist in the source
3. **Audit Trail**: Maintain audit records for compliance
4. **Data Archiving**: Preserve archived data while syncing active records
5. **Partial Sync**: Only sync and delete records within a specific date range

## Snapshot Write Method

The `snapshotWriteMethod` configuration controls how records are written to the target during snapshot synchronization operations.

### Available Methods

- **UPSERT** (default): Updates existing records if they exist, otherwise inserts new ones. This ensures the target is an exact mirror of the source.
- **INSERT**: Only inserts new records that don't exist in the target. Existing records are not updated.

### Configuration

Set the `snapshotWriteMethod` at the entity/table level in your YAML configuration:

```yaml
DRIVERS:
  keys: [ID]
  snapshotWriteMethod: "UPSERT"  # Default behavior
  
VEHICLES:
  keys: [VEHICLE_ID]
  snapshotWriteMethod: "INSERT"  # Only add new vehicles, don't update existing
```

### Use Cases

#### UPSERT Mode (Default)

Best for:

- **Full synchronization**: Keep target as an exact copy of source
- **Master data management**: Ensure all changes are propagated
- **Real-time replication**: Maintain consistency between systems

#### INSERT Mode

Best for:

- **Append-only logs**: Historical records that should never be modified
- **Audit trails**: Preserve original entries without updates
- **Time-series data**: Add new data points without changing historical values
- **Incremental loading**: When you only want to add new records

### Interaction with Snapshot Delete Filter

The `snapshotWriteMethod` works in conjunction with `snapshotDeleteFilter`:
- `snapshotWriteMethod` controls HOW records are written (UPSERT vs INSERT)
- `snapshotDeleteFilter` controls WHICH records are preserved from deletion

Example combining both:

```yaml
ORDERS:
  keys: [ORDER_ID]
  # Only insert new orders, don't update existing ones
  snapshotWriteMethod: "INSERT"
  # Don't delete completed orders during snapshot
  snapshotDeleteFilter:
    clauses:
      - column: "STATUS"
        type: "string"
        operation: "Equal"
        value: "COMPLETED"
```

## User Defined Functions (UDFs)

Gluesync supports User Defined Functions (UDFs) for custom data transformation during synchronization. UDFs can be written in Java or Kotlin and are automatically compiled and deployed when entities are created.

### UDF Configuration

UDFs are configured at the table level in the YAML configuration. Each UDF requires:

- `name`: The name of the UDF function (must match the filename without extension)
- `type`: The programming language (Java or Kotlin)

Example configuration:

```yaml
customProperties:
  target:
    udf:
      - name: "UPPERCASE_NAMES"
        type: "Java"
      - name: "FORMAT_PHONE"
        type: "Kotlin"
```

### UDF File Structure

UDF source files should be placed in a directory specified by the `UDF_PATH` environment variable (defaults to current directory). The file must be named exactly as the UDF name with the appropriate extension:

- Java: `.java`
- Kotlin: `.kt`

Example for `UPPERCASE_NAMES.java`:

```java
public class UPPERCASE_NAMES {
    public String transform(String value) {
        return value != null ? value.toUpperCase() : null;
    }
}
```

### Docker Integration

When running in a Docker container:

1. Mount your UDF source directory to `/opt/udfs` in the container
2. Set `UDF_PATH` environment variable if using a different path

Example docker-compose.yml:

```yaml
services:
  gluesync-bootstrapper:
    environment:
      - UDF_PATH=/opt/udfs
    volumes:
      - ./my-udfs:/opt/udfs
```

### Logging

UDF processing logs are written to the standard log file with `UDF` prefix. Look for entries like:

```log
[UDF] Compiling UDF: UPPERCASE_NAMES (Java) from /opt/udfs/UPPERCASE_NAMES.java
[UDF] Successfully compiled UDF: UPPERCASE_NAMES
```

### Best Practices

1. **Naming Conventions**:
   - Use UPPERCASE_UNDERSCORE for UDF names to match database naming conventions
   - Keep UDF names descriptive but concise
   - Match the class name exactly to the UDF name in the configuration

2. **Error Handling**:
   - Always handle null inputs in your UDFs
   - Include input validation for type safety
   - Use try-catch blocks for operations that might fail

3. **Performance**:
   - Keep UDFs lightweight as they're executed for each row
   - Avoid complex computations or external service calls in UDFs
   - Consider caching expensive operations when possible

4. **Testing**:
   - Test UDFs thoroughly before deployment
   - Include edge cases in your tests (null values, empty strings, etc.)
   - Verify behavior with different input types

### Agent Configuration

The agent configuration defines the connection details for source and target databases. Example:

```json
{
  "agents": [
    {
      "agentType": "SOURCE",
      "agentTag": "mssql-ct",
      "hostCredentials": {
        "connectionName": "MS SQL Server source",
        "host": "localhost",
        "port": 1433,
        "databaseName": "demo"
      }
    },
    {
      "agentType": "TARGET",
      "agentTag": "universal-file-store-agent",
      "hostCredentials": {
        "connectionName": "FTP target",
        "host": "ftp.example.com",
        "port": 21,
        "username": "ftpuser",
        "password": "ftppass",
        "protocol": "FTP",
        "enableTls": false,
        "customPath": "/data/exports",
        "fileType": "Parquet"
      }
    }
  ]
}
```

### Universal file store agent

The bootstrapper supports configuring the **Universal file store agent** as a target. This agent enables replication to network file storage endpoints using protocols such as FTP, FTPS, SFTP, WebDAV, SMB, and NFS.

#### Supported protocols

| Protocol | Default port | Description |
| :--- | :--- | :--- |
| FTP | 21 | Standard File Transfer Protocol |
| FTPS | 990 | FTP over implicit TLS |
| SFTP | 22 | SSH File Transfer Protocol |
| WebDAV | 80 | Web-based Distributed Authoring and Versioning |
| WebDAVS | 443 | WebDAV over HTTPS |
| SMB / CIFS | 445 | Server Message Block file sharing |
| NFS | 2049 | Network File System |

#### Supported file formats

| Format | Description |
| :--- | :--- |
| Parquet | Columnar storage optimized for analytics (default) |
| JSON | Line-delimited JSON records |
| CSV | Comma-separated values |

#### Configuration example

```json
{
  "agentType": "TARGET",
  "agentTag": "universal-file-store-agent",
  "hostCredentials": {
    "connectionName": "FTP target",
    "host": "ftp.example.com",
    "port": 21,
    "username": "ftpuser",
    "password": "ftppass",
    "protocol": "FTP",
    "enableTls": false,
    "customPath": "/data/exports",
    "fileType": "Parquet"
  }
}
```

#### Connection fields

- `protocol`: The file transfer protocol. Options: `FTP`, `FTPS`, `SFTP`, `WebDAV`, `WebDAVS`, `SMB`, `NFS`.
- `enableTls`: Enable secure transport. Defaults to `false`.
- `customPath`: Remote root directory for file output. Defaults to `/`.
- `fileType`: Output file format. Options: `Parquet`, `JSON`, `CSV`. Defaults to `Parquet`.

## Getting Started

1. Clone the repository:
```bash
git clone https://gitlab.com/molo17-public/gluesync/gluesync-bootstrapper.git
```

2. Install the required Python packages:
```bash
pip3 install -r requirements.txt --break-system-packages
```

3. Configure your schema in `table-list-template.yaml`
4. Set up your agent configuration in `config.json` (if you want bootstrapper to configure also agents' connection properties)
5. Start the synchronization process

### Before you start

The tool is interactive, that means that it requires you to have a Gluesync deployed and running since it directly talks to the CoreHub API to retrieve all the needed informations from source and target databases involved in the pipeline setup you're configuring.

### Getting a valid authentication token

In order to use the tool you need to get a valid authentication token from the CoreHub API. You can get a valid token in at least 3 ways:

1. By logging in to the CoreHub Web UI and copying the token from the browser's cookies;
2. By looking for the token within the main.py script execution logs if you're automating the process end-to-end;
3. By using an http client like Postman or curl to call the CoreHub API and get a valid token.

In the following example we will show you how to get a valid token using curl:

```bash
curl -X POST https://<corehub-url>/authentication/login -H "Content-Type: application/json" -d '{"username":"<username>","password":"<password>"}'
```

Default username and password are `admin` and `admin`. Keep in mind that at the first login you will be prompted to set a new password, you will need to use that new password to get a valid token.

### Usage of main.py

The `main.py` script is the main entry point for the Gluesync Bootstrapper. It is used to start the synchronization process, creating a new pipeline and starting the agents based on the given config.json file. That config file should contain the connection properties for the source and target databases.

To run the script, use the following command:

```bash
python main.py --config <path_to_config> --token <auth_token> [--pipeline-name NAME] [--skip-errors] [--chunk-size <number>]
```

- `--pipeline-name`: Optional name for the pipeline. If not provided, a random name will be generated.

Alternatively, you can configure the script using environment variables:

```bash
# Set environment variables
export FILE_CONF_PATH="./my-config.json"
export CORE_HUB_URL="https://my-corehub:1717"
export DEFAULT_PASSWORD="my-secure-password"
export SOURCE_TYPE="mssql"
export TARGET_TYPE="couchbase"

# Run the script
python main.py
```

Available environment variables:

- `FILE_CONF_PATH`: Path to the configuration file (default: `./config.json`)
- `CORE_HUB_URL`: URL of the CoreHub service (default: `https://localhost:1717`)
- `DEFAULT_PASSWORD`: Password for authentication (default: `admin`)
- `SOURCE_TYPE`: Source agent type (default: `RDBMS`)
- `TARGET_TYPE`: Target agent type (default: `NoSQL`)

**Note**: Source and target schema information is now automatically read from the `table-list-template.yaml` file in the `schemas` section, eliminating the need for `CREATE_ENTITIES_FROM_SCHEMA` and `TARGET_SCHEMA` environment variables.

### Transactions Auditing

The bootstrapper supports configuring the **Transactions Auditing** feature via YAML. This feature allows you to track every transaction synchronized by Gluesync into a dedicated `GLUESYNC.TRANSACTIONS_AUDIT` table on a target agent of your choice.

#### Configuration

To enable auditing for a pipeline, add the `transactionsAudit` block to your schema configuration in `table-list-template.yaml`:

```yaml
transactionsAudit:
  enabled: true
  targetAgentId: "target-db-alias" # Can be the UUID or the agentTag (alias)
  deepTrace: false                # Optional: log full query statements (default: false)
```

#### Target Agent Resolution

When auditing is enabled, the bootstrapper resolves the `targetAgentId` using the following logic:

1.  **By UUID**: Matches against the unique internal identifier of the agent.
2.  **By Alias (agentTag)**: Matches against the human-readable tag (e.g., `mssql-target`) defined in your configuration.
3.  **Automatic Fallback**: If `targetAgentId` is missing or not found, but the pipeline has exactly **one** target agent, that agent is automatically selected as the audit target.

#### What is Audited?

When enabled, Gluesync records the following for every transaction:
- Timestamp (nanoseconds)
- Pipeline and Entity IDs/Names
- Source and Target table names
- Number of records processed
- End-to-end duration (latency)
- Affected keys
- Detailed performance metrics (JSON)
- Success status and error details (if any)
- Full SQL query (if `deepTrace` is enabled)

The `GLUESYNC.TRANSACTIONS_AUDIT` table is automatically provisioned on the target database during the bootstrap process.

Parameters:

- `--config`: Required. Path to the agent configuration file
- `--token`: Required. Authentication token for API access
- `--skip-errors`: Optional, default is `true`. Continue execution even if errors occur
- `--chunk-size`: Optional. Number of entities to process in each chunk (default: 50)
- `--enable-scheduling`: Optional. Enable creation of schedules from YAML config

Note: The script will create a new pipeline and start the agents based on the given config.json file. It will also perform a chained call to `create_all_entities.py` to create all the entities in the CoreHub if a `table-list-template.yaml` is provided.

### Scheduling with Chronos

The Gluesync Bootstrapper now integrates with Chronos for automated scheduling of pipeline and entity operations. You can define schedules in the `table-list-template.yaml` file at two levels:

1. **Pipeline-level schedules**: Applied to the entire pipeline
2. **Entity-level schedules**: Applied to specific entities

#### Configuring Schedules

Schedules can be configured using either a cron expression or a user-friendly schedule definition:

```yaml
# Using cron expression
schedules:
  - name: "Daily snapshot"
    description: "Create snapshot daily at midnight"
    task_type: "entity_snapshot"  # entity_start, entity_stop, entity_snapshot
    cron_expression: "0 0 * * *"  # Every day at midnight
    enabled: true
    with_snapshot: false  # Optional, default is false

# Using user-friendly schedule
schedules:
  - name: "Weekend refresh"
    description: "Refresh data on weekends"
    task_type: "entity_start"  # entity_start, entity_stop, entity_snapshot
    schedule:
      days_of_week: ["saturday", "sunday"]
      hour: 2
      minute: 0
    with_snapshot: true  # Take snapshot before starting the entity
    enabled: true
```

#### Task Types

Supported task types for pipeline-level schedules:

- `pipeline_start`: Start the entire pipeline
- `pipeline_stop`: Stop the entire pipeline
- `pipeline_snapshot`: Create a snapshot of the entire pipeline
- `pipeline_redo`: Trigger redo (snapshot + CDC restart) for the entire pipeline
- `pipeline_enter_maintenance`: Enter maintenance mode for the pipeline
- `pipeline_exit_maintenance`: Exit maintenance mode for the pipeline

Supported task types for entity-level schedules:

- `entity_start`: Start a specific entity
- `entity_stop`: Stop a specific entity
- `entity_snapshot`: Create a snapshot of a specific entity

#### Maintenance Mode Scheduling

Maintenance mode allows you to schedule when a pipeline enters and exits maintenance mode. This is useful for:

- **Planned maintenance windows**: Schedule regular maintenance periods (e.g., weekends, overnight)
- **System updates**: Automatically enter maintenance mode before system updates
- **Resource management**: Reduce system load during specific time periods
- **Data consistency**: Ensure data consistency during backup or migration operations

**Example: Weekend Maintenance Window**

```yaml
schedules:
  - name: "Weekend maintenance mode - enter"
    description: "Enter maintenance mode every Saturday at midnight"
    task_type: "pipeline_enter_maintenance"
    schedule:
      days_of_week: ["saturday"]
      hour: 0
      minute: 0
    enabled: true
  
  - name: "Weekend maintenance mode - exit"
    description: "Exit maintenance mode every Monday at 6:00 AM"
    task_type: "pipeline_exit_maintenance"
    schedule:
      days_of_week: ["monday"]
      hour: 6
      minute: 0
    enabled: true
```

**Example: Nightly Maintenance Window**

```yaml
schedules:
  - name: "Nightly maintenance - enter"
    description: "Enter maintenance mode every night at 11 PM"
    task_type: "pipeline_enter_maintenance"
    cron_expression: "0 23 * * 1-5"  # Weekdays at 11 PM
    enabled: true
  
  - name: "Nightly maintenance - exit"
    description: "Exit maintenance mode every morning at 6 AM"
    task_type: "pipeline_exit_maintenance"
    cron_expression: "0 6 * * 2-6"  # Tuesday-Saturday at 6 AM
    enabled: true
```

**Note**: When scheduling maintenance mode, ensure that the exit schedule is properly aligned with the enter schedule to avoid leaving the pipeline in maintenance mode indefinitely.

## Advanced Features

### CoreHub Autodiscovery

The bootstrapper includes a CoreHub autodiscovery feature that automatically detects and connects to the CoreHub service without requiring manual configuration of the CoreHub URL. This feature:

- Simplifies deployment by reducing configuration needs
- Makes the system more resilient to network changes
- Falls back to the `CORE_HUB_URL` environment variable if autodiscovery fails
- Provides detailed logging about the discovery process

To override autodiscovery and use a specific CoreHub URL:

```bash
export CORE_HUB_URL="https://your-corehub-instance:1717"
```

### SSL/TLS Support

The bootstrapper supports secure communication via SSL/TLS across all components:

- GluesyncSDK client for WebSocket connections
- CoreHub client for API requests
- Chronos client for scheduling service

SSL features include:

- Automatic URL scheme conversion (HTTP → HTTPS when SSL is enabled)
- Consistent certificate verification settings across all HTTP methods
- Configurable certificate verification (can be disabled in development environments)

Configure SSL using the following environment variables:

```bash
# Enable SSL for all communications
export SSL_ENABLED="true"

# Skip certificate verification (useful in development environments)
export SSL_SKIP_VERIFY="false"
```

When SSL is enabled, all HTTP URLs will be automatically converted to HTTPS.

#### Enabling Scheduling

Scheduling can be enabled in two ways:

1. **Environment Variable**: Set `ENABLE_SCHEDULING=true` (enabled by default)
2. **Command Line Flag**: Use the `--enable-scheduling` flag when running the script

```bash
python create_all_entities.py --pipeline <pipeline_id> --enable-scheduling [...other args]
```

Note: The Chronos service must be running and accessible at the URL specified by the `CHRONOS_URL` environment variable (default: `http://gluesync-chronos:8000`).

### Usage of create_all_entities.py

The `create_all_entities.py` script is used to create all the entities in the CoreHub. It will create the entities based on the given settings from the `table-list-template.yaml`, such as:

- Schemas
- Tables
- Columns definition
- Custom properties

To run the script, use the following command:

```bash
python create_all_entities.py --pipeline <pipeline_id> --source-schema <source_schema> --target-schema <target_schema> --source-type <source_agent_type> --target-type <target_agent_type> --yaml-file <path_to_yaml_config> --token <auth_token> [--skip-errors] [--chunk-size <number>]
```

Parameters:

- `--pipeline`: Required. The ID of the pipeline to create entities for
- `--source-schema`: Required. Source schema name
- `--target-schema`: Required. Target schema name
- `--source-type`: Required. Source agent type (e.g., `RDBMS` for RDBMSs, `NoSQL` for NoSQL databases)
- `--target-type`: Required. Target agent type (e.g., `NoSQL` for NoSQL databases, `RDBMS` for RDBMSs)
- `--yaml-file`: Required. Path to the YAML configuration file
- `--token`: Required. Authentication token for API access
- `--skip-errors`: Optional. Continue execution even if errors occur
- `--chunk-size`: Optional. Number of entities to process in each chunk (default: 50)

Accepted values for `source-type` and `target-type`:
- `RDBMS` (for any RDBMS)
- `NoSQL` (for NoSQL databases, Kafka, AWSS3, etc.)

Available environment variables:
- `CREATE_TABLE_IF_NOT_EXISTS`: Create table if not exists (default: `false`)
- `CORE_HUB_URL`: URL of the CoreHub service (default: `https://localhost:1717`)
- `DEFAULT_PASSWORD`: Password for authentication (default: `admin`)
- `TARGET_TYPE`: Target agent type (default: `NoSQL`)
- `ENTITY_START_TIMEOUT`: Entity start timeout (default: `4`)
- `ENABLE_SCHEDULING`: Enable scheduling (default: `true`)
- `USE_SDK`: Use SDK (default: `true`)
- `SSL_ENABLED`: Enable SSL (default: `false`)
- `SSL_SKIP_VERIFY`: Skip SSL verification (default: `false`)
- `CHRONOS_URL`: URL of the Chronos service (default: `http://gluesync-chronos:8000`)

## Requirements

- Source database system (e.g., MS SQL Server)
- Target database system (e.g., Couchbase)
- Proper network connectivity between source and target systems

## Unlocked Schema Feature

### Overview

The unlocked schema feature allows tables to have different column counts and data types between source and target systems. This is particularly useful when:

- The target has additional columns not present in the source
- Data types need to be transformed differently than standard mappings
- Complex transformations are required that go beyond simple column mappings

### Configuration

To enable unlocked schema for a table, add the `unlockedSchema: true` flag in the table configuration:

```yaml
ARTICLES:
  keys: [ID]
  name: ARTICLES
  unlockedSchema: true  # Enable unlocked schema
  customProperties:
    target:
      # UDF is mandatory for unlocked schema tables
      udf:
        - name: UDF_ARTICLES_TRANSFORM
          type: java
```

### Important Notes

1. **UDF is Mandatory**: When using unlocked schema, a User Defined Function (UDF) is required to handle the data transformation between source and target
2. **Column Mappings**: Column definitions can be omitted or partial when using unlocked schema, as the UDF handles the actual mapping
3. **Validation**: The system will validate that a UDF is defined for any table with `unlockedSchema: true`

### Technical Details

When unlocked schema is enabled:

- The `columnsMappingMatrix` contains a single entry with `sourceColumnId: 0` and `targetColumnId: 0`
- The `tablesWithUnlockedSchema` array includes the target table ID
- Column and data type validation is bypassed, delegating all transformation logic to the UDF

### Example Use Case

Consider a scenario where:

- Source has columns: `ID`, `ARTICLE_NAME`, `DESCRIPTION`
- Target needs columns: `ID`, `CURRENCY` (with `ARTICLE_NAME` and `DESCRIPTION` excluded)

With unlocked schema, the UDF can:

1. Map only the `ID` field
2. Add a default or calculated value for `CURRENCY`
3. Exclude unwanted source columns

This flexibility is not possible with standard locked schema where column counts and types must match.

## Target-Only Columns

### Overview

Target-only columns are fields that exist only in the target system but not in the source. This feature is **only supported with unlocked schema** and allows you to have additional columns in the target that are populated by UDFs.

### Requirements

⚠️ **Important**: Target-only columns require `unlockedSchema: true` to be set for the table. They are not supported with locked schema (default).

### Use Cases

- **Metadata fields**: Adding sync timestamps, processing status, or audit trails
- **Calculated fields**: Derived or computed values not present in source
- **System fields**: Internal identifiers or tracking information
- **Extended attributes**: Additional business data specific to the target system

### Configuration

Target-only columns can be declared using the `targetOnlyColumns` field in your table configuration when `unlockedSchema: true` is set. The feature supports two formats:

#### Simple Format (Recommended)

```yaml
ARTICLES:
  unlockedSchema: true
  targetOnlyColumns:
    - CURRENCY
    - CREATED_AT
    - PROCESSING_STATUS
```

#### Object Format (Required for complete column specification)

```yaml
ARTICLES:
  unlockedSchema: true
  targetOnlyColumns:
    - name: CURRENCY
      type: varchar
      dataLength: 10
      numericPrecision: 0
      numericScale: 0
      isNullable: false
    - name: CREATED_AT
      type: datetime
      dataLength: 19
      numericPrecision: 0
      numericScale: 0
      isNullable: false
```

### Column Discovery Behavior

The system implements intelligent column discovery for target-only columns:

1. **Discovered Columns Priority**: If a target-only column matches an existing discovered column from the target table, the system uses the discovered column's properties (type, dataLength, numericPrecision, numericScale, isNullable) instead of the user-defined ones.

2. **User-Defined Fallback**: If a target-only column is not found in the target table, the system uses the user-defined properties.

3. **Validation**: If a target-only column is not found in the target table and no complete user-defined properties are provided, the system throws an error requiring all properties to be specified.

This behavior ensures that:

- Existing target table structures are respected when possible
- Users have full control when defining new columns
- The system maintains data integrity and consistency

### Example Usage

```yaml
ARTICLES:
  unlockedSchema: true
  targetOnlyColumns:
    - CURRENCY  # Will use discovered properties if column exists, otherwise defaults
    - name: CREATED_AT  # Must specify all properties since column doesn't exist
      type: datetime
      dataLength: 19
      numericPrecision: 0
      numericScale: 0
      isNullable: false
  customProperties:
    target:
      udf:
        - name: UDF_ARTICLES_TRANSFORM
          type: java
```

### Technical Details

- **Column IDs**: Target-only columns receive sequential IDs following the mapped source columns
- **Column Mapping**: In `columnsMappingMatrix`, target-only columns have `sourceColumnId: 0` indicating no source mapping
- **Discovery Priority**: If a target-only column exists in the target table, discovered properties are used over user-defined ones
- **Required Fields**: When using object format, you must specify: `name`, `type`, `dataLength`, `numericPrecision`, `numericScale`, `isNullable`
- **Default Values**: When using simple string format, defaults are: `varchar` type, `dataLength: 1024`, `numericPrecision: 0`, `numericScale: 0`, `isNullable: false`
- **UDF Integration**: UDFs can populate these columns with calculated values or metadata

### Important Notes

1. **Requires Unlocked Schema**: Target-only columns are only supported when `unlockedSchema: true` is set
2. **UDF is Mandatory**: Since target-only columns work only with unlocked schema, a UDF is always required
3. **Target-only columns are added after all source-mapped columns in the target definition**
4. **Required Fields for Object Format**: When using object format, you must specify: `name`, `type`, `dataLength`, `numericPrecision`, `numericScale`, `isNullable`
5. **Discovery Priority**: If a target-only column exists in the target table, the system uses discovered column properties over user-defined ones
6. **These columns can be populated through UDF transformations**
7. **In `columnsMappingMatrix`, target-only columns have `sourceColumnId: 0` to indicate no source mapping**

## Logical Partitions

### Overview

Logical partitions allow Gluesync to break very large tables into deterministic ranges during snapshot operations. When enabled, the bootstrapper automatically asks CoreHub to compute optimal ranges for the specified column and patches the entity configuration with those ranges so that snapshots can execute in parallel.

### Configuration

Declare the partition column under `customProperties.source.partitions`. Two formats are supported:

#### Simple format (defaults to 10 partitions)

```yaml
ORDERS:
  customProperties:
    source:
      partitions: "ORDER_ID"
```

#### Extended format (custom partition count)

```yaml
ORDERS:
  customProperties:
    source:
      partitions:
        column: "ORDER_ID"
        maxPartitionsNumber: 20
```

### Execution Flow

1. The bootstrapper creates the entity without `partitionSettings`.
2. After creation, it retrieves the definitive `entityId` from CoreHub.
3. For each table that declared `partitions`, it calls:
   - `POST /pipelines/{pipelineId}/config/entities/{entityId}/computed-logical-partitions`
   - Body: `{"maxPartitionsNumber": <value>, "column": { id, name, table, type }}`
4. Using the returned `partitions` array (`id`, `startValue`, `endValue`), it issues a second `PUT /config/entities` containing the full `partitionSettings` only for that entity.

If the computation fails or returns an empty list, the bootstrapper logs a warning and, unless `--skip-errors` is set, aborts the run to avoid creating an entity with an invalid configuration.

### Default Maximum

The number of partitions defaults to **10** and can be overridden globally with the `MAX_LOGICAL_PARTITIONS` environment variable:

```bash
export MAX_LOGICAL_PARTITIONS=15
```

Per-table overrides take precedence via the extended YAML format.

### Example Request Sequence

```text
POST /pipelines/{pipelineId}/config/entities/{entityId}/computed-logical-partitions
{ "maxPartitionsNumber": 10, "column": { ... } }

PUT /pipelines/{pipelineId}/config/entities
{ "entities": [ { "entityId": "...", "agentEntities": [... partitionSettings ...] } ] }
```

### Notes

- Only single-table entities support logical partitions at the moment.
- The partition column must exist in the discovery metadata; otherwise the bootstrapper logs a warning and skips the feature for that table.
- Computed ranges are stored in Gluesync so that subsequent snapshots reuse the same configuration without recomputation.

## Support

For support and bug reports, please create an issue in the GitLab repository.

## Contributing

1. Fork the repository
2. Create your feature branch
3. Commit your changes
4. Push to the branch
5. Create a new Merge Request

## DBMoto Metadata Conversion

### Overview
The included `parse_dbmoto_metadata_xml.py` script converts DbMoto metadata XML files into YAML configurations compatible with the Gluesync bootstrapper. It extracts database schemas, tables, and fields from the XML and generates structured YAML files.

### Requirements

- Python 3.9+
- pip3 package manager

## Gluesync Automator (desktop executable)

Gluesync Automator is a lightweight web-based wrapper that simplifies running `create_all_entities.py` without needing to install Python dependencies manually.

### Features

- Cross-platform desktop executable generated via PyInstaller
- Embedded FastAPI server served on `http://localhost:8080`
- Minimal UI to:
  - Authenticate to CoreHub (username/password, TLS toggle, skip verification)
  - Upload YAML configuration files
  - Toggle script options (skip errors, create tables, scheduling)
  - Stream execution logs in real time

### Local development

```bash
pip3 install --break-system-packages -r requirements.txt
python3 run_automator.py --open-browser
```

This launches the UI and opens the browser automatically. The default port is 8080.

### Building executables manually

```bash
pip3 install --break-system-packages pyinstaller
pyinstaller automator.spec --clean --distpath .automator-build/dist --workpath .automator-build/build
```

Outputs will be located under `.automator-build/dist/gluesync-automator/`.

#### macOS-specific build

For a native macOS executable (including VERSION support derived from the latest `automator-*` git tag), run from the repository root:

```bash
pyinstaller automator-macos.spec --clean --distpath .automator-build/dist --workpath .automator-build/build
```

The resulting binary will be available at:

- `.automator-build/dist/gluesync-automator-macos`

### GitLab CI automation

Pushing a tag named `automator-<version>` (for example, `automator-1.0.0`) triggers the `automator_build` job in `.gitlab-ci.yml`. The job:

1. Installs dependencies and PyInstaller
2. Builds the executable using `automator.spec`
3. Publishes the artifact under `.automator-build/dist/`

Artifacts are named `gluesync-automator-<tag>` and retained for two weeks.

### Usage

The DbMoto XML converter supports two usage modes:

### 1. Command Line Interface (CLI)

Run the script directly on your local machine:

#### Basic Usage

```bash
python3 parse_dbmoto_metadata_xml.py /path/to/your/dbmoto_export.xml
```

#### Advanced Usage

```bash
python3 parse_dbmoto_metadata_xml.py /path/to/your/dbmoto_export.xml \
  --output-dir ./output_configs \
  --template ./custom-template.yaml \
  --include-targets \
  --force-schemas "SOURCE_SCHEMA:TARGET_SCHEMA"
```

#### Arguments

- `xml_path` (required): Path to the DbMoto metadata XML file
- `--output-dir`: Directory to save generated YAML files (default: `schemas_yaml`)
- `--template`: Path to template YAML file (optional)
- `--include-targets`: Process target connections (default: `true`)
- `--no-include-targets`: Disable target connection processing
- `--force-schemas`: Override schema mappings

#### Output

- YAML configuration files in the output directory
- `conversion_report.txt` with processing details
- Console output with progress information

### 2. AWS Lambda API

Deploy as a serverless API for programmatic access:

### Notes

- The script automatically handles different naming conventions in the XML
- Missing names will be automatically generated (e.g., `Schema_123`, `Table_456`)
- Field types are converted to lowercase for consistency

### Troubleshooting

- Ensure the XML file is a valid DbMoto metadata export
- Check file permissions for both input and output directories
- Verify that the template file (if specified) is a valid YAML file
- Consult the [DbMoto XML Hierarchy Documentation](dbmoto-converter/dbmoto_xml_hierarchy.md) for understanding the XML structure

### Quick Deploy

1. **Prerequisites:**
   - AWS CLI configured with appropriate permissions
   - Python 3.9+ installed locally

2. **Deploy to AWS:**
   ```bash
   # Navigate to the API directory
   cd dbmoto-converter

   # Deploy to AWS (requires AWS CLI configured)
   ./deploy.sh
   ```

3. **Get the API endpoint:**
   The deployment script will output the API endpoint URL and S3 bucket name.

### GitLab CI/CD

The repository has two separate deployment pipelines:

#### **1. DbMoto Converter (Lambda + WordPress)**

Triggered by tags matching `dbmoto-*` (e.g., `dbmoto-1.0.0`, `dbmoto-v2.1.3`)

```bash
# Deploy Lambda API and WordPress plugin
git tag dbmoto-1.0.0
git push origin dbmoto-1.0.0
```

This triggers:

1. **Lambda function** deployment to AWS (manual approval required)
2. **WordPress plugin** deployment to hosting via FTP (automatic, after Lambda success)

#### **2. Bootstrapper Docker Image**

Triggered by tags matching `tag-*` (e.g., `tag-1.0.0`, `tag-v2.1.3`)

```bash
# Deploy Docker image
git tag tag-1.0.0
git push origin tag-1.0.0
```

#### Required GitLab CI/CD Variables

```
AWS_ACCESS_KEY_ID=your_aws_access_key_id
AWS_SECRET_ACCESS_KEY=your_aws_secret_access_key
FTP_USER=your_ftp_username
FTP_PASSWORD=your_ftp_password
FTP_SITE=c1107198.sgvps.net
```

### WordPress Integration

The API includes WordPress integration options:

#### Option 1: Simple HTML Page

Use the HTML code in `dbmoto-converter/wordpress-integration.html` for quick integration.

#### Option 2: WordPress Plugin

Install the plugin from `dbmoto-converter/wordpress-plugin/` for a full-featured solution with shortcode support.

Both options provide:

- Drag & drop file upload
- Progress indicators
- Direct ZIP file download
- Error handling and validation

#### Python Example

```python
import requests
import zipfile
import io

# Convert XML file
response = requests.post(
    'https://your-endpoint.execute-api.region.amazonaws.com/prod/convert',
    files={'xml_file': open('metadata.xml', 'rb')},
    data={'include_targets': 'true'}
)

result = response.json()
print(f"Generated {result['stats']['yaml_files_generated']} YAML files")

# Download and extract the zip file
zip_response = requests.get(result['stats']['zip_file_url'])
zip_file = zipfile.ZipFile(io.BytesIO(zip_response.content))
zip_file.extractall('conversion_outputs')
print("Files extracted to: conversion_outputs/")
```

#### Advanced Usage

```python
# With custom template and forced schema mappings
response = requests.post(
    'https://your-api-endpoint.execute-api.region.amazonaws.com/prod/convert',
    files={
        'xml_file': open('metadata.xml', 'rb'),
        'template_file': open('custom-template.yaml', 'rb')  # Optional
    },
    data={
        'include_targets': 'true',
        'force_schemas': 'SOURCE_SCHEMA:TARGET_SCHEMA'  # Optional
    }
)
```

### API Response Format

```json
{
  "status": "success",
  "message": "Successfully processed 3 YAML files",
  "request_id": "12345678-1234-1234-1234-123456789012",
  "results": {
    "zip_file": {
      "name": "conversion_outputs_12345678-1234-1234-1234-123456789012.zip",
      "url": "https://s3-url/conversion_outputs_12345678-1234-1234-1234-123456789012.zip",
      "size": 15360
    }
  },
  "stats": {
    "yaml_files_generated": 3,
    "tables_processed": 15,
    "zip_file_url": "https://s3-url/conversion_outputs_12345678-1234-1234-1234-123456789012.zip",
    "zip_file_size": 15360
  }
}
```

### Download Results

The API returns a single zip file containing all generated files:

```bash
# Download the zip file
curl -o conversion_outputs.zip "$ZIP_FILE_URL"

# Extract contents
unzip conversion_outputs.zip
```

### Parameters

- `xml_file` (required): DbMoto XML metadata file
- `template_file` (optional): Custom YAML template file
- `include_targets` (optional): Process target connections (default: `true`)
- `force_schemas` (optional): Override schema mappings (format: `SOURCE:TARGET,SOURCE2:TARGET2`)

### Files and Limits

- **XML file size**: Up to 10MB (API Gateway limit)
- **Processing timeout**: 15 minutes
- **Result URLs**: Valid for 1 hour
- **Output storage**: S3 bucket (automatically created)

### Cost Estimation

- **Lambda**: ~$0.0000002 per request + $0.00001667 per GB-second
- **API Gateway**: ~$3.50 per million requests
- **S3**: ~$0.023 per GB stored + $0.0004 per 1,000 requests
- **Example**: 100 conversions/month ≈ $0.50

### Manual Deployment

If you prefer manual deployment:

```bash
# 1. Navigate to the API directory
cd dbmoto-converter

# 2. Package the Lambda function
pip install -r requirements.txt -t lambda-package/
cp ../parse_dbmoto_metadata_xml.py lambda-package/
cp ../table-list-template-basic.yaml lambda-package/
cd lambda-package && zip -r ../lambda-package.zip .

# 3. Deploy CloudFormation
aws cloudformation create-stack \
  --stack-name dbmoto-xml-converter \
  --template-body file://cloudformation-template.yaml \
  --capabilities CAPABILITY_IAM
```

### Cleanup

To remove the deployment:
```bash
aws cloudformation delete-stack --stack-name dbmoto-xml-converter
```

### Security Considerations

- **Authentication**: Add API Gateway authorizers for production use
- **CORS**: Configure for web applications
- **File validation**: XML content is validated during processing
- **Rate limiting**: Configure API Gateway throttling

## MCP Server (AI Agent Integration)

Gluesync exposes a **Model Context Protocol (MCP) server** that lets any MCP-compatible AI agent (Claude Desktop, Cursor, OpenClaw, custom agents) control CoreHub pipeline operations directly — no manual API calls needed.

### What agents can do

| Tool | Description |
|---|---|
| `list_pipelines` | List all pipelines |
| `get_pipeline` | Full config and agent details |
| `get_pipeline_status` | Health summary (syncing / idle / erroring / paused) |
| `start_entity_sync` | Start sync, optionally with snapshot |
| `stop_entity_sync` | Stop sync for one or all entities |
| `export_pipeline_yaml` | Export pipeline config to YAML |
| `get_notifications` | Recent CoreHub events and errors |
| *(and more…)* | See [`mcp_server/README.md`](mcp_server/README.md) for the full list |

### Quick start — SSE (Automator running)

If the Automator is already running, the MCP server is available immediately:

```
http://localhost:<AUTOMATOR_PORT>/mcp/sse
```

Point your MCP client at that URL. No extra setup required.

### Quick start — stdio (Claude Desktop / CLI)

```json
{
  "mcpServers": {
    "gluesync": {
      "command": "python3",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/path/to/gluesync-bootstrapper",
      "env": {
        "COREHUB_TOKEN": "your-token",
        "CORE_HUB_URL": "https://localhost:1717"
      }
    }
  }
}
```

### Authentication

Pass a CoreHub JWT as the `token` argument to any tool call, or set `COREHUB_TOKEN` in the environment once and omit it from every call.

→ Full documentation, example agent interactions, and all available tools: [`mcp_server/README.md`](mcp_server/README.md)
