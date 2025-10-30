# Copyright (c) 2025 MOLO17
# Author: Daniele Angeli
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import xml.etree.ElementTree as ET
import os
import yaml
import argparse
import sys

def parse_arguments():
    parser = argparse.ArgumentParser(description='Parse DbMoto metadata XML and generate YAML configurations.')
    parser.add_argument('xml_path', type=str, help='Path to the DbMoto metadata XML file')
    parser.add_argument('--output-dir', type=str, default='schemas_yaml',
                      help='Directory to save generated YAML files (default: schemas_yaml)')
    template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'table-list-template-basic.yaml')
    parser.add_argument('--template', type=str, 
                      help='Path to template YAML file (optional)',
                      default=template_path)
    parser.add_argument('--include-targets', action='store_const', const=True, default=True,
                    help='Also process schemas from target connections (IsSource=N) (default: True)')
    parser.add_argument('--no-include-targets', action='store_const', dest='include_targets', const=False,
                    help='Do not process schemas from target connections (sets include-targets to False)')
    parser.add_argument('--force-schemas', type=str,
                    help='Force schema mappings (format: SOURCE:TARGET,SOURCE2:TARGET2)')
    return parser.parse_args()

# Parse command line arguments (only if running as CLI script)
if __name__ == "__main__" and len(sys.argv) > 1:
    args = parse_arguments()
else:
    # Create a mock args object for Lambda/serverless context
    class MockArgs:
        def __init__(self):
            self.xml_path = None
            self.output_dir = 'schemas_yaml'
            self.template = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'table-list-template-basic.yaml')
            self.include_targets = True
            self.force_schemas = None
    args = MockArgs()

# Override with environment variables if running in Lambda/serverless environment
XML_PATH = os.environ.get('XML_PATH') or os.path.expanduser(args.xml_path) if args.xml_path else None
OUTPUT_DIR = os.environ.get('OUTPUT_DIR') or args.output_dir
TEMPLATE_PATH = os.environ.get('TEMPLATE_PATH') or args.template
INCLUDE_TARGETS = os.environ.get('INCLUDE_TARGETS', str(args.include_targets)).lower() == 'true'
FORCE_SCHEMAS = os.environ.get('FORCE_SCHEMAS') or args.force_schemas

# Update args object for compatibility with existing code
args.xml_path = XML_PATH
args.output_dir = OUTPUT_DIR
args.template = TEMPLATE_PATH
args.include_targets = INCLUDE_TARGETS
args.force_schemas = FORCE_SCHEMAS

# Global statistics tracking
conversion_stats = {
    'xml_path': XML_PATH,
    'start_time': None,
    'end_time': None,
    'connections': {'source': 0, 'target': 0, 'total': 0},
    'schemas': 0,
    'tables': {'total': 0, 'with_fields': 0, 'with_primary_keys': 0},
    'fields': 0,
    'primary_keys': 0,
    'groups': 0,
    'chains': 0,
    'replications': 0,
    'schema_mappings': 0,
    'yaml_files_exported': 0,
    'tables_exported': 0,
    'errors': [],
    'warnings': []
}

