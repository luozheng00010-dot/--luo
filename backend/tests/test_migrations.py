"""P1-02 验收：空库可升级；外键、唯一键生效；备份后可恢复。"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Asset, Category, UsageEvent


def test_empty_db_upgrade_and_tables(session_factory):
    session = session_factory()
    from sqlalchemy import inspect

    from app.models import Base

    tables = set(inspect(session.bind).get_table_names())
    expected = {t.lower() for t in Base.metadata.tables}
    assert expected <= tables, f"缺表: {expected - tables}"
    session.close()


def test_foreign_keys_enforced(session_factory):
    session = session_factory()
    # category_id 外键：引用不存在的品类应被拒绝（PRAGMA foreign_keys=ON）
    asset = Asset(sha256="a" * 64, category_id="nonexistent")
    session.add(asset)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_sha256_unique(session_factory):
    session = session_factory()
    session.add(Asset(sha256="b" * 64))
    session.commit()
    session.add(Asset(sha256="b" * 64))
    with pytest.raises(IntegrityError):
        session.commit()


def test_usage_event_idempotent_unique(session_factory):
    """usage_events revision_id+asset_id 唯一（幂等记账，计划 5.3）。"""
    from app.models import EditRevision, Project

    session = session_factory()
    project = Project(name="p", script="文案")
    session.add(project)
    session.commit()
    revision = EditRevision(project_id=project.id, revision=1)
    session.add(revision)
    asset = Asset(sha256="c" * 64)
    session.add(asset)
    session.commit()
    session.add(UsageEvent(revision_id=revision.id, asset_id=asset.id, success_sequence=1))
    session.commit()
    session.add(UsageEvent(revision_id=revision.id, asset_id=asset.id, success_sequence=1))
    with pytest.raises(IntegrityError):
        session.commit()


def test_backup_restore(session_factory, config, tmp_path):
    """通过 SQLite 在线备份接口获得一致性快照（计划 6.1 / 10）。"""
    session = session_factory()
    session.add(Category(name="通用", name_key="通用", system_code="general", is_builtin=True))
    session.commit()

    src_path = config.db_path
    dst_path = tmp_path / "backup.sqlite3"
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    src.backup(dst)  # 在线备份 API
    dst.close()
    src.close()

    check = sqlite3.connect(dst_path)
    tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    check.close()
    assert "assets" in tables and "categories" in tables


@pytest.mark.parametrize(
    "sku",
    ["00123", "AB-001", "  spaced  ", "x" * 64],
)
def test_sku_text_preserved(session_factory, sku):
    """货号为文本：保留前导零、大小写与连字符（计划 4.6.1/T27）。"""
    session = session_factory()
    asset = Asset(sha256="d" * 64, sku=sku)
    session.add(asset)
    session.commit()
    fetched = session.get(Asset, asset.id)
    assert fetched.sku == sku
    session.close()
