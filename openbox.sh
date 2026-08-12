#!/bin/bash
# Desktop sessions open a chrome-less app window by default; pass the
# user's flag through so --app-window / --no-app-window override it.
exec python3 /app/share/openbox/web_app.py "$@"
