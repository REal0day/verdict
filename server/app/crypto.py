"""
At-rest encryption using AES-256-GCM with a single server master key
(IRS_ENCRYPTION_KEY).

Two formats:

* In-DB blobs (`encrypt`/`decrypt`): nonce(12) || ct — whole-buffer.
* On-disk streams (`encrypt_stream`/`decrypt_iter`): a sequence of
  independent GCM frames so multi-GB files never sit in RAM. Each frame
  is  u32be(len(ct)) || nonce(12) || ct  where ct includes the 16-byte
  tag. Plaintext chunk size is `STREAM_CHUNK` (4 MiB).
"""
import base64
import hashlib
import io
import os
import struct
from typing import BinaryIO, Iterator

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

STREAM_CHUNK = 4 * 1024 * 1024
_HDR = struct.Struct(">I")


def _master_key() -> bytes:
    raw = settings.encryption_key
    if not raw:
        raise RuntimeError(
            "IRS_ENCRYPTION_KEY is not set. Generate one (see .env.example)."
        )
    key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    if len(key) != 32:
        raise RuntimeError("IRS_ENCRYPTION_KEY must decode to 32 bytes.")
    return key


def encrypt(plaintext: bytes, aad: bytes = b"") -> bytes:
    key = _master_key()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad or None)
    return nonce + ct


def decrypt(blob: bytes, aad: bytes = b"") -> bytes:
    key = _master_key()
    nonce, ct = blob[:12], blob[12:]
    return AESGCM(key).decrypt(nonce, ct, aad or None)


def encrypt_str(s: str) -> bytes:
    return encrypt(s.encode("utf-8"))


def decrypt_str(blob: bytes) -> str:
    return decrypt(blob).decode("utf-8")


# ---- streaming (chunked GCM) ----------------------------------------------

def encrypt_stream(src: BinaryIO, dst: BinaryIO) -> tuple[str, int]:
    """Encrypt `src` into `dst` as a sequence of GCM frames. Returns
    (sha256 of plaintext, plaintext byte count)."""
    gcm = AESGCM(_master_key())
    h = hashlib.sha256()
    total = 0
    while True:
        chunk = src.read(STREAM_CHUNK)
        if not chunk:
            break
        h.update(chunk)
        total += len(chunk)
        nonce = os.urandom(12)
        ct = gcm.encrypt(nonce, chunk, None)
        dst.write(_HDR.pack(len(ct)))
        dst.write(nonce)
        dst.write(ct)
    return h.hexdigest(), total


def decrypt_iter(src: BinaryIO) -> Iterator[bytes]:
    """Yield plaintext chunks from a stream produced by `encrypt_stream`."""
    gcm = AESGCM(_master_key())
    while True:
        hdr = src.read(_HDR.size)
        if not hdr:
            return
        if len(hdr) != _HDR.size:
            raise ValueError("truncated stream header")
        (clen,) = _HDR.unpack(hdr)
        nonce = src.read(12)
        ct = src.read(clen)
        if len(nonce) != 12 or len(ct) != clen:
            raise ValueError("truncated stream frame")
        yield gcm.decrypt(nonce, ct, None)


class DecryptReader(io.RawIOBase):
    """File-like `.read(n)` adapter over `decrypt_iter(src)` so callers
    like `tarfile.addfile` / `shutil.copyfileobj` can consume a stream
    without materialising it."""

    def __init__(self, src: BinaryIO):
        self._it = decrypt_iter(src)
        self._buf = b""

    def readable(self) -> bool:
        return True

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            chunks = [self._buf]; self._buf = b""
            chunks.extend(self._it)
            return b"".join(chunks)
        while len(self._buf) < n:
            try:
                self._buf += next(self._it)
            except StopIteration:
                break
        out, self._buf = self._buf[:n], self._buf[n:]
        return out
