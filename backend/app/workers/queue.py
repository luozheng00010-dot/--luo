"""持久化任务队列（计划 3.2 / 5.2）。

状态机：
queued → running → succeeded
                 → failed → queued（重试）
                 → cancelling → cancelled
                 → needs_input → queued（补充信息后继续）

领取通过单条 UPDATE 的原子比较完成，避免两个 Worker 重复领取；
失联任务按 lease_until 过期回收（需先核查心跳，不能误判仍运行的 Worker）。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import Job, utcnow

logger = logging.getLogger(__name__)

TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


def enqueue(
    session: Session,
    job_type: str,
    payload: dict | None = None,
    *,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
) -> tuple[Job, bool]:
    """入队。幂等键存在时返回已存在任务（created=False）。"""
    if idempotency_key:
        existing = session.execute(
            select(Job).where(Job.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing, False
    job = Job(
        type=job_type,
        payload_json=payload or {},
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
    )
    session.add(job)
    session.commit()
    return job, True


def claim_next(session: Session, worker_id: str, lease_seconds: int, *, max_races: int = 50) -> Job | None:
    """原子领取一个 queued 任务。

    select-then-update 的竞争失败（另一 Worker 抢到同一行）在函数内重试，
    只有当队列中确实没有 queued 任务时才返回 None——否则上层会把
    “竞争失败”误判为“队列已空”。
    """
    now = utcnow()
    for _ in range(max_races):
        candidate_id = session.execute(
            select(Job.id)
            .where(Job.state == "queued")
            .order_by(Job.created_at)
            .limit(1)
        ).scalar_one_or_none()
        if candidate_id is None:
            return None
        result = session.execute(
            update(Job)
            .where(
                Job.id == candidate_id,
                Job.state == "queued",
            )
            .values(
                state="running",
                lease_owner=worker_id,
                lease_until=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
                attempt=Job.attempt + 1,
            )
        )
        session.commit()
        if result.rowcount == 1:
            job = session.get(Job, candidate_id)
            logger.info("job_claimed", extra={"job_id": job.id, "worker": worker_id})
            return job
        # 竞争失败：立即重试下一个候选
    return None


def heartbeat(session: Session, job_id: str, worker_id: str, lease_seconds: int) -> bool:
    """续租：仅当仍归属该 Worker 时生效。"""
    now = utcnow()
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_owner == worker_id, Job.state == "running")
        .values(heartbeat_at=now, lease_until=now + timedelta(seconds=lease_seconds))
    )
    session.commit()
    return result.rowcount == 1


def mark_progress(session: Session, job_id: str, worker_id: str, stage: str, progress: float) -> bool:
    result = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_owner == worker_id, Job.state == "running")
        .values(stage=stage, progress=progress, heartbeat_at=utcnow())
    )
    session.commit()
    return result.rowcount == 1


def succeed(session: Session, job_id: str, worker_id: str, result: dict | None = None) -> bool:
    result_q = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_owner == worker_id, Job.state == "running")
        .values(state="succeeded", progress=1.0, result_json=result, finished_at=utcnow())
    )
    session.commit()
    return result_q.rowcount == 1


def fail(
    session: Session,
    job_id: str,
    worker_id: str,
    error_code: str,
    error_detail: str = "",
    *,
    needs_input: bool = False,
) -> str:
    """失败处理：可重试则回 queued，否则落 failed/needs_input。返回新状态。"""
    job = session.get(Job, job_id)
    if job is None or job.lease_owner != worker_id or job.state != "running":
        return job.state if job else "missing"
    job.error_code = error_code
    job.error_detail = error_detail
    if needs_input:
        job.state = "needs_input"
    elif job.attempt < job.max_attempts:
        job.state = "queued"
        job.lease_owner = None
        job.lease_until = None
    else:
        job.state = "failed"
        job.finished_at = utcnow()
    session.commit()
    logger.info(
        "job_failed", extra={"job_id": job_id, "code": error_code, "state": job.state}
    )
    return job.state


def request_cancel(session: Session, job_id: str) -> bool:
    """取消请求：queued 直接取消；running 进入 cancelling，由 Worker 自行终止。"""
    job = session.get(Job, job_id)
    if job is None or job.state in TERMINAL_STATES:
        return False
    if job.state == "queued":
        job.state = "cancelled"
        job.finished_at = utcnow()
    elif job.state in ("running", "needs_input"):
        job.state = "cancelling"
    session.commit()
    return True


def confirm_cancelled(session: Session, job_id: str, worker_id: str) -> bool:
    result_q = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.lease_owner == worker_id, Job.state == "cancelling")
        .values(state="cancelled", finished_at=utcnow())
    )
    session.commit()
    return result_q.rowcount == 1


def resume_needs_input(session: Session, job_id: str, payload_patch: dict | None = None) -> bool:
    job = session.get(Job, job_id)
    if job is None or job.state != "needs_input":
        return False
    if payload_patch:
        merged = dict(job.payload_json or {})
        merged.update(payload_patch)
        job.payload_json = merged
    job.state = "queued"
    job.lease_owner = None
    job.lease_until = None
    job.error_code = None
    session.commit()
    return True


def recover_expired_leases(session: Session, *, requeue: bool = True) -> list[str]:
    """回收过期租约。

    计划 5.2：超时租约经核查后回收。此处核查方式：租约过期超过宽限窗口
    （lease_until 已过）才回收；Worker 心跳会不断续租，正常运行的 Worker
    不会被误判。回收后重新入队或标记失败。
    """
    now = utcnow()
    expired = session.execute(
        select(Job).where(Job.state == "running", Job.lease_until < now)
    ).scalars().all()
    recovered: list[str] = []
    for job in expired:
        recovered.append(job.id)
        if requeue and job.attempt < job.max_attempts:
            job.state = "queued"
            job.lease_owner = None
            job.lease_until = None
            job.error_code = "lease_expired"
        else:
            job.state = "failed"
            job.finished_at = utcnow()
            job.error_code = job.error_code or "lease_expired"
    session.commit()
    for job_id in recovered:
        logger.info("lease_recovered", extra={"job_id": job_id})
    return recovered
