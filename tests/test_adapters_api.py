"""End-to-end HTTP tests for Gemini and DeepSeek API Key adapters via a local HTTP server."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from typing import Any

import pytest

from crewline.domain.capabilities import AgentCapability
from crewline.domain.models import ProviderId, TaskStatus
from crewline.engine.service import CrewlineService


class _MockAPIState:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.fail_status: int | None = None
        self.fail_body: str = ""


def _start_mock_server() -> tuple[ThreadingHTTPServer, _MockAPIState, str]:
    state = _MockAPIState()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            body = json.loads(raw_body) if raw_body else {}
            state.requests.append(
                {
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
            )

            if state.fail_status is not None:
                self.send_response(state.fail_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(state.fail_body.encode("utf-8"))
                return

            if ":generateContent" in self.path:
                resp = {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": "Gemini 分析结论：建议拆分模块并增加单元测试。"}
                                ]
                            }
                        }
                    ]
                }
            elif self.path.endswith("/chat/completions"):
                resp = {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "DeepSeek 分析结论：先保障 SQLite 原子锁，再并行执行。",
                            }
                        }
                    ]
                }
            else:
                self.send_response(404)
                self.end_headers()
                return

            encoded = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, state, f"http://{host}:{port}"


def test_gemini_and_deepseek_api_key_adapters_http_e2e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, state, base_url = _start_mock_server()
    try:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

        service = CrewlineService(tmp_path / ".crewline")
        service.init_workspace()
        service.add_agent(
            "gemini-local",
            ProviderId.GEMINI,
            model="gemini-2.5-flash",
            api_key_env="GEMINI_API_KEY",
            base_url=f"{base_url}/v1beta",
            overwrite=True,
        )
        service.add_agent(
            "deepseek-local",
            ProviderId.DEEPSEEK,
            model="deepseek-chat",
            api_key_env="DEEPSEEK_API_KEY",
            base_url=base_url,
            overwrite=True,
        )

        # Missing env vars -> doctor reports MISSING_ENV and analysis-only capability.
        g_doc = service.doctor("gemini-local")[0]
        d_doc = service.doctor("deepseek-local")[0]
        assert g_doc.ready is False and g_doc.status_label == "MISSING_ENV"
        assert d_doc.ready is False and d_doc.status_label == "MISSING_ENV"
        assert g_doc.capabilities == [AgentCapability.ANALYSIS]
        assert d_doc.capabilities == [AgentCapability.ANALYSIS]

        # Set keys in env only (never written to config).
        g_secret = "AIzaSyLocalTestGeminiKey1234567890"
        d_secret = "sk-localtestdeepseekkey1234567890"
        monkeypatch.setenv("GEMINI_API_KEY", g_secret)
        monkeypatch.setenv("DEEPSEEK_API_KEY", d_secret)

        assert service.doctor("gemini-local")[0].ready is True
        assert service.doctor("deepseek-local")[0].ready is True

        t_gemini = service.create_task("Gemini 架构评审", "评估状态机拆分", tmp_path, "gemini-local")
        t_deepseek = service.create_task("DeepSeek 并发分析", "评估并发锁设计", tmp_path, "deepseek-local")

        outcomes = service.run_tasks([t_gemini.id, t_deepseek.id], max_workers=2)
        assert all(o.succeeded for o in outcomes)

        assert service.get_task_detail(t_gemini.id).task.status == TaskStatus.IN_REVIEW
        assert service.get_task_detail(t_deepseek.id).task.status == TaskStatus.IN_REVIEW

        # Verify actual HTTP requests sent.
        gemini_reqs = [r for r in state.requests if ":generateContent" in r["path"]]
        deepseek_reqs = [r for r in state.requests if r["path"] == "/chat/completions"]
        assert len(gemini_reqs) == 1
        assert gemini_reqs[0]["headers"]["x-goog-api-key"] == g_secret
        assert g_secret not in gemini_reqs[0]["path"]
        assert len(deepseek_reqs) == 1
        assert deepseek_reqs[0]["headers"]["authorization"] == f"Bearer {d_secret}"

        # Verify logs contain analysis output and NEVER leak keys.
        _, g_log = service.read_logs(t_gemini.id)
        _, d_log = service.read_logs(t_deepseek.id)
        assert "Gemini 分析结论" in g_log and g_secret not in g_log
        assert "DeepSeek 分析结论" in d_log and d_secret not in d_log

        # Verify HTTP error containing a secret returns card to todo & redacts secret.
        state.fail_status = 401
        state.fail_body = json.dumps({"error": f"invalid key {d_secret}"})
        t_fail = service.create_task("错误脱敏验证", "触发 401", tmp_path, "deepseek-local")
        fail_outcome = service.run_tasks([t_fail.id])[0]
        assert fail_outcome.succeeded is False
        failed_task = service.get_task_detail(t_fail.id).task
        assert failed_task.status == TaskStatus.TODO
        assert failed_task.last_error is not None
        assert d_secret not in failed_task.last_error
        assert "[REDACTED]" in failed_task.last_error
    finally:
        server.shutdown()
        server.server_close()
