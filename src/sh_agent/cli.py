from __future__ import annotations

import json
import sys
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML

from . import __version__
from .client import stream_chat
from .config import get_active_provider, load_config, save_config
from .pricing import estimate_cost
from .shell import SYSTEM_PROMPT, parse_bash_action, run_bash
from .tui import (
    CYAN,
    DARK,
    GRAY,
    GREEN,
    PROMPT_STYLE,
    RED,
    YELLOW,
    EscInterruptWatcher,
    R,
    SlashCompleter,
    Spinner,
    pick_model,
    print_banner,
    render_action,
    render_stats,
    run_onboarding,
)
from .updater import check_latest_version, self_update


def _ask_supplement(steer_session: PromptSession) -> str | None:
    print(f"{DARK}│{R} {YELLOW}⏸ Прервано (история сохранена){R}")
    try:
        text = steer_session.prompt(
            HTML("<b><ansiyellow>↳ Дополнить</ansiyellow></b> <ansibrightblack>(Enter — стоп)</ansibrightblack>: ")
        ).strip()
        return text or None
    except (KeyboardInterrupt, EOFError):
        return None


def _run_turn(
    provider: dict[str, Any],
    messages: list[dict[str, Any]],
    session_state: dict[str, float],
    steer_session: PromptSession | None = None,
) -> None:
    model = provider["active_model"]
    base_url = provider["base_url"]
    api_key = provider.get("api_key", "")

    total_prompt = 0
    total_cached = 0
    total_comp = 0
    first_ttft = 0

    while True:
        short_model = model.split("/")[-1] if "/" in model else model
        spin = Spinner(f"Thinking · {short_model}", CYAN)
        spin.start()
        streamed_any_text = False

        def on_first() -> None:
            spin.stop()

        def on_token(delta: str) -> None:
            nonlocal streamed_any_text
            if not streamed_any_text:
                streamed_any_text = True
                sys.stdout.write("\n")
            sys.stdout.write(delta)
            sys.stdout.flush()

        interrupted = False
        res = None

        try:
            with EscInterruptWatcher():
                res = stream_chat(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    on_first_event=on_first,
                    on_token=on_token,
                )
        except KeyboardInterrupt:
            interrupted = True
        except Exception as e:
            spin.stop()
            print(f"\n{RED}✗ API error:{R} {e}\n")
            return
        finally:
            spin.stop()

        if streamed_any_text:
            sys.stdout.write("\n")
            sys.stdout.flush()

        if interrupted or (res is not None and res.cancelled):
            if res is not None and res.content.strip():
                messages.append({"role": "assistant", "content": res.content})
            if steer_session is None:
                print(f"{YELLOW}⏸ Прервано{R}\n")
                return
            supplement = _ask_supplement(steer_session)
            if supplement:
                messages.append({"role": "user", "content": supplement})
                continue
            print()
            return

        assert res is not None
        if first_ttft == 0:
            first_ttft = res.ttft_ms
        total_prompt += res.prompt_tokens
        total_cached += res.cached_tokens
        total_comp += res.completion_tokens

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": res.content or None}
        if res.tool_calls:
            assistant_msg["tool_calls"] = res.tool_calls
        messages.append(assistant_msg)

        if not res.tool_calls:
            break

        cancelled_mid_tools = False
        for idx, tc in enumerate(res.tool_calls):
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError:
                args = {}
            cmd = str(args.get("command", ""))
            parsed = parse_bash_action(cmd)

            with EscInterruptWatcher():
                with Spinner(f"{parsed.kind} · {parsed.target}", GRAY):
                    b_res = run_bash(cmd)

            render_action(parsed, b_res)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id") or f"call_{idx}",
                    "content": f"[exit {b_res.exit_code}]\n{b_res.output}",
                }
            )

            if b_res.cancelled:
                for rem_idx, rem_tc in enumerate(res.tool_calls[idx + 1 :], start=idx + 1):
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": rem_tc.get("id") or f"call_{rem_idx}",
                            "content": "[interrupted by user]",
                        }
                    )
                cancelled_mid_tools = True
                break

        if cancelled_mid_tools:
            if steer_session is None:
                print(f"{YELLOW}⏸ Прервано{R}\n")
                return
            supplement = _ask_supplement(steer_session)
            if supplement:
                messages.append({"role": "user", "content": supplement})
                continue
            print()
            return

    turn_cost, _ = estimate_cost(model, total_prompt, total_cached, total_comp)
    session_state["cost"] += turn_cost
    render_stats(first_ttft, total_prompt, total_cached, total_comp, turn_cost, session_state["cost"])


def _run_update() -> None:
    with Spinner("Updating sh-agent from GitHub...", CYAN):
        ok, msg = self_update()
    if ok:
        print(f"{GREEN}✓{R} {msg}\n")
    else:
        print(f"{RED}✗ Update failed:{R} {msg}\n")


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        print(
            f"{CYAN}shx{R} v{__version__} — bash-only ultra-economical AI agent\n\n"
            "Usage:\n"
            "  shx             Launch interactive agent\n"
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
        _run_update()
        return

    cfg = load_config()
    provider = get_active_provider(cfg)

    if provider is None:
        print_banner()
        provider = run_onboarding(cfg)

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    session_state = {"cost": 0.0}

    if len(sys.argv) > 1:
        prompt_text = " ".join(sys.argv[1:]).strip()
        if prompt_text:
            messages.append({"role": "user", "content": prompt_text})
            _run_turn(provider, messages, session_state)
            return

    update_ver = check_latest_version()
    print_banner(provider["name"], provider["active_model"], update_ver=update_ver)

    session = PromptSession(
        completer=SlashCompleter(),
        complete_while_typing=True,
        style=PROMPT_STYLE,
    )
    steer_session = PromptSession()

    while True:
        try:
            user_input = session.prompt(HTML("<b><ansicyan>❯</ansicyan></b> ")).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            print(f"\n{GRAY}bye{R}")
            break

        if not user_input:
            continue

        if user_input == "/exit":
            print(f"{GRAY}bye{R}")
            break

        if user_input == "/new":
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            session_state["cost"] = 0.0
            print(f"{GREEN}✓{R} {GRAY}New chat (context & cost reset){R}\n")
            continue

        if user_input == "/update":
            _run_update()
            continue

        if user_input == "/model":
            models = provider.get("models") or []
            if not models:
                print(f"{YELLOW}No synced models. Run /provider to re-sync.{R}\n")
                continue
            chosen = pick_model(models, provider.get("active_model", ""))
            if chosen and chosen != provider.get("active_model"):
                provider["active_model"] = chosen
                cfg["providers"][provider["name"]] = provider
                save_config(cfg)
                print(f"{GREEN}✓{R} Model: {CYAN}{chosen}{R}\n")
            continue

        if user_input == "/provider":
            provider = run_onboarding(cfg)
            print_banner(provider["name"], provider["active_model"])
            continue

        if user_input.startswith("/"):
            print(f"{GRAY}Commands: /model, /new, /provider, /update, /exit{R}\n")
            continue

        messages.append({"role": "user", "content": user_input})
        _run_turn(provider, messages, session_state, steer_session=steer_session)


if __name__ == "__main__":
    main()
