#!/bin/bash
# Script to create a comprehensive multi-size ICO file for Windows
# Run this on a system with ImageMagick installed

# Input SVG file
SVG_FILE="automator_app/static/gluesync-bootstrapper.svg"
OUTPUT_ICO="automator_app/static/gluesync-icon.ico"

# Check if ImageMagick is installed
if ! command -v magick &> /dev/null && ! command -v convert &> /dev/null; then
    echo "Error: ImageMagick is required. Install with: brew install imagemagick"
    exit 1
fi

# Use magick or convert command
MAGICK_CMD="magick"
if ! command -v magick &> /dev/null; then
    MAGICK_CMD="convert"
fi

echo "Creating multi-size ICO from SVG..."

# Create PNG files for each size
sizes=(16 24 32 48 64 128 256)
temp_files=()

for size in "${sizes[@]}"; do
    temp_png="temp_${size}.png"
    temp_files+=("$temp_png")

    # Convert SVG to PNG at specific size
    $MAGICK_CMD -background transparent -size ${size}x${size} "$SVG_FILE" -resize ${size}x${size} "$temp_png"
    echo "Created ${size}x${size} PNG"
done

# Combine all PNGs into a single ICO file
$MAGICK_CMD "${temp_files[@]}" "$OUTPUT_ICO"

# Clean up temporary files
rm "${temp_files[@]}"

echo "Created multi-size ICO: $OUTPUT_ICO"
echo "Contains sizes: ${sizes[*]}"

# Verify the ICO
if command -v file &> /dev/null; then
    file "$OUTPUT_ICO"
fi
