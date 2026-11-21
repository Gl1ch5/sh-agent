from __future__ import annotations

import os
import select
import shutil
import signal
import sys
import threading
import time
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

from . import __version__
from .config import default_provider_name, fetch_models, normalize_base_url, save_config
from .pricing import format_cost
from .shell import BashResult, ParsedAction

# Custom ANSI 256/TrueColor Palette (Catppuccin-inspired)
R = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CYAN = "\x1b[38;5;117m"
TEAL = "\x1b[38;5;80m"
GREEN = "\x1b[38;5;114m"
RED = "\x1b[38;5;203m"
YELLOW = "\x1b[38;5;221m"
MAGENTA = "\x1b[38;5;183m"
BLUE = "\x1b[38;5;111m"
WHITE = "\x1b[38;5;255m"
GRAY = "\x1b[38;5;243m"
DARK = "\x1b[38;5;238m"

SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/model", "Switch model from synced list"),
    ("/new", "New chat (reset context)"),
    ("/provider", "Add / reconfigure custom provider"),
    ("/update", "Check & install latest version"),
    ("/exit", "Exit shx"),
]

PROMPT_STYLE = Style.from_dict(
    {
        "completion-menu.completion": "bg:#181825 #cdd6f4",
        "completion-menu.completion.current": "bg:#89dceb #11111b bold",
        "completion-menu.meta.completion": "bg:#181825 #6c7086",
        "completion-menu.meta.completion.current": "bg:#89dceb #11111b",
        "scrollbar.background": "bg:#181825",
        "scrollbar.button": "bg:#45475a",
    }
)


def term_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def fit(text: str, max_len: int) -> str:
    if max_len <= 4:
        return text[:max_len]
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def fmt_ms(ms: int) -> str:
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


class Spinner:
    """Custom non-wrapping single-line braille spinner."""

    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, text: str, color: str = CYAN) -> None:
        self.text = text
        self.color = color
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active = False

    def __enter__(self) -> "Spinner":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()

    def start(self) -> None:
        if self._active or not sys.stdout.isatty():
            return
        self._active = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            w = term_width()
            msg = fit(self.text, max(10, w - 5))
            frame = self.FRAMES[i % len(self.FRAMES)]
            sys.stdout.write(f"\r\x1b[2K{self.color}{frame}{R} {GRAY}{msg}{R}")
            sys.stdout.flush()
            i += 1
            time.sleep(0.07)

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.25)
        if sys.stdout.isatty():
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()


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


def print_banner(
    provider_name: str | None = None,
    model: str | None = None,
    update_ver: str | None = None,
) -> None:
    w = term_width()
    box_w = max(32, min(w - 1, 62))
    inner = box_w - 4

    title_plain = f"⚡ shx v{__version__}"
    dash_count = max(1, box_w - len(title_plain) - 5)
    top = f"{DARK}╭─{R} {BOLD}{CYAN}⚡ shx{R} {GRAY}v{__version__}{R} {DARK}{'─' * dash_count}╮{R}"
    bot = f"{DARK}╰{'─' * (box_w - 2)}╯{R}"

    lines = [top]
    if provider_name and model:
        short_model = model.split("/")[-1] if w < 64 and "/" in model else model
        row1 = fit(f"{short_model} · {provider_name}", inner)
        pad1 = " " * max(0, inner - len(row1))
        lines.append(f"{DARK}│{R} {WHITE}{row1}{R}{pad1} {DARK}│{R}")

        cwd_name = Path.cwd().name or str(Path.cwd())
        if update_ver:
            row2_txt = fit(f"↑ v{update_ver} available (/update) · {cwd_name}", inner)
            c2 = YELLOW
        else:
            row2_txt = fit(f"{cwd_name} · / cmds · Esc/Ctrl+C стоп", inner)
            c2 = GRAY
        pad2 = " " * max(0, inner - len(row2_txt))
        lines.append(f"{DARK}│{R} {c2}{row2_txt}{R}{pad2} {DARK}│{R}")
    else:
        row1 = fit("bash-only agent · first-time setup", inner)
        pad1 = " " * max(0, inner - len(row1))
        lines.append(f"{DARK}│{R} {GRAY}{row1}{R}{pad1} {DARK}│{R}")
    lines.append(bot)

    print("\n" + "\n".join(lines))


