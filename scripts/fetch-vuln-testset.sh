#!/usr/bin/env bash
# Download EVERY curated case from scripts/vuln-testset.txt into one folder,
# ready to hand to Verdict. Writes a _FINDING.md per repo (the bug report to
# paste into the case) and an INDEX.md.
#
#   scripts/fetch-vuln-testset.sh [dest_dir]     # default: ./vuln-testset
set -euo pipefail
cd "$(dirname "$0")/.."
dest="${1:-./vuln-testset}"
manifest="scripts/vuln-testset.txt"
mkdir -p "$dest"

index="$dest/INDEX.md"
{ echo "# Verdict vulnerable-source test set"; echo
  echo "Full vulnerable source (no fix, no .git). Point SOURCE_ROOT at this dir,"
  echo "open a case, pick the repo as the local source, paste the finding from"
  echo "the repo's _FINDING.md, and run the pipeline. Grade against fixed_ref."
  echo; echo "| repo | CVE | vulnerable | fixed |"; echo "|---|---|---|---|"; } > "$index"

grep -v '^#' "$manifest" | grep -v '^[[:space:]]*$' | while IFS='|' read -r name url vref fref cve title finding; do
  scripts/fetch-vuln-repo.sh "$name" "$url" "$vref" "$dest"
  { echo "# $name — $cve"; echo; echo "**$title**"; echo
    echo "- vulnerable ref: \`$vref\`  ·  fixed ref: \`$fref\`  ·  $cve"
    echo "- source: whole tree under \`$name/\` (fix removed)"; echo
    echo "## Bug report (paste this as the case's report)"; echo; echo "$finding"; } > "$dest/$name/_FINDING.md"
  echo "| \`$name\` | $cve | \`$vref\` | \`$fref\` |" >> "$index"
done

echo
echo "Done → $dest"
echo "Next: set SOURCE_ROOT=$(cd "$dest" && pwd) in .env, docker compose up -d,"
echo "then open a case per repo and paste its _FINDING.md."
