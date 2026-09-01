"""Long-poll the Verdict server for prompts the owner submitted via the portal,
run them through headless Claude, stream structured progress events back as
they happen, and post the final result.

Up to `remote_max_concurrent` Claude processes run in parallel so the
analyst can keep several Workbench sessions going at once.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

import httpx
import platformdirs

from . import __version__
from .config import AgentConf

log = logging.getLogger("irs-agent.remote")

_POLL_TIMEOUT_S = 30
_MAX_OUTPUT = 200_000
_TOOL_PREVIEW = 400
_SESS_ROOT = Path(platformdirs.user_data_dir("irs-agent")) / "sessions"


def start(conf: AgentConf):
    if not getattr(conf, "remote_prompt", True):
        log.info("remote prompt disabled in config")
        return
    t = threading.Thread(target=_loop, args=(conf,), daemon=True, name="remote-prompt")
    t.start()
    log.info("remote-prompt poll loop started (timeout %ds, max %d concurrent)",
             getattr(conf, "remote_timeout_s", 1800),
             getattr(conf, "remote_max_concurrent", 3))


def _loop(conf: AgentConf):
    client = httpx.Client(
        base_url=conf.server_url.rstrip("/"),
        headers={"X-Agent-Key": conf.api_key, "X-Agent-Version": __version__},
        verify=conf.verify_tls,
        timeout=_POLL_TIMEOUT_S + 10,
    )
    max_slots = max(1, getattr(conf, "remote_max_concurrent", 3))
    slots = threading.Semaphore(max_slots)
    while True:
        slots.acquire()
        released = False  # set once the slot is handed off or released, so the
                          # finally below never double-releases or leaks it
        try:
            job = _poll_once(client)
            if job is None:
                continue
            cmd = job.get("command")
            if cmd == "set_anthropic_key":
                conf.anthropic_api_key = job.get("key") or ""
                conf.save()
                log.info("anthropic key %s via portal",
                         "updated" if conf.anthropic_api_key else "cleared")
                _ack_key_applied(client)
                continue
            if cmd == "upgrade":
                slots.release()
                released = True
                _drain(slots, max_slots)
                _self_upgrade(conf)
                continue
            if cmd:
                # Unknown control command from a newer server — ignore it rather
                # than fall through and treat it as a job, which would KeyError
                # on the missing request_id and kill this loop for good.
                log.warning("ignoring unknown poll command: %r", cmd)
                continue
            if not job.get("request_id"):
                log.warning("poll returned a job with no request_id; skipping: %r", job)
                continue
            threading.Thread(
                target=_run_job, args=(conf, client, job, slots),
                daemon=True, name=f"remote-{job['request_id'][:8]}",
            ).start()
            released = True  # the job thread now owns the slot and releases it
        except Exception:
            log.exception("poll loop iteration failed; continuing")
        finally:
            if not released:
                slots.release()


def _drain(slots: threading.Semaphore, n: int):
    """Wait for all in-flight jobs to finish before we re-exec."""
    log.info("upgrade requested — waiting for %d slot(s) to drain", n)
    for _ in range(n):
        slots.acquire()
    for _ in range(n):
        slots.release()


def _self_upgrade(conf: AgentConf):
    url = conf.server_url.rstrip("/") + "/ui/agent/source.tar.gz"
    log.info("self-upgrade: downloading %s", url)
    try:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
            with httpx.stream("GET", url,
                              headers={"X-Agent-Key": conf.api_key},
                              verify=conf.verify_tls, timeout=120) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes():
                    f.write(chunk)
            tarball = f.name
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "--upgrade", "--force-reinstall", "--no-deps", tarball],
                       check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", tarball],
                       check=True)
        os.unlink(tarball)
    except Exception as e:
        log.error("self-upgrade failed: %s — continuing on old version", e)
        return
    log.info("self-upgrade installed — re-exec'ing")
    argv = [sys.executable, "-m", "irs_agent", "run"]
    os.execv(sys.executable, argv)


def _ack_key_applied(client: httpx.Client):
    """Confirm to the server that the pushed Anthropic key was saved, so it stops
    re-sending it. Best-effort: if this fails the server just re-delivers on the
    next poll, so the key is never silently lost."""
    try:
        client.post("/agent/remote/key-applied")
    except Exception as e:
        log.warning("failed to ack anthropic key application: %s", e)


def _poll_once(client: httpx.Client) -> dict | None:
    try:
        r = client.get("/agent/remote/poll")
    except Exception as e:
        log.warning("poll failed: %s", e)
        time.sleep(5)
        return None
    if r.status_code == 204:
        return None
    if r.status_code != 200:
        log.warning("poll -> %s %s", r.status_code, r.text[:200])
        time.sleep(5)
        return None
    return r.json()


def _run_job(conf: AgentConf, client: httpx.Client, job: dict,
             slots: threading.Semaphore):
    rid = job["request_id"]
    try:
        sink = _EventSink(client, rid)
        cwd = job.get("cwd")
        workspace = None
        if job.get("bundle_url"):
            try:
                workspace = _materialize(conf, job, sink)
                cwd = workspace
            except Exception as e:
                log.exception("materialize failed for %s", rid)
                sink.emit({"type": "error",
                           "text": f"workspace materialize failed: {e}"})
                _post_result(client, rid, False, "",
                             f"workspace materialize failed: {e}", None, None)
                return
        ok, out, err, sid = _run_claude(
            job.get("prompt", ""), cwd,
            resume=job.get("resume"),
            timeout_s=getattr(conf, "remote_timeout_s", 1800),
            anthropic_api_key=conf.anthropic_api_key,
            sink=sink,
            # per-session model from the portal wins; agent config is the fallback
            model=job.get("model") or getattr(conf, "claude_model", ""),
            # engagement authorisation for active-testing sessions (server-decided)
            append_system_prompt=job.get("append_system_prompt") or "",
            # active-testing sessions run headless with no one to grant approvals
            bypass_permissions=bool(job.get("bypass_permissions")),
        )
        sink.close()
        _post_result(client, rid, ok, out, err, sid, workspace)
    except Exception as e:
        log.exception("job %s crashed", rid)
        _post_result(client, rid, False, "", f"agent crashed: {e}", None, None)
    finally:
        slots.release()


def _materialize(conf: AgentConf, job: dict, sink: "_EventSink") -> str:
    """Download the session bundle and extract it into a per-session
    scratch dir. Returns the absolute workspace path."""
    sid = job.get("session_id") or job["request_id"]
    dest = _SESS_ROOT / sid
    dest.mkdir(parents=True, exist_ok=True)
    url = conf.server_url.rstrip("/") + job["bundle_url"]
    sink.emit({"type": "bundle", "phase": "download", "dest": str(dest)})
    log.info("materialize: downloading %s -> %s", url, dest)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
        with httpx.stream("GET", url,
                          headers={"X-Agent-Key": conf.api_key},
                          verify=conf.verify_tls, timeout=300) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                f.write(chunk)
        tarball = f.name
    try:
        sink.emit({"type": "bundle", "phase": "extract", "dest": str(dest)})
        n = _safe_extract(tarball, dest)
        sink.emit({"type": "bundle", "phase": "ready",
                   "dest": str(dest), "files": n})
    finally:
        try:
            os.unlink(tarball)
        except OSError:
            pass
    return str(dest)


def _safe_extract(tarball: str, dest: Path) -> int:
    dest = dest.resolve()
    n = 0
    with tarfile.open(tarball, "r:*") as tf:
        for m in tf.getmembers():
            if not (m.isreg() or m.isdir()):
                continue
            target = (dest / m.name).resolve()
            if dest not in target.parents and target != dest:
                raise ValueError(f"unsafe path in bundle: {m.name}")
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is None:
                continue
            with open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            os.chmod(target, m.mode & 0o777 or 0o644)
            n += 1
    return n


class _EventSink:
    """Ships slimmed stream-json events to the server one at a time so the
    portal can render a live phase timeline."""

    def __init__(self, client: httpx.Client, rid: str):
        self.client = client
        self.rid = rid
        self.disabled = False
        self.bytes = 0

    def emit(self, ev: dict):
        if self.disabled:
            return
        try:
            payload = json.dumps(ev)
        except Exception:
            return
        self.bytes += len(payload)
        if self.bytes > _MAX_OUTPUT:
            self._post({"type": "truncated"})
            self.disabled = True
            return
        self._post(ev)

    def _post(self, ev: dict):
        try:
            r = self.client.post(f"/agent/remote/{self.rid}/chunk",
                                 json={"event": ev}, timeout=15)
            if r.status_code == 404:
                self.disabled = True
        except Exception as e:
            log.debug("chunk post failed (continuing): %s", e)

    def close(self):
        pass


def _post_result(client: httpx.Client, rid: str, ok: bool, out: str, err: str,
                 claude_sid: str | None, workspace: str | None):
    body = {"ok": ok, "output": out, "error": err,
            "claude_session_id": claude_sid, "workspace": workspace}
    for attempt in range(4):
        try:
            r = client.post(f"/agent/remote/{rid}/result", json=body, timeout=30)
            if r.status_code in (204, 404):
                return
            log.warning("result -> %s %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("post result failed (try %d): %s", attempt + 1, e)
        time.sleep(2 * (attempt + 1))


def _decode(b) -> str:
    if b is None:
        return ""
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return str(b)


def _hint(inp: dict) -> str:
    for k in ("file_path", "path", "command", "pattern", "url",
              "query", "description"):
        v = inp.get(k)
        if v:
            return str(v)[:200]
    return ""


def _slim(ev: dict) -> dict | None:
    """Reduce a raw stream-json event to just what the UI needs."""
    t = ev.get("type")
    if t == "system":
        return {"type": "system", "subtype": ev.get("subtype"),
                "session_id": ev.get("session_id"),
                "model": ev.get("model"), "cwd": ev.get("cwd")}
    if t == "assistant":
        content = (ev.get("message") or {}).get("content") or []
        out: list[dict] = []
        for c in content:
            ct = c.get("type")
            if ct == "text" and c.get("text"):
                out.append({"type": "text", "text": c["text"]})
            elif ct == "thinking":
                txt = (c.get("thinking") or "").strip()
                if txt:
                    out.append({"type": "thinking", "text": txt[:_TOOL_PREVIEW]})
            elif ct == "tool_use":
                out.append({"type": "tool_use", "name": c.get("name", "?"),
                            "hint": _hint(c.get("input") or {})})
        return {"type": "assistant", "content": out} if out else None
    if t == "user":
        content = (ev.get("message") or {}).get("content") or []
        out = []
        for c in content:
            if c.get("type") == "tool_result":
                raw = c.get("content")
                if isinstance(raw, list):
                    txt = "".join(p.get("text", "") for p in raw
                                  if isinstance(p, dict))
                else:
                    txt = str(raw or "")
                out.append({"type": "tool_result",
                            "is_error": bool(c.get("is_error")),
                            "preview": txt[:_TOOL_PREVIEW]})
        return {"type": "tool_result", "results": out} if out else None
    if t == "result":
        return {"type": "result", "subtype": ev.get("subtype"),
                "duration_ms": ev.get("duration_ms"),
                "total_cost_usd": ev.get("total_cost_usd"),
                "num_turns": ev.get("num_turns")}
    return None


def _run_claude(prompt: str, cwd: str | None, *, resume: str | None,
                timeout_s: int, anthropic_api_key: str, sink: _EventSink,
                model: str = "", append_system_prompt: str = "",
                bypass_permissions: bool = False
                ) -> tuple[bool, str, str, str | None]:
    exe = shutil.which("claude")
    if not exe:
        return False, "", "`claude` CLI not found on PATH on the agent host", None
    workdir = Path(cwd).expanduser() if cwd else Path.home()
    if not workdir.is_dir():
        return False, "", f"working directory does not exist: {workdir}", None

    argv = [exe, "-p", "--verbose", "--output-format", "stream-json"]
    if model:
        argv += ["--model", model]
    if append_system_prompt:
        argv += ["--append-system-prompt", append_system_prompt]
    if bypass_permissions:
        # Active-testing sessions run headless — no one can answer permission
        # prompts, and the harness must pip-install + run python/curl/nmap/etc.
        argv += ["--dangerously-skip-permissions"]
    if resume:
        argv += ["--resume", resume]
    argv.append(prompt)

    env = os.environ.copy()
    if anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = anthropic_api_key

    log.info("running claude in %s (timeout %ds, resume=%s, model=%s, active_testing=%s, skip_perms=%s)",
             workdir, timeout_s, bool(resume), model or "<cli-default>",
             bool(append_system_prompt), bool(bypass_permissions))
    sink.emit({"type": "launch", "cwd": str(workdir), "resume": bool(resume),
               "model": model or None, "active_testing": bool(append_system_prompt)})

    try:
        p = subprocess.Popen(
            argv, cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
    except Exception as e:
        return False, "", f"failed to launch claude: {e}", None

    deadline = time.monotonic() + timeout_s
    final_text: str | None = None
    final_err: str | None = None
    claude_sid: str | None = resume
    text_acc: list[str] = []

    try:
        assert p.stdout is not None
        for line in p.stdout:
            if time.monotonic() > deadline:
                p.kill()
                final_err = f"claude timed out after {timeout_s}s in {workdir}"
                sink.emit({"type": "error", "text": final_err})
                break
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                sink.emit({"type": "text", "text": line[:500]})
                continue
            t = ev.get("type")
            if t == "system" and ev.get("session_id"):
                claude_sid = ev["session_id"]
            if t == "result":
                if ev.get("subtype") == "success":
                    final_text = str(ev.get("result") or "")
                else:
                    final_err = str(ev.get("result") or ev.get("error")
                                     or ev.get("subtype")
                                     or "claude reported an error")
            if t == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "text" and c.get("text"):
                        text_acc.append(c["text"])
            slim = _slim(ev)
            if slim:
                sink.emit(slim)
        rc = p.wait(timeout=10)
    except Exception as e:
        p.kill()
        return False, "\n".join(text_acc)[:_MAX_OUTPUT], f"stream read failed: {e}", claude_sid

    stderr = _decode(p.stderr.read() if p.stderr else "")[:4000]

    if final_err:
        return False, "\n".join(text_acc)[:_MAX_OUTPUT], (
            final_err + (f"\n--- stderr ---\n{stderr}" if stderr else "")
        ), claude_sid
    if rc != 0 and final_text is None:
        return False, "\n".join(text_acc)[:_MAX_OUTPUT], (
            stderr or f"claude exited {rc}"
        ), claude_sid

    out = final_text if final_text is not None else "\n".join(text_acc)
    return True, out[:_MAX_OUTPUT], "", claude_sid
