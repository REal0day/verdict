"""Local source under SOURCE_ROOT — resolve, confine, browse.

Option 1 of the Investigations source model: reference code on disk instead of
copying it. The operator mounts SOURCE_ROOT read-only (see docker-compose.yml);
a case's `local_path` is a subdir *relative to that root*. This module resolves
it, confines it to the root (no `..`/symlink escape), and lists it for the
wizard picker. If SOURCE_ROOT isn't configured, local-path is unavailable and
the caller falls back to upload (option 3).
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import settings


class SourceError(Exception):
    """A local-path problem the caller should surface as a 400."""


def root() -> Path | None:
    """The mounted SOURCE_ROOT, or None when it isn't usable.

    "Usable" means configured, present, and non-empty — the placeholder mount
    (an empty dir) reads as not-configured so the UI can say "set SOURCE_ROOT"
    rather than show an empty picker.
    """
    m = (settings.source_mount or "").strip()
    if not m:
        return None
    p = Path(m)
    if not p.is_dir():
        return None
    try:
        next(p.iterdir())
    except (StopIteration, OSError):
        return None
    return p


def available() -> bool:
    return root() is not None


def resolve(subpath: str) -> Path:
    """Resolve a subpath under SOURCE_ROOT, confined to it. Raises SourceError."""
    base = root()
    if base is None:
        raise SourceError(
            "No source root is configured. Set SOURCE_ROOT in .env and restart, "
            "or attach the source by upload instead."
        )
    rel = (subpath or "").strip().lstrip("/")
    base_r = base.resolve()
    target = (base_r / rel).resolve()
    # Confinement: the resolved target must live inside the resolved root.
    if target != base_r and base_r not in target.parents:
        raise SourceError("Path escapes the source root.")
    if not target.exists():
        raise SourceError(f"Path not found under the source root: {rel or '.'}")
    if not target.is_dir():
        raise SourceError("Source path must be a directory.")
    return target


def browse(subpath: str = "") -> dict:
    """List immediate subdirectories of a path under SOURCE_ROOT, for the picker."""
    base = root()
    if base is None:
        return {"available": False, "path": "", "dirs": []}
    target = resolve(subpath)
    base_r = base.resolve()
    rel = "" if target == base_r else str(target.relative_to(base_r))
    dirs = []
    try:
        for entry in sorted(os.scandir(target), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(entry.name)
            except OSError:
                continue
    except OSError as e:
        raise SourceError(f"Could not read the directory: {type(e).__name__}")
    return {"available": True, "path": rel, "dirs": dirs}
