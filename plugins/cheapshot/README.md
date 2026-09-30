# cheapshot

A Claude Code plugin that gives the agent one MCP tool, `oneshot`. The tool sends one
stateless request to Claude through the `claude` CLI (so it uses your existing Claude Code
login) and stores the answer in a local SQLite cache. When the same
input comes in again, the stored answer comes back and no API call is made.

## Tool: `oneshot`

| Arg       | Default                              | Notes                                              |
|-----------|--------------------------------------|----------------------------------------------------|
| `prompt`  | required                             | The full user message                              |
| `system`  | none                                 | Optional system prompt                             |
| `model`   | `$CHEAPSHOT_MODEL` or `claude-sonnet-5` |                                                    |
| `effort`  | model default                        | `low` / `medium` / `high` / `xhigh` / `max`        |
| `output_schema` | none                           | JSON Schema the answer must match; parsed into `data` |
| `files`   | none                                 | Absolute paths of UTF-8 text files to include before the prompt |
| `refresh` | `false`                              | Skip the cache, run the request again, overwrite   |

It returns `{text, data, cached, model, cache_key, created_at}`. `data` is the parsed answer
when `output_schema` is set, otherwise `null`.

- The cache key is a SHA-256 of `(model, prompt, system, effort)`, plus `output_schema` when set, plus each file's
  path and SHA-256 when `files` is set. Editing a file changes the key; the file contents are
  not stored in the cache. Leaving `model` out and
  passing the default model explicitly produce the same key.
- Sessions share the cache. If one is already running a request, others that send the same one
  wait for its answer (`cached: true`) instead of paying for it again. If that call fails, they
  get the same error. If it stops without an answer or an error (cancelled, or its session
  died), the next waiter runs it itself.
- Only complete answers are stored. Refusals and answers cut off at `max_tokens` raise an
  error and are not cached.
- Every object in `output_schema` needs `"additionalProperties": false`. Claude Code enforces
  the schema with a synthetic tool and an extra turn, so schema calls cost a little more.
- `src/cheapshot/claude.py` documents what the CLI request still carries beyond a bare API call.

## Configuration

- Credentials: whatever the `claude` CLI on your PATH is logged in with.
- Cache location: `$CHEAPSHOT_CACHE_DIR`, else `$CLAUDE_PLUGIN_DATA`, else `~/.cache/cheapshot`.
- `CHEAPSHOT_MAX_FILE_BYTES` (default 1000000): cap on the total size of `files` in one call.
- `CHEAPSHOT_MAX_CONCURRENCY` (default 4): most `claude` processes one session's server runs at once.
  Extra calls queue.

## Is it paying off?

Each entry counts the calls it answered without a model call (`hits`), and keeps the CLI's
full JSON result for the call that produced it (`response`: tokens, cost, timings), minus the
answer text:

```sh
sqlite3 ~/.claude/plugins/data/cheapshot-cheapshot/cache.sqlite3 "
  SELECT count(*) AS entries, sum(hits) AS hits,
         round(sum(json_extract(response, '$.total_cost_usd')), 4) AS cost_usd,
         round(sum(hits * json_extract(response, '$.total_cost_usd')), 4) AS saved_usd
  FROM results"
```

`cost_usd` is the list-price cost of the model calls made; `saved_usd` is what the hits would
have cost. Entries from before this was recorded have no `response`.

## Development

```sh
uv sync
uv run pytest
uv run cheapshot   # runs the MCP server on stdio
```
