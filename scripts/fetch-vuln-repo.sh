#!/usr/bin/env bash
# Clone ONE repo at a vulnerable ref into a folder you can hand to Verdict,
# then scrub files that would give the answer away (changelogs, security notes).
#
#   scripts/fetch-vuln-repo.sh <name> <git_url> <ref> [dest_dir]
#
# The tree it produces is the FULL vulnerable source with no fix and no .git.
# Point SOURCE_ROOT at the dest dir and pick <name> as the case's local source.
set -euo pipefail
name="${1:?usage: fetch-vuln-repo.sh <name> <git_url> <ref> [dest]}"
url="${2:?missing git_url}"
ref="${3:?missing ref}"
dest="${4:-./vuln-testset}"

out="$dest/$name"
mkdir -p "$dest"
rm -rf "$out"

echo "→ $name @ $ref"
# Shallow clone at the tag/branch; fall back to full clone + checkout if the
# host doesn't allow --branch on a tag.
if ! git clone --depth 1 --branch "$ref" "$url" "$out" 2>/dev/null; then
  echo "  (shallow --branch failed; full clone + checkout)"
  git clone "$url" "$out" >/dev/null 2>&1
  ( cd "$out" && git checkout -q "$ref" )
fi

# Drop version-control + answer-key files so we test detection, not reading.
rm -rf "$out/.git" "$out/.github"
find "$out" -maxdepth 2 -type f \( \
     -iname 'CHANGELOG*' -o -iname 'CHANGES*' -o -iname 'HISTORY*' \
  -o -iname 'SECURITY*'  -o -iname 'ADVISOR*'  -o -iname '*.cve' \
  -o -iname 'RELEASE*'   -o -iname 'NEWS*' \) -delete 2>/dev/null || true

files=$(find "$out" -type f | wc -l | tr -d ' ')
echo "  ✓ $out  ($files files, scrubbed)"
