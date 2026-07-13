# Claude Usage TUI

An independent, local-only terminal viewer for Claude Code token activity. It
draws daily, weekly, and cumulative graphs and always reports how much of the
calendar is backed by exact turns versus historical aggregate allocation.

![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## Install

With [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/ivanopcode/claude-usage.git
```

Or from a checkout:

```bash
uv tool install .
```

The runtime uses only the Python standard library and sends no data anywhere.

## Use

```bash
# Open the interactive daily graph
claude-usage

# Open a particular view
claude-usage weekly
claude-usage cumulative

# Compatibility with the earlier prototype
claude-usage usage daily

# Show exactly how much is observed and reconstructed
claude-usage audit

# Refresh only the local JSONL index
claude-usage scan
```

Inside the TUI, use the arrow keys or `d`, `w`, and `c` to change views. Press
`q` to exit. Add `--once --no-color` for a non-interactive plain-text render.

## Where the numbers come from

The tool never queries Anthropic and does not scrape CodexBar. It reads files
already maintained on your machine:

| Source | Owner | Used for | Accuracy |
|---|---|---|---|
| `~/.claude/projects/**/*.jsonl` | Claude Code | Per-turn timestamp, model, input/output and cache tokens | Exact for records that still exist when indexed |
| `~/.claude/usage-tui.db` | This tool | Incremental, durable index of the exact JSONL fields above | Exact copy of parsed token records |
| `~/.claude/stats-cache.json` | Claude Code | Retained model totals and per-day direct-token activity | Real local aggregate; may outlive original transcripts |

The index deliberately retains already parsed turns if Claude Code later
removes their JSONL file. It contains token metadata only, not prompts,
responses, filenames from source code, or transcript content.

### Exact versus reconstructed

For each model, the tool first sums exact indexed turns. If Claude's local
`modelUsage` total is larger, the difference is known to exist but its original
cache-token timestamps are no longer available. The tool distributes only that
difference across Claude's retained `dailyModelTokens` activity dates.

Consequently:

- `Lifetime` reconciles to the newest available local per-model total.
- Exact JSONL dates keep their exact token counts.
- Historical daily, weekly, peak, and streak values are approximate wherever
  an aggregate-only difference exists; the TUI marks these with `≈`.
- `claude-usage audit` prints exact and allocated totals globally and per model.
- Missing dates cannot be invented. If neither JSONL nor `stats-cache.json`
  covers a period, the tool has no evidence for that period.

This is **token activity**, including cache reads and cache creation. It is not
the percentage of a Claude subscription quota, an Anthropic invoice, or a
server-authoritative account history. Subscription windows and dynamic limits
are not present in these local history files.

## Privacy and storage

- All parsing and rendering happens locally.
- There are no runtime dependencies, telemetry, network requests, or API keys.
- The database defaults to `~/.claude/usage-tui.db` and can be overridden with
  `CLAUDE_USAGE_TUI_DB` or `--db`.
- `CLAUDE_STATS_CACHE` can point at a different stats cache.
- Use `--projects-dir PATH` to scan one non-default transcript directory.

## Project scope and provenance

This is a focused implementation containing only:

- a minimal incremental JSONL collector;
- source reconciliation and accuracy reporting;
- the terminal renderer and command dispatcher;
- tests and packaging.

It does not include the browser dashboard, pricing model, Docker setup, VS Code
extension, or general session analytics from
[`phuryn/claude-usage`](https://github.com/phuryn/claude-usage). That project
informed the early prototype and is credited, with its MIT notice, in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). The visual presentation is
inspired by the Codex token activity interface; no Codex source or assets are
included.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile cli.py collector.py usage_tui.py
```

## License

MIT © 2026 Ivan Oparin. See [`LICENSE`](LICENSE) and
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
