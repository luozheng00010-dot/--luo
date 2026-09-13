"""P1-05 验收：密钥不进数据库与日志；本地服务校验来源及会话令牌（T24）。"""

from __future__ import annotations

import json
import logging

from app.api.main import ensure_session_token
from app.logging_setup import JsonFormatter, redact_obj
from app.services import secrets, settings_service


def test_secret_stored_in_keyring_not_db(session_factory):
    session = session_factory()
    secrets.set_secret("vision_api_key", "sk-very-secret-123")
    assert secrets.get_secret("vision_api_key") == "sk-very-secret-123"
    # settings 表中只允许存引用名，不存明文
    settings_service.set_setting(session, "vision_api_key_ref", "vision_api_key")
    raw = list(session.execute(__import__("sqlalchemy").text("SELECT key, value_json FROM settings")))
    for _key, value in raw:
        assert "sk-very-secret-123" not in json.dumps(value, ensure_ascii=False)


def test_log_redaction():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1,
        msg="调用完成", args=(), exc_info=None,
    )
    record.api_key = "sk-abc123"
    record.session_token = "tok-xyz"
    record.job_id = "job-1"
    out = json.loads(formatter.format(record))
    assert out["api_key"] == "***"
    assert out["session_token"] == "***"
    assert out["job_id"] == "job-1"


def test_redact_obj_nested():
    data = redact_obj({"Authorization": "Bearer x", "nested": {"password": "p", "ok": 1}})
    assert data["Authorization"] == "***"
    assert data["nested"]["password"] == "***"
    assert data["nested"]["ok"] == 1


def test_middleware_requires_token(client, config):
    # 无令牌 → 401
    bad = client.__class__(client.app, base_url="http://127.0.0.1")
    resp = bad.get("/api/settings")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_middleware_rejects_wrong_host(client, config):
    bad = client.__class__(client.app, base_url="http://evil.example.com")
    resp = bad.get("/healthz")
    assert resp.status_code == 401
    assert resp.json()["error"] == "forbidden_host"


def test_middleware_rejects_foreign_origin(client, config):
    resp = client.get("/healthz", headers={"Origin": "http://evil.example.com"})
    assert resp.status_code == 401
    assert resp.json()["error"] == "forbidden_origin"


def test_healthz_and_settings_with_token(client, session_factory):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"

    resp = client.put("/api/settings", json={"key": "render.crf", "value": 20})
    assert resp.status_code == 200
    resp = client.get("/api/settings")
    assert resp.json()["settings"]["render.crf"] == 20


def test_session_token_persisted(config):
    token1 = ensure_session_token(config)
    token2 = ensure_session_token(config)
    assert token1 == token2
    assert len(token1) >= 32
    # 0600 权限：其他用户不可读
    mode = config.session_token_file.stat().st_mode & 0o777
    assert mode == 0o600
