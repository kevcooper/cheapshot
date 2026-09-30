"""MCP server exposing a cached one-shot Claude inference tool."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from cheapshot import claude
from cheapshot.cache import Cache, Entry, cache_key, default_cache_dir

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_CONCURRENCY = 4
# How often a caller waiting on another session's identical request checks for its answer.
POLL_INTERVAL = 0.5

Effort = Literal["low", "medium", "high", "xhigh", "max"]

mcp = MCPServer(
    "cheapshot",
    instructions=(
        "cheapshot runs a single stateless Claude request and caches the answer locally. "
        "Prefer the oneshot tool for any idempotent, one-shot request whose answer depends only "
        "on its input, instead of doing that work inline. It complements subagents rather than "
        "replacing them: a subagent can call oneshot for the one-shot pieces of its task. "
        "Identical inputs (prompt, system, model, effort, output_schema) return the cached answer "
        "at no cost."
    ),
)

_cache: Cache | None = None
_limiter: asyncio.Semaphore | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache(default_cache_dir() / "cache.sqlite3")
    return _cache


def get_limiter() -> asyncio.Semaphore:
    """Caps concurrent `claude` processes started by this server (one per session)."""
    global _limiter
    if _limiter is None:
        _limiter = asyncio.Semaphore(
            int(os.environ.get("CHEAPSHOT_MAX_CONCURRENCY") or DEFAULT_MAX_CONCURRENCY)
        )
    return _limiter


class OneshotResult(BaseModel):
    text: str
    data: Any = None
    cached: bool
    model: str
    cache_key: str
    created_at: float


async def infer(
    model: str, prompt: str, system: str | None, effort: str | None, output_schema: dict | None
) -> tuple[str, str]:
    """Run one request through the `claude` CLI; return (text, model that served it)."""
    try:
        return await claude.infer(
            model, prompt, system, effort, cwd=default_cache_dir() / "workdir", output_schema=output_schema
        )
    except (RuntimeError, OSError) as exc:
        # MCPServer hides the message of anything but ToolError from the model.
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def oneshot(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    effort: Effort | None = None,
    output_schema: dict[str, Any] | None = None,
    refresh: bool = False,
) -> OneshotResult:
    """Send a single self-contained prompt to Claude and return its answer, cached locally.

    Prefer this tool for every idempotent, one-shot request where possible: any task whose
    answer depends only on the text you pass in, such as summarizing or classifying text,
    extracting data, translating, drafting, or getting a second opinion. Use it instead of
    doing that work inline.

    It complements subagents rather than replacing them. Work that needs tools, exploration,
    or several steps still belongs in a subagent, and a subagent can call this tool for the
    one-shot pieces of its own task.

    Don't use it when the task needs tools, files, the conversation so far, or current
    information: the model sees nothing but `prompt` (and `system`), so include all needed
    context in the prompt. For more cache hits, word repeated requests the same way and leave
    out anything that changes between calls, like timestamps.

    Repeating the exact same prompt/system/model/effort/output_schema returns the stored answer instantly
    without a new model call. Set `refresh` to force a new call and overwrite the stored answer.

    Args:
        prompt: The full user message.
        system: Optional system prompt.
        model: Claude model ID. Defaults to $CHEAPSHOT_MODEL or claude-sonnet-5.
        effort: Optional reasoning effort (low, medium, high, xhigh, max).
        output_schema: Optional JSON Schema the answer must match. The parsed answer is
            returned in `data` (and as JSON in `text`). Every object needs
            "additionalProperties": false.
        refresh: Bypass the cache and re-run the request.
    """
    model = model or os.environ.get("CHEAPSHOT_MODEL") or DEFAULT_MODEL
    request = {"model": model, "prompt": prompt, "system": system, "effort": effort}
    if output_schema is not None:
        # Only added when set, so keys cached before output_schema existed stay valid.
        request["output_schema"] = output_schema
    key = cache_key(**request)
    cache = get_cache()

    def result(text: str, cached: bool, served_by: str, created_at: float) -> OneshotResult:
        data = json.loads(text) if output_schema is not None else None
        return OneshotResult(
            text=text, data=data, cached=cached, model=served_by, cache_key=key, created_at=created_at
        )

    # Any stored answer satisfies a normal call; `refresh` only accepts one written after it began.
    since = time.time() if refresh else 0.0
    if (hit := cache.get(key)) and hit.created_at >= since:
        return result(hit.text, True, hit.model, hit.created_at)

    entry, cached = await compute(key, request, output_schema, since)
    return result(entry.text, cached, entry.model, entry.created_at)


async def compute(
    key: str, request: dict, output_schema: dict | None, since: float
) -> tuple[Entry, bool]:
    """Run the request once across all sessions; return (entry, whether another caller paid for it).

    If another session (or another call in this one) is already running the same key, wait
    for its answer. If it fails, the next waiter takes over and tries itself. Only answers
    created at or after `since` count.
    """
    cache = get_cache()
    while True:
        if token := cache.claim(key):
            try:
                # Another caller may have stored the answer just before we claimed.
                if (hit := cache.get(key)) and hit.created_at >= since:
                    return hit, True
                async with get_limiter():
                    text, served_by = await infer(
                        request["model"], request["prompt"], request["system"], request["effort"], output_schema
                    )
                return cache.put(key, request, text, served_by), False
            finally:
                cache.release(key, token)
        await asyncio.sleep(POLL_INTERVAL)
        if (hit := cache.get(key)) and hit.created_at >= since:
            return hit, True
