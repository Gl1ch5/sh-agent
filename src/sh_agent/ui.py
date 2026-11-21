from __future__ import annotations

from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.text import Text

from .config import (
    default_provider_name,
    fetch_models,
    normalize_base_url,
    save_config,
)
from .shell import BashResult, ParsedAction

SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/model", "Switch model from synced list"),
    ("/new", "New chat (reset context)"),
    ("/provider", "Add / reconfigure custom provider"),
    ("/exit", "Exit shx"),
]

PROMPT_STYLE = Style.from_dict(
    {
        "completion-menu.completion": "bg:#181825 #cdd6f4",
        "completion-menu.completion.current": "bg:#a6e3a1 #11111b bold",
        "completion-menu.meta.completion": "bg:#181825 #6c7086",
        "completion-menu.meta.completion.current": "bg:#a6e3a1 #11111b",
        "scrollbar.background": "bg:#181825",
        "scrollbar.button": "bg:#45475a",
    }
)


class SlashCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/") or " " in text:
            return
        prefix = text.lower()
        for cmd, desc in SLASH_COMMANDS:
            if cmd.startswith(prefix):
                yield Completion(
                    cmd,
                    start_position=-len(text),
                    display=cmd,
                    display_meta=desc,
                )


def print_banner(console: Console, provider_name: str | None = None, model: str | None = None) -> None:
    title = Text()
    title.append("❯_ shx ", style="bold bright_green")
    title.append("· bash-only ultra-economical agent", style="dim")
    cwd_name = Path.cwd().name or str(Path.cwd())
    if provider_name and model:
        subtitle = (
            f"[bright_magenta]{provider_name}[/bright_magenta] "
            f"[bright_white]{model}[/bright_white]  "
            f"[dim]│  📁 {cwd_name}  │  / cmds · Ctrl+C cancel[/dim]"
        )
    else:
        subtitle = "[dim]first-time setup · custom OpenAI-compatible provider[/dim]"
    console.print(Panel.fit(f"{title.markup}\n{subtitle}", border_style="bright_green", padding=(0, 2)))


def render_action_line(console: Console, parsed: ParsedAction, res: BashResult) -> None:
    icons = {
        "write": ("✎ write", "bright_green"),
        "read": ("◉ read ", "bright_cyan"),
        "grep": ("⌕ grep ", "bright_magenta"),
        "find": ("⌕ find ", "bright_magenta"),
        "bash": ("⚡ bash ", "bright_yellow"),
    }
    label, color = icons.get(parsed.kind, ("⚡ bash ", "bright_yellow"))

    if res.cancelled:
        status_str = "[yellow]⏹ cancelled[/yellow]"
    elif res.exit_code == 0:
        status_str = f"[green]✓ 0[/green]"
    else:
        status_str = f"[red]✗ {res.exit_code}[/red]"

    meta_parts: list[str] = []
    if parsed.detail:
        meta_parts.append(parsed.detail)
    elif res.lines > 0 and parsed.kind in ("read", "grep", "find"):
        meta_parts.append(f"{res.lines}L")
    meta_parts.append(f"{res.elapsed_ms}ms")
    meta = " · ".join(meta_parts)

    console.print(
        f"[{color}]{label}[/{color}]  [bright_white]{parsed.target}[/bright_white]  "
        f"[dim]({meta})[/dim]  {status_str}"
    )


