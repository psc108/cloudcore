#!/usr/bin/env bash
# Build ui/index.html from source files in ui/src/
# Usage: bash ui/build.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SCRIPT_DIR/src"
OUT="$SCRIPT_DIR/index.html"
TMP="$(mktemp)"

# ── Assemble ──────────────────────────────────────────────────────────────────
cat "$SRC/html/head.html"   >> "$TMP"
echo "<style>"               >> "$TMP"
cat "$SRC/css/main.css"      >> "$TMP"
echo "</style>"              >> "$TMP"

# External libs (xterm) — kept in head.html already, body follows
cat "$SRC/html/body.html"   >> "$TMP"

echo "<script>"              >> "$TMP"
for f in "$SRC"/js/*.js; do
    cat "$f"                 >> "$TMP"
    echo ""                  >> "$TMP"
done
echo "</script>"             >> "$TMP"

cat "$SRC/html/tail.html"   >> "$TMP"

# ── Validate JS ───────────────────────────────────────────────────────────────
if command -v node &>/dev/null; then
    python3 - "$TMP" << 'PYEOF'
import re, sys
html = open(sys.argv[1], encoding='utf-8').read()
m = re.search(r'<script>(.*?)</script>', html, re.DOTALL)
open('/tmp/_cc_build_check.js', 'w').write(m.group(1))
PYEOF
    node --check /tmp/_cc_build_check.js
    echo "  JS syntax OK"
fi

# ── Write output ──────────────────────────────────────────────────────────────
mv "$TMP" "$OUT"
echo "Built: $OUT ($(wc -l < "$OUT") lines)"
