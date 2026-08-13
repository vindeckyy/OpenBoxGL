#!/usr/bin/env python3
import importlib.util
import json
import sys
from pathlib import Path


def main():
    entry, hook = Path(sys.argv[1]), sys.argv[2]
    spec = importlib.util.spec_from_file_location("openbox_plugin", entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, hook, None)
    payload = json.load(sys.stdin)
    result = function(payload) if function else payload
    json.dump(result if isinstance(result, dict) else payload, sys.stdout)


if __name__ == "__main__":
    main()
