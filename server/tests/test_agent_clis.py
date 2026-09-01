"""Pluggable agent CLIs (Phase 4).

The Workbench used to shell out to `claude` and parse Claude Code's
stream-json, which made remote sessions Claude-only. These pin the adapter
contract — especially that the generic adapter never hands a prompt to a shell.
"""
import pathlib, sys

# The agent package sits beside `server/` in the repo, but the Docker image
# vendors it at /srv/agent_src. Look in both so the suite runs either way.
def _find_agent() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve()
    for cand in (here.parents[2] / "agent", pathlib.Path("/srv/agent_src")):
        if (cand / "irs_agent" / "clis.py").exists():
            return cand
    raise RuntimeError("could not locate the agent package")


AGENT = _find_agent()
sys.path.insert(0, str(AGENT))

import pytest

from irs_agent.clis import (
    ClaudeCodeAdapter, GenericCLIAdapter, get_adapter,
)


# ---------------- registry ----------------

@pytest.mark.parametrize("name", ["claude", "claude-code", "claude_code"])
def test_claude_names_resolve_to_the_claude_adapter(name):
    assert isinstance(get_adapter(name), ClaudeCodeAdapter)


def test_unknown_names_fall_back_to_generic():
    a = get_adapter("something-else", "mytool --message {prompt}")
    assert isinstance(a, GenericCLIAdapter)


def test_claude_is_the_default():
    assert isinstance(get_adapter(""), ClaudeCodeAdapter)


# ---------------- claude adapter ----------------

def test_claude_argv_preserves_the_existing_contract():
    a = ClaudeCodeAdapter()
    launch = a.build(exe="/usr/bin/claude", prompt="do the thing", cwd="/w",
                     model="claude-opus-4-5", resume="sess-1",
                     append_system_prompt="AUTHORISED", bypass_permissions=True)
    argv = launch.argv
    assert argv[0] == "/usr/bin/claude"
    assert argv[1:5] == ["-p", "--verbose", "--output-format", "stream-json"]
    assert "--model" in argv and "claude-opus-4-5" in argv
    assert "--append-system-prompt" in argv and "AUTHORISED" in argv
    assert "--dangerously-skip-permissions" in argv
    assert "--resume" in argv and "sess-1" in argv
    assert argv[-1] == "do the thing", "prompt must be the final argument"


def test_claude_omits_flags_that_were_not_asked_for():
    argv = ClaudeCodeAdapter().build(
        exe="claude", prompt="hi", cwd="/w").argv
    for flag in ("--model", "--resume", "--append-system-prompt",
                 "--dangerously-skip-permissions"):
        assert flag not in argv


def test_claude_parses_a_session_id():
    p = ClaudeCodeAdapter().parse_line(
        '{"type":"system","subtype":"init","session_id":"abc","model":"m","cwd":"/w"}')
    assert p.session_id == "abc"


def test_claude_parses_assistant_text_and_tool_calls():
    p = ClaudeCodeAdapter().parse_line(
        '{"type":"assistant","message":{"content":['
        '{"type":"text","text":"hello"},'
        '{"type":"tool_use","name":"Bash","input":{"command":"ls -la"}}]}}')
    assert p.text_chunk == "hello"
    blocks = p.events[0]["content"]
    assert {"type": "text", "text": "hello"} in blocks
    tool = [b for b in blocks if b["type"] == "tool_use"][0]
    assert tool["name"] == "Bash" and tool["hint"] == "ls -la"


def test_claude_parses_a_successful_result():
    p = ClaudeCodeAdapter().parse_line(
        '{"type":"result","subtype":"success","result":"final answer"}')
    assert p.final_text == "final answer" and p.final_err is None


def test_claude_parses_an_error_result():
    p = ClaudeCodeAdapter().parse_line(
        '{"type":"result","subtype":"error_max_turns","result":"gave up"}')
    assert p.final_err == "gave up"


def test_claude_surfaces_non_json_lines_rather_than_dropping_them():
    p = ClaudeCodeAdapter().parse_line("npm WARN something")
    assert p.events == [{"type": "text", "text": "npm WARN something"}]


# ---------------- generic adapter ----------------

def test_generic_substitutes_placeholders():
    a = GenericCLIAdapter("mytool --model {model} --message {prompt}")
    argv = a.build(exe="/bin/mytool", prompt="find bugs", cwd="/w",
                   model="qwen2.5-coder:7b").argv
    assert argv == ["/bin/mytool", "--model", "qwen2.5-coder:7b",
                    "--message", "find bugs"]


def test_generic_appends_the_prompt_when_the_template_omits_it():
    argv = GenericCLIAdapter("mytool run").build(
        exe="/bin/mytool", prompt="hello", cwd="/w").argv
    assert argv == ["/bin/mytool", "run", "hello"]


def test_generic_never_lets_a_prompt_reach_a_shell():
    """A prompt with shell metacharacters must stay one inert argument."""
    nasty = 'hi; rm -rf / && echo "$(whoami)" `id` | tee /tmp/x'
    argv = GenericCLIAdapter("mytool --message {prompt}").build(
        exe="/bin/mytool", prompt=nasty, cwd="/w").argv
    assert argv == ["/bin/mytool", "--message", nasty]
    assert len(argv) == 3, "the prompt must not be split into extra arguments"


def test_generic_handles_a_prompt_with_spaces_and_quotes():
    prompt = 'review "src/main.py" for issues'
    argv = GenericCLIAdapter("t {prompt}").build(
        exe="/bin/t", prompt=prompt, cwd="/w").argv
    assert argv == ["/bin/t", prompt]


def test_generic_streams_stdout_as_text():
    p = GenericCLIAdapter("t {prompt}").parse_line("some output")
    assert p.text_chunk == "some output"
    assert p.events == [{"type": "text", "text": "some output"}]


def test_generic_declares_no_resume_support():
    assert GenericCLIAdapter("t").supports_resume is False
    assert ClaudeCodeAdapter().supports_resume is True


def test_generic_without_a_command_finds_no_executable():
    """Guards the 'no CLI configured' error path in remote.py."""
    assert GenericCLIAdapter("").find_exe() is None


def test_agent_no_longer_hardcodes_the_claude_binary():
    src = AGENT / "irs_agent/remote.py"
    text = src.read_text()
    assert 'shutil.which("claude")' not in text
    assert "get_adapter(" in text
