#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ENV_FILE="$HERE/local_review_env.sh"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV_FILE"

: "${GITHUB_WEBHOOK_SECRET:?}"
: "${WEBHOOK_URL:=http://127.0.0.1:${PORT:-8000}/webhook}"

: "${REPO_FULL_NAME:=octo/demo}"
: "${PR_NUMBER:=1}"
: "${INSTALLATION_ID:=1}"

BODY=$(cat <<JSON
{
  "action": "opened",
  "pull_request": {"number": $PR_NUMBER, "draft": false},
  "repository": {"full_name": "$REPO_FULL_NAME"},
  "installation": {"id": $INSTALLATION_ID}
}
JSON
)

SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" -binary | xxd -p -c 256)"

curl -sS -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: test-$(date +%s)" \
  -H "X-Hub-Signature-256: $SIG" \
  --data "$BODY"
echo
