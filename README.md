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

cheapshot is distributed through the [kevcooper plugin marketplace](https://github.com/kevcooper/claude-plugins),
a catalog of Claude Code plugins. Installing is two steps: add the marketplace once, then
install cheapshot from it.

### 1. Add the marketplace

In Claude Code:

```
/plugin marketplace add kevcooper/claude-plugins
```

Or from your terminal:

```
claude plugin marketplace add kevcooper/claude-plugins
```

You only do this once per machine. The marketplace is registered as `kevcooper`, and
`/plugin marketplace list` should now show it.

### 2. Install cheapshot

In Claude Code:

```
/plugin install cheapshot@kevcooper
```

Or from your terminal:

```
claude plugin install cheapshot@kevcooper
```

Then start a new session so the `oneshot` tool loads.

### Updating

To pull the latest version, refresh the marketplace and then update the plugin:

```
claude plugin marketplace update kevcooper
claude plugin update cheapshot@kevcooper
```

Restart Claude Code to load the new version.

### Installing for a whole project

To have everyone working in a repo get cheapshot, pass `--scope project` to both commands:

```
claude plugin marketplace add kevcooper/claude-plugins --scope project
claude plugin install cheapshot@kevcooper --scope project
```

This writes the following to `.claude/settings.json`, which you commit so teammates are
offered the plugin when they trust the folder:

```json
{
  "extraKnownMarketplaces": {
    "kevcooper": {
      "source": { "source": "github", "repo": "kevcooper/claude-plugins" }
    }
  },
  "enabledPlugins": {
    "cheapshot@kevcooper": true
  }
}
```

### Migrating from `cheapshot@cheapshot`

Earlier versions were installed from a marketplace in this repo, which has been removed
and won't receive updates. Switch over with:

```
/plugin uninstall cheapshot@cheapshot
/plugin marketplace remove cheapshot
```

then follow steps 1 and 2 above.

## Documentation

See [plugins/cheapshot/README.md](plugins/cheapshot/README.md) for the tool's arguments,
caching behavior, configuration, and what gets stored and sent.

## License

[MIT](LICENSE)
