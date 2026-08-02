#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

failures=0
total=0
for file in test_*.py; do
  total=$((total + 1))
  if python3 -B "$file" >/tmp/openbox-test-${file}.log 2>&1; then
    echo "PASS  ${file}"
  else
    echo "FAIL  ${file}"
    cat /tmp/openbox-test-${file}.log
    failures=$((failures + 1))
  fi
done

echo
echo "=== ${total} test files, ${failures} failed ==="
if [ "$failures" -gt 0 ]; then
  exit 1
fi
