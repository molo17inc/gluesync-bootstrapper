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

import argparse
import base64
import gzip
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional
import yaml

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
    'connections_details': [],
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

# Known type aliases that should be normalized to canonical names
TYPE_ALIASES = {
    "TIMESTMP": "TIMESTAMP",
}


def parse_connect_params(params_text: str) -> Dict[str, str]:
    """Parse the semicolon-separated ConnectParams string into a dict with lowercase keys."""
    parsed: Dict[str, str] = {}
    if not params_text:
        return parsed
    for part in params_text.split(';'):
        part = part.strip()
        if not part:
            continue
        if '=' in part:
            key, value = part.split('=', 1)
            parsed[key.strip().lower()] = value.strip()
        else:
            parsed[part.lower()] = ""
    return parsed


def _is_allowed_xml_char(codepoint: int) -> bool:
    """Return True if the codepoint is allowed in XML 1.0."""
    return (
        codepoint in (0x9, 0xA, 0xD)
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def sanitize_xml_file(xml_path: str) -> None:
    """Sanitize an XML file in-place, replacing invalid characters with spaces.

    This performs a best-effort cleanup so that parsers like xml.etree.ElementTree
    do not fail on constructs such as "&#x0;" or other invalid XML 1.0 characters.
    """

    if not os.path.isfile(xml_path):
        return

    try:
        with open(xml_path, "rb") as f:
            data = f.read()
    except OSError:
        return

    if not data:
        return

    # Detect encoding from XML declaration, fallback to UTF-8
    encoding = "utf-8"
    try:
        header_chunk = data[:200]
        m = re.search(br'encoding=["\']([A-Za-z0-9_\-]+)["\']', header_chunk)
        if m:
            declared = m.group(1).decode("ascii", errors="ignore")
            if declared:
                encoding = declared
    except Exception:
        # On any issue, keep default encoding
        pass

    try:
        text = data.decode(encoding, errors="replace")
    except LookupError:
        # Unknown codec, fallback to UTF-8
        encoding = "utf-8"
        text = data.decode(encoding, errors="replace")

    # 1) Replace explicit "&#x0;" references with a space
    text = text.replace("&#x0;", " ")

    # 2) Replace raw control characters not allowed in XML 1.0 with spaces
    cleaned_chars = []
    for ch in text:
        cp = ord(ch)
        if cp < 0x20 and ch not in ("\t", "\n", "\r"):
            cleaned_chars.append(" ")
        else:
            cleaned_chars.append(ch)
    text = "".join(cleaned_chars)

    # 3) Replace numeric character references that point to invalid XML chars
    entity_pattern = re.compile(r"&#(x[0-9A-Fa-f]+|\d+);")

    def _replace_entity(match: re.Match) -> str:
        body = match.group(1)
        try:
            if body[0] in ("x", "X"):
                cp = int(body[1:], 16)
            else:
                cp = int(body, 10)
        except ValueError:
            # Non interpretable reference -> neutralize
            return " "

        if not _is_allowed_xml_char(cp):
            return " "
        return match.group(0)

    text = entity_pattern.sub(_replace_entity, text)

    try:
        new_data = text.encode(encoding, errors="replace")
    except LookupError:
        new_data = text.encode("utf-8", errors="replace")

    if new_data != data:
        try:
            with open(xml_path, "wb") as f:
                f.write(new_data)
        except OSError:
            # Best-effort: if we cannot write back, we leave the original file
            pass

def parse_xml():
    xml_path = os.environ.get('XML_PATH') or args.xml_path
    if not xml_path:
        raise ValueError("XML path is required. Provide it via CLI argument or XML_PATH environment variable.")
    print(f"Parsing XML: {xml_path}")
    if not os.path.isfile(xml_path):
        raise FileNotFoundError(f"XML file not found: {xml_path}")

    # Sanitize XML content before parsing to avoid invalid character errors
    sanitize_xml_file(xml_path)

    # Parse the XML file
    tree = ET.parse(xml_path)
    root = tree.getroot()
    
    # Reset per-run stats
    conversion_stats['connections_details'] = []
    
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
        connect_params_raw = conn_elem.findtext("ConnectParams", "")
        connect_params = parse_connect_params(connect_params_raw)
        db_version = connect_params.get("dbversion") or ""
        data_source_name = connect_params.get("datasourcename") or ""
        connection_type = connect_params.get("connectiontype") or ""
        database_type = data_source_name or connection_type or conn_elem.findtext("Type") or ""
        
        if conn_id:
            connections[conn_id] = {
                "id": conn_id,
                "name": conn_name,
                "is_source": is_source,
                "schemas": {},
                "db_version": db_version,
                "data_source_name": data_source_name,
                "connection_type": connection_type,
                "database_type": database_type,
                "connect_params": connect_params,
            }
            
            conversion_stats['connections_details'].append({
                "id": conn_id,
                "name": conn_name,
                "role": "SOURCE" if is_source else "TARGET",
                "database_type": database_type,
                "db_version": db_version,
                "data_source_name": data_source_name,
                "connection_type": connection_type,
            })
            
            role_label = "SOURCE" if is_source else "TARGET"
            type_label = database_type or "Unknown database"
            version_label = db_version or "unknown version"
            print(f"  Found {role_label} connection: {conn_name} (ID: {conn_id}) -> {type_label} ({version_label})")
            
            if is_source:
                source_connections.add(conn_id)
            else:
                target_connections.add(conn_id)
    
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
        field_type_raw = field_elem.findtext("Type")
        field_type = field_type_raw.strip() if field_type_raw else "VARCHAR"
        field_type = TYPE_ALIASES.get(field_type.upper(), field_type)
        field_size = field_elem.findtext("Size") or "0"
        field_precision = field_elem.findtext("Precision") or "0"
        field_scale = field_elem.findtext("Scale") or "0"
        allow_null = (field_elem.findtext("AllowNull", "Y").strip().upper() == "Y")
        
        # Extract primary key position
        primary_key_pos = field_elem.findtext("PrimaryKeyPos")
        
        # Add the field to its table
        if table_id in tables:
            if "fields" not in tables[table_id]:
                tables[table_id]["fields"] = []
            if "primary_keys" not in tables[table_id]:
                tables[table_id]["primary_keys"] = []
            
            try:
                data_length = int(field_size)
            except (TypeError, ValueError):
                data_length = 0

            try:
                numeric_precision = int(field_precision)
            except (TypeError, ValueError):
                numeric_precision = 0

            try:
                numeric_scale = int(field_scale)
            except (TypeError, ValueError):
                numeric_scale = 0

            field_data = {
                "name": field_name,
                "type": field_type,
                "data_length": data_length,
                "numeric_precision": numeric_precision,
                "numeric_scale": numeric_scale,
                "allow_null": allow_null
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
    
    # Extract field mappings from DBMMFieldMappings
    print("\nExtracting field mappings...")
    field_mappings = {}  # Structure: {replication_id: {src_field_id: {target_field_id, target_expression, src_expression}}}
    record_id_mappings = {}  # Structure: {replication_id: {target_field_id: src_expression}} for [!RecordID] expressions
    
    for mapping_elem in root.findall("./tables/DBMMFieldMappings"):
        mapping_id = mapping_elem.findtext("FieldMappingID")
        repl_id = mapping_elem.findtext("ReplicationID")
        src_field_id = mapping_elem.findtext("SrcFieldID")
        trg_field_id = mapping_elem.findtext("TrgFieldID")
        src_expression = mapping_elem.findtext("SrcExpression")
        is_forth = mapping_elem.findtext("IsForth", "Y").upper() == "Y"
        
        if repl_id and is_forth:  # Only process forward mappings
            if repl_id not in field_mappings:
                field_mappings[repl_id] = {}
            
            if src_field_id:
                # Direct field-to-field mapping
                field_mappings[repl_id][src_field_id] = {
                    "target_field_id": trg_field_id,
                    "src_expression": src_expression,
                    "type": "direct"
                }
            elif src_expression:
                # Expression-based mapping - store with None src_field_id
                if trg_field_id:
                    field_mappings[repl_id][f"expr_{trg_field_id}"] = {
                        "target_field_id": trg_field_id,
                        "src_expression": src_expression,
                        "type": "expression"
                    }
                    # Check for [!RecordID] expression
                    if "[!RecordID]" in src_expression:
                        if repl_id not in record_id_mappings:
                            record_id_mappings[repl_id] = {}
                        record_id_mappings[repl_id][trg_field_id] = src_expression
                        print(f"    Found [!RecordID] mapping: replication {repl_id}, target field {trg_field_id}")
    
    total_mappings = sum(len(m) for m in field_mappings.values())
    print(f"Found {total_mappings} field mappings across {len(field_mappings)} replications")
    
    # Build field ID to name lookup for resolving target field names
    print("\nBuilding field ID to name lookup...")
    field_id_to_name = {}
    for field_elem in root.findall("./tables/DBMMFields"):
        field_id = field_elem.findtext("FieldID")
        table_id = field_elem.findtext("TableID")
        field_name = None
        for name_tag in ["n", "Name", "name"]:
            field_name = field_elem.findtext(name_tag)
            if field_name:
                break
        if field_id and table_id and field_name:
            field_id_to_name[(table_id, field_id)] = field_name
    print(f"Built lookup with {len(field_id_to_name)} field ID entries")
    
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
    
    return connections, groups, chains, replications, source_to_target_schemas, field_mappings, field_id_to_name, record_id_mappings

def export_as_yaml(connections, groups, chains, replications, source_to_target_schemas, field_mappings, field_id_to_name, record_id_mappings=None, output_dir=None, template_file=None):
    # Use environment variables if parameters are not provided (Lambda mode)
    if output_dir is None:
        output_dir = os.environ.get('OUTPUT_DIR')
    if template_file is None:
        template_file = os.environ.get('TEMPLATE_PATH')
    
    # Fallback to command line arguments if available (CLI mode)
    if output_dir is None and 'args' in globals():
        output_dir = args.output_dir
    if template_file is None and 'args' in globals():
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
        template_schemas = {name.lower(): (name, schema) for name, schema in template_structure['schemas'].items()}
    
    # Function to find best matching template schema
    def find_matching_template_schema(schema_name):
        # First try exact match
        if schema_name.lower() in template_schemas:
            _, schema = template_schemas[schema_name.lower()]
            return schema
        
        # Try partial match (case insensitive)
        for template_lower, (template_name, schema) in template_schemas.items():
            if schema_name.lower() in template_lower or template_lower in schema_name.lower():
                return schema
        
        # No match found, return None
        return None
    
    # Process connections and schemas based on mode
    # Check for manual override schemas
    manual_overrides = {}
    force_schemas = os.environ.get('FORCE_SCHEMAS')
    if force_schemas:
        # Parse format: SOURCE_SCHEMA:TARGET_SCHEMA,SOURCE_SCHEMA2:TARGET_SCHEMA2
        for mapping in force_schemas.split(','):
            if ':' in mapping:
                src, tgt = mapping.split(':', 1)
                manual_overrides[src.strip()] = tgt.strip()
                print(f"Manual override: {src.strip()} -> {tgt.strip()}")
    
    # Get include_targets flag
    include_targets = os.environ.get('INCLUDE_TARGETS', 'true').lower() == 'true'
    
    # Fallback to args if available (CLI mode)
    if not force_schemas and 'args' in globals() and hasattr(args, 'force_schemas'):
        force_schemas = args.force_schemas
        if force_schemas:
            for mapping in force_schemas.split(','):
                if ':' in mapping:
                    src, tgt = mapping.split(':', 1)
                    manual_overrides[src.strip()] = tgt.strip()
                    print(f"Manual override: {src.strip()} -> {tgt.strip()}")
    
    if 'args' in globals() and hasattr(args, 'include_targets'):
        include_targets = args.include_targets

    # Build lookup for tables by ID and map source tables to their target counterparts
    table_lookup = {}
    for conn in connections.values():
        for schema in conn["schemas"].values():
            for table_id, table in schema["tables"].items():
                table_lookup[table_id] = {
                    "table": table,
                    "schema_name": schema["name"],
                    "connection_name": conn["name"],
                    "is_source": conn["is_source"],
                }
    
    source_to_target_tables = {}
    for repl_id, repl in replications.items():
        src_id = repl.get("src_table_id")
        trg_id = repl.get("trg_table_id")
        if src_id and trg_id:
            source_to_target_tables.setdefault(src_id, []).append((trg_id, repl_id))
    
    # Process each connection and schema
    for conn in connections.values():
        # Skip target connections UNLESS we have manual overrides or --include-targets flag
        if not conn["is_source"] and not include_targets and not manual_overrides:
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
                # Create whitelist of all table names (prefer target names when available)
                whitelist = []
                
                # Create custom table definitions with column details
                custom_tables = {}
                for table_name, table in tables_with_fields.items():
                    # Convert field list to column definitions matching template format
                    columns = []
                    
                    # Find the replication and target info for this table
                    target_ids = source_to_target_tables.get(table["id"], [])
                    target_info = None
                    repl_id_for_table = None
                    target_table_id = None
                    
                    for target_id, repl_id in target_ids:
                        target_info = table_lookup.get(target_id)
                        if target_info and not target_info["is_source"]:
                            repl_id_for_table = repl_id
                            target_table_id = target_id
                            break
                    
                    # Get field mappings for this replication
                    table_field_mappings = field_mappings.get(repl_id_for_table, {}) if repl_id_for_table else {}
                    
                    for field in table["fields"]:
                        # Determine target field name using field mappings
                        target_field_name = field["name"]
                        target_field = None
                        
                        # Find source field ID by looking up in the field_id_to_name dict
                        src_field_id = None
                        for (tid, fid), fname in field_id_to_name.items():
                            if tid == table["id"] and fname == field["name"]:
                                src_field_id = fid
                                break
                        
                        # If we found the source field ID and have mappings, look up the target
                        if src_field_id and table_field_mappings:
                            mapping = table_field_mappings.get(src_field_id)
                            if mapping and mapping.get("target_field_id"):
                                # Look up target field name from target table
                                target_field_id = mapping["target_field_id"]
                                target_field_name = field_id_to_name.get((target_table_id, target_field_id), field["name"])
                                if target_field_name != field["name"]:
                                    print(f"        Mapped field: {field['name']} -> {target_field_name}")
                        
                        col_def = {
                            "name": target_field_name,
                            "type": field.get("type") or "VARCHAR",
                            "dataLength": field.get("data_length", 0),
                            "numericPrecision": field.get("numeric_precision", 0),
                            "numericScale": field.get("numeric_scale", 0),
                            "isNullable": field.get("allow_null", True)
                        }
                        
                        # Store source field name if it differs from target (for GlueSync column mapping)
                        if target_field_name != field["name"]:
                            col_def["sourceName"] = field["name"]
                        
                        columns.append(col_def)
                    
                    # Add special _RRN column if there's a [!RecordID] mapping for this replication
                    if record_id_mappings and repl_id_for_table in record_id_mappings:
                        for trg_field_id, src_expr in record_id_mappings[repl_id_for_table].items():
                            # Look up target field name - this becomes the column name (target side)
                            target_field_name = field_id_to_name.get((target_table_id, trg_field_id), "ID")
                            rrn_col = {
                                "name": target_field_name,
                                "type": "DECIMAL",
                                "dataLength": 15,
                                "numericPrecision": 0,
                                "numericScale": 0,
                                "isNullable": False,
                                "sourceName": "_RRN"
                            }
                            columns.append(rrn_col)
                            print(f"      Added _RRN source column mapped to target field '{target_field_name}'")
                    
                    # Determine the mapped target table name if available
                    export_table_name = table_name
                    target_schema_name = None
                    target_conn_name = None
                    target_ids_with_repl = source_to_target_tables.get(table["id"], [])
                    for target_id, repl_id in target_ids_with_repl:
                        target_info = table_lookup.get(target_id)
                        if target_info and not target_info["is_source"]:
                            export_table_name = target_info["table"]["name"]
                            target_schema_name = target_info["schema_name"]
                            target_conn_name = target_info["connection_name"]
                            break
                    else:
                        if target_ids_with_repl:
                            print(f"      Warning: Could not resolve target table IDs {[t[0] for t in target_ids_with_repl]} for source table {table_name}")
                    
                    whitelist_name = table["name"]
                    if whitelist_name not in whitelist:
                        whitelist.append(whitelist_name)
                    
                    # Create the table entry with column definitions
                    table_config = {
                        "name": export_table_name,
                        "columns": columns
                    }
                    if export_table_name != table_name:
                        print(f"      Mapped source table '{table_name}' -> target table '{export_table_name}' (schema: {target_schema_name}, connection: {target_conn_name})")
                    
                    # Add primary keys if found, otherwise fallback:
                    # - If _RRN column exists, use it as the only key
                    # - Otherwise, use ALL columns as keys (composite key)
                    if table.get("primary_keys"):
                        # Use simple string array format to match template
                        table_config["keys"] = table["primary_keys"]
                        print(f"      Added {len(table['primary_keys'])} primary key(s) to table {table_name}: {table['primary_keys']}")
                    else:
                        # No primary keys found - apply fallback strategy
                        # Check if _RRN column was added (RecordID mapping)
                        has_rrn = any(col.get("name") == "_RRN" for col in columns)
                        if has_rrn:
                            table_config["keys"] = ["_RRN"]
                            print(f"      No primary keys found for table {table_name}, using _RRN as key (fallback)")
                        else:
                            # Use all source column names as composite key
                            # Use sourceName if present, otherwise name (which equals source name in that case)
                            all_column_keys = [col.get("sourceName") or col.get("name") for col in columns if (col.get("sourceName") or col.get("name"))]
                            table_config["keys"] = all_column_keys
                            print(f"      No primary keys found for table {table_name}, using all {len(all_column_keys)} columns as composite key (fallback)")
                    
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
        output_dir = os.environ.get('OUTPUT_DIR')
    
    # Fallback to command line arguments if available (CLI mode)
    if output_dir is None and 'args' in globals():
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
        
        template_file = os.environ.get('TEMPLATE_PATH')
        if template_file is None and 'args' in globals() and hasattr(args, 'template'):
            template_file = args.template
        f.write(f"Template File: {template_file}\n")
        
        f.write(f"Start Time: {conversion_stats['start_time'].strftime('%Y-%m-%d %H:%M:%S') if conversion_stats['start_time'] else 'N/A'}\n")
        f.write(f"End Time: {conversion_stats['end_time'].strftime('%Y-%m-%d %H:%M:%S') if conversion_stats['end_time'] else 'N/A'}\n")
        f.write(f"Duration: {duration.total_seconds():.2f} seconds\n" if duration else "Duration: N/A\n")
        
        include_targets = os.environ.get('INCLUDE_TARGETS', 'true').lower() == 'true'
        if 'args' in globals() and hasattr(args, 'include_targets'):
            include_targets = args.include_targets
        f.write(f"Include Targets: {include_targets}\n")
        
        force_schemas = os.environ.get('FORCE_SCHEMAS')
        if force_schemas is None and 'args' in globals() and hasattr(args, 'force_schemas'):
            force_schemas = args.force_schemas
        if force_schemas:
            f.write(f"Forced Schema Mappings: {force_schemas}\n")
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
        
        # Connection details
        if conversion_stats['connections_details']:
            f.write("CONNECTION DETAILS\n")
            f.write("-" * 80 + "\n")
            for conn in conversion_stats['connections_details']:
                f.write(f"{conn['role']}: {conn['name']} (ID: {conn['id']})\n")
                f.write(f"  Database Type: {conn.get('database_type') or 'N/A'}\n")
                f.write(f"  Data Source Name: {conn.get('data_source_name') or 'N/A'}\n")
                f.write(f"  Connection Type: {conn.get('connection_type') or 'N/A'}\n")
                f.write(f"  DB Version: {conn.get('db_version') or 'N/A'}\n")
                f.write("\n")
        else:
            f.write("CONNECTION DETAILS\n")
            f.write("-" * 80 + "\n")
            f.write("No connection metadata recorded.\n\n")
        
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
        template_path = os.environ.get('TEMPLATE_PATH')
    
    # Fallback to args if available (CLI mode)
    if template_path is None and 'args' in globals() and hasattr(args, 'template'):
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
            
            # Parse the XML and get connections, groups, chains, replications, schema mappings, field mappings, and record ID mappings
            connections, groups, chains, replications, source_to_target_schemas, field_mappings, field_id_to_name, record_id_mappings = parse_xml()
            
            # Print hierarchy summary
            print("\n=== Database Structure ===")
            for conn_id, conn in connections.items():
                print(f"\nConnection: {conn['name']} (ID: {conn_id})")
                for schema_id, schema in conn['schemas'].items():
                    print(f"  Schema: {schema['name']} (ID: {schema_id})")
                    print(f"    Tables: {len(schema.get('tables', {}))} tables")
            
            # Export as YAML files
            print("\nExporting to YAML files...")
            exported = export_as_yaml(connections, groups, chains, replications, source_to_target_schemas, field_mappings, field_id_to_name, record_id_mappings)
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

