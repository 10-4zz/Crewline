"""Offline tests for standalone frozen runtime helpers, TLS CA resolution, and local install.sh."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tarfile
import threading

import pytest

from crewline.domain.models import ProviderId
from crewline.engine.service import CrewlineService
from crewline.runtime import (
    clean_os_environ,
    clean_subprocess_env,
    crewline_self_command,
    crewline_worker_env,
    is_frozen,
    resolve_ca_paths,
)
from crewline.workspace.terminal_sessions import TerminalSessions

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def _make_mock_release(
    tmp_path: Path,
    version_text: str,
    name: str = "crewline-0.1.0-linux-x86_64",
    *,
    build_glibc: str = "2.31",
) -> tuple[Path, Path]:
    src_dir = tmp_path / "pkg_src" / name
    internal_dir = src_dir / "_internal"
    internal_dir.mkdir(parents=True, exist_ok=True)
    bin_path = src_dir / "crewline"
    bin_path.write_text(
        f"#!/usr/bin/env bash\n"
        f"if [[ \"$1\" == \"--version\" ]]; then echo \"{version_text}\"; exit 0; fi\n"
        f"echo \"mock:{version_text}:$*\"\n",
        encoding="utf-8",
    )
    bin_path.chmod(0o755)
    (src_dir / "BUILD_INFO.txt").write_text(
        f"package=crewline\nbuild_glibc={build_glibc}\n",
        encoding="utf-8",
    )
    tarball = tmp_path / f"{name}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(src_dir, arcname=name)
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    sha_file = tmp_path / f"{name}.tar.gz.sha256"
    sha_file.write_text(f"{digest}  {tarball.name}\n", encoding="utf-8")
    return tarball, sha_file


def test_runtime_helpers_source_vs_frozen_and_worker_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Source mode
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert is_frozen() is False
    assert crewline_self_command() == [sys.executable, "-m", "crewline"]
    src_env = clean_subprocess_env({"LD_LIBRARY_PATH": "/custom/lib", "FOO": "1"})
    assert src_env["LD_LIBRARY_PATH"] == "/custom/lib"
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in crewline_worker_env(src_env)

    # Frozen mode with LD_LIBRARY_PATH_ORIG present
    fake_exe = tmp_path / "release" / "crewline"
    fake_internal = tmp_path / "release" / "_internal"
    fake_internal.mkdir(parents=True)
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(fake_internal), raising=False)
    assert is_frozen() is True
    assert crewline_self_command() == [str(fake_exe.resolve())]

    cleaned_with_orig = clean_subprocess_env(
        {
            "LD_LIBRARY_PATH": f"{fake_internal}:/user/lib",
            "LD_LIBRARY_PATH_ORIG": "/user/lib",
            "_MEIPASS2": str(fake_internal),
            "PYINSTALLER_RESET_ENVIRONMENT": "1",
            "PATH": "/usr/bin",
        }
    )
    assert cleaned_with_orig["LD_LIBRARY_PATH"] == "/user/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in cleaned_with_orig
    assert "_MEIPASS2" not in cleaned_with_orig
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in cleaned_with_orig

    # Worker env sets PYINSTALLER_RESET_ENVIRONMENT=1 while keeping restored LD_LIBRARY_PATH
    worker_env = crewline_worker_env(
        {
            "LD_LIBRARY_PATH": f"{fake_internal}:/user/lib",
            "LD_LIBRARY_PATH_ORIG": "/user/lib",
            "PATH": "/usr/bin",
        }
    )
    assert worker_env["LD_LIBRARY_PATH"] == "/user/lib"
    assert worker_env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"

    # Frozen mode WITHOUT LD_LIBRARY_PATH_ORIG preserves user LD_LIBRARY_PATH and strips only _MEIPASS
    cleaned_no_orig = clean_subprocess_env(
        {
            "LD_LIBRARY_PATH": f"{fake_internal}:/preset/user/lib",
            "PATH": "/usr/bin",
        }
    )
    assert cleaned_no_orig["LD_LIBRARY_PATH"] == "/preset/user/lib"

    cleaned_only_meipass = clean_subprocess_env(
        {
            "LD_LIBRARY_PATH": str(fake_internal),
            "PATH": "/usr/bin",
        }
    )
    assert "LD_LIBRARY_PATH" not in cleaned_only_meipass

    # clean_os_environ temporarily restores LD_LIBRARY_PATH and reverts on exit
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{fake_internal}:/user/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/user/lib")
    with clean_os_environ():
        assert os.environ.get("LD_LIBRARY_PATH") == "/user/lib"
        assert "LD_LIBRARY_PATH_ORIG" not in os.environ
    assert os.environ.get("LD_LIBRARY_PATH") == f"{fake_internal}:/user/lib"


def test_terminal_sessions_frozen_worker_sets_reset_and_preserves_ld_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = CrewlineService(tmp_path / ".crewline")
    service.init_workspace()
    task = service.create_task("冻结模式后台启动", "desc", tmp_path, "mock-dev")

    fake_exe = tmp_path / "bundle" / "crewline"
    fake_internal = tmp_path / "bundle" / "_internal"
    fake_internal.mkdir(parents=True)
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    monkeypatch.setattr(sys, "_MEIPASS", str(fake_internal), raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{fake_internal}:/orig/sys/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/orig/sys/lib")

    sessions = TerminalSessions(service)
    sessions.tmux = "/usr/bin/tmux"

    # Non-worker env (e.g. attach) restores LD_LIBRARY_PATH and does NOT set PYINSTALLER_RESET_ENVIRONMENT.
    attach_env = sessions._environment()
    assert attach_env["LD_LIBRARY_PATH"] == "/orig/sys/lib"
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in attach_env

    captured: dict[str, object] = {}

    def fake_call(command: list[str], env: dict[str, str] | None = None):
        captured["command"] = command
        captured["env"] = env
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(sessions, "_call", fake_call)
    monkeypatch.setattr(sessions, "is_alive", lambda _tid: False)
    monkeypatch.setattr(service, "get_workbench_run", lambda _lid: None)

    with pytest.raises(Exception):
        sessions.start(task.id)

    cmd = captured["command"]
    env = captured["env"]
    assert isinstance(cmd, list)
    assert str(fake_exe.resolve()) in cmd
    assert "-m" not in cmd
    assert "_workbench-run" in cmd
    assert isinstance(env, dict)
    assert env["LD_LIBRARY_PATH"] == "/orig/sys/lib"
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_ca_resolution_precedence_and_https_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sys_ca = tmp_path / "system-ca.crt"
    fake_sys_ca.write_text("dummy", encoding="utf-8")
    fake_sys_dir = tmp_path / "system-certs"
    fake_sys_dir.mkdir()
    user_ca = tmp_path / "user-ca.crt"
    user_ca.write_text("dummy", encoding="utf-8")

    # 1. Frozen mode uses Linux system CA paths when SSL_CERT_FILE/DIR are unset.
    cafile, capath = resolve_ca_paths(
        environ={},
        frozen=True,
        linux_ca_files=(str(fake_sys_ca),),
        linux_ca_dirs=(str(fake_sys_dir),),
    )
    assert cafile == str(fake_sys_ca)
    assert capath == str(fake_sys_dir)

    # 2. Explicit SSL_CERT_FILE / SSL_CERT_DIR ALWAYS override system defaults.
    cafile2, capath2 = resolve_ca_paths(
        environ={"SSL_CERT_FILE": str(user_ca)},
        frozen=True,
        linux_ca_files=(str(fake_sys_ca),),
        linux_ca_dirs=(str(fake_sys_dir),),
    )
    assert cafile2 == str(user_ca)
    assert capath2 is None

    # 3. End-to-end local HTTPS server test (no real API account): verify TLS uses SSL_CERT_FILE.
    openssl_bin = shutil.which("openssl")
    if not openssl_bin:
        pytest.skip("openssl not installed")

    cert_file = tmp_path / "localhost.crt"
    key_file = tmp_path / "localhost.key"
    subprocess.run(
        [
            openssl_bin,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key_file),
            "-out",
            str(cert_file),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "HTTPS CA OK"}}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        service = CrewlineService(tmp_path / ".crewline")
        service.init_workspace()
        service.add_agent(
            "ds-https",
            ProviderId.DEEPSEEK,
            api_key_env="DEEPSEEK_API_KEY",
            base_url=f"https://{host}:{port}",
            overwrite=True,
        )
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-https-dummy-123456")
        monkeypatch.setattr(sys, "frozen", True, raising=False)

        # Without SSL_CERT_FILE: system CA rejects untrusted self-signed cert.
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        t_fail = service.create_task("TLS拒签验证", "test", tmp_path, "ds-https")
        out_fail = service.run_tasks([t_fail.id])[0]
        assert out_fail.succeeded is False

        # With SSL_CERT_FILE pointing to self-signed cert: TLS succeeds.
        monkeypatch.setenv("SSL_CERT_FILE", str(cert_file))
        out_ok = service.run_tasks([t_fail.id])[0]
        assert out_ok.succeeded is True
        _, log_text = service.read_logs(t_fail.id)
        assert "HTTPS CA OK" in log_text
    finally:
        server.shutdown()
        server.server_close()


def test_install_sh_relative_prefix_and_failure_preserves_existing(tmp_path: Path) -> None:
    tar1, _ = _make_mock_release(tmp_path / "v1", "crewline, version 0.1.0-v1")
    tar2, sha2 = _make_mock_release(tmp_path / "v2", "crewline, version 0.1.0-v2")

    # 1. Install v1 using a RELATIVE --prefix; symlink must be absolute and work from any cwd.
    rel_parent = tmp_path / "work"
    rel_parent.mkdir()
    res1 = subprocess.run(
        [str(INSTALL_SH), str(tar1), "--prefix", "./rel-install"],
        cwd=str(rel_parent),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res1.returncode == 0, res1.stderr + res1.stdout
    installed_bin = rel_parent / "rel-install" / "bin" / "crewline"
    assert installed_bin.is_symlink()
    link_target = os.readlink(installed_bin)
    assert os.path.isabs(link_target), f"Expected absolute symlink target, got: {link_target}"
    # Execute from a different directory (tmp_path) to prove relative cwd is not needed.
    ver1 = subprocess.run(
        [str(installed_bin), "--version"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert ver1.stdout.strip() == "crewline, version 0.1.0-v1"
    orig_target = os.path.realpath(installed_bin)

    # 2. Corrupt checksum for v2 -> install fails and preserves working v1 untouched.
    bad_sha = tmp_path / "v2" / "bad.sha256"
    bad_sha.write_text("0" * 64 + f"  {tar2.name}\n", encoding="utf-8")
    res_bad = subprocess.run(
        [str(INSTALL_SH), str(tar2), "--checksum", str(bad_sha), "--prefix", "./rel-install"],
        cwd=str(rel_parent),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_bad.returncode != 0
    assert "SHA-256 校验失败" in res_bad.stderr
    assert os.path.realpath(installed_bin) == orig_target

    # 3. Failure during bin write (unwritable bin dir) cleans up staged release & keeps old entrypoint.
    # Use a non-root check or a bad --bin-dir path where bin_dir is a regular file to trigger atomic flip failure even as root!
    conflict_file = tmp_path / "not_a_dir"
    conflict_file.write_text("blocker", encoding="utf-8")
    releases_dir = rel_parent / "rel-install" / "share" / "crewline" / "releases"
    before_releases = set(releases_dir.iterdir())
    res_fail_bin = subprocess.run(
        [
            str(INSTALL_SH),
            str(tar2),
            "--checksum",
            str(sha2),
            "--bin-dir",
            str(conflict_file),
            "--data-dir",
            str(rel_parent / "rel-install" / "share" / "crewline"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_fail_bin.returncode != 0
    assert set(releases_dir.iterdir()) == before_releases
    assert os.path.realpath(installed_bin) == orig_target

    # 4. Valid upgrade to v2 with relative --prefix succeeds and replaces entrypoint cleanly.
    res2 = subprocess.run(
        [str(INSTALL_SH), str(tar2), "--checksum", str(sha2), "--prefix", "./rel-install"],
        cwd=str(rel_parent),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res2.returncode == 0, res2.stderr + res2.stdout
    ver2 = subprocess.run([str(installed_bin), "--version"], capture_output=True, text=True, check=True)
    assert ver2.stdout.strip() == "crewline, version 0.1.0-v2"
