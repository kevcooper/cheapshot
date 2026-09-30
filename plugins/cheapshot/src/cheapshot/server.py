"""MCP server exposing a cached one-shot Claude inference tool."""

from __future__ import annotations

import os
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from cheapshot import claude
from cheapshot.cache import Cache, cache_key, default_cache_dir

DEFAULT_MODEL = "claude-opus-5"

Effort = Literal["low", "medium", "high", "xhigh", "max"]

mcp = MCPServer(
    "cheapshot",
    instructions=(
        "cheapshot runs a single stateless Claude request and caches the answer locally. "
        "Identical inputs (prompt, system, model, effort) return the cached answer at no cost."
    ),
)

_cache: Cache | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache(default_cache_dir() / "cache.sqlite3")
    return _cache


class OneshotResult(BaseModel):
    text: str
    cached: bool
    model: str
    cache_key: str
    created_at: float


async def infer(model: str, prompt: str, system: str | None, effort: str | None) -> tuple[str, str]:
    """Run one request through the `claude` CLI; return (text, model that served it)."""
    try:
        return await claude.infer(model, prompt, system, effort, cwd=default_cache_dir() / "workdir")
    except (RuntimeError, OSError) as exc:
        # MCPServer hides the message of anything but ToolError from the model.
        raise ToolError(str(exc)) from exc


@mcp.tool()
async def oneshot(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    effort: Effort | None = None,
    refresh: bool = False,
) -> OneshotResult:
    """Send a single self-contained prompt to Claude and return its answer, cached locally.

    Use this for stateless questions whose answer depends only on the input: summarizing
    or classifying text, extracting data, drafting, or getting a second opinion. The model
    sees nothing but `prompt` (and `system`), so include all needed context in the prompt.

    Repeating the exact same prompt/system/model/effort returns the stored answer instantly
    without a new model call. Set `refresh` to force a new call and overwrite the stored answer.

    Args:
        prompt: The full user message.
        system: Optional system prompt.
        model: Claude model ID. Defaults to $CHEAPSHOT_MODEL or claude-opus-5.
        effort: Optional reasoning effort (low, medium, high, xhigh, max).
        refresh: Bypass the cache and re-run the request.
    """
    model = model or os.environ.get("CHEAPSHOT_MODEL") or DEFAULT_MODEL
    request = {"model": model, "prompt": prompt, "system": system, "effort": effort}
    key = cache_key(**request)
    cache = get_cache()

    if not refresh and (hit := cache.get(key)):
        return OneshotResult(text=hit.text, cached=True, model=hit.model, cache_key=key, created_at=hit.created_at)

    text, served_by = await infer(model, prompt, system, effort)
    entry = cache.put(key, request, text, served_by)
    return OneshotResult(text=text, cached=False, model=served_by, cache_key=key, created_at=entry.created_at)
