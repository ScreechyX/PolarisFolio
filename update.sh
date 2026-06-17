#!/usr/bin/env bash
#
# Auto-deploy script for PolarisFolio.
#
# Pulls the latest code for the deploy branch, reinstalls dependencies if
# requirements.txt changed, and restarts the systemd service — but only if
# something actually changed.
#
# Triggered by:
#   • the GitHub webhook endpoint (POST /webhook/github), and/or
#   • the polarisfolio-update.timer safety-net timer.
#
# The app user must be allowed to restart the service without a password:
#   <appuser> ALL=(root) NOPASSWD: /usr/bin/systemctl restart polarisfolio
#
set -euo pipefail

cd "$(dirname "$0")"

BRANCH="${POLARISFOLIO_BRANCH:-${GITHUB_DEPLOY_BRANCH:-main}}"
SERVICE="${POLARISFOLIO_SERVICE:-polarisfolio}"
LOG="${POLARISFOLIO_UPDATE_LOG:-$HOME/.polarisfolio_update.log}"

{
  echo "=== $(date -Is) deploy check (branch: $BRANCH) ==="

  before="$(git rev-parse HEAD 2>/dev/null || echo none)"
  git fetch --quiet origin "$BRANCH"
  git checkout --quiet "$BRANCH"
  # Match remote exactly — the container is a deploy target, not a workspace.
  git reset --hard --quiet "origin/$BRANCH"
  after="$(git rev-parse HEAD)"

  if [ "$before" = "$after" ]; then
    echo "already up to date ($after)"
    exit 0
  fi

  echo "updating: $before -> $after"

  # Reinstall deps only when requirements.txt changed
  if ! git diff --quiet "$before" "$after" -- requirements.txt 2>/dev/null; then
    echo "requirements.txt changed — installing dependencies"
    if [ -x "./venv/bin/pip" ]; then
      ./venv/bin/pip install -q -r requirements.txt || echo "pip install failed"
    else
      pip install -q -r requirements.txt || echo "pip install failed"
    fi
  fi

  echo "restarting $SERVICE"
  sudo systemctl restart "$SERVICE"
  echo "deploy complete ($after)"
} >> "$LOG" 2>&1
