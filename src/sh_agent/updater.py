from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Optional

import httpx

from . import __version__

REPO_GIT = "git+https://github.com/Gl1ch5/sh-agent.git"
VERSION_URL = "https://raw.githubusercontent.com/Gl1ch5/sh-agent/main/version.json"


def _ver_tuple(v: str) -> tuple[int, ...]:
    parts = []
    for p in v.strip().lstrip("v").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_latest_version(timeout: float = 1.5) -> Optional[str]:
    """Return newer version string if remote > local, else None."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(VERSION_URL)
            if r.status_code != 200:
                return None
            remote = str(r.json().get("version", "")).strip()
            if remote and _ver_tuple(remote) > _ver_tuple(__version__):
                return remote
    except Exception:
        return None
    return None


def self_update() -> tuple[bool, str]:
    """Update sh-agent from GitHub main using uv or pip."""
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "tool", "install", "--force", REPO_GIT]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", REPO_GIT]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            return True, "Updated to latest version. Restart shx."
        err = (proc.stderr or proc.stdout or "update failed").strip().splitlines()[-1]
        return False, err
    except Exception as e:
        return False, str(e)
