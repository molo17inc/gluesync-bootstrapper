#!/bin/bash

# WordPress Plugin Deployment Helper
# This script helps test the FTP connection and plugin structure

set -e

echo "🔧 DbMoto WordPress Plugin Deployment Helper"
echo "=========================================="

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Configuration
FTP_HOST="c1107198.sgvps.net"
REMOTE_PATH="/molo17.com/public_html/wp-content/plugins/dbmoto-converter"
PLUGIN_DIR="dbmoto-converter/wordpress-plugin/dbmoto-converter"

# Check if required tools are installed
check_dependencies() {
    echo "📋 Checking dependencies..."

    if ! command -v lftp &> /dev/null; then
        echo -e "${RED}❌ lftp is not installed. Please install it first.${NC}"
        echo "   Ubuntu/Debian: sudo apt-get install lftp"
        echo "   macOS: brew install lftp"
        exit 1
    fi

    if ! command -v zip &> /dev/null; then
        echo -e "${RED}❌ zip is not installed. Please install it first.${NC}"
        exit 1
    fi

    echo -e "${GREEN}✅ Dependencies OK${NC}"
}

# Test FTP connection
test_ftp_connection() {
    echo "🔗 Testing FTP connection..."

    if [ -z "$FTP_USER" ] || [ -z "$FTP_PASSWORD" ]; then
        echo -e "${YELLOW}⚠️  FTP credentials not set. Set FTP_USER and FTP_PASSWORD environment variables.${NC}"
        echo "   Example:"
        echo "   export FTP_USER=your_username"
        echo "   export FTP_PASSWORD=your_password"
        return 1
    fi

    # Test FTP connection
    if lftp -c "open -u $FTP_USER,$FTP_PASSWORD ftp://$FTP_HOST; ls" &> /dev/null; then
        echo -e "${GREEN}✅ FTP connection successful${NC}"
        return 0
    else
        echo -e "${RED}❌ FTP connection failed${NC}"
        echo "   Please check your credentials and network connection."
        return 1
    fi
}

# Validate plugin structure
validate_plugin() {
    echo "🔍 Validating plugin structure..."

    if [ ! -d "$PLUGIN_DIR" ]; then
        echo -e "${RED}❌ Plugin directory not found: $PLUGIN_DIR${NC}"
        exit 1
    fi

    # Check required files
    required_files=(
        "dbmoto-converter.php"
        "js/converter.js"
        "css/converter.css"
    )

    for file in "${required_files[@]}"; do
        if [ ! -f "$PLUGIN_DIR/$file" ]; then
            echo -e "${RED}❌ Required file missing: $file${NC}"
            exit 1
        fi
    done

    echo -e "${GREEN}✅ Plugin structure valid${NC}"
}

# Test local plugin functionality
test_plugin_locally() {
    echo "🧪 Testing plugin locally..."

    # Check if main plugin file has valid PHP
    if php -l "$PLUGIN_DIR/dbmoto-converter.php" &> /dev/null; then
        echo -e "${GREEN}✅ Plugin PHP syntax valid${NC}"
    else
        echo -e "${RED}❌ Plugin PHP syntax error${NC}"
        exit 1
    fi

    # Check if JavaScript has basic syntax
    if node -c "$PLUGIN_DIR/js/converter.js" 2>/dev/null; then
        echo -e "${GREEN}✅ Plugin JavaScript syntax valid${NC}"
    else
        echo -e "${YELLOW}⚠️  JavaScript syntax check failed (Node.js not available or syntax error)${NC}"
    fi
}

# Create deployment package
create_deployment_package() {
    echo "📦 Creating deployment package..."

    PACKAGE_NAME="dbmoto-converter-$(date +%Y%m%d-%H%M%S).zip"

    cd "$PLUGIN_DIR"
    zip -r "../../$PACKAGE_NAME" . --quiet
    cd ../..

    echo -e "${GREEN}✅ Deployment package created: $PACKAGE_NAME${NC}"
    echo "   Size: $(du -h "$PACKAGE_NAME" | cut -f1)"
}

# Simulate deployment (dry run)
simulate_deployment() {
    echo "🎭 Simulating deployment (dry run)..."

    echo "   Would upload to: ftp://$FTP_HOST$REMOTE_PATH"
    echo "   Files to upload:"
    find "$PLUGIN_DIR" -type f | head -10 | sed "s|^$PLUGIN_DIR|   |"
    if [ $(find "$PLUGIN_DIR" -type f | wc -l) -gt 10 ]; then
        echo "   ... and $(($(find "$PLUGIN_DIR" -type f | wc -l) - 10)) more files"
    fi
}

# Main execution
main() {
    echo ""

    check_dependencies
    echo ""

    validate_plugin
    echo ""

    test_plugin_locally
    echo ""

    if test_ftp_connection; then
        echo ""
        simulate_deployment
        echo ""

        create_deployment_package
        echo ""

        echo -e "${GREEN}🎉 Ready for deployment!${NC}"
        echo ""
        echo "To deploy:"
        echo "1. Set your FTP credentials:"
        echo "   export FTP_USER=your_username"
        echo "   export FTP_PASSWORD=your_password"
        echo ""
        echo "2. Run the actual deployment:"
        echo "   lftp -c \"open -u \$FTP_USER,\$FTP_PASSWORD ftp://$FTP_HOST; mirror -R $PLUGIN_DIR $REMOTE_PATH\""
        echo ""
        echo "3. Or use the GitLab CI pipeline with proper variables set"
    else
        echo ""
        echo -e "${YELLOW}⚠️  FTP connection issues detected.${NC}"
        echo "   The plugin package was still created for manual upload."
        echo ""
        create_deployment_package
    fi
}

# Run main function
main
