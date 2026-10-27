#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT:${PYTHONPATH:-}"

# A test run must never hand a path or a URL to the desktop: that opens a
# browser window or a file manager behind the user's back.
export OPENBOX_DISABLE_EXTERNAL_OPEN=1

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
TEST_DIRS=("tests" ".")
# Same tolerance as scripts/check_tests.py and scripts/run_windows_tests.py. A
# wall-clock budget (test_auto_import's 10k-ROM throughput, test_perf_writes'
# mutation latency) measures the runner as much as the code, so running a suite
# exactly once let CI fail suites the local gate passed. Reliability catalog
# row 48 documented this for the Windows runner only; this runner, which the
# Linux, macOS and release jobs use, had the same single chance.
ATTEMPTS=3
failures=0
retried=0
retried_files=""
total=0
found=0
for dir in "${TEST_DIRS[@]}"; do
  if [ ! -d "$dir" ]; then continue; fi
  for file in "$dir"/test_*.py; do
    [ -e "$file" ] || continue
    found=1
    total=$((total + 1))
    base="$(basename "$file")"
    passed=0
    for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
      log_file="$TEMP_LOG_DIR/$base.$attempt.log"
      if python3 -B "$file" >"$log_file" 2>&1; then
        passed=1
        break
      fi
    done
    if [ "$passed" -eq 1 ]; then
      if [ "$attempt" -eq 1 ]; then
        echo "PASS  $file"
      else
        retried=$((retried + 1))
        retried_files="$retried_files $file"
        # Never silent: the first attempt's output is printed, so a real
        # intermittent defect still shows up in the log.
        echo "RETRY $file (passed on attempt $attempt of $ATTEMPTS)"
        sed 's/^/    /' "$TEMP_LOG_DIR/$base.1.log"
      fi
    else
      echo "FAIL  $file"
      cat "$TEMP_LOG_DIR/$base.$ATTEMPTS.log"
      failures=$((failures + 1))
    fi
  done
  if [ "$dir" = "tests" ] && [ "$found" -eq 1 ]; then break; fi
done

echo
echo "=== $total test files, $failures failed ==="
if [ "$retried" -gt 0 ]; then
  echo "=== $retried passed only after a retry:$retried_files"
fi
if [ "$failures" -gt 0 ]; then
  exit 1
fi
