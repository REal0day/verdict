"""Keep `irs-agent run` alive without depending on any one init system.

Two independent concerns, deliberately split:

1. Stay alive after logout + restart on crash.  Handled entirely in-process by
   a small supervisor (`start`/`stop`/`status`) that spawns the worker as a
   detached child and respawns it with backoff.  No systemd, no root, works the
   same on Linux/macOS/Windows.

2. Start on boot.  This unavoidably needs a hook into whatever the OS launches
   at boot, so `install-service` auto-detects the best available mechanism and
   degrades gracefully: systemd -> cron @reboot -> printed manual steps on
   Linux, launchd on macOS, Task Scheduler on Windows.  The boot hook just runs
   `irs-agent start`, so the supervisor above is what actually keeps it up.
"""
import getpass
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import CONF_DIR

log = logging.getLogger("irs-agent.service")

PID_FILE = CONF_DIR / "supervisor.pid"
LOG_FILE = CONF_DIR / "agent.log"

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

# crash-restart backoff
_MIN_BACKOFF = 1
_MAX_BACKOFF = 60
_HEALTHY_RUN_S = 30   # a child that lived this long resets the backoff


# ---------- command resolution ----------

def _agent_argv() -> list[str]:
    """Command prefix that re-invokes this agent. Prefers the installed console
    script (absolute, so it survives a PATH-less boot context); falls back to
    `python -m irs_agent`."""
    p = Path(sys.argv[0])
    if p.name.startswith("irs-agent") and p.is_absolute() and p.exists():
        return [str(p)]
    cand = Path(sys.executable).with_name("irs-agent" + (".exe" if IS_WINDOWS else ""))
    if cand.exists():
        return [str(cand)]
    return [sys.executable, "-m", "irs_agent"]


# ---------- pid file ----------

def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------- foreground supervisor (the _supervise command) ----------

def supervise() -> int:
    """Run the worker as a child, respawn on crash. This process is the thing
    boot hooks and `start` keep alive; it writes the pid file for itself."""
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    existing = _read_pid()
    if existing and existing != os.getpid() and _alive(existing):
        log.error("supervisor already running (pid %d)", existing)
        return 1
    PID_FILE.write_text(str(os.getpid()))

    stopping = {"flag": False}
    child: dict[str, subprocess.Popen | None] = {"proc": None}

    def _handle(signum, _frame):
        stopping["flag"] = True
        p = child["proc"]
        if p and p.poll() is None:
            p.terminate()

    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)

    worker = _agent_argv() + ["run"]
    backoff = _MIN_BACKOFF
    try:
        while not stopping["flag"]:
            log.info("supervisor: launching worker %s", " ".join(worker))
            started = time.monotonic()
            try:
                p = subprocess.Popen(worker)
            except Exception as e:
                log.error("supervisor: failed to launch worker: %s", e)
                time.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue
            child["proc"] = p
            rc = p.wait()
            child["proc"] = None
            if stopping["flag"]:
                break
            ran = time.monotonic() - started
            if ran >= _HEALTHY_RUN_S:
                backoff = _MIN_BACKOFF
            log.warning("supervisor: worker exited (rc=%s) after %.0fs; "
                        "restarting in %ds", rc, ran, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)
    finally:
        try:
            if PID_FILE.exists() and _read_pid() == os.getpid():
                PID_FILE.unlink()
        except OSError:
            pass
    log.info("supervisor: stopped")
    return 0


# ---------- start / stop / status ----------

def start() -> int:
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"agent already running (supervisor pid {pid})")
        return 0
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    argv = _agent_argv() + ["_supervise"]
    logf = open(LOG_FILE, "a")
    if IS_WINDOWS:
        DETACHED = 0x00000008  # DETACHED_PROCESS
        NEWGRP = 0x00000200    # CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(argv, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                         creationflags=DETACHED | NEWGRP, close_fds=True)
    else:
        # start_new_session detaches from the controlling terminal and the
        # login session, so the agent outlives logout without linger tricks.
        subprocess.Popen(argv, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    # give the supervisor a moment to claim the pid file
    for _ in range(20):
        time.sleep(0.1)
        np = _read_pid()
        if np and _alive(np):
            print(f"agent started (supervisor pid {np}); logs at {LOG_FILE}")
            return 0
    print(f"agent launched; check {LOG_FILE} if it does not come up", file=sys.stderr)
    return 0


def stop() -> int:
    pid = _read_pid()
    if not pid or not _alive(pid):
        print("agent not running")
        if PID_FILE.exists():
            try:
                PID_FILE.unlink()
            except OSError:
                pass
        return 0
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(50):
        if not _alive(pid):
            print("agent stopped")
            return 0
        time.sleep(0.1)
    if not IS_WINDOWS:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    print("agent killed")
    return 0


def status() -> int:
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"running (supervisor pid {pid})")
        return 0
    print("not running")
    return 3


# ---------- boot persistence (install-service) ----------

def _has_systemd() -> bool:
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()


def _has_cron() -> bool:
    return shutil.which("crontab") is not None


def install_service(method: str = "auto", system: bool = False) -> int:
    """Register the agent to start on boot. method: auto|systemd|cron|launchd|
    schtasks|manual."""
    if method == "auto":
        method = _autodetect()
        log.info("install-service: auto-detected method '%s'", method)
    fn = {
        "systemd": lambda: _install_systemd(system),
        "cron": _install_cron,
        "launchd": _install_launchd,
        "schtasks": _install_schtasks,
        "manual": _print_manual,
    }.get(method)
    if not fn:
        print(f"unknown method '{method}'", file=sys.stderr)
        return 2
    return fn()