def parse_xml():
    print(f"Parsing XML: {XML_PATH}")
    
    # Parse the XML file
    tree = ET.parse(XML_PATH)
    root = tree.getroot()
    
    # First, extract groups information
    print("Extracting groups and chains...")
    groups = {}
    chains = {}
    
    for group_elem in root.findall("./tables/DBMMGroups"):
        group_id = group_elem.findtext("GroupID")
        group_name = group_elem.findtext("Name")
        group_type = group_elem.findtext("Type")
        
        if group_id and group_name:
            if group_type == "0":  # Regular group
                groups[group_id] = {
                    "name": group_name,
                    "type": "group",
                    "description": group_elem.findtext("Description") or ""
                }
                print(f"  Found group: {group_name} (ID: {group_id})")
            elif group_type == "1":  # Chain
                chains[group_id] = {
                    "name": group_name,
                    "type": "chain",
                    "description": group_elem.findtext("Description") or ""
                }
                print(f"  Found chain: {group_name} (ID: {group_id})")
    
    # Extract replications to track group/chain memberships
    replications = {}
    for repl_elem in root.findall("./tables/DBMMReplications"):
        repl_id = repl_elem.findtext("ReplicationID")
        group_id = repl_elem.findtext("GroupID")
        name = repl_elem.findtext("Name")
        properties = {}
        
        # Parse properties string into a dictionary
        props_text = repl_elem.findtext("Properties", "")
        for prop in props_text.split(';'):
            if '=' in prop:
                key, value = prop.split('=', 1)
                properties[key] = value
        
        if repl_id and group_id and name:
            replications[repl_id] = {
                "name": name,
                "group_id": group_id,
                "src_table_id": repl_elem.findtext("SrcTableID"),
                "trg_table_id": repl_elem.findtext("TrgTableID"),
                "group_priority": int(properties.get('GroupPriority', '0')) if group_id in chains else 0
            }
    
    # Extract database connections and identify source vs target
    print("Extracting connections...")
    connections = {}
    source_connections = set()
    target_connections = set()
    
    for conn_elem in root.findall("./tables/DBMMConnections"):
        conn_id = conn_elem.findtext("ConnectionID")
        
        # Try to get connection name from several possible tags
        conn_name = None
        for name_tag in ["n", "Name", "name"]:
            conn_name = conn_elem.findtext(name_tag)
            if conn_name:
                break
        
        # If no name found, use a default name with the ID
        if not conn_name:
            conn_name = f"Connection_{conn_id}"
        
        # Check if this is a source or target connection
        is_source = conn_elem.findtext("IsSource", "N").upper() == "Y"
        
        if conn_id:
            connections[conn_id] = {
                "id": conn_id,
                "name": conn_name,
                "is_source": is_source,
                "schemas": {}
            }
            
            if is_source:
                source_connections.add(conn_id)
                print(f"  Found SOURCE connection: {conn_name} (ID: {conn_id})")
            else:
                target_connections.add(conn_id)
                print(f"  Found TARGET connection: {conn_name} (ID: {conn_id})")
    
    print(f"Found {len(connections)} database connections ({len(source_connections)} source, {len(target_connections)} target)")
    
    # Extract schemas
    print("Extracting schemas...")
    schemas = {}
    for schema_elem in root.findall("./tables/DBMMSchemas"):
        schema_id = schema_elem.findtext("SchemaID")
        conn_id = schema_elem.findtext("ConnectionID")
        
        # Try to get schema name from several possible tags
        schema_name = None
        for name_tag in ["n", "Name", "name"]:
            schema_name = schema_elem.findtext(name_tag)
            if schema_name:
                break
        
        # If no name found, use a default name with the ID
        if not schema_name:
            schema_name = f"Schema_{schema_id}"
        
        if schema_id and conn_id in connections:
            schema = {
                "id": schema_id,
                "name": schema_name,
                "connection_id": conn_id,
                "tables": {}
            }
            schemas[schema_id] = schema
            # Link schema to connection
            connections[conn_id]["schemas"][schema_id] = schema
            print(f"  Found schema: {schema_name} (ID: {schema_id})")
    
    print(f"Found {len(schemas)} schemas linked to connections")
    
    # Extract tables
    print("Extracting tables...")
    tables = {}
    table_count = 0
    for table_elem in root.findall("./tables/DBMMTables"):
        table_id = table_elem.findtext("TableID")
        schema_id = table_elem.findtext("SchemaID")
        
        # Try to get table name from several possible tags
        table_name = None
        for name_tag in ["n", "Name", "name"]:
            table_name = table_elem.findtext(name_tag)
            if table_name:
                break
        
        # If no name found, use a default name with the ID
        if not table_name:
            table_name = f"Table_{table_id}"
        
        if table_id:
            table = {
                "id": table_id,
                "name": table_name,
                "schema_id": schema_id,
                "fields": []
            }
            tables[table_id] = table
            
            # Link table to schema if possible
            if schema_id and schema_id in schemas:
                schemas[schema_id]["tables"][table_id] = table
                table_count += 1
    
    print(f"Found {table_count} tables linked to schemas (out of {len(tables)} total tables)")
    
    # Extract fields and primary keys
    print("Extracting fields and primary keys...")
    field_count = 0
    primary_key_count = 0
    skipped_composite_keys = 0
    
    for field_elem in root.findall("./tables/DBMMFields"):
        field_id = field_elem.findtext("FieldID")
        table_id = field_elem.findtext("TableID")
        
        # Try to get field name from several possible tags
        field_name = None
        for name_tag in ["n", "Name", "name"]:
            field_name = field_elem.findtext(name_tag)
            if field_name:
                break
        
        # If no name found, use a default name with the ID
        if not field_name:
            field_id = field_elem.findtext("FieldID")
            field_name = f"Field_{field_id}"
        
        # Extract type information
        field_type = field_elem.findtext("Type") or "VARCHAR"
        field_size = field_elem.findtext("Size") or "255"
        field_precision = field_elem.findtext("Precision") or "0"
        field_scale = field_elem.findtext("Scale") or "0"
        
        # Extract primary key position
        primary_key_pos = field_elem.findtext("PrimaryKeyPos")
        
        # Format the type string based on the data type - strip out size/precision specifications
        sql_type = field_type.lower()
        
        # Add the field to its table
        if table_id in tables:
            if "fields" not in tables[table_id]:
                tables[table_id]["fields"] = []
            if "primary_keys" not in tables[table_id]:
                tables[table_id]["primary_keys"] = []
            
            field_data = {
                "name": field_name,
                "type": sql_type
            }
            tables[table_id]["fields"].append(field_data)
            
            # Handle primary key information
            if primary_key_pos is not None:
                try:
                    pk_pos_int = int(primary_key_pos)
                    if pk_pos_int >= 1:
                        # This is a primary key (position >= 1) - handles clustered indexes with multiple PK columns
                        tables[table_id]["primary_keys"].append(field_name)
                        primary_key_count += 1
                        print(f"    Found primary key: {field_name} in table {tables[table_id]['name']} (Table ID: {table_id}) - PrimaryKeyPos={primary_key_pos}")
                    elif pk_pos_int == 0:
                        # This is not a primary key (position 0) - log and skip
                        skipped_composite_keys += 1
                        print(f"    Skipping non-primary key: {field_name} in table {tables[table_id]['name']} (Table ID: {table_id}) - PrimaryKeyPos=0")
                except ValueError:
                    # Handle non-numeric PrimaryKeyPos values
                    print(f"    Warning: Invalid PrimaryKeyPos value '{primary_key_pos}' for field {field_name} in table {tables[table_id]['name']} (Table ID: {table_id})")
        
        field_count += 1
    
    print(f"Found {field_count} fields linked to tables")
    print(f"Found {primary_key_count} primary keys (PrimaryKeyPos>=1)")
    print(f"Skipped {skipped_composite_keys} non-primary key fields (PrimaryKeyPos=0)")
    
    # Print summary of tables with/without fields
    tables_with_fields = sum(1 for t in tables.values() if t["fields"])
    tables_with_primary_keys = sum(1 for t in tables.values() if t.get("primary_keys"))
    print(f"Tables with fields: {tables_with_fields}")
    print(f"Tables without fields: {len(tables) - tables_with_fields}")
    print(f"Tables with primary keys: {tables_with_primary_keys}")
    
    # Build source-to-target schema mapping from replications
    print("\nBuilding source-to-target schema mappings...")
    source_to_target_schemas = {}
    
    for repl_id, repl in replications.items():
        src_table_id = repl['src_table_id']
        trg_table_id = repl['trg_table_id']
        
        if src_table_id and trg_table_id:
            # Find source table's schema
            src_schema_name = None
            src_conn_name = None
            for conn_id, conn in connections.items():
                if conn['is_source']:  # Only look in source connections
                    for schema_id, schema in conn["schemas"].items():
                        if src_table_id in schema["tables"]:
                            src_schema_name = schema["name"]
                            src_conn_name = conn["name"]
                            break
                    if src_schema_name:
                        break
            
            # Find target table's schema
            trg_schema_name = None
            trg_conn_name = None
            for conn_id, conn in connections.items():
                if not conn['is_source']:  # Only look in target connections
                    for schema_id, schema in conn["schemas"].items():
                        if trg_table_id in schema["tables"]:
                            trg_schema_name = schema["name"]
                            trg_conn_name = conn["name"]
                            break
                    if trg_schema_name:
                        break
            
            # Map source schema to target schema
            if src_schema_name and trg_schema_name:
                if src_schema_name not in source_to_target_schemas:
                    source_to_target_schemas[src_schema_name] = trg_schema_name
                    print(f"  Mapped source schema '{src_schema_name}' ({src_conn_name}) -> target schema '{trg_schema_name}' ({trg_conn_name})")
                elif source_to_target_schemas[src_schema_name] != trg_schema_name:
                    # Multiple target schemas for same source - log warning but keep first mapping
                    print(f"  Warning: Source schema '{src_schema_name}' maps to multiple targets: '{source_to_target_schemas[src_schema_name]}' and '{trg_schema_name}'")
    
    print(f"Found {len(source_to_target_schemas)} unique source-to-target schema mappings")
    
    # Print summary of groups and chains
    print(f"\nFound {len(groups)} groups and {len(chains)} chains in the DBMoto configuration")
    print(f"Found {len(replications)} replications with group/chain assignments")
    
    # Update global statistics
    conversion_stats['connections']['source'] = len(source_connections)
    conversion_stats['connections']['target'] = len(target_connections)
    conversion_stats['connections']['total'] = len(connections)
    conversion_stats['schemas'] = len(schemas)
    conversion_stats['tables']['total'] = len(tables)
    conversion_stats['tables']['with_fields'] = tables_with_fields
    conversion_stats['tables']['with_primary_keys'] = tables_with_primary_keys
    conversion_stats['fields'] = field_count
    conversion_stats['primary_keys'] = primary_key_count
    conversion_stats['groups'] = len(groups)
    conversion_stats['chains'] = len(chains)
    conversion_stats['replications'] = len(replications)
    conversion_stats['schema_mappings'] = len(source_to_target_schemas)
    
    return connections, groups, chains, replications, source_to_target_schemas

