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
from .clis import get_adapter
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
        adapter = get_adapter(
            getattr(conf, "cli", "claude") or "claude",
            getattr(conf, "cli_command", "") or "",
        )
        ok, out, err, sid = _run_cli(
            adapter,
            job.get("prompt", ""), cwd,
            resume=job.get("resume"),
            timeout_s=getattr(conf, "remote_timeout_s", 1800),
            anthropic_api_key=conf.anthropic_api_key,
            sink=sink,
            # per-session model from the portal wins; agent config is the fallback
            model=job.get("model") or getattr(conf, "cli_model", "")
                  or getattr(conf, "claude_model", ""),
            # engagement authorisation for active-testing sessions (server-decided)
            append_system_prompt=job.get("append_system_prompt") or "",
            # active-testing sessions run headless with no one to grant approvals
            bypass_permissions=bool(job.get("bypass_permissions")),
        )
        sink.close()
        _post_result(client, rid, ok, out, err, sid, workspace,
                     cli=adapter.name)
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
                 claude_sid: str | None, workspace: str | None,
                 cli: str | None = None):
    body = {"ok": ok, "output": out, "error": err,
            "claude_session_id": claude_sid, "workspace": workspace,
            "cli": cli}
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


def _run_cli(adapter, prompt: str, cwd: str | None, *, resume: str | None,
             timeout_s: int, anthropic_api_key: str, sink: _EventSink,
             model: str = "", append_system_prompt: str = "",
             bypass_permissions: bool = False
             ) -> tuple[bool, str, str, str | None]:
    """Run one turn through a CLI adapter and stream its events to the server."""
    label = adapter.display_name
    exe = adapter.find_exe()
    if not exe:
        if adapter.name == "generic" and not getattr(adapter, "command", ""):
            return False, "", (
                "No agent CLI configured. Set `cli` and `cli_command` in the "
                "agent config (e.g. cli=generic, "
                'cli_command="aider --message {prompt}").'
            ), None
        return False, "", f"{label} executable not found on PATH on the agent host", None

    workdir = Path(cwd).expanduser() if cwd else Path.home()
    if not workdir.is_dir():
        return False, "", f"working directory does not exist: {workdir}", None

    if resume and not adapter.supports_resume:
        # Better to start a fresh turn than to fail: the caller keeps history.
        log.info("%s cannot resume; starting a fresh turn", label)
        resume = None

    launch = adapter.build(
        exe=exe, prompt=prompt, cwd=str(workdir), model=model, resume=resume,
        append_system_prompt=append_system_prompt,
        bypass_permissions=bypass_permissions,
    )

    env = os.environ.copy()
    if anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = anthropic_api_key
    env.update(launch.env)

    log.info("running %s in %s (timeout %ds, resume=%s, model=%s, skip_perms=%s)",
             label, workdir, timeout_s, bool(resume), model or "<cli-default>",
             bool(bypass_permissions))
    sink.emit({"type": "launch", "cwd": str(workdir), "resume": bool(resume),
               "model": model or None, "cli": adapter.name,
               "active_testing": bool(append_system_prompt)})

    try:
        p = subprocess.Popen(
            launch.argv, cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
    except Exception as e:
        return False, "", f"failed to launch {label}: {e}", None

    deadline = time.monotonic() + timeout_s
    final_text: str | None = None
    final_err: str | None = None
    session_id: str | None = resume
    text_acc: list[str] = []

    try:
        assert p.stdout is not None
        for line in p.stdout:
            if time.monotonic() > deadline:
                p.kill()
                final_err = f"{label} timed out after {timeout_s}s in {workdir}"
                sink.emit({"type": "error", "text": final_err})
                break
            line = line.strip()
            if not line:
                continue
            parsed = adapter.parse_line(line)
            if parsed.session_id:
                session_id = parsed.session_id
            if parsed.text_chunk:
                text_acc.append(parsed.text_chunk)
            if parsed.final_text is not None:
                final_text = parsed.final_text
            if parsed.final_err:
                final_err = parsed.final_err
            for ev in parsed.events:
                sink.emit(ev)
        rc = p.wait(timeout=10)
    except Exception as e:
        p.kill()
        return False, "\n".join(text_acc)[:_MAX_OUTPUT], f"stream read failed: {e}", session_id

    stderr = _decode(p.stderr.read() if p.stderr else "")[:4000]

    if final_err:
        return False, "\n".join(text_acc)[:_MAX_OUTPUT], (
            final_err + (f"\n--- stderr ---\n{stderr}" if stderr else "")
        ), session_id
    if rc != 0 and final_text is None:
        return False, "\n".join(text_acc)[:_MAX_OUTPUT], (
            stderr or f"{label} exited {rc}"
        ), session_id

    out = final_text if final_text is not None else "\n".join(text_acc)
    return True, out[:_MAX_OUTPUT], "", session_id