def _autodetect() -> str:
    if IS_WINDOWS:
        return "schtasks"
    if IS_MACOS:
        return "launchd"
    if _has_systemd():
        return "systemd"
    if _has_cron():
        return "cron"
    return "manual"


def _install_systemd(system: bool) -> int:
    exe = " ".join(_agent_argv())
    unit = f"""[Unit]
Description=Verdict collector agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exe} run
Restart=always
RestartSec=5

[Install]
WantedBy={'multi-user.target' if system else 'default.target'}
"""
    if system:
        path = Path("/etc/systemd/system/irs-agent.service")
        unit = unit.replace("[Service]\n", f"[Service]\nUser={getpass.getuser()}\n")
        try:
            path.write_text(unit)
        except PermissionError:
            print(f"need root to write {path}; re-run with sudo", file=sys.stderr)
            return 1
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", "--now", "irs-agent"], check=False)
        print(f"installed system service at {path}; check: "
              "systemctl status irs-agent")
        return 0
    path = Path.home() / ".config/systemd/user/irs-agent.service"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", "irs-agent"], check=False)
    # the whole reason we are here: a user unit dies on logout without linger.
    linger = subprocess.run(["loginctl", "enable-linger", getpass.getuser()],
                            capture_output=True, text=True)
    if linger.returncode != 0:
        print("WARNING: could not enable linger; the agent will stop on logout. "
              f"Run: sudo loginctl enable-linger {getpass.getuser()}",
              file=sys.stderr)
    print(f"installed user service at {path} (linger "
          f"{'on' if linger.returncode == 0 else 'OFF — see warning'}); "
          "check: systemctl --user status irs-agent")
    return 0


def _install_cron() -> int:
    exe = " ".join(_agent_argv())
    line = f"@reboot {exe} start  # irs-agent"
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    existing = cur.stdout if cur.returncode == 0 else ""
    lines = [l for l in existing.splitlines() if "# irs-agent" not in l]
    lines.append(line)
    new = "\n".join(lines) + "\n"
    p = subprocess.run(["crontab", "-"], input=new, text=True)
    if p.returncode != 0:
        print("failed to install crontab entry", file=sys.stderr)
        return 1
    print("installed @reboot crontab entry; starting now")
    return start()


def _install_launchd() -> int:
    label = "com.irs.agent"
    argv = _agent_argv() + ["run"]
    items = "".join(f"        <string>{a}</string>\n" for a in argv)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{items}    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardErrorPath</key><string>{LOG_FILE}</string>
    <key>StandardOutPath</key><string>{LOG_FILE}</string>
</dict>
</plist>
"""
    path = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plist)
    subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
    subprocess.run(["launchctl", "load", str(path)], check=False)
    print(f"installed launchd agent at {path}; check: launchctl list | grep irs")
    return 0


def _install_schtasks() -> int:
    cmd = " ".join(_agent_argv() + ["start"])
    p = subprocess.run(
        ["schtasks", "/Create", "/TN", "irs-agent", "/SC", "ONLOGON",
         "/TR", cmd, "/RL", "LIMITED", "/F"],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        print(f"schtasks failed: {p.stderr.strip()}", file=sys.stderr)
        return 1
    print("installed scheduled task 'irs-agent' (runs at logon); starting now")
    return start()


def _print_manual() -> int:
    exe = " ".join(_agent_argv())
    print(
        "No supported boot mechanism detected. Start the agent manually and\n"
        "arrange for it to run at boot however your system allows:\n\n"
        f"    {exe} start        # background + auto-restart, survives logout\n"
        f"    {exe} status\n"
        f"    {exe} stop\n\n"
        "For boot persistence add an equivalent of `@reboot` for your init\n"
        "system that runs the `start` command above."
    )
    return 0


def uninstall_service() -> int:
    """Best-effort removal across whatever might have been installed."""
    stop()
    removed = []
    if not IS_WINDOWS and not IS_MACOS:
        for path, user in ((Path.home() / ".config/systemd/user/irs-agent.service", True),
                           (Path("/etc/systemd/system/irs-agent.service"), False)):
            if path.exists():
                scope = ["--user"] if user else []
                subprocess.run(["systemctl", *scope, "disable", "--now", "irs-agent"],
                               capture_output=True)
                try:
                    path.unlink()
                    removed.append(str(path))
                except OSError:
                    pass
        if _has_cron():
            cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            if cur.returncode == 0 and "# irs-agent" in cur.stdout:
                kept = [l for l in cur.stdout.splitlines() if "# irs-agent" not in l]
                subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True)
                removed.append("crontab @reboot entry")
    if IS_MACOS:
        path = Path.home() / "Library/LaunchAgents/com.irs.agent.plist"
        if path.exists():
            subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
            path.unlink()
            removed.append(str(path))
    if IS_WINDOWS:
        subprocess.run(["schtasks", "/Delete", "/TN", "irs-agent", "/F"],
                       capture_output=True)
        removed.append("scheduled task irs-agent")
    print("removed: " + (", ".join(removed) if removed else "nothing found"))
    return 0
