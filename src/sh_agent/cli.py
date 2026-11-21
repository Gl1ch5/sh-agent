from __future__ import annotations

import json
import queue
import sys
import threading
import time
from typing import Any

import httpx

from . import __version__
from .client import stream_chat
from .config import get_active_provider, load_config, save_config
from .pricing import estimate_cost, format_cost
from .shell import SYSTEM_PROMPT, parse_bash_action, run_bash
from .tui import (
    CYAN,
    GREEN,
    RED,
    SLASH_COMMANDS,
    ChatEntry,
    R,
    TermIO,
    TUIState,
    draw_frame,
    run_onboarding,
)
from .updater import check_latest_version, self_update


class AgentWorker:
    """Runs one agent turn in a background thread with instant non-destructive cancel."""

    def __init__(
        self,
        provider: dict[str, Any],
        messages: list[dict[str, Any]],
        ev_q: queue.Queue[tuple[str, Any]],
    ) -> None:
        self.provider = provider
        self.messages = messages
        self.ev_q = ev_q
        self.cancel_event = threading.Event()
        self._active_resp: httpx.Response | None = None
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            if self._active_resp is not None:
                try:
                    self._active_resp.close()
                except Exception:
                    pass

    def join(self, timeout: float = 1.5) -> None:
        self._thread.join(timeout=timeout)

    def _set_resp(self, resp: httpx.Response | None) -> None:
        with self._lock:
            self._active_resp = resp

    def _run(self) -> None:
        model = self.provider["active_model"]
        base_url = self.provider["base_url"]
        api_key = self.provider.get("api_key", "")

        total_prompt = 0
        total_cached = 0
        total_comp = 0
        first_ttft = 0

        try:
            while not self.cancel_event.is_set():
                short_model = model.split("/")[-1] if "/" in model else model
                self.ev_q.put(("status", f"Thinking · {short_model}"))

                def on_token(delta: str) -> None:
                    self.ev_q.put(("token", delta))

                try:
                    res = stream_chat(
                        base_url=base_url,
                        api_key=api_key,
                        model=model,
                        messages=self.messages,
                        on_token=on_token,
                        cancel_event=self.cancel_event,
                        on_response=self._set_resp,
                    )
                finally:
                    self._set_resp(None)

                if res.cancelled or self.cancel_event.is_set():
                    if res.content.strip():
                        self.messages.append({"role": "assistant", "content": res.content})
                        self.ev_q.put(("flush_stream", res.content))
                    self.ev_q.put(("cancelled", None))
                    return

                if first_ttft == 0:
                    first_ttft = res.ttft_ms
                total_prompt += res.prompt_tokens
                total_cached += res.cached_tokens
                total_comp += res.completion_tokens

                if res.content:
                    self.ev_q.put(("flush_stream", res.content))

                assistant_msg: dict[str, Any] = {"role": "assistant", "content": res.content or None}
                if res.tool_calls:
                    assistant_msg["tool_calls"] = res.tool_calls
                self.messages.append(assistant_msg)

                if not res.tool_calls:
                    break

                for idx, tc in enumerate(res.tool_calls):
                    fn = tc.get("function") or {}
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = json.loads(raw_args) if raw_args.strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                    cmd = str(args.get("command", ""))
                    parsed = parse_bash_action(cmd)

                    self.ev_q.put(("status", f"{parsed.kind} · {parsed.target}"))
                    b_res = run_bash(cmd, cancel_event=self.cancel_event)
                    self.ev_q.put(("action", (parsed, b_res)))

                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id") or f"call_{idx}",
                            "content": f"[exit {b_res.exit_code}]\n{b_res.output}",
                        }
                    )

                    if b_res.cancelled or self.cancel_event.is_set():
                        for rem_idx, rem_tc in enumerate(res.tool_calls[idx + 1 :], start=idx + 1):
                            self.messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": rem_tc.get("id") or f"call_{rem_idx}",
                                    "content": "[interrupted by user]",
                                }
                            )
                        self.ev_q.put(("cancelled", None))
                        return

            turn_cost, _ = estimate_cost(model, total_prompt, total_cached, total_comp)
            cache_pct = int(round((total_cached / total_prompt) * 100)) if total_prompt > 0 and total_cached > 0 else None
            self.ev_q.put(("done", (first_ttft, total_prompt, total_cached, total_comp, turn_cost, cache_pct)))
        except Exception as e:
            if self.cancel_event.is_set():
                self.ev_q.put(("cancelled", None))
            else:
                self.ev_q.put(("error", str(e)))


