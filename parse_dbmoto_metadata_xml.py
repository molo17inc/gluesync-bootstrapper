import xml.etree.ElementTree as ET
import os
import yaml
import argparse

def parse_arguments():
    parser = argparse.ArgumentParser(description='Parse DbMoto metadata XML and generate YAML configurations.')
    parser.add_argument('xml_path', type=str, help='Path to the DbMoto metadata XML file')
    parser.add_argument('--output-dir', type=str, default='schemas_yaml',
                      help='Directory to save generated YAML files (default: schemas_yaml)')
    parser.add_argument('--template', type=str, 
                      default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'table-list-template-basic.yaml'),
                      help='Path to template YAML file (default: table-list-template-basic.yaml in script directory)')
    return parser.parse_args()

# Parse command line arguments
args = parse_arguments()
XML_PATH = os.path.expanduser(args.xml_path)

def parse_xml():
    print(f"Parsing XML: {XML_PATH}")
    
    # Parse the XML file
    tree = ET.parse(XML_PATH)
    root = tree.getroot()
    
    # Extract database connections
    print("Extracting connections...")
    connections = {}
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
        
        if conn_id:
            connections[conn_id] = {
                "id": conn_id,
                "name": conn_name,
                "schemas": {}
            }
            print(f"  Found connection: {conn_name} (ID: {conn_id})")
    
    print(f"Found {len(connections)} database connections")
    
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
    
    # Extract fields
    print("Extracting fields...")
    field_count = 0
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
        
        # Format the type string based on the data type - strip out size/precision specifications
        sql_type = field_type.lower()
        
        # Add the field to its table
        if table_id in tables:
            if "fields" not in tables[table_id]:
                tables[table_id]["fields"] = []
            
            field_data = {
                "name": field_name,
                "type": sql_type
            }
            tables[table_id]["fields"].append(field_data)
        field_count += 1
    
    print(f"Found {field_count} fields linked to tables")
    
    # Print summary of tables with/without fields
    tables_with_fields = sum(1 for t in tables.values() if t["fields"])
    print(f"Tables with fields: {tables_with_fields}")
    print(f"Tables without fields: {len(tables) - tables_with_fields}")
    
    return connections

def export_as_yaml(connections, output_dir=None, template_file=None):
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
    
    # Extract default custom properties and schedules from template if available
    default_custom_props = {}
    default_schedules = []
    if template_structure and 'schemas' in template_structure and template_structure['schemas']:
        # Get first schema as example
        example_schema = list(template_structure['schemas'].values())[0]
        if 'customProperties' in example_schema:
            default_custom_props = example_schema['customProperties']
        if 'schedules' in example_schema:
            default_schedules = example_schema['schedules']
    
    # Process each connection and schema
    for conn in connections.values():
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
                        # Each column is a dict with field name as key and properties as value
                        column_entry = {
                            field["name"]: {
                                "name": field["name"]  # Preserve original case 
                                # "type": field.get("type", "varchar(255)").split('(')[0]  # Type commented out
                            }
                        }
                        columns.append(column_entry)
                    
                    # Create the table entry with column definitions
                    custom_tables[table_name] = {
                        "name": table_name,  # Preserve original case
                        "columns": columns
                    }
                
                # Create the schema structure that matches table-list-template.yaml
                yaml_data = {
                    "schemas": {
                        schema_name: {
                            "target": "public",  # Default target schema
                            "customProperties": default_custom_props,
                            "schedules": default_schedules,
                            "tables": {
                                "whitelist": whitelist,
                                "custom": custom_tables
                            },
                            "_source_database": conn_name  # Keeping this for reference
                        }
                    }
                }
                
                # Create filename and write YAML
                filename = f"{conn_name}__{schema_name}.yaml".replace("/", "_")
                filepath = os.path.join(output_dir, filename)
                
                with open(filepath, "w") as f:
                    yaml.dump(yaml_data, f, sort_keys=False, default_flow_style=False, allow_unicode=True)
                
                exported_count += 1
                print(f"Exported: {filename} ({len(whitelist)} tables)")
            else:
                print(f"Skipped: {conn_name}.{schema_name} (no tables with fields)")
    
    return exported_count

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
    # Ensure output directory exists
    os.makedirs(args.output_dir, exist_ok=True)
    
    connections = parse_xml()
    
    # Print hierarchy summary
    print("\n=== Database Structure ===")
    for conn_id, conn in connections.items():
        print(f"\nConnection: {conn['name']} (ID: {conn_id})")
        for schema_id, schema in conn['schemas'].items():
            print(f"  Schema: {schema['name']} (ID: {schema_id})")
            print(f"    Tables: {len(schema.get('tables', {}))} tables")
    
    # Export as YAML files using command line arguments
    export_as_yaml(connections)
    
    # View reference template structure
    view_table_list_template()
    
    # Export as YAML files
    print("\nExporting to YAML files...")
    exported = export_as_yaml(connections)
    print(f"\nDone! {exported} YAML files created in ./schemas_yaml/")
    print("These files match the structure needed for table-list-template.yaml in gluesync-bootstrapper.")

