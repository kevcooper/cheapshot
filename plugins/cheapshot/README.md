# cheapshot

A Claude Code plugin that gives the agent one MCP tool, `oneshot`. The tool sends one
stateless request to Claude and stores the answer in a local SQLite cache. When the same
input comes in again, the stored answer comes back and no API call is made.

## Tool: `oneshot`

| Arg       | Default                              | Notes                                              |
|-----------|--------------------------------------|----------------------------------------------------|
| `prompt`  | required                             | The full user message                              |
| `system`  | none                                 | Optional system prompt                             |
| `model`   | `$CHEAPSHOT_MODEL` or `claude-opus-5` |                                                    |
| `effort`  | model default                        | `low` / `medium` / `high` / `xhigh` / `max`        |
| `refresh` | `false`                              | Skip the cache, run the request again, overwrite   |

It returns `{text, cached, model, cache_key, created_at}`.

- The cache key is a SHA-256 of `(model, prompt, system, effort)`. Leaving `model` out and
  passing the default model explicitly produce the same key.
- Only complete answers are stored. Refusals and answers cut off at `max_tokens` raise an
  error and are not cached.
- On Opus 5 / 5.5 and Fable 5 / 5.1, server-side refusal fallbacks (`fallbacks: "default"`) are on.

## Configuration

- Credentials: the standard Anthropic SDK resolution (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an `ant auth login` profile).
- Cache location: `$CHEAPSHOT_CACHE_DIR`, else `$CLAUDE_PLUGIN_DATA`, else `~/.cache/cheapshot`.

## Development

```sh
uv sync
uv run pytest
uv run cheapshot   # runs the MCP server on stdio
```
