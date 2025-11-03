"""
PyInstaller runtime hook to ensure root-level modules are importable on Linux.
This hook adds the _MEIPASS directory to sys.path to make root modules available.
"""
import sys
import os

# Get the base path where PyInstaller extracts files
if hasattr(sys, '_MEIPASS'):
    # Running as PyInstaller bundle
    base_path = sys._MEIPASS
    
    # Add the extraction directory to sys.path if not already present
    if base_path not in sys.path:
        sys.path.insert(0, base_path)
    
    # Explicitly ensure root modules are importable
    root_modules = [
        'create_user_defined_functions',
        'create_all_tables',
        'create_all_entities',
        'commons',
        'main',
        'parse_dbmoto_metadata_xml',
    ]
    
    for module_name in root_modules:
        module_file = os.path.join(base_path, f'{module_name}.py')
        if not os.path.exists(module_file):
            module_file = os.path.join(base_path, f'{module_name}.pyc')
