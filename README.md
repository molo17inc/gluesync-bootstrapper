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

Gluesync Bootstrapper is a configuration tool for setting up database schema mappings between source and target systems. It allows you to define table structures, column mappings, and connection properties using YAML configuration files, enabling precise control over how data is transferred and transformed across different database systems.

## Features

- Real-time data synchronization between source and target databases
- Support for multiple database systems (MS SQL Server, Couchbase, etc.)
- Configurable filtering and transformation rules
- Customizable document key generation
- Flexible polling intervals and batch processing
- TTL (Time To Live) management for target documents
- Type-safe column mapping and transformation

## Configuration

### Basic Structure

The configuration consists of two main parts:
1. Schema configuration (`table-list-template.yaml`)
2. Agent configuration (`config.json`)

### Schema Configuration

The schema configuration defines how tables should be synchronized and transformed. Example:

```yaml
schemas:
  dbo:
    target: public
    customProperties:
      source:
        maxItemsCountPerIteration: 1000
        pollingIntervalMilliseconds: 100
      target:
        ttlValue: 10000000
    tables:
      whitelist:
        - DRIVERS
        - VEHICLES
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
```

The column configuration supports:
- Source column name as the key
- Target column name via the `name` property
- Column type via the `type` property
- Automatic type mapping between different database systems

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
git clone https://gitlab.com/molo17srl/products/gluesync/gluesync-bootstrapper.git
```

2. Configure your schema in `table-list-template.yaml`
3. Set up your agent configuration in `config.json`
4. Start the synchronization process

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

## Project Status

Active development
