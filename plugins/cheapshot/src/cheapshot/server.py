"""MCP server exposing a cached one-shot Claude inference tool."""

from __future__ import annotations

import json
import os
from typing import Any, Literal

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
        "Identical inputs (prompt, system, model, effort, output_schema) return the cached answer "
        "at no cost."
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

    Use this for stateless questions whose answer depends only on the input: summarizing
    or classifying text, extracting data, drafting, or getting a second opinion. The model
    sees nothing but `prompt` (and `system`), so include all needed context in the prompt.

    Repeating the exact same prompt/system/model/effort/output_schema returns the stored answer instantly
    without a new model call. Set `refresh` to force a new call and overwrite the stored answer.

    Args:
        prompt: The full user message.
        system: Optional system prompt.
        model: Claude model ID. Defaults to $CHEAPSHOT_MODEL or claude-opus-5.
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

    if not refresh and (hit := cache.get(key)):
        return result(hit.text, True, hit.model, hit.created_at)

    text, served_by = await infer(model, prompt, system, effort, output_schema)
    entry = cache.put(key, request, text, served_by)
    return result(text, False, served_by, entry.created_at)
