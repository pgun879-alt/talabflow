#!/usr/bin/env bash
# Reproducible end-to-end demo. No bot token, no network, no hosting.
#
# Drives a scripted customer conversation through the offline transport, shows the resulting
# orders, moves one through the pipeline, delivers the customer notification, proves the
# notification is not sent twice, and exports a spreadsheet.
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "error: $PYTHON not found. Create the environment first:" >&2
  echo "  python3 -m venv .venv && ./.venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

DEMO_DB="data/demo.sqlite3"
export TALABFLOW_DATABASE_URL="sqlite:///$DEMO_DB"
export TALABFLOW_TRANSPORT=scripted
export TALABFLOW_JWT_SECRET="demo-only-secret-not-for-production-use-x"
export TALABFLOW_ENVIRONMENT=development
export TALABFLOW_LOG_LEVEL=WARNING
# The demo customers are in Algeria and type their numbers the local way.
export TALABFLOW_PHONE_DEFAULT_REGION=DZ

rule() { printf '\n\033[1;36m%s\033[0m\n' "── $* ─────────────────────────────────────────"; }

rm -f "$DEMO_DB" "$DEMO_DB"-wal "$DEMO_DB"-shm
mkdir -p data

rule "1. Applying database migrations"
./.venv/bin/alembic upgrade head 2>&1 | grep -E "Running upgrade|already" || true
echo "schema ready"

rule "2. Creating staff accounts"
"$PYTHON" -m talabflow.cli create-staff amina --admin --password "demo-admin-password" \
  || echo "(already exists)"
"$PYTHON" -m talabflow.cli create-staff karim --password "demo-staff-password" \
  || echo "(already exists)"
"$PYTHON" -m talabflow.cli list-staff

rule "3. Two customers place orders through chat (offline scripted transport)"
"$PYTHON" scripts/demo_conversation.py

rule "4. What staff see"
"$PYTHON" -m talabflow.cli list-orders

rule "5. Staff move an order through the pipeline"
"$PYTHON" scripts/demo_pipeline.py

rule "6. Exporting to a spreadsheet"
"$PYTHON" -m talabflow.cli export data/demo-orders.xlsx
"$PYTHON" -m talabflow.cli export data/demo-orders.csv

rule "Done"
cat <<'EOF'
Everything above ran with no bot token and no network.

To explore the staff API:

    TALABFLOW_DATABASE_URL=sqlite:///data/demo.sqlite3 \
    TALABFLOW_JWT_SECRET="demo-only-secret-not-for-production-use-x" \
      ./.venv/bin/python -m talabflow.cli serve

then, in another terminal:

    curl -s -X POST http://127.0.0.1:8000/v1/auth/token \
      -H 'Content-Type: application/json' \
      -d '{"username":"amina","password":"demo-admin-password"}'

Use the returned access_token as "Authorization: Bearer <token>".
Interactive docs: http://127.0.0.1:8000/docs

To run against real Telegram instead, set TALABFLOW_TRANSPORT=telegram and
TALABFLOW_TELEGRAM_BOT_TOKEN=<your token from @BotFather>, then:
    ./.venv/bin/python -m talabflow.cli run-bot
    ./.venv/bin/python -m talabflow.cli run-worker    # in another terminal
EOF
