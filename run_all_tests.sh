#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT:${PYTHONPATH:-}"

TEMP_LOG_DIR="$(mktemp -d /tmp/openbox-test-logs.XXXXXX)"
trap 'rm -rf "$TEMP_LOG_DIR"' EXIT

# Isolate state: import-time DATA binding in openbox.py resolves
# OPENBOX_DATA_DIR once per test process. Without this, any test file that
# imports state modules before setting its own temp dir writes fixtures
# into the developer's REAL library (seen: 100 synthetic games).
SUITE_DATA_DIR="$(mktemp -d /tmp/openbox-test-data.XXXXXX)"
export OPENBOX_DATA_DIR="$SUITE_DATA_DIR"
trap 'rm -rf "$TEMP_LOG_DIR" "$SUITE_DATA_DIR"' EXIT

# Layout: run tests/ when it contains test files, otherwise fall back to root test_*.py.
# When tests/ yields files the root walk is skipped (early break below).
# Per-file timeout keeps one hang from killing the whole run; the log tail
# names the file. Environment skips (gamescope/7z/webkit/AppImage tooling) are
# counted and printed instead of passing silently; OPENBOX_STRICT_SKIPS=1
# makes any skip a failure.
TEST_TIMEOUT="${OPENBOX_TEST_TIMEOUT:-300}"
# Word-bounded so test names like `test_..._skipped` do not count as skips.
SKIP_RE='(^|[^[:alnum:]_])[Ss]kipp(ed|ing)\b'
TEST_DIRS=("tests" ".")
failures=0
total=0
found=0
skips=0
skip_files=0
for dir in "${TEST_DIRS[@]}"; do
  if [ ! -d "$dir" ]; then continue; fi
  for file in "$dir"/test_*.py; do
    [ -e "$file" ] || continue
    found=1
    total=$((total + 1))
    log_file="$TEMP_LOG_DIR/$(basename "$file").log"
    status=0
    timeout "$TEST_TIMEOUT" python3 -B "$file" >"$log_file" 2>&1 || status=$?
    file_skips="$(grep -ciE "$SKIP_RE" "$log_file" || true)"
    if [ "$file_skips" -gt 0 ]; then
      skips=$((skips + file_skips))
      skip_files=$((skip_files + 1))
      while IFS= read -r note; do
        echo "SKIP  $file: $note"
      done < <(grep -iE "$SKIP_RE" "$log_file" | head -3)
    fi
    if [ "$status" -eq 0 ]; then
      echo "PASS  $file"
    elif [ "$status" -eq 124 ]; then
      echo "TIMEOUT  $file (${TEST_TIMEOUT}s)"
      cat "$log_file"
      failures=$((failures + 1))
    else
      echo "FAIL  $file (exit $status)"
      cat "$log_file"
      failures=$((failures + 1))
    fi
  done
  if [ "$dir" = "tests" ] && [ "$found" -eq 1 ]; then break; fi
done

echo
echo "=== $total test files, $failures failed, $skips skip note(s) in $skip_files file(s) ==="
if [ "${OPENBOX_STRICT_SKIPS:-0}" = "1" ] && [ "$skips" -gt 0 ]; then
  echo "strict skips enabled: $skips environment skip note(s) are failures" >&2
  exit 1
fi
if [ "$failures" -gt 0 ]; then
  exit 1
fi
