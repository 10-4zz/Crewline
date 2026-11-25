"""Runtime helpers for source vs PyInstaller-frozen standalone execution."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import ssl
import sys
from typing import Iterator, Mapping, Sequence

LINUX_SYSTEM_CA_FILES: tuple[str, ...] = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/pki/tls/cacert.pem",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",
    "/etc/ssl/cert.pem",
)

LINUX_SYSTEM_CA_DIRS: tuple[str, ...] = (
    "/etc/ssl/certs",
    "/etc/pki/tls/certs",
)


def is_frozen() -> bool:
    """Return True when running from a PyInstaller standalone bundle."""
    return bool(getattr(sys, "frozen", False))


def crewline_self_command() -> list[str]:
    """Return argv prefix to re-invoke Crewline (e.g. inside tmux background sessions).

    - Frozen standalone executable: invoke the resolved binary directly.
    - Source / Python environment: invoke `python -m crewline`.
    """
    if is_frozen():
        return [str(Path(sys.executable).resolve())]
    return [sys.executable, "-m", "crewline"]


def _strip_meipass_entries(ld_library_path: str) -> str:
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return ld_library_path
    meipass_norm = os.path.normpath(str(meipass))
    kept = [
        entry
        for entry in ld_library_path.split(os.pathsep)
        if entry and os.path.normpath(entry) != meipass_norm
    ]
    return os.pathsep.join(kept)


def clean_subprocess_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an environment safe for external binaries (git, tmux, pagers, official CLIs).

    In PyInstaller-frozen Linux executables:
    - If LD_LIBRARY_PATH_ORIG is present, restore LD_LIBRARY_PATH from it (preserving
      any preset user library paths and removing unset/empty values cleanly).
    - If LD_LIBRARY_PATH_ORIG is absent, strip only PyInstaller's _MEIPASS directory
      so a preset user LD_LIBRARY_PATH is never accidentally discarded.
    - Clear internal reset/meipass variables so external tools see a clean environment.
    """
    env = dict(os.environ if base_env is None else base_env)
    if is_frozen():
        if "LD_LIBRARY_PATH_ORIG" in env:
            orig = env.pop("LD_LIBRARY_PATH_ORIG")
            if orig:
                env["LD_LIBRARY_PATH"] = orig
            else:
                env.pop("LD_LIBRARY_PATH", None)
        elif "LD_LIBRARY_PATH" in env:
            stripped = _strip_meipass_entries(env["LD_LIBRARY_PATH"])
            if stripped:
                env["LD_LIBRARY_PATH"] = stripped
            else:
                env.pop("LD_LIBRARY_PATH", None)
        env.pop("_MEIPASS2", None)
        env.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
    return env


def crewline_worker_env(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return environment for spawning an independent background Crewline worker process.

    Combines LD_LIBRARY_PATH restoration with PYINSTALLER_RESET_ENVIRONMENT=1 (PyInstaller
    6.9+) so the detached tmux worker initializes as an independent top-level process
    and preserves the caller's original LD_LIBRARY_PATH when invoking git/tools.
    """
    env = clean_subprocess_env(base_env)
    if is_frozen():
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def resolve_ca_paths(
    environ: Mapping[str, str] | None = None,
    *,
    frozen: bool | None = None,
    linux_ca_files: Sequence[str] = LINUX_SYSTEM_CA_FILES,
    linux_ca_dirs: Sequence[str] = LINUX_SYSTEM_CA_DIRS,
) -> tuple[str | None, str | None]:
    """Resolve CA bundle file/dir for HTTPS API calls.

    Precedence:
    1. User-provided SSL_CERT_FILE / SSL_CERT_DIR environment variables.
    2. In frozen bundles (or when OpenSSL's compiled-in default CA path is missing,
       such as Conda-built Python on another Linux host), use standard Linux system
       CA paths.
    3. Otherwise return (None, None) to use OpenSSL's built-in defaults.
    """
    env = os.environ if environ is None else environ
    env_cafile = env.get("SSL_CERT_FILE", "").strip() or None
    env_capath = env.get("SSL_CERT_DIR", "").strip() or None
    if env_cafile or env_capath:
        return env_cafile, env_capath

    use_frozen = is_frozen() if frozen is None else frozen
    defaults = ssl.get_default_verify_paths()
    default_exists = bool(
        (defaults.cafile and Path(defaults.cafile).is_file())
        or (defaults.capath and Path(defaults.capath).is_dir())
    )
    if use_frozen or not default_exists:
        sys_cafile = next((p for p in linux_ca_files if Path(p).is_file()), None)
        sys_capath = next((p for p in linux_ca_dirs if Path(p).is_dir()), None)
        if sys_cafile or sys_capath:
            return sys_cafile, sys_capath

    return None, None


def build_ssl_context(environ: Mapping[str, str] | None = None) -> ssl.SSLContext:
    """Create a verified TLS SSLContext respecting SSL_CERT_FILE/SSL_CERT_DIR and Linux system CAs."""
    cafile, capath = resolve_ca_paths(environ=environ)
    if cafile or capath:
        return ssl.create_default_context(cafile=cafile, capath=capath)
    return ssl.create_default_context()


@contextmanager
def clean_os_environ() -> Iterator[None]:
    """Temporarily restore process LD_LIBRARY_PATH when invoking helpers like click's pager."""
    if not is_frozen():
        yield
        return

    sentinel = object()
    keys = (
        "LD_LIBRARY_PATH",
        "LD_LIBRARY_PATH_ORIG",
        "_MEIPASS2",
        "PYINSTALLER_RESET_ENVIRONMENT",
    )
    saved = {key: os.environ.get(key, sentinel) for key in keys}
    cleaned = clean_subprocess_env(os.environ)
    try:
        for key in keys:
            if key in cleaned:
                os.environ[key] = cleaned[key]
            else:
                os.environ.pop(key, None)
        yield
    finally:
        for key, value in saved.items():
            if value is sentinel:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
