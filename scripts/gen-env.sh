#!/usr/bin/env bash
#
# gen-env.sh — generate/rotate secrets in .env
#
# Generates fresh random values for:
#   IRS_SECRET_KEY               (JWT signing key)
#   IRS_ENCRYPTION_KEY           (AES-256-GCM master key for at-rest encryption)
#   IRS_BOOTSTRAP_ADMIN_PASSWORD (initial admin password)
#
# All other keys in .env (API keys, DB url, etc.) are preserved.
#
# Usage:
#   ./scripts/gen-env.sh                 # interactive, asks before overwriting
#   ./scripts/gen-env.sh -y              # no prompt (CI)
#   ./scripts/gen-env.sh --only-missing  # only fill keys that are empty/placeholder
#   ./scripts/gen-env.sh -f path/to/.env # target a different env file
#
set -euo pipefail

ENV_FILE=".env"
TEMPLATE=".env.example"
ASSUME_YES=0
ONLY_MISSING=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -y|--yes)          ASSUME_YES=1; shift ;;
    --only-missing)    ONLY_MISSING=1; shift ;;
    -f|--file)         ENV_FILE="$2"; shift 2 ;;
    -h|--help)         grep '^#' "$0" | sed 's/^#//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# repo root = parent of this script's dir
cd "$(dirname "$0")/.."

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }

gen_secret_key() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
}
gen_enc_key() {
  python3 -c 'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
}
gen_password() {
  python3 -c 'import secrets; print(secrets.token_urlsafe(18))'
}

# get current value of KEY from $ENV_FILE ('' if missing)
current() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || { echo ""; return; }
  grep -E "^${key}=" "$ENV_FILE" | head -n1 | cut -d= -f2- || true
}

is_placeholder() {
  local v="$1"
  [[ -z "$v" ]] && return 0
  case "$v" in
    REPLACE_WITH_GENERATED_KEY|change-me*|admin|dev-secret) return 0 ;;
  esac
  return 1
}

# set KEY=VAL in $ENV_FILE (replace if present, append if not)
set_kv() {
  local key="$1" val="$2"
  if [[ -f "$ENV_FILE" ]] && grep -qE "^${key}=" "$ENV_FILE"; then
    # use python for a safe, portable in-place replace (no sed -i quirks, no regex-escaping of val)
    python3 - "$ENV_FILE" "$key" "$val" <<'PY'
import sys, io
path, key, val = sys.argv[1], sys.argv[2], sys.argv[3]
out = io.StringIO()
with open(path, "r", encoding="utf-8") as f:
    for line in f:
        if line.split("=", 1)[0] == key:
            out.write(f"{key}={val}\n")
        else:
            out.write(line)
with open(path, "w", encoding="utf-8") as f:
    f.write(out.getvalue())
PY
  else
    printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
  fi
}

# ---- seed .env from template if missing ----
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$TEMPLATE" ]]; then
    cp "$TEMPLATE" "$ENV_FILE"
    yellow "No $ENV_FILE found — created from $TEMPLATE."
  else
    : > "$ENV_FILE"
    yellow "No $ENV_FILE or $TEMPLATE found — created empty $ENV_FILE."
  fi
fi

CUR_SECRET="$(current IRS_SECRET_KEY)"
CUR_ENC="$(current IRS_ENCRYPTION_KEY)"
CUR_PW="$(current IRS_BOOTSTRAP_ADMIN_PASSWORD)"

# ---- decide which keys to (re)generate ----
declare -A WILL_SET=()

maybe() {
  local key="$1" cur="$2"
  if [[ $ONLY_MISSING -eq 1 ]]; then
    if is_placeholder "$cur"; then WILL_SET["$key"]=1; fi
  else
    WILL_SET["$key"]=1
  fi
}
maybe IRS_SECRET_KEY "$CUR_SECRET"
maybe IRS_ENCRYPTION_KEY "$CUR_ENC"
maybe IRS_BOOTSTRAP_ADMIN_PASSWORD "$CUR_PW"

if [[ ${#WILL_SET[@]} -eq 0 ]]; then
  green "All required keys already set in $ENV_FILE — nothing to do."
  exit 0
fi

# ---- warn ----
echo
red    "============================================================"
red    "  WARNING: this will modify $ENV_FILE"
red    "============================================================"
echo   "The following keys will be (re)generated:"
for k in "${!WILL_SET[@]}"; do echo "  - $k"; done
echo
if [[ -n "${WILL_SET[IRS_ENCRYPTION_KEY]:-}" ]] && ! is_placeholder "$CUR_ENC"; then
  red  "  !! IRS_ENCRYPTION_KEY already has a real value."
  red  "  !! Rotating it makes ALL existing encrypted reports PERMANENTLY"
  red  "  !! unreadable unless you keep the old key."
  echo
fi
if [[ -n "${WILL_SET[IRS_SECRET_KEY]:-}" ]] && ! is_placeholder "$CUR_SECRET"; then
  yellow "  • Rotating IRS_SECRET_KEY will invalidate all active login sessions"
  yellow "    (users and agents using JWTs must re-authenticate)."
  echo
fi
yellow "A timestamped backup of the current file will be saved alongside it."
echo

if [[ $ASSUME_YES -ne 1 ]]; then
  read -r -p "Type 'yes' to continue: " ans
  if [[ "$ans" != "yes" ]]; then
    echo "Aborted — no changes made."
    exit 1
  fi
fi

# ---- backup ----
BACKUP="${ENV_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
cp "$ENV_FILE" "$BACKUP"

# ---- write ----
[[ -n "${WILL_SET[IRS_SECRET_KEY]:-}" ]]               && set_kv IRS_SECRET_KEY               "$(gen_secret_key)"
[[ -n "${WILL_SET[IRS_ENCRYPTION_KEY]:-}" ]]           && set_kv IRS_ENCRYPTION_KEY           "$(gen_enc_key)"
if [[ -n "${WILL_SET[IRS_BOOTSTRAP_ADMIN_PASSWORD]:-}" ]]; then
  NEW_PW="$(gen_password)"
  set_kv IRS_BOOTSTRAP_ADMIN_PASSWORD "$NEW_PW"
fi

chmod 600 "$ENV_FILE" 2>/dev/null || true

echo
green "Done."
echo  "  updated: $ENV_FILE"
echo  "  backup : $BACKUP"
[[ -n "${NEW_PW:-}" ]] && yellow "  bootstrap admin password: $NEW_PW  (only applies on FIRST server start)"
echo
echo  "Remember to set your AI provider keys (e.g. ANTHROPIC_API_KEY) in $ENV_FILE."
