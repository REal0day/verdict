#!/usr/bin/env bash
# Run the full test suite + frontend typecheck and append a timestamped record
# to test-records/. Keeps a durable history of what passed when.
#
#   ./scripts/record-tests.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p test-records
TS=$(date -u +%Y%m%dT%H%M%SZ)
COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "nogit")

echo "Running backend suite in the server container…"
docker compose exec -T server sh -c 'rm -rf /srv/tests'
docker compose cp server/tests server:/srv/tests >/dev/null
docker compose exec -T server sh -c \
  'pip install pytest -q >/dev/null 2>&1; cd /srv && python -m pytest tests/ -v -p no:cacheprovider 2>&1' \
  > /tmp/pyout.txt || true
PYVER=$(docker compose exec -T server python --version 2>&1 | tr -d '\r')

echo "Typechecking frontend…"
( cd frontend && npx tsc -b >/tmp/tscout.txt 2>&1 ) || true

PASS=$(grep -c "PASSED" /tmp/pyout.txt || true)
FAIL=$(grep -c "FAILED" /tmp/pyout.txt || true)
ERR=$(grep -c "ERROR" /tmp/pyout.txt || true)
SUMMARY=$(grep -E "passed|failed|error" /tmp/pyout.txt | tail -1 | sed 's/=//g' | xargs || echo "no summary")
OUT="test-records/${TS}.md"

{
  echo "# Test record — ${TS}"; echo
  echo "| field | value |"; echo "|---|---|"
  echo "| Timestamp (UTC) | ${TS} |"
  echo "| Git commit | \`${COMMIT}\` |"
  echo "| Runtime | ${PYVER} (in Docker), pytest |"
  echo "| Backend result | **${SUMMARY}** |"
  echo "| Frontend tsc -b | **$([ -s /tmp/tscout.txt ] && echo FAILED || echo clean)** |"
  echo "| Backend PASSED / FAILED / ERROR | ${PASS} / ${FAIL} / ${ERR} |"
  echo; echo "## Backend — per-test results"; echo
  python3 - <<'PY'
import re, collections
by = collections.OrderedDict()
for ln in open("/tmp/pyout.txt").read().splitlines():
    m = re.match(r'^(tests/[^:]+)::([^ ]+)\s+(PASSED|FAILED|ERROR|SKIPPED)', ln)
    if m:
        f, name, res = m.groups()
        by.setdefault(f, []).append((name, res))
for f, tests in by.items():
    n = sum(1 for _, r in tests if r == "PASSED")
    print(f"### `{f}` — {n}/{len(tests)} passed\n")
    for name, res in tests:
        print(f"- {'✓' if res=='PASSED' else '✗ '+res} `{name}`")
    print()
PY
  echo "## Frontend"; echo '```'
  echo "tsc -b: $([ -s /tmp/tscout.txt ] && cat /tmp/tscout.txt | head -20 || echo 'clean (exit 0)')"
  echo '```'
} > "$OUT"
echo "Wrote $OUT"
