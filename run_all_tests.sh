#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT:${PYTHONPATH:-}"

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
    if python3 -B "$file" >/tmp/openbox-test-$(basename "$file").log 2>&1; then
      echo "PASS  $file"
    else
      echo "FAIL  $file"
      cat /tmp/openbox-test-$(basename "$file").log
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
