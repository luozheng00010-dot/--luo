"""API 路由（P1 范围：健康、设置、密钥、任务）。

后续阶段在此模块内扩展 assets/projects/storyboard/render 等接口。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.errors import NotFoundError
from app.models import Job, ProviderCall, utcnow
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
