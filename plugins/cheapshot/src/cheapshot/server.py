"""MCP server exposing a cached one-shot Claude inference tool."""

from __future__ import annotations

import os
from typing import Literal

import anthropic
from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel

from cheapshot.cache import Cache, cache_key, default_cache_dir

DEFAULT_MODEL = "claude-opus-5"
MAX_TOKENS = 64000

# Models that accept server-side refusal fallbacks (`fallbacks: "default"`).
FALLBACK_MODELS = {"claude-opus-5", "claude-opus-5-5", "claude-fable-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

Effort = Literal["low", "medium", "high", "xhigh", "max"]

mcp = MCPServer(
    "cheapshot",
    instructions=(
        "cheapshot runs a single stateless Claude request and caches the answer locally. "
        "Identical inputs (prompt, system, model, effort) return the cached answer at no cost."
    ),
)

_cache: Cache | None = None
_client: anthropic.AsyncAnthropic | None = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache(default_cache_dir() / "cache.sqlite3")
    return _cache


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


class OneshotResult(BaseModel):
    text: str
    cached: bool
    model: str
    cache_key: str
    created_at: float


async def infer(model: str, prompt: str, system: str | None, effort: str | None) -> tuple[str, str]:
    """Run one streamed request; return (text, model that served it)."""
    params: dict = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        params["system"] = system
    if effort:
        params["output_config"] = {"effort": effort}

    client = get_client()
    if model in FALLBACK_MODELS:
        stream = client.beta.messages.stream(**params, betas=[FALLBACK_BETA], fallbacks="default")
    else:
        stream = client.messages.stream(**params)
    async with stream as s:
        message = await s.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError(f"Request was refused: {message.stop_details}")
    if message.stop_reason == "max_tokens":
        raise RuntimeError(f"Response truncated at max_tokens={MAX_TOKENS}; not cached.")
    text = "".join(block.text for block in message.content if block.type == "text")
    return text, message.model


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
    without a new API call. Set `refresh` to force a new call and overwrite the stored answer.

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
