#!/bin/bash

echo "🧹 Cleaning up unnecessary files..."

# Remove old testing files
rm -f arrivals_enhanced.py
rm -f tester.html
rm -f index.html
echo "✓ Removed testing files"

# Remove original template files
rm -rf src/
echo "✓ Removed src/ folder"

# Remove template JSON (keeping schedule_store.json)
rm -f classes.json
echo "✓ Removed classes.json template"

# Remove GTFS folder if stops.txt is in root
if [ -f stops.txt ] && [ -d gtfs_subway ]; then
    rm -rf gtfs_subway/
    echo "✓ Removed gtfs_subway/ folder (using stops.txt instead)"
fi

# Remove Python cache
rm -rf __pycache__/
echo "✓ Removed __pycache__"

echo ""
echo "✅ Cleanup complete!"
echo ""
echo "📁 Remaining production files:"
ls -lh | grep -v "^d" | grep -v "^total"
