# DbMoto XML Metadata Hierarchy and Dependencies

## Overview
This document describes the hierarchical structure and relationships between nodes in the DbMoto metadata XML file.

## Node Hierarchy and Relationships

```
metadata (root)
└── tables
    ├── DBMMConnections (Database Connections)
    │   ├── ConnectionID (Primary Key)
    │   ├── Name (Connection name)
    │   ├── Type (Connection type)
    │   ├── IsSource (Y/N - determines if source or target)
    │   ├── ConnectParams
    │   ├── ConnectString
    │   └── Properties
    │
    ├── DBMMSchemas (Database Schemas)
    │   ├── SchemaID (Primary Key)
    │   ├── ConnectionID (Foreign Key -> DBMMConnections.ConnectionID)
    │   ├── Name (Schema name)
    │   └── Properties
    │
    ├── DBMMTables (Database Tables)
    │   ├── TableID (Primary Key)
    │   ├── SchemaID (Foreign Key -> DBMMSchemas.SchemaID)
    │   ├── Name (Table name)
    │   └── Properties
    │
    ├── DBMMFields (Table Fields/Columns)
    │   ├── FieldID (Primary Key)
    │   ├── TableID (Foreign Key -> DBMMTables.TableID)
    │   ├── Name (Field name)
    │   ├── PrimaryKeyPos (0=regular field, >=1=primary key position)
    │   ├── DataType
    │   ├── Size
    │   └── Properties
    │
    ├── DBMMGroups (Replication Groups & Chains)
    │   ├── GroupID (Primary Key)
    │   ├── Name (Group/Chain name)
    │   ├── Type (0=Group, 1=Chain)
    │   └── Description
    │
    └── DBMMReplications (Replication Definitions)
        ├── ReplicationID (Primary Key)
        ├── SrcTableID (Foreign Key -> DBMMTables.TableID)
        ├── TrgTableID (Foreign Key -> DBMMTables.TableID)
        ├── GroupID (Foreign Key -> DBMMGroups.GroupID)
        ├── Name (Replication name)
        ├── ReplMode (4 or 8 - replication mode)
        ├── ReplStatus (Replication status)
        └── Properties (Key-value pairs including GroupPriority)
```

## Dependency Relationships

### Primary Dependencies (Parent -> Child)

1. **DBMMConnections -> DBMMSchemas**
   - One connection contains multiple schemas
   - Linked via: `DBMMSchemas.ConnectionID = DBMMConnections.ConnectionID`

2. **DBMMSchemas -> DBMMTables**
   - One schema contains multiple tables
   - Linked via: `DBMMTables.SchemaID = DBMMSchemas.SchemaID`

3. **DBMMTables -> DBMMFields**
   - One table contains multiple fields
   - Linked via: `DBMMFields.TableID = DBMMTables.TableID`

4. **DBMMGroups -> DBMMReplications**
   - One group/chain contains multiple replications
   - Linked via: `DBMMReplications.GroupID = DBMMGroups.GroupID`

### Cross-References (Many-to-Many)

5. **DBMMTables <-> DBMMReplications**
   - Source relationship: `DBMMReplications.SrcTableID = DBMMTables.TableID`
   - Target relationship: `DBMMReplications.TrgTableID = DBMMTables.TableID`
   - Each replication links a source table to a target table

## Data Flow Path

To trace data flow from source to target:

```
Source Connection (IsSource=Y)
    └── Source Schema
        └── Source Table
            └── Source Fields
                    ↓
                DBMMReplications
                    ↓
            Target Fields
        └── Target Table
    └── Target Schema
└── Target Connection (IsSource=N)
```

## Key Business Rules

1. **Connection Types**:
   - `IsSource=Y`: Source database connections (data originates here)
   - `IsSource=N`: Target database connections (data destinations)

2. **Replication Direction**:
   - All replications flow from source connections to target connections
   - No bi-directional replications detected (no reverse table pairs)

3. **Group Types**:
   - `Type=0`: Regular replication group
   - `Type=1`: Replication chain (ordered sequence)

4. **ReplMode Values**:
   - `ReplMode=4`: Standard replication mode (mirroring?)
   - `ReplMode=8`: Alternative replication mode (bi-directional?)
   - Both modes only replicate Source->Target

5. **Primary Keys**:
   - `PrimaryKeyPos=0`: Regular field (not a primary key)
   - `PrimaryKeyPos>=1`: Primary key field (position in composite key)

## Schema Mapping Logic

To determine source-to-target schema mappings:

1. Start with a replication (`DBMMReplications`)
2. Get source table ID (`SrcTableID`)
3. Find source table's schema via `DBMMTables.SchemaID`
4. Get source schema name via `DBMMSchemas.Name`
5. Get target table ID (`TrgTableID`)
6. Find target table's schema via `DBMMTables.SchemaID`
7. Get target schema name via `DBMMSchemas.Name`
8. Result: Source Schema -> Target Schema mapping

## Special Cases

### Orphaned Schemas
Some schemas may exist in the metadata but have no associated replications:
- Example: SNDDATOS (8 tables, no replications as source or target)

### Multiple Target Mappings
A source schema may map to multiple target schemas:
- Example: ASESP maps to LBDATOS, USDATOS, SCDATOS (multiple warnings)

### Missing Mappings
Schemas without replication mappings fall back to using source schema name as target

## Properties Field Structure

The Properties field in replications contains semicolon-separated key-value pairs:
- ModifiedAt: Modification timestamp
- CreatedBy: User who created the replication
- GroupPriority: Priority within a chain (for Type=1 groups)
- MirroringInterval: Sync timing interval
- CircularBufferSize: Internal buffer size
- ConflictResolver: How to handle conflicts (e.g., SourceServerWins)
