# cheapshot

A Claude Code plugin that adds one MCP tool, `oneshot`. The tool runs a single self-contained
Claude request and caches the answer on your machine, so asking the same thing again, from any
session, returns instantly and costs nothing.

Use it for work like summarizing or reviewing a file, extracting structured data, drafting,
or any check you run repeatedly. Files are passed by path and keyed by their contents, so an
edited file gets a fresh answer.

## Requirements

- macOS or Linux (Windows isn't supported)
- [Claude Code](https://code.claude.com), logged in; the tool runs requests through the `claude`
  CLI on your PATH and uses your existing login
- [uv](https://docs.astral.sh/uv/), which starts the server and installs its Python dependencies

## Install

In Claude Code:

```
/plugin marketplace add kevcooper/cheapshot
/plugin install cheapshot@cheapshot
```

Then start a new session.

## Documentation

See [plugins/cheapshot/README.md](plugins/cheapshot/README.md) for the tool's arguments,
caching behavior, configuration, and what gets stored and sent.

## License

[MIT](LICENSE)
