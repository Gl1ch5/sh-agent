from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

CONFIG_DIR = Path.home() / ".sh-agent"
CONFIG_PATH = CONFIG_DIR / "config.json"


def normalize_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def default_provider_name(base_url: str) -> str:
    host = urlparse(base_url).netloc.split(":")[0]
    parts = [p for p in host.split(".") if p not in ("api", "www", "com", "ai", "io", "org", "net")]
    return parts[0] if parts else "custom"


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {"active_provider": None, "providers": {}}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"active_provider": None, "providers": {}}
        data.setdefault("active_provider", None)
        data.setdefault("providers", {})
        return data
    except Exception:
        return {"active_provider": None, "providers": {}}


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if os.name == "posix":
        try:
            os.chmod(CONFIG_PATH, 0o600)
        except OSError:
            pass


def get_active_provider(cfg: dict[str, Any]) -> dict[str, Any] | None:
    name = cfg.get("active_provider")
    providers = cfg.get("providers") or {}
    if name and name in providers:
        return providers[name]
    if providers:
        first_name = next(iter(providers))
        cfg["active_provider"] = first_name
        return providers[first_name]
    return None


def fetch_models(base_url: str, api_key: str, timeout: float = 15.0) -> list[str]:
    url = f"{normalize_base_url(base_url)}/models"
    headers = {"Accept": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        payload = resp.json()

    raw = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []

    seen: set[str] = set()
    models: list[str] = []
    for item in raw:
        mid = item.get("id") if isinstance(item, dict) else (item if isinstance(item, str) else None)
        if mid and mid not in seen:
            seen.add(mid)
            models.append(mid)
    return models
