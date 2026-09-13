"""独立 Python Worker（计划 3.2）：领取任务、心跳续租、执行处理器。"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy.orm import Session, sessionmaker

from app.models import Job, utcnow
from app.workers import queue as q
from app.workers.registry import get_handler, register_handler  # noqa: F401  再导出保持兼容

logger = logging.getLogger(__name__)



class Worker:
    def __init__(
        self,
        session_factory: sessionmaker,
        worker_id: str,
        *,
        lease_seconds: int = 120,
        heartbeat_seconds: int = 20,
        poll_interval: float = 0.2,
    ) -> None:
        self.session_factory = session_factory
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        # 确保任何 Worker 实例都带全部任务处理器（幂等注册）
        from app.workers import handlers as job_handlers

        job_handlers.register_all()

    # ---- 供处理器使用的辅助 ----

    def check_cancel(self, job_id: str, session: Session) -> bool:
        """检查取消请求；若处于 cancelling 则确认取消并返回 True。"""
        job = session.get(Job, job_id)
        if job.state == "cancelling":
            q.confirm_cancelled(session, job_id, self.worker_id)
            return True
        return False

    def renew_lease(self, session: Session, job_id: str) -> None:
        q.heartbeat(
            session, job_id, self.worker_id, self.lease_seconds + self.heartbeat_seconds * 2
        )

    # ---- 主循环 ----

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> str | None:
        """领取并处理一个任务；返回 job_id（无任务返回 None）。"""
        session = self.session_factory()
        try:
            job = q.claim_next(session, self.worker_id, self.lease_seconds)
            if job is None:
                return None
        finally:
            session.close()

        self._execute(job.id, job.type, job.payload_json or {})
        return job.id

    def _execute(self, job_id: str, job_type: str, payload: dict) -> None:
        session = self.session_factory()
        try:
            handler = get_handler(job_type)
            if handler is None:
                q.fail(session, job_id, self.worker_id, "no_handler", f"未注册的任务类型 {job_type}")
                return
            result = handler(session, payload, self, job_id)
            # 处理器正常返回后确认取消状态
            job = session.get(Job, job_id)
            if job.state == "cancelling":
                q.confirm_cancelled(session, job_id, self.worker_id)
                logger.info("job_cancelled", extra={"job_id": job_id})
            else:
                q.succeed(session, job_id, self.worker_id, result)
                logger.info("job_succeeded", extra={"job_id": job_id})
        except Exception as exc:
            try:
                session.rollback()
                code = getattr(exc, "code", "handler_crash")
                q.fail(session, job_id, self.worker_id, code, str(exc))
            except Exception:
                logger.exception("fail_handler_error", extra={"job_id": job_id})
            logger.exception("job_handler_error", extra={"job_id": job_id, "type": job_type})
        finally:
            session.close()

    def run(
        self,
        *,
        max_jobs: int | None = None,
        exit_when_empty: bool = False,
        idle_timeout: float | None = None,
    ) -> int:
        """处理任务直到停止。exit_when_empty 用于批处理收尾；idle_timeout 防止空转。"""
        processed = 0
        idle_deadline = None if idle_timeout is None else time.monotonic() + idle_timeout
        while not self._stop.is_set():
            if max_jobs is not None and processed >= max_jobs:
                break
            job_id = self.run_once()
            if job_id is None:
                if exit_when_empty:
                    break
                if idle_deadline is not None and time.monotonic() > idle_deadline:
                    break
                time.sleep(self.poll_interval)
                continue
            processed += 1
        return processed


class RecoveryService(threading.Thread):
    """后台回收过期租约（计划 5.2：失联任务回收，双重执行防护靠租约核查）。"""

    def __init__(self, session_factory: sessionmaker, interval: float = 10.0) -> None:
        super().__init__(daemon=True, name="lease-recovery")
        self.session_factory = session_factory
        self.interval = interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            session = self.session_factory()
            try:
                q.recover_expired_leases(session)
            finally:
                session.close()
            self._stop.wait(self.interval)


def ensure_job_lease_valid(session: Session, job_id: str) -> bool:
    """核查任务租约是否仍有效（用于启动时核对中断状态）。"""
    job = session.get(Job, job_id)
    if job is None:
        return False
    if job.lease_until is None:
        return job.state != "running"
    return job.lease_until > utcnow()


def main() -> None:
    """独立 Worker 进程入口（计划 3.2：独立 Python Worker）。"""
    import uuid

    from app.config import get_config
    from app.db import make_engine, make_session_factory
    from app.logging_setup import setup_logging

    setup_logging()
    config = get_config()
    config.ensure_dirs()
    from app.db import run_migrations

    run_migrations(config)
    from app.workers import handlers as job_handlers

    job_handlers.register_all()
    factory = make_session_factory(make_engine(config))
    worker = Worker(factory, f"worker-{uuid.uuid4().hex[:8]}", lease_seconds=config.lease_seconds)
    recovery = RecoveryService(factory)
    recovery.start()
    logger.info("worker_starting", extra={"worker": worker.worker_id})
    try:
        worker.run()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop()
        recovery.stop()


if __name__ == "__main__":
    main()
