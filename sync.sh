#!/usr/bin/env bash
# Push a working copy of this repo to a remote dev box over rsync+ssh.
#
# Set VERDICT_SYNC_DEST to your own target, e.g.
#   export VERDICT_SYNC_DEST="user@dev-host:~/git/verdict/"
#   ./sync.sh
set -euo pipefail

DEST="${VERDICT_SYNC_DEST:-}"
if [[ -z "$DEST" ]]; then
  echo "VERDICT_SYNC_DEST is not set." >&2
  echo 'Example: export VERDICT_SYNC_DEST="user@dev-host:~/git/verdict/"' >&2
  exit 1
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/"

rsync -avz --delete -e ssh \
  --exclude='.git' \
  --exclude='.env' \
  --exclude='export' \
  --exclude='.venv' \
  --exclude='.pytest_cache' \
  --exclude='__pycache__' \
  --exclude='node_modules' \
  "$SRC" "$DEST"