def export_as_yaml(connections, groups, chains, replications, source_to_target_schemas, output_dir=None, template_file=None):
    # Use command line arguments if parameters are not provided
    if output_dir is None:
        output_dir = args.output_dir
    if template_file is None:
        template_file = args.template
        
    """Generate YAML files for each schema with its tables and fields, matching table-list-template.yaml structure"""
    os.makedirs(output_dir, exist_ok=True)
    exported_count = 0
    
    # Load the template structure to reference
    template_structure = None
    if os.path.exists(template_file):
        with open(template_file, 'r') as f:
            template_content = f.read()
            try:
                template_structure = yaml.safe_load(template_content)
            except Exception as e:
                print(f"Warning: Could not parse template file: {e}")
    
    # Create a mapping of table IDs to their group/chain assignments from replications
    table_assignments = {}
    
    # Assign groups and chains to tables
    for repl_id, repl in replications.items():
        group_id = repl['group_id']
        table_id = repl['src_table_id']  # Using source table ID for assignment
        
        if group_id in groups:
            if table_id not in table_assignments:
                table_assignments[table_id] = {}
            table_assignments[table_id]['groupId'] = groups[group_id]['name']
            
        elif group_id in chains:
            if table_id not in table_assignments:
                table_assignments[table_id] = {}
            table_assignments[table_id]['chainId'] = chains[group_id]['name']
    
    # Extract schemas from template if available
    template_schemas = {}
    if template_structure and 'schemas' in template_structure:
        template_schemas = {name.lower(): schema for name, schema in template_structure['schemas'].items()}
    
    # Function to find best matching template schema
    def find_matching_template_schema(schema_name):
        # First try exact match
        if schema_name.lower() in template_schemas:
            return template_schemas[schema_name.lower()]
        
        # Try partial match (case insensitive)
        for template_name, schema in template_schemas.items():
            if schema_name.lower() in template_name.lower() or template_name.lower() in schema_name.lower():
                return schema
        
        # No match found, return None
        return None
    
    # Process connections and schemas based on mode
    # Check for manual override schemas
    manual_overrides = {}
    if args.force_schemas:
        # Parse format: SOURCE_SCHEMA:TARGET_SCHEMA,SOURCE_SCHEMA2:TARGET_SCHEMA2
        for mapping in args.force_schemas.split(','):
            if ':' in mapping:
                src, tgt = mapping.split(':', 1)
                manual_overrides[src.strip()] = tgt.strip()
                print(f"Manual override: {src.strip()} -> {tgt.strip()}")
    
    # Process each connection and schema
    for conn in connections.values():
        # Skip target connections UNLESS we have manual overrides or --include-targets flag
        if not conn["is_source"] and not args.include_targets and not manual_overrides:
            continue
            
        conn_name = conn["name"]
        for schema in conn["schemas"].values():
            schema_name = schema["name"]
            
            # Process tables with fields
            tables_with_fields = {}
            for table in schema["tables"].values():
                if table["fields"]:  # Only include tables with fields
                    tables_with_fields[table["name"]] = table
            
            # Only export if there are tables with fields
            if tables_with_fields:
                # Create whitelist of all table names
                whitelist = list(tables_with_fields.keys())
                
                # Create custom table definitions with column details
                custom_tables = {}
                for table_name, table in tables_with_fields.items():
                    # Convert field list to column definitions matching template format
                    columns = []
                    for field in table["fields"]:
                        # Each column is a simple key-value pair: source_name -> target_name
                        column_entry = {
                            field["name"]: field["name"]  # Simple mapping: source -> target
                        }
                        columns.append(column_entry)
                    
                    # Create the table entry with column definitions
                    table_config = {
                        "name": table_name,  # Preserve original case
                        "columns": columns
                    }
                    
                    # Add primary keys if found, otherwise let engine autodiscover
                    if table.get("primary_keys"):
                        # Use simple string array format to match template
                        table_config["keys"] = table["primary_keys"]
                        print(f"      Added {len(table['primary_keys'])} primary key(s) to table {table_name}: {table['primary_keys']}")
                    else:
                        # No primary keys found - let engine autodiscover them
                        print(f"      No primary keys found for table {table_name}, will be autodiscovered by engine")
                    
                    # Add group/chain assignments if this table is in any replication
                    if table["id"] in table_assignments:
                        table_config.update(table_assignments[table["id"]])
                    
                    custom_tables[table_name] = table_config
                
                # Find matching template for this schema
                template_schema = find_matching_template_schema(schema_name)
                
                # Determine target schema using multiple sources in priority order:
                # 1. Manual override (command line)
                # 2. Template file (if exists and has target defined)
                # 3. Schema-level replication mapping (from source_to_target_schemas)
                # 4. Source schema name as fallback
                target_schema = None
                if schema_name in manual_overrides:
                    target_schema = manual_overrides[schema_name]
                    print(f"      Using target schema '{target_schema}' from manual override")
                elif template_schema and template_schema.get('target'):
                    target_schema = template_schema['target']
                    print(f"      Using target schema '{target_schema}' from template")
                elif schema_name in source_to_target_schemas:
                    target_schema = source_to_target_schemas[schema_name]
                    print(f"      Using target schema '{target_schema}' from replication mapping")
                else:
                    # No replication mapping found, use source schema as fallback
                    target_schema = schema_name
                    print(f"      Using source schema '{target_schema}' as fallback (no replication mapping found)")
                custom_props = template_schema.get('customProperties', {}) if template_schema else {}
                schedules = template_schema.get('schedules', []) if template_schema else []
                
                # Create the schema structure that matches table-list-template.yaml
                # Use direct schema name as root key (no 'schemas' wrapper)
                yaml_data = {
                    schema_name: {
                        "target": target_schema,
                        "tables": {
                            "whitelist": whitelist,
                            "custom": custom_tables
                        }
                    }
                }
                
                # Only add customProperties if they exist and are not empty
                if custom_props:
                    yaml_data[schema_name]["customProperties"] = custom_props
                    
                # Only add schedules if they exist and are not empty
                if schedules:
                    yaml_data[schema_name]["schedules"] = schedules
                
                # Create filename and write YAML
                filename = f"{conn_name}__{schema_name}.yaml".replace("/", "_")
                filepath = os.path.join(output_dir, filename)
                
                with open(filepath, "w") as f:
                    # Write header comment with source database information
                    from datetime import datetime
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"# Generated from DbMoto metadata XML\n")
                    f.write(f"# Conversion timestamp: {timestamp}\n")
                    f.write(f"# Source database connection: {conn_name}\n")
                    f.write(f"# Schema: {schema_name}\n")
                    f.write(f"# Tables converted: {len(whitelist)}\n\n")
                    
                    yaml.dump(yaml_data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
                
                exported_count += 1
                conversion_stats['tables_exported'] += len(whitelist)
                print(f"Exported: {filename} ({len(whitelist)} tables)")
            else:
                print(f"Skipped: {conn_name}.{schema_name} (no tables with fields)")
    
    conversion_stats['yaml_files_exported'] = exported_count
    return exported_count

def write_conversion_report(output_dir=None):
    """Write a comprehensive conversion report to conversion_report.txt"""
    if output_dir is None:
        output_dir = args.output_dir
    
    from datetime import datetime
    
    # Calculate duration
    duration = None
    if conversion_stats['start_time'] and conversion_stats['end_time']:
        duration = conversion_stats['end_time'] - conversion_stats['start_time']
    
    report_path = os.path.join(output_dir, 'conversion_report.txt')
    
    with open(report_path, 'w') as f:
        f.write("=" * 80 + "\n")
        f.write("DbMoto to Gluesync YAML Conversion Report\n")
        f.write("=" * 80 + "\n\n")
        
        # Conversion metadata
        f.write("CONVERSION METADATA\n")
        f.write("-" * 80 + "\n")
        f.write(f"Source XML File: {conversion_stats['xml_path']}\n")
        f.write(f"Output Directory: {os.path.abspath(output_dir)}\n")
        f.write(f"Template File: {args.template}\n")
        f.write(f"Start Time: {conversion_stats['start_time'].strftime('%Y-%m-%d %H:%M:%S') if conversion_stats['start_time'] else 'N/A'}\n")
        f.write(f"End Time: {conversion_stats['end_time'].strftime('%Y-%m-%d %H:%M:%S') if conversion_stats['end_time'] else 'N/A'}\n")
        f.write(f"Duration: {duration.total_seconds():.2f} seconds\n" if duration else "Duration: N/A\n")
        f.write(f"Include Targets: {args.include_targets}\n")
        if args.force_schemas:
            f.write(f"Forced Schema Mappings: {args.force_schemas}\n")
        f.write("\n")
        
        # Database structure summary
        f.write("DATABASE STRUCTURE SUMMARY\n")
        f.write("-" * 80 + "\n")
        f.write(f"Total Connections: {conversion_stats['connections']['total']}\n")
        f.write(f"  - Source Connections: {conversion_stats['connections']['source']}\n")
        f.write(f"  - Target Connections: {conversion_stats['connections']['target']}\n")
        f.write(f"Total Schemas: {conversion_stats['schemas']}\n")
        f.write(f"Total Tables: {conversion_stats['tables']['total']}\n")
        f.write(f"  - Tables with Fields: {conversion_stats['tables']['with_fields']}\n")
        f.write(f"  - Tables with Primary Keys: {conversion_stats['tables']['with_primary_keys']}\n")
        f.write(f"Total Fields: {conversion_stats['fields']}\n")
        f.write(f"Total Primary Keys: {conversion_stats['primary_keys']}\n")
        f.write("\n")
        
        # Replication configuration
        f.write("REPLICATION CONFIGURATION\n")
        f.write("-" * 80 + "\n")
        f.write(f"Groups: {conversion_stats['groups']}\n")
        f.write(f"Chains: {conversion_stats['chains']}\n")
        f.write(f"Replications: {conversion_stats['replications']}\n")
        f.write(f"Schema Mappings (Source -> Target): {conversion_stats['schema_mappings']}\n")
        f.write("\n")
        
        # Export results
        f.write("EXPORT RESULTS\n")
        f.write("-" * 80 + "\n")
        f.write(f"YAML Files Exported: {conversion_stats['yaml_files_exported']}\n")
        f.write(f"Tables Exported: {conversion_stats['tables_exported']}\n")
        f.write(f"Export Success Rate: {(conversion_stats['tables_exported'] / conversion_stats['tables']['with_fields'] * 100) if conversion_stats['tables']['with_fields'] > 0 else 0:.2f}%\n")
        f.write("\n")
        
        # Warnings
        if conversion_stats['warnings']:
            f.write("WARNINGS\n")
            f.write("-" * 80 + "\n")
            for i, warning in enumerate(conversion_stats['warnings'], 1):
                f.write(f"{i}. {warning}\n")
            f.write("\n")
        
        # Errors
        if conversion_stats['errors']:
            f.write("ERRORS\n")
            f.write("-" * 80 + "\n")
            for i, error in enumerate(conversion_stats['errors'], 1):
                f.write(f"{i}. {error}\n")
            f.write("\n")
        
        # Summary
        f.write("SUMMARY\n")
        f.write("-" * 80 + "\n")
        if conversion_stats['errors']:
            f.write(f"Status: COMPLETED WITH ERRORS ({len(conversion_stats['errors'])} errors)\n")
        elif conversion_stats['warnings']:
            f.write(f"Status: COMPLETED WITH WARNINGS ({len(conversion_stats['warnings'])} warnings)\n")
        else:
            f.write("Status: COMPLETED SUCCESSFULLY\n")
        f.write(f"\nGenerated {conversion_stats['yaml_files_exported']} YAML configuration files\n")
        f.write(f"containing {conversion_stats['tables_exported']} table definitions\n")
        f.write(f"ready for use with gluesync-bootstrapper.\n")
        f.write("\n")
        
        f.write("=" * 80 + "\n")
        f.write("End of Report\n")
        f.write("=" * 80 + "\n")
    
    print(f"\nConversion report written to: {os.path.abspath(report_path)}")
    return report_path

def view_table_list_template(template_path=None):
    """View the structure of the target table-list-template.yaml to ensure compatibility"""
    if template_path is None:
        template_path = args.template
    print(f"Viewing template: {template_path}")
    if os.path.exists(template_path):
        with open(template_path, 'r') as f:
            template_content = f.read()
            print("\nTemplate content (first 300 chars):")
            print("\n" + template_content[:300] + "...\n")  # Show first 300 chars

if __name__ == "__main__":
    from datetime import datetime
    
    # Record start time
    conversion_stats['start_time'] = datetime.now()
    
    # Check if running as CLI script (has arguments) or Lambda (no arguments)
    is_cli_mode = len(sys.argv) > 1
    
    try:
        if is_cli_mode:
            # CLI mode: require XML file path
            if not args.xml_path:
                print("ERROR: XML file path is required when running as CLI script")
                print("Usage: python parse_dbmoto_metadata_xml.py <xml_file_path> [options]")
                sys.exit(1)
            
            # Ensure output directory exists
            os.makedirs(args.output_dir, exist_ok=True)
            
            # Parse the XML and get connections, groups, chains, replications, and schema mappings
            connections, groups, chains, replications, source_to_target_schemas = parse_xml()
            
            # Print hierarchy summary
            print("\n=== Database Structure ===")
            for conn_id, conn in connections.items():
                print(f"\nConnection: {conn['name']} (ID: {conn_id})")
                for schema_id, schema in conn['schemas'].items():
                    print(f"  Schema: {schema['name']} (ID: {schema_id})")
                    print(f"    Tables: {len(schema.get('tables', {}))} tables")
            
            # Export as YAML files
            print("\nExporting to YAML files...")
            exported = export_as_yaml(connections, groups, chains, replications, source_to_target_schemas)
            print(f"\nDone! {exported} YAML files created in {os.path.abspath(args.output_dir)}/")
            print("These files match the structure needed for table-list-template.yaml in gluesync-bootstrapper.")
            
            # View reference template structure
            view_table_list_template()
            
        else:
            # Lambda/serverless mode: just run the functions (called by lambda_function.py)
            # The lambda handler will call parse_xml() and export_as_yaml() directly
            pass
    
    except Exception as e:
        conversion_stats['errors'].append(f"Fatal error during conversion: {str(e)}")
        print(f"\nERROR: {str(e)}")
        import traceback
        traceback.print_exc()
    
    finally:
        # Always write conversion report (for both CLI and Lambda modes)
        # Record end time
        conversion_stats['end_time'] = datetime.now()
        
        # Write conversion report
        write_conversion_report()

