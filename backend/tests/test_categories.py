"""P2-09 验收：预置品类、自定义品类生命周期（T29/T30/T31）。"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from app.errors import ConflictError, ValidationError
from app.models import Category
from app.services import categories as svc


def _init(session):
    svc.init_builtin_categories(session)


def test_builtin_idempotent_init(session_factory):
    session = session_factory()
    _init(session)
    _init(session)  # 重复启动不重复初始化
    rows = session.execute(select(Category)).scalars().all()
    builtins = [c for c in rows if c.is_builtin]
    assert len(builtins) == 3
    assert {c.system_code for c in builtins} == {"general", "bra", "panties"}
    session.close()


def test_create_and_duplicate_key(session_factory):
    session = session_factory()
    _init(session)
    c1 = svc.create_category(session, "家居服")
    # 首尾空格变体 → 同一规范化键 → 拒绝（T30）
    with pytest.raises(ConflictError):
        svc.create_category(session, "  家居服  ")
    # 大小写变体 → 键一致 → 拒绝
    c3 = svc.create_category(session, "Swim Wear")
    assert c3.name == "Swim Wear"
    with pytest.raises(ConflictError):
        svc.create_category(session, "swim wear")
    with pytest.raises(ConflictError):
        svc.create_category(session, "家居服")
    assert c1.id != c3.id
    session.close()


def test_empty_and_overlong_names(session_factory):
    session = session_factory()
    _init(session)
    with pytest.raises(ValidationError):
        svc.create_category(session, "   ")
    with pytest.raises(ValidationError):
        svc.create_category(session, "x" * 31)
    ok = svc.create_category(session, "x" * 30)
    assert ok.name == "x" * 30
    session.close()


def test_concurrent_same_name_single_winner(session_factory):
    """并发创建同名品类最多成功一条（T30）。"""
    session_factory()
    results: list[str] = []

    def attempt(i: int):
        from app.config import get_config
        from app.db import make_engine, make_session_factory

        engine = make_engine(get_config())
        s = make_session_factory(engine)()
        svc.init_builtin_categories(s)
        try:
            svc.create_category(s, "竞态品类")
            results.append("ok")
        except ConflictError:
            results.append("conflict")
        except Exception as exc:  # noqa: BLE001  唯一键竞争在 SQLite 可能以 IntegrityError 呈现
            results.append(f"integrity:{type(exc).__name__}")
        s.close()
        engine.dispose()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert results.count("ok") == 1, results
    check = session_factory()
    svc.init_builtin_categories(check)
    from app.models import Category

    rows = check.execute(select(Category).where(Category.name == "竞态品类")).scalars().all()
    assert len(rows) == 1
    check.close()


def test_rename_keeps_id_and_refs(session_factory):
    """重命名保持 ID，素材关联继续有效（T31）。"""
    from app.models import Asset

    session = session_factory()
    _init(session)
    cat = svc.create_category(session, "旧名称")
    asset = Asset(sha256="e" * 64, sku="AB-001", category_id=cat.id)
    session.add(asset)
    session.commit()
    old_id = cat.id
    renamed = svc.rename_category(session, cat.id, "新名称")
    assert renamed.id == old_id
    assert renamed.name == "新名称"
    fetched = session.get(Asset, asset.id)
    assert fetched.category_id == old_id
    # 预置品类不可重命名
    general = svc.get_by_system_code(session, "general")
    with pytest.raises(ValidationError):
        svc.rename_category(session, general.id, "万能")
    session.close()


def test_deactivate_rules(session_factory):
    session = session_factory()
    _init(session)
    cat = svc.create_category(session, "季节款")
    svc.set_active(session, cat.id, False)
    # 停用后不可用于新上传，但实体仍在（已有素材不失联）
    with pytest.raises(ValidationError):
        svc.validate_for_upload(session, cat.id)
    assert session.get(Category, cat.id) is not None
    svc.set_active(session, cat.id, True)  # 恢复
    assert svc.validate_for_upload(session, cat.id).id == cat.id
    # 预置不可停用
    for code in ("general", "bra", "panties"):
        preset = svc.get_by_system_code(session, code)
        with pytest.raises(ValidationError):
            svc.set_active(session, preset.id, False)
    session.close()


def test_validate_for_upload_unknown(session_factory):
    session = session_factory()
    _init(session)
    with pytest.raises(ValidationError):
        svc.validate_for_upload(session, None)
    with pytest.raises(Exception):  # noqa: B017  不存在 ID
        svc.validate_for_upload(session, "no-such-id")
    session.close()
