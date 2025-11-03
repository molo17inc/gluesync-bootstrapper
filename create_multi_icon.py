#!/usr/bin/env python3
"""
Create a comprehensive multi-size ICO file for Windows executable.
Generates ICO with standard sizes: 16, 24, 32, 48, 64, 128, 256 pixels.
"""
import struct
import os
from PIL import Image, ImageDraw
import math

def create_icon_from_svg(sizes=[16, 24, 32, 48, 64, 128, 256]):
    """Create ICO file with multiple sizes from SVG-like design"""
    # Since we can't easily parse SVG, create a simple geometric icon
    # resembling the Gluesync logo based on the SVG content

    images = []

    for size in sizes:
        # Create a new image with transparency
        img = Image.new('RGBA', (size, size), (255, 255, 255, 0))
        draw = ImageDraw.Draw(img)

        # Calculate dimensions based on size
        margin = size // 8
        rect_width = size - 2 * margin
        rect_height = size - 2 * margin

        # Draw a rounded rectangle background (like the SVG)
        # Use a simple rectangle with rounded corners effect
        corner_radius = size // 16

        # Draw main rectangle with rounded corners approximation
        draw.rectangle([margin, margin, size-margin, size-margin],
                      fill=(255, 255, 255, 255), outline=(0, 0, 0, 255), width=1)

        # Add some geometric elements similar to the SVG
        # Vertical lines (database columns representation)
        line_spacing = rect_width // 4
        for i in range(1, 4):
            x = margin + i * line_spacing
            draw.line([x, margin, x, size-margin], fill=(0, 0, 0, 255), width=1)

        # Horizontal lines (data rows representation)
        row_height = rect_height // 6
        for i in range(1, 6):
            y = margin + i * row_height
            draw.line([margin, y, size-margin, y], fill=(0, 0, 0, 255), width=1)

        images.append(img)

    return images

def save_ico(images, filename):
    """Save multiple images as ICO file"""
    # ICO file format implementation
    # Header
    num_images = len(images)
    header = struct.pack('<HHH', 0, 1, num_images)  # Reserved, Type, Count

    # Image directory entries
    directory_entries = []
    data_offset = 6 + num_images * 16  # Header + directory entries

    for img in images:
        width = img.width if img.width < 256 else 0
        height = img.height if img.height < 256 else 0
        colors = 0  # >256 colors
        reserved = 0
        planes = 1
        bpp = 32  # 32-bit RGBA

        # Convert PIL image to BMP format for ICO
        # ICO uses BMP format internally but without file header
        img_bmp = img.tobytes('raw', 'BGRA')  # Windows ICO expects BGRA

        # BMP header for ICO (BITMAPINFOHEADER)
        bmp_header = struct.pack('<LLLHHLLLLLL',
                                40,  # Header size
                                img.width, img.height * 2,  # Width, Height (doubled for ICO)
                                1,  # Planes
                                32,  # Bits per pixel
                                0,  # Compression
                                len(img_bmp),  # Image size
                                0, 0, 0, 0)  # Other BMP fields

        # ICO needs both XOR and AND masks
        # For 32-bit images, AND mask is all zeros (fully opaque)
        and_mask = b'\x00' * ((img.width + 7) // 8 * img.height)

        img_data = bmp_header + img_bmp + and_mask
        img_size = len(img_data)

        # Directory entry
        entry = struct.pack('<BBBBHHLL', width, height, colors, reserved,
                           planes, bpp, img_size, data_offset)
        directory_entries.append(entry)
        data_offset += img_size

    # Write the ICO file
    with open(filename, 'wb') as f:
        f.write(header)
        for entry in directory_entries:
            f.write(entry)

        # Write image data
        for img in images:
            img_bmp = img.tobytes('raw', 'BGRA')
            bmp_header = struct.pack('<LLLHHLLLLLL',
                                    40,  # Header size
                                    img.width, img.height * 2,  # Width, Height
                                    1,  # Planes
                                    32,  # Bits per pixel
                                    0,  # Compression
                                    len(img_bmp),  # Image size
                                    0, 0, 0, 0)  # Other BMP fields

            and_mask = b'\x00' * ((img.width + 7) // 8 * img.height)
            f.write(bmp_header + img_bmp + and_mask)

    print(f"Created ICO file: {filename} with {num_images} sizes")

if __name__ == "__main__":
    output_file = "automator_app/static/gluesync-icon.ico"

    # Create images for standard Windows icon sizes
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = create_icon_from_svg(sizes)

    # Save as ICO
    save_ico(images, output_file)

    print(f"Generated multi-size ICO with sizes: {sizes}")
