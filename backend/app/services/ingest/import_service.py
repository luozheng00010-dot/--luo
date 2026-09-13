"""导入服务：逐文件属性校验、去重、冲突处理（计划 4.1 / 4.6 / P2-01/02/08/10）。

规则要点：
- 每个导入项用稳定的导入项 ID 绑定属性，不按文件名绑定（T28）；
- SHA-256 相同的文件只建一个素材实体，保留来源路径映射（T01）；
- 重复文件属性不一致 → 导入项进入 conflict 待处理状态，不静默覆盖（T32）；
- 超过 5 秒标记 over_duration，由用户处置，不静默截断（4.1 第 5 条）；
- 校验失败仅标记该行，不影响其他行（T26）。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Asset, AssetMetadataChange, AssetSource, Category, ImportItem, Job
from app.services import categories as cat_svc
from app.services.ingest.probe import copy_to_managed, probe_video, sha256_of

logger = logging.getLogger(__name__)

MAX_DURATION_MS = 5000  # 计划 1.1：单条素材 5 秒以下


def validate_import_item(session: Session, item: ImportItem) -> str | None:
    """校验单条导入项的必填属性；返回错误码或 None。不抛异常，逐行报告（T26）。"""
    if item.sku is None or not item.sku.strip():
        return "sku_required"
    try:
        cat_svc.validate_sku(item.sku)
    except Exception:  # noqa: BLE001  统一映射为错误码
        return "sku_invalid"
    if item.category_id is None:
        return "category_required"
    category = session.get(Category, item.category_id)
    if category is None:
        return "category_not_found"
    if not category.is_active:
        return "category_inactive"
    if not Path(item.source_path).exists():
        return "source_missing"
    return None


def run_import_job(session: Session, job: Job, config=None) -> dict:
    """导入任务处理器：逐项校验 → 探测 → 哈希去重 → 建素材/冲突/异常。"""
    from app.config import AppConfig, get_config

    config = config or get_config()
    assert isinstance(config, AppConfig)
    items = session.execute(
        select(ImportItem).where(ImportItem.job_id == job.id).order_by(ImportItem.created_at)
    ).scalars().all()
    if not items:
        return {"imported": 0, "duplicates": 0, "conflicts": 0, "failed": 0}

    counts = {"imported": 0, "duplicates": 0, "conflicts": 0, "failed": 0}
    total = len(items)
    for idx, item in enumerate(items):
        error_code = validate_import_item(session, item)
        if error_code:
            item.status = "invalid"
            item.error_code = error_code
            counts["failed"] += 1
            logger.info("import_item_invalid", extra={"job_id": job.id, "item": item.id, "code": error_code})
            continue

        path = Path(item.source_path)
        try:
            # 异常文件：零字节在哈希后判空，探测失败标记
            if path.stat().st_size == 0:
                item.status = "failed"
                item.error_code = "zero_byte_file"
                counts["failed"] += 1
                continue
            probe = probe_video(path)
        except Exception as exc:  # noqa: BLE001  单文件失败不中断批任务（P2-01）
            item.status = "failed"
            item.error_code = "probe_failed"
            session.commit()
            logger.info("import_item_failed", extra={"job_id": job.id, "item": item.id, "err": str(exc)[:120]})
            counts["failed"] += 1
            continue

        digest = sha256_of(path)
        existing = session.execute(
            select(Asset).where(Asset.sha256 == digest)
        ).scalar_one_or_none()

        if existing is not None:
            # 文件去重：同一文件只一个素材实体（T01）；属性不一致 → 冲突待处理（T32）
            new_sku = cat_svc.validate_sku(item.sku)
            same_attr = (
                existing.sku == new_sku
                and (existing.category_id or None) == (item.category_id or None)
            )
            src = AssetSource(asset_id=existing.id, source_path=str(path))
            session.add(src)
            if same_attr:
                item.status = "duplicate"
                item.asset_id = existing.id
                item.error_code = None
                counts["duplicates"] += 1
            else:
                item.status = "conflict"
                item.asset_id = existing.id
                item.error_code = "metadata_conflict"
                counts["conflicts"] += 1
            session.commit()
            continue

        # 新素材：托管复制 + 建实体
        asset = Asset(
            sha256=digest,
            sku=cat_svc.validate_sku(item.sku),
            category_id=item.category_id,
            duration_ms=probe.duration_ms,
            fps_num=probe.fps_num,
            fps_den=probe.fps_den,
            width=probe.width,
            height=probe.height,
            rotation=probe.rotation,
            has_audio=probe.has_audio,
            over_duration=probe.duration_ms > MAX_DURATION_MS,
        )
        session.add(asset)
        session.flush()
        try:
            managed = copy_to_managed(path, config.data_dir / "library", asset.id)
            asset.managed_path = str(managed)
            asset.source_mode = "managed"
        except Exception as exc:  # noqa: BLE001  空间不足等
            session.rollback()
            fresh_item = session.get(ImportItem, item.id)
            fresh_item.status = "failed"
            fresh_item.error_code = "copy_failed"
            counts["failed"] += 1
            logger.info("import_copy_failed", extra={"job_id": job.id, "item": item.id, "err": str(exc)[:120]})
            continue
        session.add(AssetSource(asset_id=asset.id, source_path=str(path)))
        item.status = "imported"
        item.asset_id = asset.id
        item.error_code = None
        counts["imported"] += 1
        # 抽帧/缩略图/有效时段为独立后台任务（P2-03）
        from app.workers import queue as job_queue

        job_queue.enqueue(
            session, "prepare_media", {"asset_id": asset.id},
            idempotency_key=f"prepare_media:{asset.id}",
        )
        if probe.duration_ms > MAX_DURATION_MS:
            logger.info(
                "import_over_duration",
                extra={"job_id": job.id, "item": item.id, "duration_ms": probe.duration_ms},
            )
        session.commit()
        if job.id:
            from app.workers.queue import mark_progress

            mark_progress(session, job.id, job.lease_owner or "", "import", (idx + 1) / total)

    return counts


def resolve_conflict(session: Session, item_id: str, *, use_existing: bool, new_sku: str | None = None,
                     new_category_id: str | None = None) -> ImportItem:
    """处理重复导入的属性冲突：沿用已有属性，或显式修改原素材属性（4.6.2 第 6 条）。"""
    item = session.get(ImportItem, item_id)
    if item is None or item.status != "conflict":
        from app.errors import NotFoundError

        raise NotFoundError("冲突导入项不存在")
    asset = session.get(Asset, item.asset_id)
    if use_existing:
        item.status = "duplicate"
        item.error_code = None
    else:
        old = {"sku": asset.sku, "category_id": asset.category_id}
        if new_sku is not None:
            asset.sku = cat_svc.validate_sku(new_sku)
        if new_category_id is not None:
            cat_svc.validate_for_upload(session, new_category_id)
            asset.category_id = new_category_id
        session.add(
            AssetMetadataChange(
                asset_id=asset.id,
                old_values_json=old,
                new_values_json={"sku": asset.sku, "category_id": asset.category_id},
            )
        )
        item.status = "duplicate"
        item.error_code = None
    session.commit()
    return item


def relocate_missing_source(session: Session, asset_id: str, new_path: str) -> AssetSource | None:
    """引用失联后重新定位（P2-02 验收点）。"""
    src = session.execute(
        select(AssetSource).where(AssetSource.asset_id == asset_id).order_by(
            AssetSource.last_seen_at.desc()
        )
    ).scalars().first()
    if src is None:
        return None
    src.source_path = str(Path(new_path).resolve())
    from app.models import utcnow

    src.last_seen_at = utcnow()
    session.commit()
    return src


def cleanup_managed_copy(asset_id: str) -> None:
    """测试辅助：删除托管副本。"""
    from app.config import get_config

    managed_dir = get_config().data_dir / "library"
    for f in managed_dir.glob(f"{asset_id}*"):
        f.unlink(missing_ok=True)
    if not any(managed_dir.iterdir()):
        shutil.rmtree(managed_dir, ignore_errors=True)
