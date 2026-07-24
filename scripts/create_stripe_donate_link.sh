#!/usr/bin/env bash
# Create (or recreate) a Stripe Payment Link for README donations.
# Requires STRIPE_SECRET_KEY in the environment. Never commit that key.
# Optional: load from ~/.env if present (gitignored).
set -euo pipefail

if [ -f "${HOME}/.env" ]; then
  # shellcheck disable=SC1091
  set -a
  source "${HOME}/.env"
  set +a
fi

if [ -z "${STRIPE_SECRET_KEY:-}" ]; then
  echo "Set STRIPE_SECRET_KEY in the environment or ~/.env (see .env.example)." >&2
  exit 1
fi

amount_cents="${STRIPE_DONATE_AMOUNT_CENTS:-500}"
product_name="${STRIPE_DONATE_PRODUCT_NAME:-Buy me a coffee - OpenBox}"

response="$(curl -sS https://api.stripe.com/v1/payment_links \
  -H "Authorization: Bearer ${STRIPE_SECRET_KEY}" \
  -d "line_items[0][price_data][currency]=usd" \
  -d "line_items[0][price_data][product_data][name]=${product_name}" \
  -d "line_items[0][price_data][unit_amount]=${amount_cents}" \
  -d "line_items[0][quantity]=1" \
  -d "submit_type=donate")"

python3 - <<'PY' "$response"
import json, sys
data = json.loads(sys.argv[1])
if url := data.get("url"):
    print(url)
else:
    message = data.get("error", {}).get("message", data)
    raise SystemExit(f"Stripe API error: {message}")
PY
