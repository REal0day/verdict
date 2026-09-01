"""One-shot scan + upload of pre-existing artifacts.

Used by `irs-agent import` (and invoked from install.sh) so that a user can
backfill .md reports and POC artifacts that existed on the machine before the
agent was installed. Everything goes through the same `Uploader` plumbing as
the watcher / hook paths, so dedup-by-sha just works and re-runs are no-ops.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

from .config import AgentConf
from .filters import should_skip

# Default directories to search if not added explicitly via --path. We pick
# up the configured collectors' paths automatically too.
_DEFAULT_EXTRA = [
    "~/my-reports",
    "~/reports",
    "~/claude-reports",       # also a collector default; harmless overlap
    "~/.claude/projects",     # ditto
]

# Don't recurse into well-known noise dirs.
_SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn",
    "node_modules", ".venv", "venv", "__pycache__", ".cache",
    ".npm", ".cargo", ".rustup", "dist", "build",
}

# Cap per-attachment size so we don't try to ship a 5 GB tarball.
_MAX_ATTACH_BYTES = 50 * 1024 * 1024


def run_import(conf: AgentConf, extra_paths: list[str] | None = None, assume_yes: bool = False) -> int:
    if not conf.api_key or not conf.server_url:
        print("agent not initialised; run `irs-agent init ...` first", file=sys.stderr)
        return 2

    roots = _resolve_roots(conf, extra_paths or [])
    if not roots:
        print("no directories to scan.")
        return 0

    print("Scanning for existing reports + POC files…")
    for r in roots:
        marker = "" if r.exists() else " (missing)"
        print(f"  {r}{marker}")

    reports: list[Path] = []
    attachments: list[Path] = []
    for r in roots:
        if not r.exists() or not r.is_dir():
            continue
        for p in _walk(r):
            if not p.is_file():
                continue
            if should_skip(p):
                continue
            is_poc = any(seg.lower() == "poc" for seg in p.parts)
            if p.suffix.lower() == ".md":
                reports.append(p)
            elif is_poc:
                if p.stat().st_size <= _MAX_ATTACH_BYTES:
                    attachments.append(p)
                else:
                    print(f"  skip (too large): {p}")

    reports = sorted(set(map(_resolved, reports)))
    attachments = sorted(set(map(_resolved, attachments)))

    if not reports and not attachments:
        print("\nNothing to upload — no .md reports or poc/ files found.")
        return 0

    print(f"\nFound {len(reports)} report(s) and {len(attachments)} POC file(s):")
    for p in reports:
        print(f"  [md]  {p}  ({_size(p)})")
    for p in attachments:
        print(f"  [poc] {p}  ({_size(p)})")

    if not assume_yes:
        if not sys.stdin.isatty():
            print("\nNon-interactive shell; re-run with --yes to upload.")
            return 0
        ans = input(f"\nUpload all {len(reports) + len(attachments)}? [Y/n] ").strip().lower()
        if ans not in ("", "y", "yes"):
            print("aborted.")
            return 0

    # Upload via the existing Uploader. It dedupes by sha so re-runs are safe.
    from .uploader import Uploader
    up = Uploader(conf)

    print()
    ok_md = ok_att = 0
    for p in reports:
        try:
            up.maybe_upload(p, "claude_code")   # no session_id (orphan/historical)
            ok_md += 1
        except Exception as e:
            print(f"  failed: {p}: {e}", file=sys.stderr)
    for p in attachments:
        try:
            up.maybe_upload_attachment(p)        # no session_id either
            ok_att += 1
        except Exception as e:
            print(f"  failed: {p}: {e}", file=sys.stderr)

    print(f"\nDone. {ok_md} report(s) + {ok_att} POC file(s) processed "
          f"(unchanged files were skipped by sha dedup).")
    return 0


# ---- helpers ----

def _resolve_roots(conf: AgentConf, extra: list[str]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    candidates: list[str] = list(extra) + list(_DEFAULT_EXTRA)
    for col in conf.collectors or []:
        candidates.extend(col.paths or [])
    for s in candidates:
        p = Path(os.path.expandvars(os.path.expanduser(s))).resolve()
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _walk(root: Path) -> Iterable[Path]:
    """os.walk that prunes noisy directories in-place."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES and not d.startswith(".cache")]
        d = Path(dirpath)
        for name in filenames:
            yield d / name


def _resolved(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


def _size(p: Path) -> str:
    try:
        n = p.stat().st_size
    except OSError:
        return "?"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/1024/1024:.1f} MB"
