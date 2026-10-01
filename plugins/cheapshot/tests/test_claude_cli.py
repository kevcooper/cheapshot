"""Detect Claude Code changes that break what claude.py relies on.

claude.py drives the `claude` CLI with flags and undocumented environment variables so its
requests come close to a bare API call. These tests run the real CLI with exactly those flags and
that environment, pointed at a local server that records the request and fails it, so no model
call is made. A failure means the installed Claude Code no longer honors one of the settings.

Tests marked `live` make real, very cheap calls (Claude Haiku 4.5) to check the CLI's JSON
output, which the capture server can't produce. They run only with CHEAPSHOT_LIVE_TESTS=1.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cheapshot import claude

pytestmark = pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH")

MODEL = "claude-opus-5"
IDENTITY = "You are a Claude agent, built on Anthropic's Claude Agent SDK."
SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer"}},
    "required": ["n"],
    "additionalProperties": False,
}
live = pytest.mark.skipif(
    os.environ.get("CHEAPSHOT_LIVE_TESTS") != "1", reason="set CHEAPSHOT_LIVE_TESTS=1 to run"
)


def cli_version() -> str:
    return subprocess.run(["claude", "--version"], capture_output=True, text=True).stdout.strip()


@pytest.fixture
def capture():
    """A local stand-in for the Messages API that records request bodies and rejects them."""
    bodies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            if self.path.startswith("/v1/messages") and "count_tokens" not in self.path:
                bodies.append(json.loads(body))
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"type":"error","error":{"type":"invalid_request_error","message":"captured"}}'
            )

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", bodies
    server.shutdown()


def send(
    capture, tmp_path, *, system="Be terse.", effort=None, output_schema=None, env=None, attachments=()
):
    """Run the CLI the way claude.infer does and return the request it sent."""
    url, bodies = capture
    proc = subprocess.run(
        claude.command(MODEL, system, effort, output_schema),
        input=claude.message(list(attachments), "Say hi"),
        cwd=tmp_path,
        env=claude.child_env() | (env or {}) | {"ANTHROPIC_BASE_URL": url},
        capture_output=True,
        timeout=120,
    )
    assert bodies, (
        f"claude {cli_version()} sent no request; a flag may have been removed or renamed.\n"
        f"exit {proc.returncode}, stderr: {proc.stderr.decode()[-2000:]}"
    )
    return bodies[-1]


def text_blocks(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [block.get("text", "") for block in content]


def test_system_prompt_is_ours_after_the_fixed_identity_line(capture, tmp_path):
    body = send(capture, tmp_path)
    # A billing header block (CLAUDE_CODE_ATTRIBUTION_HEADER=0) or Claude Code's default
    # prompt (--system-prompt) would show up here as extra or different blocks.
    assert [block["text"] for block in body["system"]] == [IDENTITY, "Be terse."], cli_version()


def test_no_tools_without_a_schema(capture, tmp_path):
    assert not send(capture, tmp_path).get("tools"), f"--tools '' stopped working ({cli_version()})"


def test_no_context_management(capture, tmp_path):
    # CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1.
    assert "context_management" not in send(capture, tmp_path), cli_version()


def test_prompt_caching_is_on_by_default(capture, tmp_path):
    body = send(capture, tmp_path)
    # Breakpoints on the end of the system prompt and on the prompt itself.
    assert body["system"][-1].get("cache_control", {}).get("type") == "ephemeral", cli_version()
    prompt_block = body["messages"][0]["content"][-1]
    assert prompt_block["text"] == "Say hi"
    assert prompt_block.get("cache_control", {}).get("type") == "ephemeral", cli_version()


def test_prompt_caching_can_be_turned_off(capture, tmp_path, monkeypatch):
    monkeypatch.setenv("CHEAPSHOT_PROMPT_CACHING", "0")  # child_env maps it to DISABLE_PROMPT_CACHING
    body = send(capture, tmp_path, attachments=["<file>a</file>"])
    assert "cache_control" not in json.dumps(body), cli_version()


def test_files_are_separate_blocks_with_their_own_cache_breakpoint(capture, tmp_path):
    body = send(capture, tmp_path, attachments=["<file>a</file>", "<file>b</file>"])
    *_, first, second, prompt = body["messages"][0]["content"]
    assert [first["text"], second["text"], prompt["text"]] == ["<file>a</file>", "<file>b</file>", "Say hi"]
    # Only the last file is marked: the cache entry ending there covers every file before it.
    # With Claude Code's marks on the system prompt and the prompt, that's the API's limit of 4.
    assert "cache_control" not in first
    assert second["cache_control"]["type"] == "ephemeral", cli_version()
    assert json.dumps(body).count('"cache_control"') <= 4, cli_version()


def test_messages_carry_only_the_known_extras(capture, tmp_path):
    body = send(capture, tmp_path)
    user, *rest = body["messages"]
    assert user["role"] == "user"
    *extras, prompt = text_blocks(user["content"])
    assert prompt == "Say hi"
    # With OAuth login the only extra is the account email reminder. Other reminders here (date,
    # model, IDE context) mean something the calling session sets is leaking through child_env,
    # or a new reminder type that CLAUDE_CODE_DISABLE_ATTACHMENTS=1 doesn't cover.
    for extra in extras:
        assert "# userEmail" in extra, f"unexpected block from claude {cli_version()}: {extra[:300]}"
    # One trailing system message with the environment, which --safe-mode keeps free of
    # CLAUDE.md, memory, and git status.
    assert [m["role"] for m in rest] == ["system"], cli_version()
    environment = "\n".join(text_blocks(rest[0]["content"]))
    # Wrapped in <system-reminder> tags when prompt caching is on.
    assert environment.removeprefix("<system-reminder>\n").startswith("# Environment"), environment[:300]
    for leak in ("claudeMd", "gitStatus", "Memory"):
        assert leak not in environment, f"{leak} leaked into the request ({cli_version()})"


def test_effort_is_passed_and_the_callers_session_effort_is_not(capture, tmp_path):
    assert send(capture, tmp_path, effort="low")["output_config"] == {"effort": "low"}
    # child_env drops the calling session's CLAUDE_EFFORT; if it leaked, this would be "max".
    body = send(capture, tmp_path, env={"CLAUDE_EFFORT": "max"})
    assert body.get("output_config", {}).get("effort") != "max", cli_version()


def test_json_schema_becomes_one_structured_output_tool(capture, tmp_path):
    tools = send(capture, tmp_path, output_schema=SCHEMA)["tools"]
    assert [tool["name"] for tool in tools] == ["StructuredOutput"], cli_version()
    assert tools[0]["input_schema"] == SCHEMA


def test_model_is_sent_unchanged(capture, tmp_path):
    assert send(capture, tmp_path)["model"] == MODEL


@live
def test_live_result_has_the_fields_claude_py_reads(tmp_path):
    import asyncio

    text, served_by, response = asyncio.run(
        claude.infer("claude-haiku-4-5", "Reply with the single word: ok", None, "low", tmp_path)
    )
    assert text.strip().lower().startswith("ok")
    assert served_by.startswith("claude-haiku-4-5")
    for field in ("stop_reason", "modelUsage", "total_cost_usd", "usage", "terminal_reason"):
        assert field in response, f"{field} missing from claude {cli_version()} JSON output"


@live
def test_live_structured_output_field(tmp_path):
    import asyncio

    text, _, response = asyncio.run(
        claude.infer("claude-haiku-4-5", "Return n = 7.", None, "low", tmp_path, SCHEMA)
    )
    assert json.loads(text) == {"n": 7}
    assert response["structured_output"] == {"n": 7}


@live
def test_live_unknown_model_is_an_error_not_a_substitute(tmp_path):
    import asyncio

    # CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK=1: never answer with a model we didn't ask for.
    with pytest.raises(RuntimeError, match="claude failed"):
        asyncio.run(claude.infer("claude-nope-9", "hi", None, None, tmp_path))
