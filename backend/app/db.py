"""数据库引擎与会话。SQLite 启用 WAL 与外键（计划 3.2）。"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import AppConfig


def _set_sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def make_engine(config: AppConfig) -> Engine:
    url = make_url(config.database_url)
    if url.database in (None, "", ":memory:"):
        # 内存库（测试）共享同一连接，否则表结构不互通
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(config.database_url)
    event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_session(config: AppConfig) -> Session:
    return make_session_factory(make_engine(config))()


def run_migrations(config: AppConfig) -> None:
    """执行 alembic upgrade head（空库可升级，幂等；计划 P1-02）。"""
    import os
    from pathlib import Path

    from alembic import command
    from alembic.config import Config as AlembicConfig

    backend_dir = Path(__file__).resolve().parent.parent
    alembic_cfg = AlembicConfig(str(backend_dir / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(backend_dir / "migrations"))
    old = os.environ.get("AUTOEDITOR_DATABASE_URL")
    os.environ["AUTOEDITOR_DATABASE_URL"] = config.database_url
    try:
        command.upgrade(alembic_cfg, "head")
    finally:
        if old is None:
            os.environ.pop("AUTOEDITOR_DATABASE_URL", None)
        else:
            os.environ["AUTOEDITOR_DATABASE_URL"] = old
