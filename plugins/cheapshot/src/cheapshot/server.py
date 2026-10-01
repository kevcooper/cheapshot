"""MCP server exposing a cached one-shot Claude inference tool."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import time
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from cheapshot import claude, files as file_input
from cheapshot.cache import Cache, Entry, SchemaTooNew, cache_key, default_cache_dir

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_CONCURRENCY = 4
# How often a caller waiting on another session's identical request checks for its answer.
POLL_INTERVAL = 0.5

Effort = Literal["low", "medium", "high", "xhigh", "max"]

mcp = MCPServer(
    "cheapshot",
    instructions=(
        "cheapshot runs a single stateless Claude request and caches the answer locally. Use the "
        "oneshot tool for self-contained, idempotent tasks worth a separate model call: work on "
        "files already on disk (pass absolute paths in `files` rather than pasting them), long "
        "answers from short inputs, and requests likely to be repeated. Answer short tasks you "
        "can handle from context inline instead; each new call takes several seconds. It "
        "complements subagents rather than replacing them: a subagent can call oneshot for the "
        "one-shot pieces of its task. To get the most from the cache, break a larger problem into "
        "small, self-contained questions that each depend only on their own input (for example, "
        "one call per file), keep the instruction wording the same across calls, and leave out "
        "incidental context such as timestamps or the overall goal. Before running calls "
        "concurrently that share files or a long system prompt, warm the prompt cache: send one "
        "first and wait for it, then send the rest in parallel. Identical inputs (prompt, "
        "system, model, effort, output_schema, and the contents of any files) return the cached "
        "answer at no cost."
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


def surface(*errors: type[Exception]):
    """Re-raise `errors` as ToolError: MCPServer hides the message of any other exception."""

    def wrap(fn):
        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except errors as exc:
                raise ToolError(str(exc)) from exc

        return inner

    return wrap


class OneshotResult(BaseModel):
    text: str
    data: Any = None
    cached: bool
    model: str
    cache_key: str
    created_at: float


async def infer(
    model: str,
    prompt: str,
    system: str | None,
    effort: str | None,
    output_schema: dict | None,
    attachments: list[str],
    caching: claude.Caching,
) -> tuple[str, str, dict]:
    """Run one request through the `claude` CLI; return (text, model that served it, raw response)."""
    try:
        return await claude.infer(
            model,
            prompt,
            system,
            effort,
            cwd=default_cache_dir() / "workdir",
            output_schema=output_schema,
            attachments=attachments,
            caching=caching,
        )
    except (RuntimeError, OSError) as exc:
        # MCPServer hides the message of anything but ToolError from the model.
        raise ToolError(str(exc)) from exc


@mcp.tool()
@surface(SchemaTooNew)
async def oneshot(
    prompt: str,
    system: str | None = None,
    model: str | None = None,
    effort: Effort | None = None,
    output_schema: dict[str, Any] | None = None,
    files: list[str] | None = None,
    cache_prompt: bool = True,
    cache_files: bool = True,
    cache_ttl: claude.CacheTTL = "5m",
    refresh: bool = False,
) -> OneshotResult:
    """Send a single self-contained prompt to Claude and return its answer, cached locally.

    Use it for self-contained, idempotent tasks whose answer depends only on what you pass
    in, where a separate model call pays off:
    - work on files already on disk, such as summarizing, reviewing, or extracting from them
      (pass them in `files`);
    - long answers from a short input, such as drafts, rewrites, or translations;
    - requests likely to be repeated, in this session or others, such as the same check or
      summary run again.
    Answer inline instead when the task is short and you already have what you need in
    context: a new call takes several seconds, and you pay to write out the prompt.

    It complements subagents rather than replacing them. Work that needs tools, exploration,
    or several steps still belongs in a subagent, and a subagent can call this tool for the
    one-shot pieces of its own task.

    Don't use it when the task needs tools, the conversation so far, or current information:
    the model sees only `prompt`, `system`, and the contents of `files`, so include all needed
    context. To work on files that already exist, pass their absolute paths in `files` instead
    of pasting them into the prompt; naming a file in the prompt does not show it to the model.
    Don't write text to a file just to pass it here.

    Get the most from the cache by breaking a larger problem into small, self-contained
    questions whose answers depend only on their own inputs, and asking each one separately:
    - One call per file or item (review each changed file, summarize each document) rather
      than one call over all of them, so an edit to one input re-runs only its own call.
    - Keep the instruction wording, `system`, and `output_schema` the same across calls, and
      let only the input vary.
    - Leave out anything incidental that would make otherwise identical questions differ:
      timestamps, the overall task you're working on, session-specific names, or context the
      question doesn't need.
    Weigh this against the several seconds each new call takes: split where pieces are likely
    to be asked again, not into fragments. Independent calls can run in parallel.

    Warm the prompt cache before running calls concurrently that share a prefix: the same
    files, or the same long `system` prompt (for example, many inputs classified against one
    reference file). The prompt cache only has an entry once a call has finished, so calls
    sent together each pay full price for the shared part. Send one of them first and wait
    for it to finish; it can be a real question, not a throwaway. Then send the rest in
    parallel, and each reads the shared part at about a tenth of the input price. Within
    `cache_ttl` (5 minutes by default) the cache stays warm, so a long run of calls keeps
    hitting it. Turn `cache_files` off for a large file you won't ask about again.

    Repeating the exact same prompt/system/model/effort/output_schema, with files whose
    contents haven't changed, returns the stored answer instantly without a new model call.
    Set `refresh` to force a new call and overwrite the stored answer.

    Args:
        prompt: The full user message.
        system: Optional system prompt.
        model: Claude model ID. Defaults to $CHEAPSHOT_MODEL or claude-sonnet-5.
        effort: Optional reasoning effort (low, medium, high, xhigh, max).
        output_schema: Optional JSON Schema the answer must match. The parsed answer is
            returned in `data` (and as JSON in `text`). Every object needs
            "additionalProperties": false.
        files: Optional absolute paths of UTF-8 text files to include before the prompt.
            Editing a file changes the cache key.
        cache_prompt: Prompt-cache the system prompt and prompt, so a later call reusing the
            same system prompt reads it at a fraction of the input price. Default on.
        cache_files: Prompt-cache the files, so another question about the same files reads
            them at a fraction of the input price. Default on.
        cache_ttl: How long those cache entries live: "5m" (default) or "1h". Writing an
            entry costs about 1.25x the input price for 5m and 2x for 1h, and each read
            about 0.1x, so 5m pays off after one reuse and 1h after two. Use 1h only when
            the same system prompt or files will be sent again more than 5 minutes later.
            These settings never change the answer or the local cache key.
        refresh: Bypass the cache and re-run the request.
    """
    model = model or os.environ.get("CHEAPSHOT_MODEL") or DEFAULT_MODEL
    request = {"model": model, "prompt": prompt, "system": system, "effort": effort}
    # Optional fields are only added when set, so keys cached before they existed stay valid.
    if output_schema is not None:
        request["output_schema"] = output_schema
    try:
        loaded = file_input.load(files or [])
    except (ValueError, OSError) as exc:
        raise ToolError(str(exc)) from exc
    if loaded:
        # Content hashes, not contents: the key follows edits, and the database stays small.
        request["files"] = [{"path": f.path, "sha256": f.sha256} for f in loaded]
    attachments = [file_input.render(f) for f in loaded]
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
        cache.record_hit(key)
        return result(hit.text, True, hit.model, hit.created_at)

    caching = claude.Caching(prompt=cache_prompt, files=cache_files, ttl=cache_ttl)
    entry, cached = await compute(key, request, attachments, caching, output_schema, since)
    return result(entry.text, cached, entry.model, entry.created_at)


async def compute(
    key: str,
    request: dict,
    attachments: list[str],
    caching: claude.Caching,
    output_schema: dict | None,
    since: float,
) -> tuple[Entry, bool]:
    """Run the request once across all sessions; return (entry, whether another caller paid for it).

    If another session (or another call in this one) is already running the same key, wait
    for its answer, or fail with its error if it fails. If it stops without either (it was
    cancelled or its process died), take over and run it. Only answers created at or after
    `since` count.
    """
    cache = get_cache()
    waiting_since = time.time()
    while True:
        if token := cache.claim(key):
            error = None
            try:
                # Another caller may have stored the answer just before we claimed.
                if (hit := cache.get(key)) and hit.created_at >= since:
                    cache.record_hit(key)
                    return hit, True
                async with get_limiter():
                    text, served_by, response = await infer(
                        request["model"],
                        request["prompt"],
                        request["system"],
                        request["effort"],
                        output_schema,
                        attachments,
                        caching,
                    )
                return cache.put(key, request, text, served_by, response), False
            except ToolError as exc:
                error = str(exc)
                raise
            finally:
                cache.release(key, token, error)
        await asyncio.sleep(POLL_INTERVAL)
        if (hit := cache.get(key)) and hit.created_at >= since:
            cache.record_hit(key)
            return hit, True
        if error := cache.failure(key, waiting_since):
            raise ToolError(f"{error} (from a concurrent identical request)")