def run_onboarding(console: Console, cfg: dict[str, Any]) -> dict[str, Any]:
    console.print("\n[bold bright_green]Add Custom OpenAI-Compatible Provider[/bold bright_green]")
    console.print("[dim]Enter endpoint, key, and name — models sync automatically.[/dim]\n")

    session = PromptSession()

    try:
        while True:
            raw_url = session.prompt(
                HTML("<b><ansigreen>1/3 API URL</ansigreen></b> <ansibrightblack>(e.g. https://api.openai.com/v1)</ansibrightblack>: ")
            ).strip()
            if raw_url:
                base_url = normalize_base_url(raw_url)
                break
            console.print("[red]API URL is required.[/red]")

        api_key = session.prompt(
            HTML("<b><ansigreen>2/3 API Key</ansigreen></b> <ansibrightblack>(hidden, Enter if local)</ansibrightblack>: "),
            is_password=True,
        ).strip()

        suggested = default_provider_name(base_url)
        name = (
            session.prompt(
                HTML(f"<b><ansigreen>3/3 Provider Name</ansigreen></b> <ansibrightblack>[{suggested}]</ansibrightblack>: ")
            ).strip()
            or suggested
        )
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]Setup cancelled.[/dim]")
        raise SystemExit(0)

    models: list[str] = []
    sync_error: str | None = None

    with Progress(
        SpinnerColumn("dots", style="bright_green"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=24, complete_style="bright_green", finished_style="green"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"Syncing models from {name}...", total=100)
        progress.update(task, advance=35)
        try:
            models = fetch_models(base_url, api_key)
            progress.update(task, advance=65)
        except Exception as e:
            sync_error = str(e)
            progress.update(task, advance=65)

    if sync_error:
        console.print(f"[yellow]⚠ Could not fetch /models:[/yellow] [dim]{sync_error}[/dim]")
        manual = session.prompt(HTML("<b><ansigreen>Fallback Model ID</ansigreen></b> (e.g. gpt-4o-mini): ")).strip()
        models = [manual or "gpt-4o-mini"]
    elif not models:
        console.print("[yellow]⚠ Endpoint returned 0 models.[/yellow]")
        manual = session.prompt(HTML("<b><ansigreen>Model ID</ansigreen></b>: ")).strip()
        models = [manual or "default"]

    active_model = models[0]
    cfg.setdefault("providers", {})[name] = {
        "name": name,
        "base_url": base_url,
        "api_key": api_key,
        "models": models,
        "active_model": active_model,
    }
    cfg["active_provider"] = name
    save_config(cfg)

    console.print(
        f"[green]✓[/green] Saved [bold]{name}[/bold] · Synced [bold]{len(models)}[/bold] model(s) · Active: [bright_green]{active_model}[/bright_green]\n"
    )
    return cfg["providers"][name]


def pick_model(models: list[str], current_model: str) -> str | None:
    if not models:
        return None

    state = {
        "query": "",
        "idx": models.index(current_model) if current_model in models else 0,
        "selected": None,
    }

    def filtered() -> list[str]:
        q = state["query"].lower()
        if not q:
            return models
        return [m for m in models if q in m.lower()]

    def get_text():
        items = filtered()
        if items:
            state["idx"] = max(0, min(state["idx"], len(items) - 1))
        else:
            state["idx"] = 0

        lines: list[tuple[str, str]] = [
            ("class:header", f" Select model ({len(items)}/{len(models)})  filter: {state['query'] or 'type to filter…'}\n"),
            ("class:hint", " ↑/↓ move · Enter select · Esc cancel\n"),
        ]
        max_rows = 12
        start = max(0, state["idx"] - max_rows // 2)
        window = items[start : start + max_rows]
        if not window:
            lines.append(("class:dim", "   (no matching models)\n"))
        for i, m in enumerate(window):
            real_i = start + i
            cur = "● " if m == current_model else "  "
            if real_i == state["idx"]:
                lines.append(("class:selected", f" ▸ {cur}{m}\n"))
            else:
                lines.append(("class:item", f"   {cur}{m}\n"))
        return lines

    kb = KeyBindings()

    @kb.add("up")
    def _(event):
        state["idx"] = max(0, state["idx"] - 1)

    @kb.add("down")
    def _(event):
        items = filtered()
        if items:
            state["idx"] = min(len(items) - 1, state["idx"] + 1)

    @kb.add("enter")
    def _(event):
        items = filtered()
        if items:
            state["selected"] = items[state["idx"]]
        event.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        state["selected"] = None
        event.app.exit()

    @kb.add("backspace")
    def _(event):
        state["query"] = state["query"][:-1]
        state["idx"] = 0

    @kb.add("<any>")
    def _(event):
        ch = event.data
        if ch and ch.isprintable():
            state["query"] += ch
            state["idx"] = 0

    style = Style.from_dict(
        {
            "header": "bold ansigreen",
            "hint": "ansibrightblack",
            "selected": "bold reverse ansigreen",
            "item": "ansiwhite",
            "dim": "ansibrightblack",
        }
    )

    app = Application(
        layout=Layout(HSplit([Window(FormattedTextControl(get_text), always_hide_cursor=True)])),
        key_bindings=kb,
        style=style,
        full_screen=False,
    )
    app.run()
    return state["selected"]
