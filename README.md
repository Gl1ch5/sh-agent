<div align="center">

```
███████╗██╗  ██╗██╗  ██╗
██╔════╝██║  ██║╚██╗██╔╝
███████╗███████║ ╚███╔╝ 
╚════██║██╔══██║ ██╔██╗ 
███████║██║  ██║██╔╝ ██╗
╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝
```

# `sh-agent` (`shx`)

**Bash-only, ultra-economical AI agent.**  
One tool (`bash`). Smart command parsing. **Maximum prompt-cache hit rate.** Instant `Ctrl+C` cancellation.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://www.python.org/)
[![Tool: bash only](https://img.shields.io/badge/Tool-bash%20only-brightgreen.svg)](#architecture)

</div>

---

## Install

### macOS / Linux / Termux (Android)
```bash
curl -fsSL https://raw.githubusercontent.com/Gl1ch5/sh-agent/main/install.sh | bash
```

> **Termux:** auto-detected — installs `python` + `git` via `pkg` if needed and links `shx` into `$PREFIX/bin`.

### Windows (PowerShell)
```powershell
irm https://raw.githubusercontent.com/Gl1ch5/sh-agent/main/install.ps1 | iex
```

### Via `uv` (any OS)
```bash
uv tool install git+https://github.com/Gl1ch5/sh-agent.git
```

---

## First Launch

```bash
shx
```

1. **API URL** — any OpenAI-compatible endpoint (`https://api.openai.com/v1`, `https://openrouter.ai/api/v1`, `http://localhost:11434/v1`)
2. **API Key** — masked input (`Enter` to skip for local servers)
3. **Provider Name** — e.g. `openrouter`, `openai`, `local`

`shx` automatically **syncs `/models`**, saves `~/.sh-agent/config.json` (`0600`), selects the first model, and drops you straight into the prompt.

---

## Smart Command Parsing

Long shell commands and heredocs are parsed automatically into compact, readable action badges:

```
✎ write  src/app.py       (~24L · 18ms)     ✓ 0
◉ read   pyproject.toml   (22L · 9ms)       ✓ 0
⌕ grep   "TODO" src/      (4L · 14ms)       ✓ 0
⚡ bash   pytest -q        (310ms)           ✓ 0
```

- **Token Economy** — ~115 tokens total for system prompt + `bash` schema. Noisy command output is head+tail capped at 8,000 chars so logs never blow up your context.
- **High Cache Hit-Rate** — 100% static prefix (`system` + `tools`) and append-only history, with live cache telemetry (`▸ 240ms TTFT · 980 in (94% cached) · 42 out`).
- **Instant Cancellation** — press `Ctrl+C` anytime during streaming or while a command is running to abort cleanly and roll back the turn.

---

## Slash Commands

| Command | Action |
| :--- | :--- |
| `/model` | Interactive fuzzy-filter model picker |
| `/new` | New chat (clears context, keeps static prefix warm) |
| `/provider` | Add or reconfigure custom OpenAI-compatible provider |
| `/exit` | Quit `shx` |

---

## Architecture

```
src/sh_agent/
├── cli.py      # REPL loop, streaming, Ctrl+C rollback, telemetry
├── client.py   # SSE streaming client + cache-hit & TTFT tracking
├── config.py   # ~/.sh-agent/config.json + /models auto-sync
├── shell.py    # Cross-platform bash/PowerShell runner + smart action parser
└── ui.py       # Neon TUI theme, onboarding wizard, / completer, model picker
```

---

<div align="center">
<sub>MIT © <a href="https://github.com/Gl1ch5">Gl1ch5</a></sub>
</div>
