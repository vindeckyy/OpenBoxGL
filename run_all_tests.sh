#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

python3 test_changelog_features.py

for file in test_*.py; do
  echo "=== ${file} ==="
  python3 -B "$file"
done
