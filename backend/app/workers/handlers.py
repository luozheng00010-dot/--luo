"""任务处理器注册表：把 job_type 绑定到服务实现。"""

from __future__ import annotations

from app.workers.registry import register_handler


def _import_assets_handler(session, payload, worker, job_id):

    from app.config import get_config
    from app.models import Job
    from app.services.ingest.import_service import run_import_job

    job = session.get(Job, job_id)
    return run_import_job(session, job, get_config())


def _prepare_media_handler(session, payload, worker, job_id):
    import time

    from sqlalchemy import select

    from app.config import get_config
    from app.models import Asset
    from app.services.analysis.frames import compute_usable_range, extract_frames

    asset = session.execute(
        select(Asset).where(Asset.id == payload["asset_id"])
    ).scalar_one_or_none()
    if asset is None:
        return {"skipped": "asset_missing"}
    config = get_config()
    frames = extract_frames(asset, config)
    usable = compute_usable_range(asset)
    return {
        "asset_id": asset.id,
        "frames": [str(p) for p in frames],
        "usable_start_ms": usable.start_ms,
        "usable_end_ms": usable.end_ms,
        "black_flagged": usable.black_flagged,
        "extract_ms": int(time.time() * 1000),
    }


def register_all() -> None:
    register_handler("import_assets", _import_assets_handler)
    register_handler("prepare_media", _prepare_media_handler)


register_all()
