from __future__ import annotations

import os
import re
import select
import shutil
import sys
import textwrap
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .config import default_provider_name, fetch_models, normalize_base_url, save_config
from .pricing import format_cost
from .shell import BashResult, ParsedAction

# ANSI 256-color palette (Catppuccin Mocha inspired, zero neon/glow)
R = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CYAN = "\x1b[38;5;117m"
GREEN = "\x1b[38;5;114m"
RED = "\x1b[38;5;203m"
YELLOW = "\x1b[38;5;221m"
MAGENTA = "\x1b[38;5;183m"
BLUE = "\x1b[38;5;111m"
WHITE = "\x1b[38;5;255m"
GRAY = "\x1b[38;5;244m"
DARK = "\x1b[38;5;238m"
BG_SEL = "\x1b[48;5;24m"
BG_BAR = "\x1b[48;5;235m"

_ANSI_STRIP_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

SLASH_COMMANDS: list[tuple[str, str]] = [
    ("/model", "Switch model"),
    ("/new", "New chat (reset context)"),
    ("/provider", "Reconfigure provider"),
    ("/update", "Update shx from GitHub"),
    ("/exit", "Quit shx"),
]

SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
PULSE_BARS = ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█", "▇", "▆", "▅", "▄", "▃", "▂")


def _strip_ansi(s: str) -> str:
    return _ANSI_STRIP_RE.sub("", s)


def _char_w(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _vis_len(s: str) -> int:
    return sum(_char_w(c) for c in _strip_ansi(s))


def fit(text: str, max_len: int) -> str:
    if max_len <= 3:
        return text[:max_len]
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def fmt_ms(ms: int) -> str:
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms"


@dataclass
class ChatEntry:
    kind: str  # "user" | "assistant" | "action" | "stats" | "notice" | "warn"
    text: str = ""
    parsed: ParsedAction | None = None
    bash_res: BashResult | None = None
    meta: str = ""


class TermIO:
    """Cross-platform non-blocking raw terminal reader & alternate-screen manager."""

    def __init__(self) -> None:
        self._fd: int | None = None
        self._old_term: Any = None
        self._buf = bytearray()

    def __enter__(self) -> "TermIO":
        if os.name == "posix" and sys.stdin.isatty():
            import termios

            self._fd = sys.stdin.fileno()
            self._old_term = termios.tcgetattr(self._fd)
            new = termios.tcgetattr(self._fd)
            new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
            new[0] &= ~(termios.IXON | termios.ICRNL)
            new[6][termios.VMIN] = 0
            new[6][termios.VTIME] = 0
            termios.tcsetattr(self._fd, termios.TCSADRAIN, new)
        elif os.name == "nt":
            try:
                import ctypes

                k32 = ctypes.windll.kernel32
                h = k32.GetStdHandle(-11)
                mode = ctypes.c_ulong()
                k32.GetConsoleMode(h, ctypes.byref(mode))
                k32.SetConsoleMode(h, mode.value | 0x0004)
            except Exception:
                pass
        sys.stdout.write("\x1b[?1049h\x1b[?25h")
        sys.stdout.flush()
        return self

    def __exit__(self, *args: Any) -> None:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
        if os.name == "posix" and self._fd is not None and self._old_term is not None:
            try:
                import termios

                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_term)
            except Exception:
                pass

    def size(self) -> tuple[int, int]:
        sz = shutil.get_terminal_size((80, 24))
        return max(30, sz.columns), max(12, sz.lines)

    def read_key(self, timeout: float = 0.05) -> str | None:
        if os.name == "nt":
            import msvcrt

            deadline = time.perf_counter() + timeout
            while time.perf_counter() < deadline:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        ch2 = msvcrt.getwch()
                        return {"H": "UP", "P": "DOWN", "K": "LEFT", "M": "RIGHT", "G": "HOME", "O": "END", "I": "PGUP", "Q": "PGDN", "S": "DEL"}.get(ch2, "")
                    if ch == "\r":
                        return "ENTER"
                    if ch == "\x08":
                        return "BACKSPACE"
                    if ch == "\x1b":
                        return "ESC"
                    if ch == "\x03":
                        return "CTRL_C"
                    if ch == "\x04":
                        return "CTRL_D"
                    if ch == "\t":
                        return "TAB"
                    return ch
                time.sleep(0.015)
            return None

        if self._fd is None:
            time.sleep(timeout)
            return None

        if not self._buf:
            r, _, _ = select.select([self._fd], [], [], timeout)
            if not r:
                return None
            chunk = os.read(self._fd, 64)
            if not chunk:
                return None
            self._buf.extend(chunk)

        b0 = self._buf[0]
        if b0 == 0x1B:
            if len(self._buf) == 1:
                r, _, _ = select.select([self._fd], [], [], 0.025)
                if r:
                    self._buf.extend(os.read(self._fd, 32))
            if len(self._buf) == 1:
                del self._buf[0]
                return "ESC"
            seq = bytes(self._buf[:6])
            for prefix, name in (
                (b"\x1b[A", "UP"),
                (b"\x1b[B", "DOWN"),
                (b"\x1b[C", "RIGHT"),
                (b"\x1b[D", "LEFT"),
                (b"\x1b[H", "HOME"),
                (b"\x1b[F", "END"),
                (b"\x1b[3~", "DEL"),
                (b"\x1b[5~", "PGUP"),
                (b"\x1b[6~", "PGDN"),
                (b"\x1bOH", "HOME"),
                (b"\x1bOF", "END"),
            ):
                if seq.startswith(prefix):
                    del self._buf[: len(prefix)]
                    return name
            del self._buf[0]
            return "ESC"

        if b0 in (0x0D, 0x0A):
            del self._buf[0]
            return "ENTER"
        if b0 in (0x7F, 0x08):
            del self._buf[0]
            return "BACKSPACE"
        if b0 == 0x03:
            del self._buf[0]
            return "CTRL_C"
        if b0 == 0x04:
            del self._buf[0]
            return "CTRL_D"
        if b0 == 0x09:
            del self._buf[0]
            return "TAB"
        if b0 < 0x20:
            del self._buf[0]
            return ""

        need = 1
        if b0 >= 0xF0:
            need = 4
        elif b0 >= 0xE0:
            need = 3
        elif b0 >= 0xC0:
            need = 2
        while len(self._buf) < need:
            r, _, _ = select.select([self._fd], [], [], 0.02)
            if not r:
                break
            self._buf.extend(os.read(self._fd, 16))
        raw = bytes(self._buf[:need])
        del self._buf[:need]
        return raw.decode("utf-8", errors="ignore")


