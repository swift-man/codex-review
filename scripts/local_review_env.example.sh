#!/usr/bin/env bash
# Copy to `scripts/local_review_env.sh` and fill in. That file is gitignored.

# --- GitHub App ---
export GITHUB_APP_ID="123456"
export GITHUB_APP_PRIVATE_KEY_PATH="/absolute/path/to/codex-review.private-key.pem"
export GITHUB_WEBHOOK_SECRET="change-me-long-random"

# --- Codex CLI ---
# Available models (queryable via ~/.codex/models_cache.json):
#   gpt-5.5, gpt-5.4, gpt-5.4-mini, gpt-5.3-codex, gpt-5.2, codex-auto-review
export CODEX_MODEL="gpt-5.5"
export CODEX_REASONING_EFFORT="high"   # low | medium | high | xhigh
export CODEX_MAX_INPUT_TOKENS="300000"
export CODEX_TIMEOUT_SEC="600"
# Codex Desktop app bundle이 설치돼 있다면:
#   export CODEX_BIN="/Applications/Codex.app/Contents/Resources/codex"
# Homebrew 설치 CLI를 쓴다면:
#   export CODEX_BIN="/opt/homebrew/bin/codex"

# --- Repo cache / files ---
export REPO_CACHE_DIR="$HOME/.codex-review/repos"
export FILE_MAX_BYTES="204800"
# JSON/YAML/XML 같은 모호한 확장자에 대한 더 엄격한 상한 (설정/매니페스트 이름은 예외로 항상 포함).
export DATA_FILE_MAX_BYTES="20000"

# --- Server ---
export HOST="127.0.0.1"
export PORT="8000"
# 동시에 처리할 리뷰 개수. 1 이면 완전 직렬 (기본). 2~ 로 올리면 PR 이 동시에 들어왔을 때
# 병렬 처리. Codex 쿼터 여유와 맞춰 조절한다.
export REVIEW_CONCURRENCY="1"
# export DRY_RUN="1"    # uncomment to log reviews without posting
