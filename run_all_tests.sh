#!/bin/bash
cd /home/hayden/Desktop/Projects/OpenBox
python3 test_changelog_features.py
status=$?
if [ $status -ne 0 ]; then exit $status; fi
for f in test_*.py; do
  echo "=== $f ==="
  python3 -B "$f" || exit 1
done
