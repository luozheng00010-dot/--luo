"""API 路由（P1 范围：健康、设置、密钥、任务）。

后续阶段在此模块内扩展 assets/projects/storyboard/render 等接口。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFoundError, ValidationError
from app.models import ImportItem, Job, ProviderCall
from app.services import secrets, settings_service

router = APIRouter(prefix="/api")


def get_db(request: Request) -> Session:
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


# ---- 设置 ----


class SettingIn(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    value: object = None


class SecretIn(BaseModel):
    ref: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    value: str = Field(min_length=1, max_length=4096)


@router.get("/settings")
def list_settings(db: Session = Depends(get_db)) -> dict:
    return {"settings": settings_service.all_settings(db)}


@router.put("/settings")
def put_setting(body: SettingIn, db: Session = Depends(get_db)) -> dict:
    settings_service.set_setting(db, body.key, body.value)
    return {"key": body.key, "value": body.value}


@router.put("/secrets")
def put_secret(body: SecretIn) -> dict:
    """密钥只进钥匙串；数据库/设置中仅保存引用名。"""
    secrets.set_secret(body.ref, body.value)
    return {"ref": body.ref, "stored": True}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError("任务不存在")
    return {
        "id": job.id,
        "type": job.type,
        "state": job.state,
        "stage": job.stage,
        "progress": job.progress,
        "attempt": job.attempt,
        "error_code": job.error_code,
        "error_detail": job.error_detail,
        "result": job.result_json,
    }


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, db: Session = Depends(get_db)) -> dict:
    from app.workers import queue as q

    ok = q.request_cancel(db, job_id)
    if not ok:
        raise NotFoundError("任务不存在或已结束")
    job = db.get(Job, job_id)
    return {"id": job_id, "state": job.state}


@router.get("/jobs/{job_id}/calls")
def job_provider_calls(job_id: str, db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(ProviderCall).where(ProviderCall.job_id == job_id).order_by(ProviderCall.created_at)
    ).scalars().all()
    return {
        "calls": [
            {
                "provider": r.provider,
                "model": r.model,
                "usage": r.usage_json,
                "estimated_cost": r.estimated_cost,
                "currency": r.currency,
                "status": r.status,
            }
            for r in rows
        ]
    }


# ---- 品类管理（P2-09 / 计划 4.6.3 / T29-T31） ----


@router.get("/categories")
def get_categories(include_inactive: bool = False, db: Session = Depends(get_db)) -> dict:
    from app.services import categories as svc

    return {
        "categories": [
            {
                "id": c.id,
                "name": c.name,
                "system_code": c.system_code,
                "is_builtin": c.is_builtin,
                "is_active": c.is_active,
            }
            for c in svc.list_categories(db, include_inactive=include_inactive)
        ]
    }


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class CategoryPatch(BaseModel):
    name: str | None = None
    is_active: bool | None = None


@router.post("/categories", status_code=201)
def post_category(body: CategoryIn, db: Session = Depends(get_db)) -> dict:
    from app.services import categories as svc

    c = svc.create_category(db, body.name)  # 重名 → 409（计划 6.1）
    return {"id": c.id, "name": c.name, "is_builtin": c.is_builtin, "is_active": c.is_active}


@router.patch("/categories/{category_id}")
def patch_category(category_id: str, body: CategoryPatch, db: Session = Depends(get_db)) -> dict:
    from app.services import categories as svc

    if body.name is not None:
        c = svc.rename_category(db, category_id, body.name)
    elif body.is_active is not None:
        c = svc.set_active(db, category_id, body.is_active)
    else:
        from app.errors import NotFoundError, ValidationError

        raise ValidationError("无变更字段")
    return {"id": c.id, "name": c.name, "is_builtin": c.is_builtin, "is_active": c.is_active}


# ---- 素材导入（P2-01/08 / 计划 4.6.2 / T26-T28, T32） ----


class ImportItemIn(BaseModel):
    import_item_id: str = Field(min_length=1, max_length=64, description="客户端稳定导入项 ID")
    source_path: str = Field(min_length=1)
    sku: str | None = None
    category_id: str | None = None


class ImportIn(BaseModel):
    items: list[ImportItemIn] = Field(min_length=1)


@router.post("/assets/import")
def post_assets_import(body: ImportIn, db: Session = Depends(get_db)) -> dict:
    """按逐文件属性清单创建导入任务；幂等键防重复任务（计划 6.1）。"""
    import hashlib
    import json as _json

    from app.services import categories as cat_svc
    from app.workers import queue as q

    # 逐项校验必填字段与品类状态（不在此处阻塞整批：逐行报告）
    validated = []
    errors = []
    for raw in body.items:
        row: dict = {"import_item_id": raw.import_item_id, "source_path": raw.source_path}
        try:
            if raw.sku is None:
                raise ValidationError("货号必填")
            row["sku"] = cat_svc.validate_sku(raw.sku)
            cat = cat_svc.validate_for_upload(db, raw.category_id)
            row["category_id"] = cat.id
        except ValidationError as exc:
            errors.append({"import_item_id": raw.import_item_id, "error_code": "validation_error",
                           "detail": str(exc.detail if hasattr(exc, "detail") else exc)})
        validated.append(row)

    # 幂等键 = 清单内容哈希（同清单重复提交不重复建任务）
    payload = {"items": validated}
    idem = hashlib.sha256(
        _json.dumps(validated, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:32]

    job, created = q.enqueue(db, "import_assets", payload, idempotency_key=idem)
    if created:
        for row in validated:
            db.add(
                ImportItem(
                    job_id=job.id,
                    source_path=row["source_path"],
                    sku=row.get("sku"),
                    category_id=row.get("category_id"),
                )
            )
        db.commit()
    return {
        "job_id": job.id,
        "created": created,
        "invalid_items": errors,
    }


@router.get("/assets")
def get_assets(
    sku: str | None = None,
    category_id: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    db: Session = Depends(get_db),
) -> dict:
    """分页与筛选：货号精确筛选、品类、状态（计划 6.1 GET /api/assets）。"""
    from sqlalchemy import func

    from app.models import Asset

    stmt = select(Asset)
    if sku is not None:
        stmt = stmt.where(Asset.sku == sku)  # 精确匹配（T27）
    if category_id is not None:
        stmt = stmt.where(Asset.category_id == category_id)
    if status is not None:
        stmt = stmt.where(Asset.status == status)
    total = db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    rows = db.execute(
        stmt.order_by(Asset.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {
        "total": total,
        "page": page,
        "items": [
            {
                "id": a.id,
                "sku": a.sku,
                "category_id": a.category_id,
                "duration_ms": a.duration_ms,
                "width": a.width,
                "height": a.height,
                "status": a.status,
                "over_duration": a.over_duration,
                "managed_path": a.managed_path,
            }
            for a in rows
        ],
    }


@router.get("/assets/import/{job_id}/items")
def get_import_items(job_id: str, db: Session = Depends(get_db)) -> dict:
    rows = db.execute(
        select(ImportItem).where(ImportItem.job_id == job_id).order_by(ImportItem.created_at)
    ).scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "source_path": r.source_path,
                "sku": r.sku,
                "category_id": r.category_id,
                "status": r.status,
                "error_code": r.error_code,
                "asset_id": r.asset_id,
            }
            for r in rows
        ]
    }


class ConflictResolveIn(BaseModel):
    use_existing: bool
    new_sku: str | None = None
    new_category_id: str | None = None


@router.post("/assets/import-items/{item_id}/resolve-conflict")
def post_resolve_conflict(item_id: str, body: ConflictResolveIn, db: Session = Depends(get_db)) -> dict:
    from app.services.ingest.import_service import resolve_conflict

    item = resolve_conflict(
        db, item_id, use_existing=body.use_existing,
        new_sku=body.new_sku, new_category_id=body.new_category_id,
    )
    return {"id": item.id, "status": item.status, "asset_id": item.asset_id}
