"""P1 测试公共夹具：独立数据目录 + 迁移到头 + 带令牌的 API 客户端。"""

from __future__ import annotations

import os
import pathlib

import pytest

os.environ.setdefault("AUTOEDITOR_ENABLE_FALLBACK_SECRETS", "1")

from app.config import AppConfig  # noqa: E402
from app.services import secrets as secrets_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _fallback_secrets(monkeypatch):
    """测试中密钥走内存后备，不碰真实钥匙串。"""
    monkeypatch.setattr(secrets_mod, "_use_fallback", True)
    secrets_mod._FALLBACK.clear()
    yield
    secrets_mod._FALLBACK.clear()


@pytest.fixture()
def config(tmp_path: pathlib.Path) -> AppConfig:
    cfg = AppConfig(data_dir=tmp_path)
    cfg.ensure_dirs()
    return cfg


@pytest.fixture()
def session_factory(config: AppConfig):
    from alembic import command
    from alembic.config import Config as AlembicConfig

    from app.db import make_engine, make_session_factory

    migrations_dir = pathlib.Path(__file__).resolve().parent.parent / "migrations"
    alembic_cfg = AlembicConfig(str(migrations_dir.parent / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(migrations_dir))
    os.environ["AUTOEDITOR_DATABASE_URL"] = config.database_url
    command.upgrade(alembic_cfg, "head")
    engine = make_engine(config)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture()
def client(config: AppConfig):
    from fastapi.testclient import TestClient

    from app.api.main import create_app, ensure_session_token

    token = ensure_session_token(config)
    app = create_app(config)
    tc = TestClient(app, base_url="http://127.0.0.1")
    tc.headers.update({"X-Session-Token": token})
    yield tc
