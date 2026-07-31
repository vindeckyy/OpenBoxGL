#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

for file in test_*.py; do
  echo "=== ${file} ==="
  python3 -B "$file"
done
