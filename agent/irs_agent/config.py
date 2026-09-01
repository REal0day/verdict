import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

import platformdirs

CONF_DIR = Path(platformdirs.user_config_dir("irs-agent"))
CONF_FILE = CONF_DIR / "config.json"
STATE_FILE = CONF_DIR / "state.json"


@dataclass
class CollectorConf:
    name: str                   # claude_code | openai | gemini | grok | other
    paths: list[str] = field(default_factory=list)   # dirs to watch
    glob: str = "*.md"


@dataclass
class AgentConf:
    server_url: str = "https://localhost:8000"
    api_key: str = ""
    verify_tls: bool = True
    remote_prompt: bool = True
    remote_timeout_s: int = 1800
    remote_max_concurrent: int = 3
    anthropic_api_key: str = ""

    # Which coding-agent CLI runs Workbench sessions.
    #   cli = "claude"   -> Claude Code (full tool/thinking events, resumable)
    #   cli = "generic"  -> any other CLI, driven by cli_command below
    cli: str = "claude"
    # Template for cli="generic". Split with shlex before substitution, so a
    # prompt with spaces stays one argument and never reaches a shell.
    # Placeholders: {prompt} {model} {cwd}. Example:
    #   cli_command = "aider --model {model} --message {prompt}"
    cli_command: str = ""
    cli_model: str = ""         # passed as the CLI's --model; "" = its default
    claude_model: str = ""      # deprecated alias for cli_model
    collectors: list[CollectorConf] = field(default_factory=list)

    @staticmethod
    def load() -> "AgentConf":
        if not CONF_FILE.exists():
            return AgentConf()
        data = json.loads(CONF_FILE.read_text())
        cols = [CollectorConf(**c) for c in data.get("collectors", [])]
        return AgentConf(
            server_url=data.get("server_url", "https://localhost:8000"),
            api_key=data.get("api_key", ""),
            verify_tls=data.get("verify_tls", True),
            remote_prompt=data.get("remote_prompt", True),
            remote_timeout_s=int(data.get("remote_timeout_s", 1800)),
            remote_max_concurrent=int(data.get("remote_max_concurrent", 3)),
            anthropic_api_key=data.get("anthropic_api_key", ""),
            claude_model=data.get("claude_model", ""),
            collectors=cols,
        )

    def save(self):
        CONF_DIR.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        CONF_FILE.write_text(json.dumps(d, indent=2))
        try:
            os.chmod(CONF_FILE, 0o600)
        except OSError:
            pass


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
