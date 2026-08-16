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
if [[ ! "$STRIPE_SECRET_KEY" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "STRIPE_SECRET_KEY contains unsupported characters." >&2
  exit 1
fi

amount_cents="${STRIPE_DONATE_AMOUNT_CENTS:-500}"
product_name="${STRIPE_DONATE_PRODUCT_NAME:-Buy me a coffee - OpenBox}"
if [[ ! "$amount_cents" =~ ^[1-9][0-9]*$ ]]; then
  echo "STRIPE_DONATE_AMOUNT_CENTS must be a positive integer." >&2
  exit 1
fi

umask 077
curl_config="$(mktemp)"
trap 'rm -f -- "$curl_config"' EXIT
# Keep the secret out of the curl process arguments. Stripe keys are restricted
# above so they cannot inject a second curl-config directive.
printf 'header = "Authorization: Bearer %s"\n' "$STRIPE_SECRET_KEY" > "$curl_config"

response="$(curl --proto '=https' --tlsv1.2 --fail --silent --show-error --config "$curl_config" \
  https://api.stripe.com/v1/payment_links \
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
