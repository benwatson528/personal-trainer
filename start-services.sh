#!/usr/bin/env bash
# Starts everything the personal-trainer stack needs:
#   1. Open WebUI (Docker, port 3000)
#   2. FastAPI PT service (port 8000), env loaded from .env
# n8n is NOT started (pass --n8n to include it). Airflow is no longer used.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

START_N8N=0
for arg in "$@"; do
  case "$arg" in
    --n8n) START_N8N=1 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

# Load OPENROUTER_API_KEY etc. from .env
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
else
  echo "Warning: $ROOT/.env not found" >&2
fi

docker start open-webui
[[ "$START_N8N" == "1" ]] && docker start n8n

if pgrep -f "uvicorn|app.api" >/dev/null && curl -sf http://localhost:8000/health >/dev/null; then
  echo "PT service already running on :8000"
else
  nohup "$ROOT/.venv/bin/python" "$ROOT/main.py" >"$LOG_DIR/pt-service.log" 2>&1 &
  echo "PT service started (pid $!), logs: $LOG_DIR/pt-service.log"
fi

echo "Open WebUI: http://localhost:3000"
echo "PT API:     http://localhost:8000  (health: /health)"