def _run_oneshot(provider: dict[str, Any], prompt_text: str) -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt_text},
    ]
    ev_q: queue.Queue[tuple[str, Any]] = queue.Queue()
    worker = AgentWorker(provider, messages, ev_q)
    worker.start()
    try:
        while True:
            kind, val = ev_q.get()
            if kind == "token":
                sys.stdout.write(val)
                sys.stdout.flush()
            elif kind == "flush_stream":
                sys.stdout.write("\n")
                sys.stdout.flush()
            elif kind == "action":
                parsed, b_res = val
                print(f"[{parsed.kind}] {parsed.target} (exit {b_res.exit_code}, {b_res.elapsed_ms}ms)")
            elif kind == "done":
                ttft, p_tok, c_tok, o_tok, cost, pct = val
                print(f"\n▸ {ttft}ms · {p_tok:,} in ({pct or 0}% cached) · {o_tok:,} out · {format_cost(cost)}")
                break
            elif kind in ("error", "cancelled"):
                print(f"\n{val or 'Cancelled'}")
                break
    except KeyboardInterrupt:
        worker.cancel()
        worker.join(timeout=1.0)
        print("\nCancelled.")


def _run_tui(cfg: dict[str, Any], provider: dict[str, Any]) -> None:
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    state = TUIState(
        provider_name=provider["name"],
        model=provider["active_model"],
        models=list(provider.get("models") or []),
    )

    ev_q: queue.Queue[tuple[str, Any]] = queue.Queue()
    worker: AgentWorker | None = None

    def _bg_ver() -> None:
        v = check_latest_version()
        if v:
            ev_q.put(("update_ver", v))

    threading.Thread(target=_bg_ver, daemon=True).start()

    def start_turn(user_text: str) -> None:
        nonlocal worker
        state.entries.append(ChatEntry(kind="user", text=user_text))
        messages.append({"role": "user", "content": user_text})
        state.busy = True
        state.busy_label = "Thinking…"
        state.busy_started_at = time.perf_counter()
        state.streaming_text = ""
        state.scroll_offset = 0
        worker = AgentWorker(provider, messages, ev_q)
        worker.start()

    def stop_worker() -> None:
        nonlocal worker
        if worker is not None:
            worker.cancel()
            worker.join(timeout=1.5)
            while not ev_q.empty():
                k, v = ev_q.get_nowait()
                if k == "flush_stream" and v:
                    state.entries.append(ChatEntry(kind="assistant", text=v))
                    state.streaming_text = ""
                elif k == "action":
                    p, br = v
                    state.entries.append(ChatEntry(kind="action", parsed=p, bash_res=br))
            worker = None
        if state.streaming_text.strip():
            state.entries.append(ChatEntry(kind="assistant", text=state.streaming_text))
        state.streaming_text = ""
        state.busy = False

    with TermIO() as term:
        last_tick = time.perf_counter()
        draw_frame(state, term)

        while True:
            dirty = False
            while not ev_q.empty():
                kind, val = ev_q.get_nowait()
                dirty = True
                if kind == "update_ver":
                    state.update_ver = val
                elif kind == "status":
                    state.busy_label = val
                elif kind == "token":
                    state.streaming_text += val
                    state.scroll_offset = 0
                elif kind == "flush_stream":
                    if state.streaming_text or val:
                        state.entries.append(ChatEntry(kind="assistant", text=val or state.streaming_text))
                        state.streaming_text = ""
                elif kind == "action":
                    p, br = val
                    state.entries.append(ChatEntry(kind="action", parsed=p, bash_res=br))
                    state.scroll_offset = 0
                elif kind == "done":
                    ttft, p_tok, c_tok, o_tok, turn_cost, pct = val
                    state.session_cost += turn_cost
                    if pct is not None:
                        state.last_cache_pct = pct
                    parts = [f"{ttft}ms"]
                    if p_tok > 0:
                        parts.append(f"{p_tok:,} in ({pct or 0}% cached)")
                    if o_tok > 0:
                        parts.append(f"{o_tok:,} out")
                    if turn_cost > 0 or state.session_cost > 0:
                        parts.append(f"{format_cost(turn_cost)} (Σ {format_cost(state.session_cost)})")
                    state.entries.append(ChatEntry(kind="stats", text=" · ".join(parts)))
                    state.busy = False
                    worker = None
                elif kind == "cancelled":
                    state.entries.append(ChatEntry(kind="warn", text="|| Прервано (история сохранена)"))
                    state.streaming_text = ""
                    state.busy = False
                    worker = None
                elif kind == "error":
                    state.entries.append(ChatEntry(kind="warn", text=f"✗ API error: {val}"))
                    state.streaming_text = ""
                    state.busy = False
                    worker = None

            now = time.perf_counter()
            if state.busy and (now - last_tick) >= 0.07:
                state.tick += 1
                last_tick = now
                dirty = True

            if dirty:
                draw_frame(state, term)

            key = term.read_key(timeout=0.05 if state.busy else 0.15)
            if key is None:
                continue

            if state.model_picker_open:
                q = state.model_query.lower()
                items = [m for m in state.models if q in m.lower()] if q else state.models
                if key in ("ESC", "CTRL_C"):
                    state.model_picker_open = False
                elif key == "UP":
                    state.model_idx = max(0, state.model_idx - 1)
                elif key == "DOWN" and items:
                    state.model_idx = min(len(items) - 1, state.model_idx + 1)
                elif key == "BACKSPACE":
                    state.model_query = state.model_query[:-1]
                    state.model_idx = 0
                elif key == "ENTER":
                    if items:
                        chosen = items[state.model_idx]
                        provider["active_model"] = chosen
                        cfg["providers"][provider["name"]] = provider
                        save_config(cfg)
                        state.model = chosen
                        state.entries.append(ChatEntry(kind="notice", text=f"Model: {chosen}"))
                    state.model_picker_open = False
                elif len(key) == 1 and key.isprintable():
                    state.model_query += key
                    state.model_idx = 0
                draw_frame(state, term)
                continue

            if key == "CTRL_D":
                stop_worker()
                break

            if key in ("ESC", "CTRL_C"):
                if state.busy:
                    stop_worker()
                    state.entries.append(ChatEntry(kind="warn", text="|| Прервано (история сохранена)"))
                elif state.input_text:
                    state.input_text = ""
                    state.cursor = 0
                draw_frame(state, term)
                continue

            if key == "PGUP":
                state.scroll_offset += 6
                draw_frame(state, term)
                continue
            if key == "PGDN":
                state.scroll_offset = max(0, state.scroll_offset - 6)
                draw_frame(state, term)
                continue

            slash_matches = (
                [(c, d) for c, d in SLASH_COMMANDS if c.startswith(state.input_text.lower())]
                if state.input_text.startswith("/") and " " not in state.input_text
                else []
            )

            if key == "UP":
                if slash_matches:
                    state.slash_idx = max(0, state.slash_idx - 1)
                elif not state.input_text:
                    state.scroll_offset += 2
                draw_frame(state, term)
                continue

            if key == "DOWN":
                if slash_matches:
                    state.slash_idx = min(len(slash_matches) - 1, state.slash_idx + 1)
                elif not state.input_text:
                    state.scroll_offset = max(0, state.scroll_offset - 2)
                draw_frame(state, term)
                continue

            if key == "TAB" and slash_matches:
                state.input_text = slash_matches[state.slash_idx][0]
                state.cursor = len(state.input_text)
                draw_frame(state, term)
                continue

            if key == "LEFT":
                state.cursor = max(0, state.cursor - 1)
            elif key == "RIGHT":
                state.cursor = min(len(state.input_text), state.cursor + 1)
            elif key == "HOME":
                state.cursor = 0
            elif key == "END":
                state.cursor = len(state.input_text)
            elif key == "BACKSPACE":
                if state.cursor > 0:
                    state.input_text = state.input_text[: state.cursor - 1] + state.input_text[state.cursor :]
                    state.cursor -= 1
            elif key == "DEL":
                if state.cursor < len(state.input_text):
                    state.input_text = state.input_text[: state.cursor] + state.input_text[state.cursor + 1 :]
            elif key == "ENTER":
                text = state.input_text.strip()
                if not text:
                    continue
                if slash_matches and text != slash_matches[state.slash_idx][0]:
                    text = slash_matches[state.slash_idx][0]

                state.input_text = ""
                state.cursor = 0

                if text == "/exit":
                    stop_worker()
                    break
                if text == "/new":
                    stop_worker()
                    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                    state.entries.clear()
                    state.session_cost = 0.0
                    state.last_cache_pct = None
                    state.entries.append(ChatEntry(kind="notice", text="New chat (context & cost reset)"))
                    draw_frame(state, term)
                    continue
                if text == "/model":
                    if state.models:
                        state.model_picker_open = True
                        state.model_query = ""
                        state.model_idx = state.models.index(state.model) if state.model in state.models else 0
                    else:
                        state.entries.append(ChatEntry(kind="warn", text="No synced models. Run /provider."))
                    draw_frame(state, term)
                    continue
                if text == "/update":
                    stop_worker()
                    state.busy = True
                    state.busy_label = "Updating from GitHub…"
                    state.busy_started_at = time.perf_counter()
                    draw_frame(state, term)
                    ok, msg = self_update()
                    state.busy = False
                    state.entries.append(ChatEntry(kind="notice" if ok else "warn", text=msg))
                    draw_frame(state, term)
                    continue
                if text == "/provider":
                    stop_worker()
                    term.__exit__(None, None, None)
                    provider = run_onboarding(cfg)
                    term.__enter__()
                    state.provider_name = provider["name"]
                    state.model = provider["active_model"]
                    state.models = list(provider.get("models") or [])
                    state.entries.append(ChatEntry(kind="notice", text=f"Provider: {state.provider_name} · {state.model}"))
                    draw_frame(state, term)
                    continue
                if text.startswith("/"):
                    state.entries.append(ChatEntry(kind="warn", text="Commands: /model, /new, /provider, /update, /exit"))
                    draw_frame(state, term)
                    continue

                if state.busy:
                    stop_worker()
                    state.entries.append(ChatEntry(kind="warn", text="|| Дополнено пользователем"))
                start_turn(text)

            elif len(key) == 1 and key.isprintable():
                state.input_text = state.input_text[: state.cursor] + key + state.input_text[state.cursor :]
                state.cursor += 1
                state.slash_idx = 0

            draw_frame(state, term)


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(
            f"{CYAN}shx{R} v{__version__} — bash-only ultra-economical AI agent\n\n"
            "Usage:\n"
            "  shx             Launch full-screen TUI\n"
            "  shx \"<prompt>\"  Run one-shot prompt\n"
            "  shx --update    Update to latest version\n"
            "  shx --version   Show version\n\n"
            "Slash commands: /model, /new, /provider, /update, /exit"
        )
        return

    if len(sys.argv) == 2 and sys.argv[1] in ("-v", "--version"):
        print(f"sh-agent {__version__}")
        return

    if len(sys.argv) == 2 and sys.argv[1] == "--update":
        ok, msg = self_update()
        print(f"{GREEN}✓{R} {msg}" if ok else f"{RED}✗ Update failed:{R} {msg}")
        return

    cfg = load_config()
    provider = get_active_provider(cfg)
    if provider is None:
        provider = run_onboarding(cfg)

    if len(sys.argv) > 1:
        prompt_text = " ".join(sys.argv[1:]).strip()
        if prompt_text:
            _run_oneshot(provider, prompt_text)
            return

    _run_tui(cfg, provider)


if __name__ == "__main__":
    main()
