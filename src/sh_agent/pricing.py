from __future__ import annotations

import json
import re
from importlib import resources
from typing import Optional

_TABLE: dict[str, list[float]] | None = None
_DATE_SUFFIX_RE = re.compile(r"-(?:20\d{6}|\d{4}|v\d+)$")


def _load_table() -> dict[str, list[float]]:
    global _TABLE
    if _TABLE is None:
        raw = resources.files("sh_agent").joinpath("pricing.json").read_text(encoding="utf-8")
        _TABLE = json.loads(raw)
    return _TABLE


def _norm(s: str) -> str:
    s = s.lower().strip()
    if ":" in s:
        s = s.split(":", 1)[0]
    if "/" in s:
        s = s.split("/", 1)[1]
    s = _DATE_SUFFIX_RE.sub("", s)
    return s


def match_price(model_id: str) -> Optional[tuple[float, float, float, str]]:
    """Return (in_per_1m, out_per_1m, cached_per_1m, matched_key) or None."""
    table = _load_table()
    if not model_id:
        return None

    low = model_id.lower().strip()
    if low in table:
        p = table[low]
        return p[0], p[1], p[2], low

    slug = _norm(low)
    for k, p in table.items():
        if _norm(k) == slug:
            return p[0], p[1], p[2], k

    # Token-overlap fuzzy match (handles custom provider prefixes/suffixes like deepseek-v4.1-flash-1017)
    q_tokens = [t for t in re.split(r"[-_./:]+", slug) if t]
    if not q_tokens:
        return None

    best_key = None
    best_score = 0.0
    for k in table:
        k_slug = _norm(k)
        k_tokens = [t for t in re.split(r"[-_./:]+", k_slug) if t]
        if not k_tokens:
            continue
        overlap = sum(1 for t in q_tokens if t in k_tokens)
        if overlap == 0:
            continue
        # Bonus for matching the first family token (e.g. deepseek, claude, gpt, gemini, qwen)
        family_bonus = 0.5 if q_tokens[0] == k_tokens[0] else 0.0
        score = (overlap / max(len(q_tokens), len(k_tokens))) + family_bonus
        if score > best_score:
            best_score = score
            best_key = k

    if best_key and best_score >= 0.75:
        p = table[best_key]
        return p[0], p[1], p[2], best_key
    return None


def estimate_cost(
    model_id: str,
    prompt_tokens: int,
    cached_tokens: int,
    completion_tokens: int,
) -> tuple[float, Optional[str]]:
    """Return (cost_usd, matched_model_key)."""
    matched = match_price(model_id)
    if not matched:
        return 0.0, None
    p_in, p_out, p_cache, key = matched
    uncached = max(0, prompt_tokens - cached_tokens)
    cost = (uncached * p_in + cached_tokens * p_cache + completion_tokens * p_out) / 1_000_000.0
    return cost, key


def format_cost(usd: float) -> str:
    if usd <= 0:
        return "$0"
    if usd < 0.0001:
        return f"${usd:.6f}".rstrip("0").rstrip(".")
    if usd < 0.01:
        return f"${usd:.4f}".rstrip("0").rstrip(".")
    return f"${usd:.3f}"
