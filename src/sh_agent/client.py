from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .config import normalize_base_url
from .shell import TOOLS_SCHEMA


@dataclass
class StreamResult:
    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    ttft_ms: int = 0
    cancelled: bool = False


def _extract_cached(usage: dict[str, Any]) -> int:
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if isinstance(details, dict) and details.get("cached_tokens"):
        return int(details["cached_tokens"])
    for key in ("cache_read_input_tokens", "cached_tokens", "prompt_cache_hit_tokens"):
        if usage.get(key):
            return int(usage[key])
    return 0


def stream_chat(
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    on_first_event: Callable[[], None] | None = None,
    on_token: Callable[[str], None] | None = None,
) -> StreamResult:
    url = f"{normalize_base_url(base_url)}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": TOOLS_SCHEMA,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    t0 = time.perf_counter()
    timeout = httpx.Timeout(connect=15.0, read=180.0, write=30.0, pool=15.0)

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for attempt in range(2):
            with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code == 400 and attempt == 0 and "stream_options" in payload:
                    body_text = resp.read().decode("utf-8", errors="replace")
                    if "stream_options" in body_text.lower() or "include_usage" in body_text.lower():
                        payload.pop("stream_options", None)
                        continue
                    raise httpx.HTTPStatusError(
                        f"HTTP 400: {body_text[:400]}", request=resp.request, response=resp
                    )

                if resp.status_code >= 400:
                    body_text = resp.read().decode("utf-8", errors="replace")
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}: {body_text[:400]}", request=resp.request, response=resp
                    )

                return _consume_sse(resp, t0, on_first_event, on_token)

    return StreamResult()


def _consume_sse(
    resp: httpx.Response,
    t0: float,
    on_first_event: Callable[[], None] | None,
    on_token: Callable[[str], None] | None,
) -> StreamResult:
    res = StreamResult()
    tc_map: dict[int, dict[str, Any]] = {}
    first_seen = False

    def mark_first() -> None:
        nonlocal first_seen
        if not first_seen:
            first_seen = True
            res.ttft_ms = int((time.perf_counter() - t0) * 1000)
            if on_first_event:
                on_first_event()

    try:
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                break
            try:
                chunk = json.loads(raw)
            except json.JSONDecodeError:
                continue

            usage = chunk.get("usage")
            if isinstance(usage, dict):
                res.prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                res.completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                res.cached_tokens = _extract_cached(usage)

            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}

            text_delta = delta.get("content")
            if text_delta:
                mark_first()
                res.content += text_delta
                if on_token:
                    on_token(text_delta)

            tc_deltas = delta.get("tool_calls") or []
            if tc_deltas:
                mark_first()
                for tc in tc_deltas:
                    idx = int(tc.get("index", 0))
                    entry = tc_map.setdefault(
                        idx,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        entry["function"]["name"] += fn["name"]
                    if fn.get("arguments"):
                        entry["function"]["arguments"] += fn["arguments"]
    except KeyboardInterrupt:
        res.cancelled = True

    if not first_seen:
        mark_first()

    res.tool_calls = [tc_map[k] for k in sorted(tc_map)]
    return res
