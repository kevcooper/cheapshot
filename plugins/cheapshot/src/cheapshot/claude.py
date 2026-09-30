"""Run one stateless request through the `claude` CLI, stripped as close to a bare API call as it allows.

What the request still carries that a bare API call would not (none of it can be turned off
while authenticating with a Claude subscription):
- the system prompt is prefixed with "You are a Claude agent, built on Anthropic's Claude Agent SDK."
- a <system-reminder> with the signed-in account's email address
- a trailing `system` message describing the environment (cwd, platform, shell, OS)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

FLAGS = [
    "--print",
    "--output-format", "json",
    "--no-session-persistence",
    "--safe-mode",  # no CLAUDE.md, plugins, hooks, MCP servers, skills, agents, output styles
    "--tools", "",  # no tool definitions
]

# Each of these removes something from the request body.
ENV = {
    "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",  # date and model-identity reminders
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",  # billing header block at the top of `system`
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",  # context_management edits, extra betas
    "DISABLE_PROMPT_CACHING": "1",  # cache_control breakpoints
    "CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK": "1",  # never answer with a model we didn't ask for
}

# Variables Claude Code needs to authenticate or pick a provider; every other CLAUDE* variable
# (session ids, CLAUDE_EFFORT, IDE sockets, ...) would leak the calling session into the request.
KEEP = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"}
KEEP_PREFIXES = ("CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_")


def child_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE") or k in KEEP or k.startswith(KEEP_PREFIXES)
    }
    return env | ENV


def command(
    model: str, system: str | None, effort: str | None, output_schema: dict | None = None
) -> list[str]:
    # `=` form so a system prompt starting with "-" isn't parsed as a flag. An empty one still
    # replaces Claude Code's default system prompt.
    cmd = ["claude", *FLAGS, "--model", model, f"--system-prompt={system or ''}"]
    if effort:
        cmd += ["--effort", effort]
    if output_schema is not None:
        # Claude Code enforces this with a synthetic tool and an extra turn, not the API's
        # native output_config.format.
        cmd.append(f"--json-schema={json.dumps(output_schema)}")
    return cmd


async def infer(
    model: str,
    prompt: str,
    system: str | None,
    effort: str | None,
    cwd: Path,
    output_schema: dict | None = None,
) -> tuple[str, str]:
    """Run one request; return (text, model that served it).

    With `output_schema`, text is the JSON document matching it.

    `cwd` should be an empty directory: its path appears in the environment message.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *command(model, system, effort, output_schema),
        cwd=cwd,
        env=child_env(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        # The prompt goes over stdin: no argv size limit, and it can't be mistaken for a flag.
        stdout, stderr = await proc.communicate(prompt.encode())
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude exited {proc.returncode}: {stderr.decode().strip()}") from None

    if result.get("is_error") or proc.returncode != 0:
        detail = result.get("result") or stderr.decode().strip() or result.get("terminal_reason")
        raise RuntimeError(f"claude failed ({result.get('terminal_reason')}): {detail}")
    if result["stop_reason"] == "refusal":
        raise RuntimeError(f"Request was refused: {result.get('result')}")
    if result["stop_reason"] == "max_tokens":
        raise RuntimeError("Response truncated at max_tokens; not cached.")

    # modelUsage is keyed by model in the order they ran; after a refusal fallback the last one answered.
    served_by = list(result["modelUsage"])[-1]
    if output_schema is not None:
        if result.get("structured_output") is None:
            raise RuntimeError("claude returned no structured output for the schema.")
        return json.dumps(result["structured_output"]), served_by
    return result["result"], served_by
