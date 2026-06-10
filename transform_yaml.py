#!/usr/bin/env python3

import re
import yaml

def transform_yaml_file(input_file, output_file):
    """Move custom tables to whitelist in YAML file"""
    
    with open(input_file, 'r') as f:
        content = f.read()
    
    # Parse YAML
    data = yaml.safe_load(content)
    
    # Extract custom table names
    custom_tables = []
    if 'schemas' in data and 'SDRLIB' in data['schemas'] and 'tables' in data['schemas']['SDRLIB']:
        tables_section = data['schemas']['SDRLIB']['tables']
        if 'custom' in tables_section:
            custom_tables = list(tables_section['custom'].keys())
            print(f"Found {len(custom_tables)} custom tables: {custom_tables}")
    
    # Add custom tables to whitelist
    if 'whitelist' in tables_section:
        existing_whitelist = tables_section['whitelist']
        # Combine existing whitelist with custom tables, avoiding duplicates
        combined_whitelist = list(set(existing_whitelist + custom_tables))
        combined_whitelist.sort()  # Sort alphabetically
        tables_section['whitelist'] = combined_whitelist
        print(f"Updated whitelist with {len(combined_whitelist)} total tables")
    
    # Remove the custom section
    if 'custom' in tables_section:
        del tables_section['custom']
        print("Removed custom section")
    
    # Write the transformed YAML
    with open(output_file, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, indent=2)
    
    print(f"Transformation complete. Output written to {output_file}")

if __name__ == "__main__":
    input_file = "edit_backup.yaml"
    output_file = "edit_backup_transformed.yaml"
    transform_yaml_file(input_file, output_file)
