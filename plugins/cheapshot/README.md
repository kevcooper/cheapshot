# cheapshot

A Claude Code plugin that gives the agent one MCP tool, `oneshot`. The tool sends one
stateless request to Claude through the `claude` CLI (so it uses your existing Claude Code
login) and stores the answer in a local SQLite cache. When the same
input comes in again, the stored answer comes back and no API call is made.

Requires macOS or Linux, a logged-in `claude` CLI on your PATH, and
[uv](https://docs.astral.sh/uv/). Install instructions are in the
[repository README](../../README.md).

## Tool: `oneshot`

| Arg       | Default                              | Notes                                              |
|-----------|--------------------------------------|----------------------------------------------------|
| `prompt`  | required                             | The full user message                              |
| `system`  | none                                 | Optional system prompt                             |
| `model`   | `$CHEAPSHOT_MODEL` or `claude-sonnet-5` |                                                    |
| `effort`  | model default                        | `low` / `medium` / `high` / `xhigh` / `max`        |
| `output_schema` | none                           | JSON Schema the answer must match; parsed into `data` |
| `files`   | none                                 | Absolute paths of UTF-8 text files to include before the prompt |
| `cache_prompt` | `true`                          | Prompt-cache the system prompt and the prompt      |
| `cache_files` | `true`                           | Prompt-cache the files, for more questions about them |
| `cache_ttl` | `5m`                               | Prompt-cache lifetime: `5m` or `1h`                |
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

## What's stored and sent

- **Your usage:** each new answer is a real Claude request made with your `claude` login, so it
  counts against your subscription or API usage like any other Claude Code request. Cache hits
  make no request.
- **Stored locally:** the cache keeps each request (prompt, system prompt, model, effort,
  schema, and file paths with content hashes, but not file contents), the answer, and the
  CLI's JSON result for the call. It's plain text in SQLite, readable only by your user (the
  directory is `0700`, the files `0600`), and entries never expire; delete the database file to
  clear it.
- **Sent to Anthropic:** besides your prompt, system prompt, and files, the CLI adds a few
  things a bare API call wouldn't have: an identity line in the system prompt, your account's
  email address, and a message with the working directory (an empty folder in the cache
  directory), platform, shell, OS, model name, and date. See the top of
  [`src/cheapshot/claude.py`](src/cheapshot/claude.py).

## Claude Code compatibility

To keep requests close to a bare API call, `claude.py` runs the CLI with structured
(`stream-json`) input, `--safe-mode`, no tools, and a handful of environment variables that
turn off extras like reminders, the billing header, and experimental betas. Those variables are
undocumented Claude Code settings, found by capturing the requests the CLI sends, and were
verified with Claude Code 2.1.286. A later version may ignore or change them; answers would
keep working, but requests could carry more than described above. `tests/test_claude_cli.py`
checks each setting against the installed CLI by capturing the request it sends, without making
a model call; run it after updating Claude Code.

## Prompt caching

Separate from cheapshot's own cache (which answers exact repeats without any model call), the
model call itself uses Claude's prompt caching. Files go in their own content blocks ahead of
the prompt, so a different question about the same files, or a call reusing the same long
system prompt, reads that part from the cache at about a tenth of the input price. In a test
with a 19k-token file, the second question cost about 91% less than the first.

Writing a cache entry costs more than plain input: about 1.25x for the default 5-minute
lifetime and 2x for 1 hour. So 5 minutes pays off after one reuse, and 1 hour after two.
Agents choose per call with `cache_prompt`, `cache_files`, and `cache_ttl`; none of them change
the answer or the local cache key. Questions about the same files only hit the prompt cache
once the first has finished, so they should be sent one after another rather than all at once.

## Configuration

Per-call choices are tool arguments, made by the agent. These environment variables are for
you: where data lives, limits the agent shouldn't be able to raise, and your defaults.

- Credentials: whatever the `claude` CLI on your PATH is logged in with.
- Cache location: `$CHEAPSHOT_CACHE_DIR`, else `$CLAUDE_PLUGIN_DATA`, else `~/.cache/cheapshot`.
- `CHEAPSHOT_MODEL`: default model when a call doesn't set `model` (otherwise `claude-sonnet-5`).
- `CHEAPSHOT_PROMPT_CACHING`: set to `0` to turn prompt caching off for every call, whatever
  the call's `cache_*` arguments say.
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
uv run pytest                            # includes checks of the installed claude CLI (no model calls)
CHEAPSHOT_LIVE_TESTS=1 uv run pytest     # also makes three tiny Haiku calls
uv run cheapshot   # runs the MCP server on stdio
```
