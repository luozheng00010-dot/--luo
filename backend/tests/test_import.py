"""P2-01/02/08 验收：导入校验、媒体探测、SHA-256 去重、冲突处理。

对应必测场景 T26（必填拒绝）、T27（货号原样与精确查询）、T01（重复导入单实体）、
T32（重复文件不同属性 → 冲突待处理）、T04 部分（损坏/零字节不阻塞批任务）、
4.1 第 5 条（超过 5 秒标记不静默截断）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import Asset, AssetMetadataChange, AssetSource, ImportItem
from app.services import categories as cat_svc
from app.workers.worker import Worker
from tests.media import make_clip


@pytest.fixture()
def categories(session_factory):
    session = session_factory()
    cat_svc.init_builtin_categories(session)
    bra = cat_svc.get_by_system_code(session, "bra")
    general = cat_svc.get_by_system_code(session, "general")
    session.close()
    return {"bra": bra.id, "general": general.id}


def _import(client, tmp_path, items):
    return client.post("/api/assets/import", json={"items": items})


def test_import_validation_and_pipeline(client, session_factory, categories, tmp_path):
    """T26/T27/T01：必填拒绝定位到行；货号原样；同名不同文件去重为单实体。"""
    clip1 = make_clip(tmp_path / "src" / "clipA.mp4", 1.2, "0x35507a")
    clip1_renamed = tmp_path / "src" / "renamed_copy.mp4"
    clip1_renamed.write_bytes(clip1.read_bytes())  # 相同内容不同文件名（T01）

    resp = _import(client, tmp_path, [
        {"import_item_id": "i1", "source_path": str(clip1), "sku": " 00123 ",
         "category_id": categories["bra"]},
        {"import_item_id": "i2", "source_path": str(clip1_renamed), "sku": "00123",
         "category_id": categories["bra"]},
        {"import_item_id": "i3", "source_path": str(clip1), "sku": None,
         "category_id": categories["bra"]},  # 缺货号 → 逐行报错（T26）
        {"import_item_id": "i4", "source_path": str(clip1), "sku": "AB-001",
         "category_id": None},  # 缺品类
        {"import_item_id": "i5", "source_path": str(clip1), "sku": "x" * 65,
         "category_id": categories["bra"]},  # 超长
    ])
    assert resp.status_code == 200
    body = resp.json()
    invalid = {e["import_item_id"]: e for e in body["invalid_items"]}
    assert set(invalid) == {"i3", "i4", "i5"}
    assert body["created"] is True
    job_id = body["job_id"]

    # 幂等键：相同清单重复提交不创建新任务
    resp2 = _import(client, tmp_path, [
        {"import_item_id": "i1", "source_path": str(clip1), "sku": " 00123 ",
         "category_id": categories["bra"]},
        {"import_item_id": "i2", "source_path": str(clip1_renamed), "sku": "00123",
         "category_id": categories["bra"]},
        {"import_item_id": "i3", "source_path": str(clip1), "sku": None, "category_id": categories["bra"]},
        {"import_item_id": "i4", "source_path": str(clip1), "sku": "AB-001", "category_id": None},
        {"import_item_id": "i5", "source_path": str(clip1), "sku": "x" * 65,
         "category_id": categories["bra"]},
    ])
    assert resp2.json()["created"] is False
    assert resp2.json()["job_id"] == job_id

    # Worker 处理导入任务
    worker = Worker(session_factory, "w-import", lease_seconds=60)
    worker.run(max_jobs=1, exit_when_empty=True)

    session = session_factory()
    # 两个有效项 → 一个素材实体（哈希去重）+ 两条来源记录（T01）
    assets = session.execute(select(Asset)).scalars().all()
    assert len(assets) == 1
    asset = assets[0]
    assert asset.sku == "00123", "货号应去除首尾空格但保留前导零（T27）"
    assert asset.category_id == categories["bra"]
    sources = session.execute(select(AssetSource)).scalars().all()
    assert len(sources) == 2
    assert asset.managed_path is not None
    assert asset.over_duration is False
    # 导入项状态：1 imported + 1 duplicate（同内容同属性）+ 3 invalid（T01）
    items = session.execute(select(ImportItem)).scalars().all()
    statuses = [i.status for i in items]
    assert statuses.count("imported") == 1
    assert statuses.count("duplicate") == 1
    assert statuses.count("invalid") == 3
    session.close()

    # GET /api/assets?sku=00123 精确筛选（T27）
    resp = client.get("/api/assets", params={"sku": "00123"})
    assert resp.json()["total"] == 1
    resp = client.get("/api/assets", params={"sku": "00124"})
    assert resp.json()["total"] == 0
    resp = client.get("/api/assets", params={"category_id": categories["bra"]})
    assert resp.json()["total"] == 1


def test_conflicting_metadata_on_duplicate(client, session_factory, categories, tmp_path):
    """T32：重复导入相同文件但货号不同 → 冲突待处理，不静默覆盖。"""
    clip = make_clip(tmp_path / "src" / "clipB.mp4", 0.8, "0x7a3535")

    resp = _import(client, tmp_path, [
        {"import_item_id": "c1", "source_path": str(clip), "sku": "AB-001",
         "category_id": categories["bra"]},
    ])
    Worker(session_factory, "w1", lease_seconds=60).run(max_jobs=1, exit_when_empty=True)

    _import(client, tmp_path, [
        {"import_item_id": "c2", "source_path": str(clip), "sku": "AB-002",
         "category_id": categories["general"]},
    ])
    Worker(session_factory, "w2", lease_seconds=60).run(max_jobs=1, exit_when_empty=True)

    session = session_factory()
    assets = session.execute(select(Asset)).scalars().all()
    assert len(assets) == 1, "文件去重：不复制实体绕过去重"
    assert assets[0].sku == "AB-001", "原属性不被静默覆盖"
    items = session.execute(select(ImportItem)).scalars().all()
    conflict = [i for i in items if i.status == "conflict"]
    assert len(conflict) == 1 and conflict[0].error_code == "metadata_conflict"
    conflict_id = conflict[0].id
    session.close()

    # 用户显式选择：修改原素材属性（记录审计）
    client.post(f"/api/assets/import-items/{conflict_id}/resolve-conflict",
                json={"use_existing": False, "new_sku": "AB-002"})
    assert resp.status_code == 200
    session = session_factory()
    asset = session.execute(select(Asset)).scalar_one()
    assert asset.sku == "AB-002"
    changes = session.execute(select(AssetMetadataChange)).scalars().all()
    assert len(changes) == 1
    assert changes[0].old_values_json == {"sku": "AB-001", "category_id": assets[0].category_id}
    assert changes[0].new_values_json["sku"] == "AB-002"
    session.close()


def test_broken_files_do_not_block_batch(client, session_factory, categories, tmp_path):
    """T04 部分：零字节/损坏文件逐项失败，不阻塞其他素材。"""
    good = make_clip(tmp_path / "src" / "good.mp4", 0.6, "0x357a4b")
    zero = tmp_path / "src" / "zero.mp4"
    zero.write_bytes(b"")
    corrupt = tmp_path / "src" / "corrupt.mp4"
    corrupt.write_bytes(b"not a video at all" * 10)

    resp = _import(client, tmp_path, [
        {"import_item_id": "z1", "source_path": str(zero), "sku": "SK-1",
         "category_id": categories["bra"]},
        {"import_item_id": "bad1", "source_path": str(corrupt), "sku": "SK-2",
         "category_id": categories["bra"]},
        {"import_item_id": "ok1", "source_path": str(good), "sku": "SK-3",
         "category_id": categories["bra"]},
    ])
    assert resp.status_code == 200
    Worker(session_factory, "w1", lease_seconds=60).run(max_jobs=1, exit_when_empty=True)

    session = session_factory()
    items = session.execute(select(ImportItem)).scalars().all()
    status_by_sku = {i.sku: (i.status, i.error_code) for i in items}
    assert status_by_sku["SK-1"] == ("failed", "zero_byte_file")
    assert status_by_sku["SK-2"] == ("failed", "probe_failed")
    assert status_by_sku["SK-3"][0] == "imported"
    assets = session.execute(select(Asset)).scalars().all()
    assert len(assets) == 1
    session.close()


def test_over_duration_flagged_not_truncated(client, session_factory, categories, tmp_path):
    """4.1 第 5 条：超过 5 秒标记 over_duration，不静默截断。"""
    long_clip = make_clip(tmp_path / "src" / "long.mp4", 6.0, "0x5a357a")
    _import(client, tmp_path, [
        {"import_item_id": "L1", "source_path": str(long_clip), "sku": "LONG-1",
         "category_id": categories["general"]},
    ])
    Worker(session_factory, "w1", lease_seconds=60).run(max_jobs=1, exit_when_empty=True)

    session = session_factory()
    asset = session.execute(select(Asset)).scalar_one()
    assert asset.over_duration is True
    assert asset.duration_ms >= 5800, "原素材时长完整保留"
    session.close()


def test_import_item_status_endpoint(client, session_factory, categories, tmp_path):
    clip = make_clip(tmp_path / "src" / "clipX.mp4", 0.5, "0x357a76")
    resp = _import(client, tmp_path, [
        {"import_item_id": "x1", "source_path": str(clip), "sku": "X-1",
         "category_id": categories["bra"]},
    ])
    job_id = resp.json()["job_id"]
    Worker(session_factory, "w1", lease_seconds=60).run(max_jobs=1, exit_when_empty=True)
    resp = client.get(f"/api/assets/import/{job_id}/items")
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "imported"
    assert items[0]["asset_id"] is not None
