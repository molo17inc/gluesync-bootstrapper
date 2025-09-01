# Copyright notice

Copyright (c) 2024 MOLO17
Author: Daniele Angeli

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

# Gluesync Bootstrapper

Gluesync Bootstrapper is a configuration tool for setting up database schema mappings between source and target systems in an automated way, bypassing the need to manually configure entities in the Gluesync CoreHub Web UI. It allows you to define table structures, column mappings, and connection properties using YAML configuration files, enabling precise control over how data is transferred and transformed across different database systems.

## Features

- Real-time data synchronization between source and target databases
- Support for multiple database systems (MS SQL Server, Couchbase, etc.)
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
- Filtering rules
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
  
  # Example with TTL and UDF combined
  # customProperties:
  #   target:
  #     ttlValue: 20000000  # TTL in milliseconds
  #     udf:
  #       - name: "FORMAT_PHONE"
  #         type: "Kotlin"
```

The column configuration supports:
- Source column name as the key
- Target column name via the `name` property
- Column type via the `type` property
- Automatic type mapping between different database systems

## User Defined Functions (UDFs)

GlueSync supports User Defined Functions (UDFs) for custom data transformation during synchronization. UDFs can be written in Java or Kotlin and are automatically compiled and deployed when entities are created.

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
    }
  ]
}
```

## Getting Started

1. Clone the repository:
```bash
git clone https://gitlab.com/molo17-public/gluesync/gluesync-bootstrapper.git
```

2. Configure your schema in `table-list-template.yaml`
3. Set up your agent configuration in `config.json` (if you want bootstrapper to configure also agents' connection properties)
4. Start the synchronization process

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
- `SOURCE_TYPE`: Source agent type (default: `SQL`)
- `TARGET_TYPE`: Target agent type (default: `NoSQL`)

**Note**: Source and target schema information is now automatically read from the `table-list-template.yaml` file in the `schemas` section, eliminating the need for `CREATE_ENTITIES_FROM_SCHEMA` and `TARGET_SCHEMA` environment variables.

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

Supported task types for entity-level schedules:
- `entity_start`: Start a specific entity
- `entity_stop`: Stop a specific entity
- `entity_snapshot`: Create a snapshot of a specific entity

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
- `--source-type`: Required. Source agent type (e.g., `SQL` for RDBMSs, `NoSQL` for NoSQL databases)
- `--target-type`: Required. Target agent type (e.g., `NoSQL` for NoSQL databases, `SQL` for RDBMSs)
- `--yaml-file`: Required. Path to the YAML configuration file
- `--token`: Required. Authentication token for API access
- `--skip-errors`: Optional. Continue execution even if errors occur
- `--chunk-size`: Optional. Number of entities to process in each chunk (default: 50)

Accepted values for `source-type` and `target-type`:
- `SQL` (for any RDBMS)
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
The included `parse_dbmoto_metadata_xml.py` script converts DbMoto metadata XML files into YAML configurations compatible with the GlueSync bootstrapper. It extracts database schemas, tables, and fields from the XML and generates structured YAML files.

### Prerequisites
- Python 3.10
- PyYAML package (`pip3 install pyyaml --break-system-packages`)

### Usage

#### Basic Conversion
```bash
python3 parse_dbmoto_metadata_xml.py /path/to/your/dbmoto_export.xml
```

#### Advanced Options
```bash
python3 parse_dbmoto_metadata_xml.py /path/to/your/dbmoto_export.xml \
  --output-dir ./output_configs \
  --template ./custom-template.yaml
```

#### Arguments
- `xml_path` (required): Path to the DbMoto metadata XML file
- `--output-dir`: Directory to save generated YAML files (default: `schemas_yaml`)
- `--template`: Path to a template YAML file (default: `table-list-template-basic.yaml` in script directory)

### Output
The script will:
1. Parse the DbMoto XML file
2. Extract connections, schemas, tables, and fields
3. Generate YAML files in the specified output directory
4. Preserve the hierarchical structure of your database

### Notes
- The script automatically handles different naming conventions in the XML
- Missing names will be automatically generated (e.g., `Schema_123`, `Table_456`)
- Field types are converted to lowercase for consistency
- A summary of extracted items is printed to the console

### Troubleshooting
- Ensure the XML file is a valid DbMoto metadata export
- Check file permissions for both input and output directories
- Verify that the template file (if specified) is a valid YAML file

## DBMoto Metadata Conversion

### Overview
The included `parse_dbmoto_metadata_xml.py` script converts DbMoto metadata XML files into YAML configurations compatible with the GlueSync bootstrapper. It extracts database schemas, tables, and fields from the XML and generates structured YAML files.

### Prerequisites
- Python 3.10
- PyYAML package (`pip3 install pyyaml --break-system-packages`)

### Usage

#### Basic Conversion
```bash
python3 parse_dbmoto_metadata_xml.py /path/to/your/dbmoto_export.xml
```

#### Advanced Options
```bash
python3 parse_dbmoto_metadata_xml.py /path/to/your/dbmoto_export.xml \
  --output-dir ./output_configs \
  --template ./custom-template.yaml
```

#### Arguments
- `xml_path` (required): Path to the DbMoto metadata XML file
- `--output-dir`: Directory to save generated YAML files (default: `schemas_yaml`)
- `--template`: Path to a template YAML file (default: `table-list-template-basic.yaml` in script directory)

### Output
The script will:
1. Parse the DbMoto XML file
2. Extract connections, schemas, tables, and fields
3. Generate YAML files in the specified output directory
4. Preserve the hierarchical structure of your database

### Notes
- The script automatically handles different naming conventions in the XML
- Missing names will be automatically generated (e.g., `Schema_123`, `Table_456`)
- Field types are converted to lowercase for consistency
- A summary of extracted items is printed to the console

### Troubleshooting
- Ensure the XML file is a valid DbMoto metadata export
- Check file permissions for both input and output directories
- Verify that the template file (if specified) is a valid YAML file
