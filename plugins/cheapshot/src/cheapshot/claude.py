"""Run one stateless request through the `claude` CLI, stripped as close to a bare API call as it allows.

What the request still carries that a bare API call would not (none of it can be turned off
while authenticating with a Claude subscription):
- the system prompt is prefixed with "You are a Claude agent, built on Anthropic's Claude Agent SDK."
- a <system-reminder> with the signed-in account's email address
- a trailing `system` message with the environment (cwd, platform, shell, OS), the model's name,
  and today's date

The environment variables in ENV are undocumented Claude Code settings, found by capturing the
requests the CLI sends. Verified with Claude Code 2.1.286; a later version may ignore or change
them, which would make requests carry more than listed here.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

FLAGS = [
    "--print",
    # Structured input lets files and the prompt be separate content blocks, so a file can carry
    # its own cache breakpoint. It requires streamed output, which in turn requires --verbose.
    "--input-format", "stream-json",
    "--output-format", "stream-json",
    "--verbose",
    "--no-session-persistence",
    "--safe-mode",  # no CLAUDE.md, plugins, hooks, MCP servers, skills, agents, output styles
    "--tools", "",  # no tool definitions
]

# Each of these removes something from the request body, except as noted; tests/test_claude_cli.py
# checks that they still do.
ENV = {
    # Reminder attachments. No visible effect as of 2.1.286 once child_env drops the calling
    # session's variables (those were what added date and model reminders); kept as a guard.
    "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
    "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",  # billing header block at the top of `system`
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",  # context_management edits, extra betas
    # Not in the body: makes an unknown or unavailable model an error instead of a substitute.
    "CLAUDE_CODE_DISABLE_MODEL_ACCESS_FALLBACK": "1",
}

# Variables Claude Code needs to authenticate or pick a provider; every other CLAUDE* variable
# (session ids, CLAUDE_EFFORT, IDE sockets, ...) would leak the calling session into the request.
KEEP = {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"}
KEEP_PREFIXES = ("CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_")


CacheTTL = Literal["5m", "1h"]


@dataclass(frozen=True)
class Caching:
    """Which parts of a request get prompt-cache breakpoints, and for how long.

    Writing a cache entry costs more than plain input (about 1.25x for 5 minutes, 2x for an
    hour) and reading one costs about a tenth, so a breakpoint pays off when its prefix is sent
    again before it expires. None of this changes the answer.
    """

    prompt: bool = True  # Claude Code's own breakpoints: the system prompt and the prompt
    files: bool = True  # ours: after the last file, shared by any question about those files
    ttl: CacheTTL = "5m"

    def effective(self) -> Caching:
        # CHEAPSHOT_PROMPT_CACHING=0 turns all caching off, whatever the call asks for.
        if os.environ.get("CHEAPSHOT_PROMPT_CACHING") == "0":
            return Caching(prompt=False, files=False, ttl=self.ttl)
        return self

    def mark(self) -> dict:
        # The API reads a mark without `ttl` as 5 minutes, which is how Claude Code sends them.
        return {"type": "ephemeral"} if self.ttl == "5m" else {"type": "ephemeral", "ttl": self.ttl}


def child_env(caching: Caching = Caching()) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE") or k in KEEP or k.startswith(KEEP_PREFIXES)
    }
    caching = caching.effective()
    if not caching.prompt:
        env["DISABLE_PROMPT_CACHING"] = "1"
    # Always explicit: Claude Code's default depends on the login (1 hour on a subscription,
    # 5 minutes with an API key), and our file breakpoint must use the same TTL because the API
    # rejects a longer-lived breakpoint after a shorter one.
    env["CLAUDE_CODE_PROMPT_CACHE_TTL"] = caching.ttl
    return env | ENV


def message(attachments: list[str], prompt: str, caching: Caching = Caching()) -> bytes:
    """The stdin line: one text block per attachment (rendered file), then the prompt.

    With `caching.prompt`, Claude Code marks the system prompt and the last block (the prompt)
    itself. With `caching.files`, a mark on the last attachment writes a cache entry that ends at
    the files, so another question about the same files reuses it. All on, that's four
    breakpoints, the API's maximum.
    """
    blocks = [{"type": "text", "text": text} for text in attachments]
    if blocks and caching.effective().files:
        blocks[-1]["cache_control"] = caching.mark()
    blocks.append({"type": "text", "text": prompt})
    line = {"type": "user", "message": {"role": "user", "content": blocks}}
    return (json.dumps(line) + "\n").encode()


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
    attachments: list[str] = (),
    caching: Caching = Caching(),
) -> tuple[str, str, dict]:
    """Run one request; return (text, model that served it, the CLI's JSON result).

    `attachments` are text blocks placed before the prompt, such as rendered files. With
    `output_schema`, text is the JSON document matching it.

    `cwd` should be an empty directory: its path appears in the environment message.
    """
    cwd.mkdir(mode=0o700, parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        *command(model, system, effort, output_schema),
        cwd=cwd,
        env=child_env(caching),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        # The prompt goes over stdin: no argv size limit, and it can't be mistaken for a flag.
        stdout, stderr = await proc.communicate(message(list(attachments), prompt, caching))
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    result = final_result(stdout)
    if result is None:
        raise RuntimeError(f"claude exited {proc.returncode}: {stderr.decode().strip()}")

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
        return json.dumps(result["structured_output"]), served_by, result
    return result["result"], served_by, result


def final_result(stdout: bytes) -> dict | None:
    """The `result` event at the end of the CLI's stream-json output, if it got that far."""
    for line in reversed(stdout.decode(errors="replace").splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None
