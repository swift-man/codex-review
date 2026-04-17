#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

ENV_FILE="$HERE/local_review_env.sh"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy from local_review_env.example.sh" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

: "${GITHUB_APP_ID:?GITHUB_APP_ID is required}"
: "${GITHUB_WEBHOOK_SECRET:?GITHUB_WEBHOOK_SECRET is required}"

if [[ -z "${GITHUB_APP_PRIVATE_KEY_PATH:-}" && -z "${GITHUB_APP_PRIVATE_KEY:-}" ]]; then
  echo "Either GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY must be set" >&2
  exit 1
fi

VENV_DIR="${VENV_DIR:-$ROOT/.venv}"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

exec uvicorn codex_review.main:app_factory \
    --factory \
    --host "$HOST" \
    --port "$PORT" \
    --log-level info
