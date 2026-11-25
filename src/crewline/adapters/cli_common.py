"""Shared helpers for official CLI-based adapters (Codex, Claude Code, Grok Build)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping

from crewline.adapters.base import LogCallback
from crewline.runtime import clean_subprocess_env
from crewline.security.redactor import SecretRedactor


def find_cli_binary(binary_name: str) -> str | None:
    """Locate an executable on PATH or as an explicit executable path."""
    if os.path.sep in binary_name:
        path = Path(binary_name)
        if path.exists() and os.access(path, os.X_OK):
            return str(path.resolve())
        return None
    return shutil.which(binary_name)


def probe_cli_command(
    cmd: list[str],
    timeout_seconds: int = 8,
) -> tuple[int | None, str]:
    """Run a short non-destructive probe command (e.g. login status or --version)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=clean_subprocess_env(),
            check=False,
        )
        combined = "\n".join(
            part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()
        )
        return proc.returncode, combined
    except subprocess.TimeoutExpired:
        return None, f"检测命令超时（>{timeout_seconds}s）"
    except OSError as exc:
        return None, str(exc)


def run_cli_subprocess(
    cmd: list[str],
    cwd: Path,
    timeout_seconds: int,
    log_callback: LogCallback,
    redactor: SecretRedactor,
    stdin_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, list[str], bool]:
    """Execute a CLI subprocess, streaming redacted output lines to log_callback."""
    proc_env = clean_subprocess_env()
    if env:
        proc_env.update(env)

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=proc_env,
        )
    except OSError as exc:
        msg = redactor.redact_str(f"无法启动外部 CLI 进程: {exc}")
        log_callback(msg)
        return 127, [msg], False

    try:
        stdout_text, _ = proc.communicate(input=stdin_text, timeout=timeout_seconds)
        lines = [redactor.redact_str(line) for line in (stdout_text or "").splitlines()]
        for line in lines:
            log_callback(line)
        return int(proc.returncode or 0), lines, False
    except subprocess.TimeoutExpired:
        proc.kill()
        partial_out, _ = proc.communicate()
        lines = [redactor.redact_str(line) for line in (partial_out or "").splitlines()]
        for line in lines:
            log_callback(line)
        timeout_msg = f"CLI 执行超时（超过 {timeout_seconds} 秒），已终止进程。"
        log_callback(timeout_msg)
        lines.append(timeout_msg)
        return 124, lines, True