@dataclass
class TUIState:
    provider_name: str
    model: str
    models: list[str] = field(default_factory=list)
    update_ver: str | None = None
    entries: list[ChatEntry] = field(default_factory=list)
    streaming_text: str = ""
    busy: bool = False
    busy_label: str = ""
    busy_started_at: float = 0.0
    last_cache_pct: int | None = None
    session_cost: float = 0.0
    input_text: str = ""
    cursor: int = 0
    scroll_offset: int = 0
    slash_idx: int = 0
    model_picker_open: bool = False
    model_query: str = ""
    model_idx: int = 0
    tick: int = 0


def _render_entries(
    entries: list[ChatEntry],
    streaming_text: str,
    busy: bool,
    busy_label: str,
    busy_elapsed: float,
    tick: int,
    w: int,
) -> list[str]:
    lines: list[str] = []
    wrap_w = max(20, w - 5)

    for e in entries:
        if e.kind == "user":
            wrapped = textwrap.wrap(e.text, width=wrap_w) or [""]
            for i, ln in enumerate(wrapped):
                prefix = f"{CYAN}{BOLD}❯{R} " if i == 0 else "  "
                lines.append(f"{prefix}{WHITE}{BOLD}{ln}{R}")
            lines.append("")

        elif e.kind == "action" and e.parsed and e.bash_res:
            p, r = e.parsed, e.bash_res
            colors = {"write": GREEN, "read": CYAN, "grep": MAGENTA, "find": MAGENTA, "bash": BLUE}
            c = colors.get(p.kind, BLUE)
            if r.cancelled:
                dot, tag = f"{YELLOW}||{R}", f" {YELLOW}прервано{R}"
            elif r.exit_code == 0:
                dot, tag = f"{GREEN}✓{R}", ""
            else:
                dot, tag = f"{RED}✗{R}", f" {RED}exit {r.exit_code}{R}"
            extra = p.detail + " · " if p.detail else (f"{r.lines}L · " if r.lines > 0 and p.kind in ("read", "grep", "find") else "")
            meta = f"{extra}{fmt_ms(r.elapsed_ms)}"
            lines.append(f"{DARK}│{R} {dot} {BOLD}{c}{p.kind}{R}{tag} {GRAY}· {meta}{R}")
            lines.append(f"{DARK}│{R}   {GRAY}{fit(p.target, max(14, w - 6))}{R}")
            if r.exit_code != 0 and not r.cancelled and r.output and r.output != "(no output)":
                first_err = next((ln.strip() for ln in r.output.splitlines() if ln.strip()), "")
                if first_err:
                    lines.append(f"{DARK}│{R}   {RED}↳ {fit(first_err, max(14, w - 8))}{R}")

        elif e.kind == "assistant":
            for raw_ln in e.text.splitlines():
                wrapped = textwrap.wrap(raw_ln, width=wrap_w) or [""]
                for ln in wrapped:
                    lines.append(f"  {WHITE}{ln}{R}")

        elif e.kind == "stats":
            lines.append(f"{DARK}╰─{R} {GRAY}{fit(e.text, max(20, w - 5))}{R}")
            lines.append("")

        elif e.kind == "warn":
            lines.append(f"{DARK}│{R} {YELLOW}{fit(e.text, max(16, w - 4))}{R}")

        elif e.kind == "notice":
            lines.append(f"  {GREEN}✓{R} {GRAY}{fit(e.text, max(16, w - 5))}{R}")
            lines.append("")

    if streaming_text:
        for raw_ln in streaming_text.splitlines():
            wrapped = textwrap.wrap(raw_ln, width=wrap_w) or [""]
            for ln in wrapped:
                lines.append(f"  {WHITE}{ln}{R}")
        if busy:
            lines.append(f"  {CYAN}▊{R}")

    if busy and not streaming_text:
        sp = SPINNER_FRAMES[tick % len(SPINNER_FRAMES)]
        pulse = PULSE_BARS[tick % len(PULSE_BARS)]
        lbl = fit(busy_label or "Thinking…", max(14, w - 16))
        lines.append(f"{DARK}│{R} {CYAN}{sp}{R} {WHITE}{lbl}{R} {GRAY}{pulse} {busy_elapsed:.1f}s{R}")

    return lines


def draw_frame(state: TUIState, term: TermIO) -> None:
    w, h = term.size()
    out: list[str] = ["\x1b[?2026h\x1b[?25l\x1b[H"]

    # ── 1. Top Header Bar (2 rows) ──
    short_model = state.model.split("/")[-1] if w < 70 and "/" in state.model else state.model
    if state.busy:
        sp = SPINNER_FRAMES[state.tick % len(SPINNER_FRAMES)]
        status_pill = f"{CYAN}{sp} busy{R}"
    else:
        status_pill = f"{GREEN}● ready{R}"

    right_parts: list[str] = []
    if state.last_cache_pct is not None:
        right_parts.append(f"cache {state.last_cache_pct}%")
    if state.session_cost > 0:
        right_parts.append(f"{GREEN}{format_cost(state.session_cost)}{GRAY}")
    if state.update_ver:
        right_parts.append(f"{YELLOW}↑v{state.update_ver}{GRAY}")
    right_str = " · ".join(right_parts)

    left_head = f"{BOLD}{CYAN}>_ shx{R} {GRAY}v{__version__}{R}  {status_pill}  {WHITE}{fit(short_model, max(10, w - 34))}{R}"
    pad_hdr = max(1, w - _vis_len(left_head) - _vis_len(right_str) - 1)
    row1 = f"{BG_BAR} {left_head}{' ' * pad_hdr}{GRAY}{right_str} {R}"
    if _vis_len(row1) > w:
        row1 = f"{BG_BAR} {left_head}" + " " * max(0, w - _vis_len(left_head) - 1) + R

    out.append(f"\x1b[1;1H\x1b[2K{row1}")
    out.append(f"\x1b[2;1H\x1b[2K{DARK}{'─' * w}{R}")

    # ── 2. Overlays (Slash Menu or Model Picker) ──
    overlay_rows: list[str] = []
    if state.model_picker_open:
        q = state.model_query.lower()
        items = [m for m in state.models if q in m.lower()] if q else state.models
        if items:
            state.model_idx = max(0, min(state.model_idx, len(items) - 1))
        else:
            state.model_idx = 0
        box_w = max(28, min(w - 2, 68))
        inner = box_w - 4
        hdr = fit(f"Model ({len(items)}/{len(state.models)}) · filter: {state.model_query or 'type…'}", inner)
        overlay_rows.append(f"{CYAN}╭─ {BOLD}{hdr}{R}{CYAN} {'─' * max(1, box_w - len(hdr) - 5)}╮{R}")
        max_show = min(8, max(3, h - 10))
        start = max(0, state.model_idx - max_show // 2)
        window = items[start : start + max_show]
        if not window:
            overlay_rows.append(f"{CYAN}│{R} {GRAY}{fit('(no match)', inner):<{inner}}{R} {CYAN}│{R}")
        for i, m in enumerate(window):
            real_i = start + i
            mark = "● " if m == state.model else "  "
            txt = fit(mark + m, inner)
            pad = " " * max(0, inner - len(txt))
            if real_i == state.model_idx:
                overlay_rows.append(f"{CYAN}│{R} {BG_SEL}{BOLD}{WHITE}{txt}{pad}{R} {CYAN}│{R}")
            else:
                overlay_rows.append(f"{CYAN}│{R} {WHITE}{txt}{pad}{R} {CYAN}│{R}")
        overlay_rows.append(f"{CYAN}╰{'─' * (box_w - 2)}╯{R}")

    elif state.input_text.startswith("/") and " " not in state.input_text:
        pref = state.input_text.lower()
        matches = [(c, d) for c, d in SLASH_COMMANDS if c.startswith(pref)]
        if matches:
            state.slash_idx = max(0, min(state.slash_idx, len(matches) - 1))
            box_w = max(28, min(w - 2, 54))
            inner = box_w - 4
            overlay_rows.append(f"{DARK}╭{'─' * (box_w - 2)}╮{R}")
            for i, (cmd, desc) in enumerate(matches):
                row_txt = fit(f"{cmd:<10} {desc}", inner)
                pad = " " * max(0, inner - len(row_txt))
                if i == state.slash_idx:
                    overlay_rows.append(f"{DARK}│{R} {BG_SEL}{BOLD}{CYAN}{row_txt}{pad}{R} {DARK}│{R}")
                else:
                    overlay_rows.append(f"{DARK}│{R} {WHITE}{row_txt}{pad}{R} {DARK}│{R}")
            overlay_rows.append(f"{DARK}╰{'─' * (box_w - 2)}╯{R}")

    # ── 3. Chat Viewport ──
    input_rows = 3
    chat_top = 3
    chat_bottom = h - input_rows - len(overlay_rows)
    chat_h = max(1, chat_bottom - chat_top + 1)

    elapsed = (time.perf_counter() - state.busy_started_at) if state.busy else 0.0
    all_lines = _render_entries(
        state.entries,
        state.streaming_text,
        state.busy,
        state.busy_label,
        elapsed,
        state.tick,
        w,
    )

    max_scroll = max(0, len(all_lines) - chat_h)
    state.scroll_offset = max(0, min(state.scroll_offset, max_scroll))
    end_idx = len(all_lines) - state.scroll_offset
    start_idx = max(0, end_idx - chat_h)
    visible = all_lines[start_idx:end_idx]

    if not all_lines:
        welcome = [
            "",
            f"  {CYAN}{BOLD}>_ shx v{__version__}{R}  {GRAY}bash-only agent · ~70-tok prefix{R}",
            f"  {GRAY}provider: {WHITE}{state.provider_name}{GRAY} · PgUp/PgDn scroll · / commands{R}",
        ]
        visible = welcome[:chat_h]

    for row_i in range(chat_h):
        screen_r = chat_top + row_i
        txt = visible[row_i] if row_i < len(visible) else ""
        if row_i == 0 and state.scroll_offset > 0:
            tag = f"{GRAY}↑ {state.scroll_offset}L{R}"
            out.append(f"\x1b[{screen_r};1H\x1b[2K{txt}")
            col = max(1, w - _vis_len(tag) - 1)
            out.append(f"\x1b[{screen_r};{col}H{tag}")
        else:
            out.append(f"\x1b[{screen_r};1H\x1b[2K{txt}")

    for idx_ov, ov_line in enumerate(overlay_rows):
        sr = chat_bottom + 1 + idx_ov
        out.append(f"\x1b[{sr};1H\x1b[2K{ov_line}")

    # ── 4. Bottom Persistent Input Box (3 rows) ──
    box_top = h - 2
    border_c = YELLOW if state.busy else CYAN
    hint = "Enter: прервать и дополнить · Esc: стоп" if state.busy else "/ команды · Enter: отправить"
    hint_fit = fit(hint, max(10, w - 6))
    top_border = f"{border_c}╭─ {GRAY}{hint_fit} {border_c}{'─' * max(1, w - len(hint_fit) - 5)}╮{R}"
    bot_border = f"{border_c}╰{'─' * (w - 2)}╯{R}"

    inner_w = max(8, w - 6)
    inp = state.input_text
    cur = max(0, min(state.cursor, len(inp)))

    view_start = 0
    if cur > inner_w - 1:
        view_start = cur - (inner_w - 1)
    shown = inp[view_start : view_start + inner_w]
    if not inp:
        ph = "Дополнить задачу…" if state.busy else "Напиши задачу или /…"
        body = f"{GRAY}{fit(ph, inner_w)}{R}"
        pad = " " * max(0, inner_w - len(fit(ph, inner_w)))
    else:
        body = f"{WHITE}{shown}{R}"
        pad = " " * max(0, inner_w - _vis_len(shown))

    prompt_sym = f"{YELLOW}↳{R}" if state.busy else f"{CYAN}❯{R}"
    mid_row = f"{border_c}│{R} {prompt_sym} {body}{pad} {border_c}│{R}"

    out.append(f"\x1b[{box_top};1H\x1b[2K{top_border}")
    out.append(f"\x1b[{box_top + 1};1H\x1b[2K{mid_row}")
    out.append(f"\x1b[{box_top + 2};1H\x1b[2K{bot_border}")

    cur_col = 5 + _vis_len(inp[view_start:cur])
    cur_col = max(5, min(w - 2, cur_col))
    out.append(f"\x1b[{box_top + 1};{cur_col}H\x1b[?25h\x1b[?2026l")

    sys.stdout.write("".join(out))
    sys.stdout.flush()


def run_onboarding(cfg: dict[str, Any]) -> dict[str, Any]:
    """Simple pre-TUI 3-step custom OpenAI-compatible provider setup."""
    print(f"\n{BOLD}{CYAN}>_ shx v{__version__}{R} {GRAY}— custom provider setup{R}\n")
    try:
        while True:
            base_url = input(f"{CYAN}1/3 API URL{R} {GRAY}(e.g. https://api.openai.com/v1){R}: ").strip()
            if base_url:
                base_url = normalize_base_url(base_url)
                break
            print(f"{RED}API URL is required.{R}")

        import getpass

        api_key = getpass.getpass(f"{CYAN}2/3 API Key{R} {GRAY}(hidden, Enter if local){R}: ").strip()
        suggested = default_provider_name(base_url)
        name = input(f"{CYAN}3/3 Provider Name{R} {GRAY}[{suggested}]{R}: ").strip() or suggested
    except (EOFError, KeyboardInterrupt):
        print(f"\n{GRAY}Setup cancelled.{R}")
        raise SystemExit(0)

    sys.stdout.write(f"{CYAN}⠋{R} {GRAY}Syncing models from {name}...{R}")
    sys.stdout.flush()
    models: list[str] = []
    sync_error: str | None = None
    try:
        models = fetch_models(base_url, api_key)
    except Exception as e:
        sync_error = str(e)
    sys.stdout.write("\r\x1b[2K")
    sys.stdout.flush()

    if sync_error:
        print(f"{YELLOW}! Could not fetch /models:{R} {GRAY}{sync_error}{R}")
        manual = input(f"{CYAN}Fallback Model ID{R} (e.g. gpt-4o-mini): ").strip()
        models = [manual or "gpt-4o-mini"]
    elif not models:
        print(f"{YELLOW}! Endpoint returned 0 models.{R}")
        manual = input(f"{CYAN}Model ID{R}: ").strip()
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
