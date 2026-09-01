"""
Disk-backed, encrypted blob store for large session uploads.

Blobs are content-addressed under
``{IRS_DATA_DIR}/session_uploads/{sha[:2]}/{sha}.bin`` and written via
:func:`crypto.encrypt_stream`, so a leaked volume is useless without
``IRS_ENCRYPTION_KEY``. Because the path is the plaintext sha256,
identical files uploaded to different sessions share one blob on disk;
the DB row (``SessionUpload.storage_key``) is the per-session reference.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from . import crypto
from .config import settings

_SUBDIR = "session_uploads"


def _root() -> Path:
    p = Path(settings.data_dir) / _SUBDIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def _path(key: str) -> Path:
    return Path(settings.data_dir) / key


def put_stream(src: BinaryIO) -> tuple[str, str, int]:
    """Encrypt ``src`` to disk. Returns (storage_key, sha256, size).

    Writes to a temp file first, then renames into the content-addressed
    location so concurrent uploads of the same bytes are safe and a crash
    mid-write never leaves a half-file at the final path."""
    root = _root()
    fd, tmp = tempfile.mkstemp(prefix=".up-", dir=root)
    try:
        with os.fdopen(fd, "wb") as out:
            sha, size = crypto.encrypt_stream(src, out)
        sub = root / sha[:2]
        sub.mkdir(exist_ok=True)
        final = sub / f"{sha}.bin"
        if final.exists():
            os.unlink(tmp)
        else:
            os.replace(tmp, final)
        key = str(final.relative_to(Path(settings.data_dir)))
        return key, sha, size
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def open_raw(key: str) -> BinaryIO:
    """Open the encrypted blob for streaming (feed to crypto.decrypt_iter
    / DecryptReader)."""
    return open(_path(key), "rb")


def reader(key: str) -> crypto.DecryptReader:
    return crypto.DecryptReader(open_raw(key))


def remove(key: str) -> None:
    try:
        _path(key).unlink()
    except FileNotFoundError:
        pass