def render_action(parsed: ParsedAction, res: BashResult) -> None:
    w = term_width()
    colors = {
        "write": GREEN,
        "read": CYAN,
        "grep": MAGENTA,
        "find": MAGENTA,
        "bash": BLUE,
    }
    color = colors.get(parsed.kind, BLUE)

    if res.cancelled:
        dot, exit_tag = f"{YELLOW}⏸{R}", f" {YELLOW}прервано{R}"
    elif res.exit_code == 0:
        dot, exit_tag = f"{GREEN}✓{R}", ""
    else:
        dot, exit_tag = f"{RED}✗{R}", f" {RED}exit {res.exit_code}{R}"

    meta_parts: list[str] = []
    if parsed.detail:
        meta_parts.append(parsed.detail)
    elif res.lines > 0 and parsed.kind in ("read", "grep", "find"):
        meta_parts.append(f"{res.lines}L")
    meta_parts.append(fmt_ms(res.elapsed_ms))
    meta = " · ".join(meta_parts)

    target_line = fit(parsed.target, max(16, w - 6))
    print(f"{DARK}│{R} {dot} {BOLD}{color}{parsed.kind}{R}{exit_tag} {GRAY}· {meta}{R}")
    print(f"{DARK}│{R}   {GRAY}{target_line}{R}")

    if res.exit_code != 0 and not res.cancelled and res.output and res.output != "(no output)":
        first_err = next((ln.strip() for ln in res.output.splitlines() if ln.strip()), "")
        if first_err:
            err_line = fit(first_err, max(16, w - 8))
            print(f"{DARK}│{R}   {RED}↳ {err_line}{R}")


def render_stats(
    ttft_ms: int,
    prompt_tok: int,
    cached_tok: int,
    comp_tok: int,
    turn_cost: float,
    session_cost: float,
) -> None:
    parts = [f"{ttft_ms}ms"]
    if prompt_tok > 0:
        pct = int(round((cached_tok / prompt_tok) * 100)) if cached_tok > 0 else 0
        parts.append(f"{prompt_tok:,} in ({pct}% cached)")
    if comp_tok > 0:
        parts.append(f"{comp_tok:,} out")
    if turn_cost > 0 or session_cost > 0:
        parts.append(f"{GREEN}{format_cost(turn_cost)}{GRAY} (Σ {format_cost(session_cost)})")
    line = fit(" · ".join(parts), max(20, term_width() - 6))
    print(f"{DARK}╰─{R} {GRAY}{line}{R}\n")


def run_onboarding(cfg: dict[str, Any]) -> dict[str, Any]:
    print(f"\n{BOLD}{CYAN}Add Custom OpenAI-Compatible Provider{R}")
    print(f"{GRAY}Enter endpoint, key, and name — models sync automatically.{R}\n")

    session = PromptSession()

    try:
        while True:
            raw_url = session.prompt(
                HTML("<b><ansicyan>1/3 API URL</ansicyan></b> <ansibrightblack>(e.g. https://api.openai.com/v1)</ansibrightblack>: ")
            ).strip()
            if raw_url:
                base_url = normalize_base_url(raw_url)
                break
            print(f"{RED}API URL is required.{R}")

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
        print(f"\n{GRAY}Setup cancelled.{R}")
        raise SystemExit(0)

    models: list[str] = []
    sync_error: str | None = None

    with Spinner(f"Syncing models from {name}...", CYAN):
        try:
            models = fetch_models(base_url, api_key)
        except Exception as e:
            sync_error = str(e)

    if sync_error:
        print(f"{YELLOW}⚠ Could not fetch /models:{R} {GRAY}{sync_error}{R}")
        manual = session.prompt(HTML("<b><ansicyan>Fallback Model ID</ansicyan></b> (e.g. gpt-4o-mini): ")).strip()
        models = [manual or "gpt-4o-mini"]
    elif not models:
        print(f"{YELLOW}⚠ Endpoint returned 0 models.{R}")
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

    print(f"{GREEN}✓{R} {BOLD}{name}{R} · {len(models)} model(s) · Active: {CYAN}{active_model}{R}\n")
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
        return [m for m in models if q in m.lower()] if q else models

    def get_text():
        items = filtered()
        state["idx"] = max(0, min(state["idx"], len(items) - 1)) if items else 0
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
