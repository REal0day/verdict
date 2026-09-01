import base64
import datetime as dt
import hashlib
import logging
import mimetypes
from pathlib import Path

import httpx

from .config import AgentConf, load_state, save_state

log = logging.getLogger("irs-agent")

# Don't bother shipping anything bigger than 50 MiB — server caps there too.
_MAX_ATTACH = 50 * 1024 * 1024


class Uploader:
    def __init__(self, conf: AgentConf):
        self.conf = conf
        self.state = load_state()  # { path: sha256 }
        self.client = httpx.Client(
            base_url=conf.server_url.rstrip("/"),
            headers={"X-Agent-Key": conf.api_key},
            verify=conf.verify_tls,
            timeout=60,
        )

    def maybe_upload(self, path: Path, source_tool: str, session_id: str | None = None):
        try:
            raw = path.read_bytes()
        except Exception as e:
            log.debug("skip %s: %s", path, e)
            return
        sha = hashlib.sha256(raw).hexdigest()
        key = str(path.resolve())
        if self.state.get(key) == sha:
            return  # unchanged
        body = {
            "filename": path.name,
            "original_path": key,
            "source_tool": source_tool,
            "sha256": sha,
            "size_bytes": len(raw),
            "file_mtime": dt.datetime.fromtimestamp(
                path.stat().st_mtime, tz=dt.timezone.utc
            ).isoformat(),
            "content_b64": base64.b64encode(raw).decode("ascii"),
        }
        if session_id:
            body["session_id"] = session_id
        try:
            r = self.client.post("/reports/ingest", json=body)
            if r.status_code in (200, 201):
                self.state[key] = sha
                save_state(self.state)
                log.info("uploaded %s (%s)", path.name, source_tool)
            else:
                log.warning("upload %s -> %s %s", path.name, r.status_code, r.text[:200])
        except Exception as e:
            log.warning("upload %s failed: %s", path.name, e)

    def maybe_upload_attachment(self, path: Path, session_id: str | None = None):
        """Ship a non-markdown artefact (e.g. a POC file). Same dedup-by-sha
        rules as reports — re-running on the same file is idempotent."""
        try:
            raw = path.read_bytes()
        except Exception as e:
            log.debug("skip attachment %s: %s", path, e)
            return
        if len(raw) > _MAX_ATTACH:
            log.warning("attachment %s too large (%d bytes) — skipping", path, len(raw))
            return
        sha = hashlib.sha256(raw).hexdigest()
        key = "attach::" + str(path.resolve())
        if self.state.get(key) == sha:
            return  # unchanged

        ctype, _ = mimetypes.guess_type(path.name)
        body = {
            "filename": path.name,
            "original_path": str(path.resolve()),
            "session_id": session_id,
            "content_type": ctype or "application/octet-stream",
            "sha256": sha,
            "size_bytes": len(raw),
            "content_b64": base64.b64encode(raw).decode("ascii"),
        }
        try:
            r = self.client.post("/attachments/ingest", json=body)
            if r.status_code in (200, 201):
                self.state[key] = sha
                save_state(self.state)
                log.info("uploaded attachment %s (%d bytes)", path.name, len(raw))
            else:
                log.warning("attachment %s -> %s %s", path.name, r.status_code, r.text[:200])
        except Exception as e:
            log.warning("attachment %s failed: %s", path.name, e)
