"""Tests for credential reference enforcement and secret redaction in DB & logs."""

from pathlib import Path
import sqlite3

import pytest

from crewline.config.store import load_config
from crewline.domain.errors import CredentialSecurityError
from crewline.domain.models import ProviderId
from crewline.engine.service import CrewlineService
from crewline.security.redactor import SecretRedactor, validate_env_var_name


@pytest.mark.parametrize(
    "bad_value",
    [
        "sk-live-secret1234567890",
        "AIzaSyDummyKeyValue1234567890123456",
        "Bearer eyJhbGciOiJIUzI1NiJ9",
        "gemini_api_key",
        "API KEY WITH SPACE",
        "GEMINI_API_KEY=secret",
    ],
)
def test_validate_env_var_name_rejects_raw_secrets(bad_value: str) -> None:
    with pytest.raises(CredentialSecurityError):
        validate_env_var_name(bad_value)


def test_validate_env_var_name_accepts_valid_refs() -> None:
    assert validate_env_var_name("GEMINI_API_KEY") == "GEMINI_API_KEY"
    assert validate_env_var_name("DEEPSEEK_API_KEY_PROD_2") == "DEEPSEEK_API_KEY_PROD_2"


def test_config_rejects_forbidden_secret_keys(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[crewline]\nversion = "0.1"\n\n'
        '[agents.bad]\nprovider = "gemini"\nmodel = "gemini-2.5-flash"\n'
        'auth_method = "api_key"\napi_key = "AIzaSyNotAllowed1234567890123456"\n',
        encoding="utf-8",
    )
    with pytest.raises(CredentialSecurityError):
        load_config(cfg_path)


def test_add_agent_rejects_raw_api_key_in_env_option(tmp_path: Path) -> None:
    service = CrewlineService(tmp_path / ".crewline")
    service.init_workspace()
    with pytest.raises(CredentialSecurityError):
        service.add_agent(
            name="bad-ds",
            provider=ProviderId.DEEPSEEK,
            api_key_env="sk-1234567890abcdef",
        )


def test_redactor_scrubs_env_values_and_patterns() -> None:
    secret_val = "my-super-secret-value-998877"
    redactor = SecretRedactor(
        env_var_names=["TEST_SECRET_ENV"],
        environ={"TEST_SECRET_ENV": secret_val},
    )
    sample = (
        f"header Authorization: Bearer tok_abcdef123456, "
        f"x-goog-api-key: AIzaSyTestKey123456789012345678, "
        f"raw={secret_val}, openai=sk-abcdef1234567890"
    )
    cleaned = redactor.redact_str(sample)
    assert secret_val not in cleaned
    assert "tok_abcdef123456" not in cleaned
    assert "AIzaSyTestKey" not in cleaned
    assert "sk-abcdef1234567890" not in cleaned
    assert "[REDACTED]" in cleaned


def test_secrets_never_enter_db_or_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaked_secret = "super-sensitive-deepseek-key-xyz987654"
    monkeypatch.setenv("DEEPSEEK_API_KEY", leaked_secret)

    # Create a fake CLI that deliberately prints the secret to stdout and exits 1.
    fake_cli = tmp_path / "fake-codex"
    fake_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "print('DEBUG leaked=' + os.environ.get('DEEPSEEK_API_KEY', ''))\n"
        "print('Authorization: Bearer secret_token_123456789')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)

    service = CrewlineService(tmp_path / ".crewline")
    service.init_workspace()
    service.add_agent(
        name="leaky-cli",
        provider=ProviderId.CODEX,
        cli_bin=str(fake_cli),
        overwrite=True,
    )
    task = service.create_task(
        title="脱敏校验任务",
        description="确保密钥不会进入日志和数据库",
        repo_path=tmp_path,
        assigned_agent="leaky-cli",
    )
    outcomes = service.run_tasks([task.id])
    assert outcomes[0].succeeded is False

    # Verify config.toml has no secret.
    config_text = service.config_path.read_text(encoding="utf-8")
    assert leaked_secret not in config_text

    # Verify log file has no secret.
    _, log_text = service.read_logs(task.id)
    assert leaked_secret not in log_text
    assert "secret_token_123456789" not in log_text
    assert "[REDACTED]" in log_text

    # Verify raw SQLite database content has no secret.
    conn = sqlite3.connect(str(service.db_path))
    try:
        rows = conn.execute("SELECT * FROM tasks").fetchall() + conn.execute(
            "SELECT * FROM runs"
        ).fetchall()
    finally:
        conn.close()
    serialized = str(rows)
    assert leaked_secret not in serialized
    assert "secret_token_123456789" not in serialized
