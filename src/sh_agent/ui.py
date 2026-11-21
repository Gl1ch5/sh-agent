from __future__ import annotations

import os
import select
import signal
import sys
import threading
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
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.rule import Rule

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
        "completion-menu.completion.current": "bg:#89b4fa #11111b bold",
        "completion-menu.meta.completion": "bg:#181825 #6c7086",
        "completion-menu.meta.completion.current": "bg:#89b4fa #11111b",
        "scrollbar.background": "bg:#181825",
        "scrollbar.button": "bg:#45475a",
    }
)


class EscInterruptWatcher:
    """Watches for bare Esc on POSIX TTY and raises SIGINT for instant cancellation."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._old_term: Any = None

    def __enter__(self) -> "EscInterruptWatcher":
        if os.name == "posix" and sys.stdin.isatty():
            try:
                import termios
                import tty

                self._fd = sys.stdin.fileno()
                self._old_term = termios.tcgetattr(self._fd)
                tty.setcbreak(self._fd)
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
            except Exception:
                self._fd = None
                self._old_term = None
        return self

    def _run(self) -> None:
        assert self._fd is not None
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self._fd], [], [], 0.08)
                if not r:
                    continue
                ch = os.read(self._fd, 1)
                if ch == b"\x1b":
                    r2, _, _ = select.select([self._fd], [], [], 0.03)
                    if r2:
                        os.read(self._fd, 16)
                        continue
                    os.kill(os.getpid(), signal.SIGINT)
                    return
            except Exception:
                return

    def __exit__(self, *args: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)
        if self._fd is not None and self._old_term is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_term)
            except Exception:
                pass


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


def _fit(text: str, max_len: int) -> str:
    if max_len <= 4:
        return text[:max_len]
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _fmt_ms(ms: int) -> str:
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms}ms"


def print_banner(console: Console, provider_name: str | None = None, model: str | None = None) -> None:
    w = console.width
    cwd_name = Path.cwd().name or str(Path.cwd())
    console.print()
    if provider_name and model:
        short_model = model.split("/")[-1] if w < 64 and "/" in model else model
        head = _fit(f"{short_model} · {provider_name}", max(18, w - 10))
        sub = _fit(f"{cwd_name} · / cmds · Esc/Ctrl+C стоп", max(18, w - 4))
        console.print(f"[bold bright_cyan]❯_ shx[/bold bright_cyan]  [bright_white]{head}[/bright_white]")
        console.print(f"[dim]   {sub}[/dim]")
    else:
        console.print("[bold bright_cyan]❯_ shx[/bold bright_cyan]  [dim]bash agent · setup[/dim]")
    console.print(Rule(style="dim cyan"))


def render_action_line(console: Console, parsed: ParsedAction, res: BashResult) -> None:
    w = console.width
    icons = {
        "write": ("write", "bright_green"),
        "read": ("read", "bright_cyan"),
        "grep": ("grep", "bright_magenta"),
        "find": ("find", "bright_magenta"),
        "bash": ("bash", "bright_blue"),
    }
    label, color = icons.get(parsed.kind, ("bash", "bright_blue"))

    if res.cancelled:
        dot = "[yellow]⏸[/yellow]"
        exit_tag = " [yellow]прервано[/yellow]"
    elif res.exit_code == 0:
        dot = "[bright_green]●[/bright_green]"
        exit_tag = ""
    else:
        dot = "[red]●[/red]"
        exit_tag = f" [red]exit {res.exit_code}[/red]"

    meta_parts: list[str] = []
    if parsed.detail:
        meta_parts.append(parsed.detail)
    elif res.lines > 0 and parsed.kind in ("read", "grep", "find"):
        meta_parts.append(f"{res.lines}L")
    meta_parts.append(_fmt_ms(res.elapsed_ms))
    meta = " · ".join(meta_parts)

    target_line = _fit(parsed.target, max(16, w - 7))
    console.print(f"[dim]│[/dim] {dot} [{color}]{label}[/{color}]{exit_tag} [dim]· {meta}[/dim]")
    console.print(f"[dim]│   {target_line}[/dim]")

    if res.exit_code != 0 and not res.cancelled and res.output and res.output != "(no output)":
        first_err = next((ln.strip() for ln in res.output.splitlines() if ln.strip()), "")
        if first_err:
            err_line = _fit(first_err, max(16, w - 9))
            console.print(f"[dim]│[/dim]   [red]↳ {err_line}[/red]")


def run_onboarding(console: Console, cfg: dict[str, Any]) -> dict[str, Any]:
    console.print("\n[bold bright_cyan]Add Custom OpenAI-Compatible Provider[/bold bright_cyan]")
    console.print("[dim]Enter endpoint, key, and name — models sync automatically.[/dim]\n")

    session = PromptSession()

    try:
        while True:
            raw_url = session.prompt(
                HTML("<b><ansicyan>1/3 API URL</ansicyan></b> <ansibrightblack>(e.g. https://api.openai.com/v1)</ansibrightblack>: ")
            ).strip()
            if raw_url:
                base_url = normalize_base_url(raw_url)
                break
            console.print("[red]API URL is required.[/red]")

        api_key = session.prompt(
            HTML("<b><ansicyan>2/3 API Key</ansicyan></b> <ansibrightblack>(hidden, Enter if local)</ansibrightblack>: "),
            is_password=True,
        ).strip()

        suggested = default_provider_name(base_url)
        name = (
            session.prompt(
                HTML(f"<b><ansicyan>3/3 Provider Name</ansicyan></b> <ansibrightblack>[{suggested}]</ansibrightblack>: ")
            ).strip()
            or suggested
        )
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]Setup cancelled.[/dim]")
        raise SystemExit(0)

    models: list[str] = []
    sync_error: str | None = None

    with Progress(
        SpinnerColumn("dots", style="bright_cyan"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=20, complete_style="bright_cyan", finished_style="green"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task(f"Syncing {name}...", total=100)
        progress.update(task, advance=35)
        try:
            models = fetch_models(base_url, api_key)
            progress.update(task, advance=65)
        except Exception as e:
            sync_error = str(e)
            progress.update(task, advance=65)

    if sync_error:
        console.print(f"[yellow]⚠ Could not fetch /models:[/yellow] [dim]{sync_error}[/dim]")
        manual = session.prompt(HTML("<b><ansicyan>Fallback Model ID</ansicyan></b> (e.g. gpt-4o-mini): ")).strip()
        models = [manual or "gpt-4o-mini"]
    elif not models:
        console.print("[yellow]⚠ Endpoint returned 0 models.[/yellow]")
        manual = session.prompt(HTML("<b><ansicyan>Model ID</ansicyan></b>: ")).strip()
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
        f"[green]✓[/green] [bold]{name}[/bold] · {len(models)} model(s) · Active: [bright_cyan]{active_model}[/bright_cyan]\n"
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
            ("class:header", f" Model ({len(items)}/{len(models)})  filter: {state['query'] or 'type to filter…'}\n"),
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
            "header": "bold ansicyan",
            "hint": "ansibrightblack",
            "selected": "bold reverse ansicyan",
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
