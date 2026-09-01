import argparse
import logging
import socket
import sys

import httpx

from .collectors import REGISTRY
from .config import AgentConf, CollectorConf, CONF_FILE
from . import watcher


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser("irs-agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="interactive config + register agent with server")
    sp.add_argument("--server", required=True)
    sp.add_argument("--email", required=True)
    sp.add_argument("--password", required=True)
    sp.add_argument("--insecure", action="store_true", help="skip TLS verify (dev only)")
    sp.add_argument(
        "--enable", action="append", default=[],
        help=f"collectors to enable; any of: {', '.join(REGISTRY)} (repeatable)",
    )
    sp.add_argument(
        "--path", action="append", default=[],
        help="extra watch dir as NAME=PATH (repeatable), e.g. claude_code=~/work/reports",
    )

    sub.add_parser("run", help="run the agent in the foreground")
    sub.add_parser("start", help="start the agent in the background (auto-restart, survives logout)")
    sub.add_parser("stop", help="stop the background agent")
    sub.add_parser("status", help="report whether the background agent is running")
    sub.add_parser("_supervise", help=argparse.SUPPRESS)  # internal: the restart loop

    sp_svc = sub.add_parser(
        "install-service",
        help="register the agent to start on boot (auto-detects systemd/cron/launchd/schtasks)",
    )
    sp_svc.add_argument("--method", default="auto",
                        choices=["auto", "systemd", "cron", "launchd", "schtasks", "manual"],
                        help="boot mechanism to use (default: auto-detect)")
    sp_svc.add_argument("--system", action="store_true",
                        help="systemd only: install a system unit instead of a --user one (needs root)")
    sub.add_parser("uninstall-service", help="remove the boot registration installed by install-service")

    sub.add_parser("upgrade", help="pull the latest agent from the server and reinstall in place")
    sp2 = sub.add_parser("submit", help="one-shot upload of a file")
    sp2.add_argument("file")
    sp2.add_argument("--tool", default="other")

    sub.add_parser("config-path", help="print config file location")

    sub.add_parser(
        "hook-submit",
        help="read a Claude Code PostToolUse JSON payload from stdin and submit the file",
    )

    sp_hook = sub.add_parser(
        "install-claude-hook",
        help="install/remove a Claude Code PostToolUse hook in ~/.claude/settings.json",
    )
    sp_hook.add_argument("--remove", action="store_true",
                         help="remove the hook instead of installing it")

    sp_imp = sub.add_parser(
        "import",
        help="scan known directories for existing .md reports + poc/ files and upload them",
    )
    sp_imp.add_argument("--path", action="append", default=[],
                        help="extra directory to include in the scan (repeatable)")
    sp_imp.add_argument("-y", "--yes", action="store_true",
                        help="don't ask for confirmation before uploading")

    args = p.parse_args(argv)

    if args.cmd == "config-path":
        print(CONF_FILE)
        return 0

    if args.cmd == "init":
        return _init(args)

    if args.cmd == "install-claude-hook":
        from . import claude_hook
        return claude_hook.install(remove=args.remove)

    # service lifecycle — these don't read conf; the worker they spawn does its
    # own init check, so stop/status/uninstall work even on a half-set-up host.
    if args.cmd in ("start", "stop", "status", "_supervise",
                    "install-service", "uninstall-service"):
        from . import service
        if args.cmd == "start":
            return service.start()
        if args.cmd == "stop":
            return service.stop()
        if args.cmd == "status":
            return service.status()
        if args.cmd == "_supervise":
            return service.supervise()
        if args.cmd == "install-service":
            return service.install_service(method=args.method, system=args.system)
        if args.cmd == "uninstall-service":
            return service.uninstall_service()

    conf = AgentConf.load()
    if not conf.api_key:
        print("agent not initialised; run `irs-agent init ...` first", file=sys.stderr)
        return 2

    if args.cmd == "run":
        watcher.run(conf)
        return 0

    if args.cmd == "upgrade":
        return _upgrade(conf)

    if args.cmd == "submit":
        from pathlib import Path
        from .uploader import Uploader
        Uploader(conf).maybe_upload(Path(args.file), args.tool)
        return 0

    if args.cmd == "hook-submit":
        from . import claude_hook
        return claude_hook.submit_from_stdin(conf)

    if args.cmd == "import":
        from . import importer
        return importer.run_import(conf, extra_paths=args.path, assume_yes=args.yes)


def _init(args) -> int:
    verify = not args.insecure
    base = args.server.rstrip("/")
    # 1. login as the user to get a JWT
    r = httpx.post(
        f"{base}/auth/token",
        data={"username": args.email, "password": args.password},
        verify=verify, timeout=30,
    )
    r.raise_for_status()
    tok = r.json()["access_token"]
    # 2. register this host as an agent -> get api key
    r = httpx.post(
        f"{base}/agents",
        json={"hostname": socket.gethostname()},
        headers={"Authorization": f"Bearer {tok}"},
        verify=verify, timeout=30,
    )
    r.raise_for_status()
    api_key = r.json()["api_key"]

    # 3. build collector list
    enabled = args.enable or ["claude_code"]
    extra: dict[str, list[str]] = {}
    for spec in args.path:
        name, _, path = spec.partition("=")
        extra.setdefault(name, []).append(path)
    collectors = []
    for name in enabled:
        reg = REGISTRY.get(name)
        paths = (reg.default_paths if reg else []) + extra.get(name, [])
        glob = reg.glob if reg else "*.md"
        collectors.append(CollectorConf(name=name, paths=paths, glob=glob))

    conf = AgentConf(
        server_url=base, api_key=api_key, verify_tls=verify, collectors=collectors
    )
    conf.save()
    print(f"agent registered. config written to {CONF_FILE}")
    return 0


def _upgrade(conf: AgentConf) -> int:
    import subprocess, tempfile, os
    url = conf.server_url.rstrip("/") + "/ui/agent/source.tar.gz"
    print(f"downloading agent source from {url}")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as f:
        with httpx.stream("GET", url, headers={"X-Agent-Key": conf.api_key},
                          verify=conf.verify_tls, timeout=60) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                f.write(chunk)
        tarball = f.name
    try:
        print("reinstalling into current interpreter")
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "--upgrade", "--force-reinstall", "--no-deps", tarball],
                       check=True)
        subprocess.run([sys.executable, "-m", "pip", "install", tarball], check=True)
    finally:
        os.unlink(tarball)
    print("upgrade complete — restart the agent: `irs-agent stop && irs-agent start` "
          "(or `systemctl --user restart irs-agent` if you installed a systemd unit).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
