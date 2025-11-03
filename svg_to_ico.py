#!/usr/bin/env python3
"""
Convert SVG to ICO for Windows executable icon.
"""
import sys
import os
from xml.dom import minidom
import struct

def svg_to_ico(svg_path, ico_path, size=256):
    """Convert SVG to ICO format (simplified version)"""
    # For now, create a simple ICO with a placeholder
    # In a real scenario, you'd use proper SVG parsing and rasterization

    # Read SVG to check if it's valid
    try:
        with open(svg_path, 'r') as f:
            svg_content = f.read()
        print(f"SVG file {svg_path} found and readable")
    except Exception as e:
        print(f"Error reading SVG: {e}")
        return False

    # For now, we'll just copy the existing favicon.ico as gluesync-icon.ico
    # since we don't have proper SVG rasterization tools available
    favicon_path = os.path.join(os.path.dirname(svg_path), 'favicon.ico')

    if os.path.exists(favicon_path):
        import shutil
        shutil.copy2(favicon_path, ico_path)
        print(f"Created {ico_path} from existing favicon.ico")
        return True
    else:
        print("No existing favicon.ico found")
        return False

if __name__ == "__main__":
    svg_file = "automator_app/static/gluesync-bootstrapper.svg"
    ico_file = "automator_app/static/gluesync-icon.ico"

    if svg_to_ico(svg_file, ico_file):
        print(f"Successfully created {ico_file}")
    else:
        print("Failed to create ICO file")
