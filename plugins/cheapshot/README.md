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
| `model`   | `$CHEAPSHOT_MODEL` or `claude-opus-5` |                                                    |
| `effort`  | model default                        | `low` / `medium` / `high` / `xhigh` / `max`        |
| `output_schema` | none                           | JSON Schema the answer must match; parsed into `data` |
| `refresh` | `false`                              | Skip the cache, run the request again, overwrite   |

It returns `{text, data, cached, model, cache_key, created_at}`. `data` is the parsed answer
when `output_schema` is set, otherwise `null`.

- The cache key is a SHA-256 of `(model, prompt, system, effort)`, plus `output_schema` when set. Leaving `model` out and
  passing the default model explicitly produce the same key.
- Only complete answers are stored. Refusals and answers cut off at `max_tokens` raise an
  error and are not cached.
- Every object in `output_schema` needs `"additionalProperties": false`. Claude Code enforces
  the schema with a synthetic tool and an extra turn, so schema calls cost a little more.
- `src/cheapshot/claude.py` documents what the CLI request still carries beyond a bare API call.

## Configuration

- Credentials: whatever the `claude` CLI on your PATH is logged in with.
- Cache location: `$CHEAPSHOT_CACHE_DIR`, else `$CLAUDE_PLUGIN_DATA`, else `~/.cache/cheapshot`.

## Development

```sh
uv sync
uv run pytest
uv run cheapshot   # runs the MCP server on stdio
```
