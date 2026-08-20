#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT:${PYTHONPATH:-}"

TEMP_LOG_DIR="$(mktemp -d /tmp/openbox-test-logs.XXXXXX)"
trap 'rm -rf "$TEMP_LOG_DIR"' EXIT

# Support both flat and packaged layout: walk tests/ if it exists, otherwise root
TEST_DIRS=("tests" ".")
failures=0
total=0
found=0
for dir in "${TEST_DIRS[@]}"; do
  if [ ! -d "$dir" ]; then continue; fi
  for file in "$dir"/test_*.py; do
    [ -e "$file" ] || continue
    found=1
    total=$((total + 1))
    log_file="$TEMP_LOG_DIR/$(basename "$file").log"
    if python3 -B "$file" >"$log_file" 2>&1; then
      echo "PASS  $file"
    else
      echo "FAIL  $file"
      cat "$log_file"
      failures=$((failures + 1))
    fi
  done
  if [ "$dir" = "tests" ] && [ "$found" -eq 1 ]; then break; fi
done

echo
echo "=== $total test files, $failures failed ==="
if [ "$failures" -gt 0 ]; then
  exit 1
fi
