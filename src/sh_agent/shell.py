from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any

# Ultra-minimal static system prompt (~28 tokens) — immutable prefix for max cache hit rate.
SYSTEM_PROMPT = "shx: fast CLI agent (tool: bash). Run concise cmds; reply in 1-2 lines; no filler."

# Minimal single tool schema (~42 tokens) — immutable prefix for max cache hit rate.
TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run shell cmd",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]

MAX_OUTPUT_CHARS = 6_000
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


@dataclass
class ParsedAction:
    kind: str       # "write" | "read" | "find" | "grep" | "bash"
    target: str     # compact file path or shortened command
    detail: str = ""


@dataclass
class BashResult:
    output: str
    exit_code: int
    elapsed_ms: int
    lines: int
    bytes_count: int
    cancelled: bool = False


_HEREDOC_WRITE_RE = re.compile(
    r"""(?:cat|tee)\s+(?:-[a-zA-Z]+\s+)*(?:<<-?\s*['"]?(\w+)['"]?\s*>\s*|>\s*([^\s;&|]+)\s*<<-?\s*['"]?(\w+)['"]?|([^\s;&|]+)\s*<<-?\s*['"]?(\w+)['"]?)""",
    re.MULTILINE,
)
_SIMPLE_REDIRECT_RE = re.compile(
    r"""^(?:cat|echo|printf|tee)\b.*?(?:>>?|tee\s+(?:-a\s+)?)\s*['"]?([^\s'"|;&]+)['"]?\s*$""",
    re.DOTALL,
)
_PS_WRITE_RE = re.compile(
    r"""(?:Set-Content|Out-File|Add-Content)\s+(?:-Path\s+)?['"]?([^\s'"]+)['"]?""",
    re.IGNORECASE,
)
_READ_RE = re.compile(
    r"""^(?:cat|head|tail|nl|bat|less|more|Get-Content)\s+(?:-[a-zA-Z0-9]+\s+(?:\d+\s+)?)*(?:--?\w+\s+)*['"]?([^\s'"|;&]+)['"]?(?:\s*\|.*)?$"""
)
_SED_READ_RE = re.compile(
    r"""^sed\s+-n\s+['"][^'"]+['"]\s+['"]?([^\s'"|;&]+)['"]?$"""
)


def _compact_cmd(cmd: str, max_len: int = 68) -> str:
    lines = [ln.strip() for ln in cmd.strip().splitlines() if ln.strip() and not ln.strip().startswith("#")]
    joined = " ; ".join(lines) if lines else cmd.strip()
    joined = re.sub(r"\s+", " ", joined)
    if len(joined) > max_len:
        return joined[: max_len - 1] + "…"
    return joined


def parse_bash_action(command: str) -> ParsedAction:
    cmd = command.strip()

    # 1. Heredoc write: cat <<'EOF' > file  OR  cat > file <<'EOF'
    m = _HEREDOC_WRITE_RE.search(cmd)
    if m:
        target = m.group(2) or m.group(4)
        if not target:
            after = cmd[m.end() :].splitlines()[0].strip() if cmd[m.end() :] else ""
            target = after.split()[0] if after else "file"
        target = target.strip("'\"")
        body_lines = max(0, len(cmd.splitlines()) - 2)
        return ParsedAction(kind="write", target=target, detail=f"~{body_lines}L" if body_lines else "")

    # 2. PowerShell write
    m_ps = _PS_WRITE_RE.search(cmd)
    if m_ps:
        return ParsedAction(kind="write", target=m_ps.group(1))

    # 3. Simple redirect write (echo/printf/tee > file)
    if "\n" not in cmd:
        m_red = _SIMPLE_REDIRECT_RE.match(cmd)
        if m_red and not cmd.startswith(("rg ", "grep ", "find ", "ls ")):
            return ParsedAction(kind="write", target=m_red.group(1))

    # 4. File read (cat/head/tail/sed -n/Get-Content)
    if "\n" not in cmd:
        m_sed = _SED_READ_RE.match(cmd)
        if m_sed:
            return ParsedAction(kind="read", target=m_sed.group(1))
        m_read = _READ_RE.match(cmd)
        if m_read:
            return ParsedAction(kind="read", target=m_read.group(1))

    # 5. Search / list
    first = cmd.split()[0] if cmd.split() else ""
    if first in ("rg", "grep", "ag", "Select-String"):
        return ParsedAction(kind="grep", target=_compact_cmd(cmd[len(first):].strip(), 62))
    if first in ("find", "fd", "ls", "tree", "dir", "Get-ChildItem"):
        return ParsedAction(kind="find", target=_compact_cmd(cmd, 62))

    return ParsedAction(kind="bash", target=_compact_cmd(cmd, 68))


def _truncate_output(text: str) -> str:
    # Token economy: strip ANSI color codes, trailing spaces, and excessive blank lines
    text = _ANSI_RE.sub("", text)
    text = "\n".join(ln.rstrip() for ln in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return "(no output)"
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    head = text[:3800]
    tail = text[-1800:]
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n\n... [{omitted} chars omitted] ...\n\n{tail}"


def _build_shell_argv(command: str) -> list[str]:
    if os.name == "nt":
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if ps:
            return [ps, "-NoProfile", "-NonInteractive", "-Command", command]
        return ["cmd", "/c", command]
    bash = shutil.which("bash")
    if bash:
        return [bash, "-lc", command]
    return ["sh", "-c", command]


def run_bash(
    command: str,
    timeout: float = 120.0,
    cancel_event: threading.Event | None = None,
) -> BashResult:
    t0 = time.perf_counter()
    argv = _build_shell_argv(command)

    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **popen_kwargs)
    chunks: list[str] = []

    def _reader() -> None:
        assert proc.stdout is not None
        try:
            chunks.append(proc.stdout.read())
        except Exception:
            pass

    rt = threading.Thread(target=_reader, daemon=True)
    rt.start()

    try:
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                _kill_proc_tree(proc)
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                return BashResult(
                    output="Cancelled by user.",
                    exit_code=130,
                    elapsed_ms=elapsed_ms,
                    lines=0,
                    bytes_count=0,
                    cancelled=True,
                )
            if (time.perf_counter() - t0) > timeout:
                _kill_proc_tree(proc)
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                return BashResult(
                    output=f"Command timed out after {int(timeout)}s.",
                    exit_code=124,
                    elapsed_ms=elapsed_ms,
                    lines=0,
                    bytes_count=0,
                )
            time.sleep(0.03)

        rt.join(timeout=1.0)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        raw = "".join(chunks)
        n_lines = raw.count("\n") + (1 if raw and not raw.endswith("\n") else 0)
        n_bytes = len(raw.encode("utf-8"))
        return BashResult(
            output=_truncate_output(raw) if raw else "(no output)",
            exit_code=proc.returncode or 0,
            elapsed_ms=elapsed_ms,
            lines=n_lines,
            bytes_count=n_bytes,
        )
    except KeyboardInterrupt:
        _kill_proc_tree(proc)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return BashResult(
            output="Cancelled by user (Ctrl+C).",
            exit_code=130,
            elapsed_ms=elapsed_ms,
            lines=0,
            bytes_count=0,
            cancelled=True,
        )


def _kill_proc_tree(proc: subprocess.Popen[str]) -> None:
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass
