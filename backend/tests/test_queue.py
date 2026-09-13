"""P1-03 验收：20 个任务不丢失不重复领取；强杀 Worker 后回收过期任务。"""

from __future__ import annotations

import threading
import time
from collections import Counter
from datetime import timedelta

from app.models import Job, utcnow
from app.workers import queue as q
from app.workers.worker import Worker


def _register_echo_handler(store: Counter, delay: float = 0.0):
    from app.workers.worker import register_handler

    def handler(session, payload, worker, job_id):
        if delay:
            time.sleep(delay)
        store[job_id] += 1
        worker.renew_lease(session, job_id)
        return {"echo": payload}

    register_handler("echo_test", handler)


def test_twenty_jobs_no_loss_no_duplicate(session_factory):
    store: Counter = Counter()
    _register_echo_handler(store)

    session = session_factory()
    ids = [q.enqueue(session, "echo_test", {"i": i})[0].id for i in range(20)]
    session.close()

    worker = Worker(session_factory, "w1", lease_seconds=30)
    processed = worker.run(max_jobs=20, exit_when_empty=True)

    assert processed == 20
    assert set(store.keys()) == set(ids)
    assert all(v == 1 for v in store.values()), "任务被重复执行"

    check = session_factory()
    states = check.execute(__import__("sqlalchemy").select(Job.state)).scalars().all()
    assert states.count("succeeded") == 20
    check.close()


def test_concurrent_workers_no_duplicate_claim(session_factory):
    store: Counter = Counter()
    _register_echo_handler(store, delay=0.005)

    session = session_factory()
    ids = [q.enqueue(session, "echo_test", {"i": i})[0].id for i in range(20)]
    session.close()

    workers = [Worker(session_factory, f"w{i}", lease_seconds=30) for i in range(3)]
    results = []

    def run(w):
        results.append(w.run(max_jobs=10, exit_when_empty=True, idle_timeout=15))

    threads = [threading.Thread(target=run, args=(w,), daemon=True) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert sum(results) == 20, f"任务丢失: 处理了 {sum(results)}"
    assert set(store.keys()) == set(ids)
    assert all(v == 1 for v in store.values()), "同一任务被多个 Worker 领取"


def test_strong_kill_recovers_expired_lease(session_factory):
    """模拟 Worker 领取后崩溃：租约过期被回收并重新执行（计划 5.2）。"""
    store: Counter = Counter()
    _register_echo_handler(store)

    session = session_factory()
    job, _ = q.enqueue(session, "echo_test", {"x": 1})
    session.close()

    # “强杀”：领取后不返回、租约不再续期
    dead = Worker(session_factory, "dead-worker", lease_seconds=1)
    s = dead.session_factory()
    claimed = q.claim_next(s, dead.worker_id, dead.lease_seconds)
    assert claimed is not None and claimed.id == job.id
    s.close()

    # 租约未过期时不可回收（防误判双重执行）
    s = session_factory()
    assert q.recover_expired_leases(s) == []
    # 租约过期后回收重排队
    job_row = s.get(Job, job.id)
    job_row.lease_until = utcnow() - timedelta(seconds=1)
    s.commit()
    recovered = q.recover_expired_leases(s)
    assert recovered == [job.id]
    s.close()

    survivor = Worker(session_factory, "w2", lease_seconds=30)
    survivor.run(max_jobs=1, exit_when_empty=True)
    assert store[job.id] == 1, "回收后应恰好执行一次"


def test_retry_cap_then_failed(session_factory):
    calls: Counter = Counter()

    from app.errors import AppError
    from app.workers.worker import register_handler

    def bad_handler(session, payload, worker, job_id):
        calls[job_id] += 1
        raise AppError("总是失败")

    register_handler("always_fail", bad_handler)

    session = session_factory()
    job, _ = q.enqueue(session, "always_fail", {}, max_attempts=2)
    session.close()

    worker = Worker(session_factory, "w1", lease_seconds=30)
    worker.run(max_jobs=1, exit_when_empty=True)  # 第一次尝试
    worker.run(max_jobs=1, exit_when_empty=True)  # 重试
    # 无第三次：已达上限
    time.sleep(0.05)
    check = session_factory()
    row = check.get(Job, job.id)
    assert row.state == "failed"
    assert row.attempt == 2
    assert calls[job.id] == 2
    check.close()


def test_cancel_running_job(session_factory):
    from app.workers.worker import register_handler

    started = threading.Event()
    release = threading.Event()

    def long_handler(session, payload, worker, job_id):
        started.set()
        for _ in range(200):
            if release.wait(0.05):
                break
            if worker.check_cancel(job_id, session):
                return None  # 取消后提前返回
        return {"done": True}

    register_handler("long_job", long_handler)

    session = session_factory()
    job, _ = q.enqueue(session, "long_job", {})
    session.close()

    worker = Worker(session_factory, "w1", lease_seconds=30)
    t = threading.Thread(target=worker.run, kwargs={"max_jobs": 1, "exit_when_empty": True}, daemon=True)
    t.start()
    assert started.wait(5)

    ctrl = session_factory()
    assert q.request_cancel(ctrl, job.id)
    release.set()
    t.join(timeout=10)

    check = session_factory()
    row = check.get(Job, job.id)
    assert row.state == "cancelled", f"期望 cancelled，实际 {row.state}"
    check.close()


def test_needs_input_resume(session_factory):
    from app.errors import AppError
    from app.workers.worker import register_handler

    attempts: Counter = Counter()

    def picky_handler(session, payload, worker, job_id):
        attempts[job_id] += 1
        if "answer" not in payload:
            raise AppError("缺少信息")
        return {"ok": payload["answer"]}

    register_handler("picky", picky_handler)

    session = session_factory()
    job, _ = q.enqueue(session, "picky", {}, max_attempts=3)
    session.close()

    # 普通失败先走重试回队（attempt=1 < max_attempts=3 → queued）
    worker = Worker(session_factory, "w1", lease_seconds=30)
    worker.run(max_jobs=1, exit_when_empty=True)
    check = session_factory()
    row = check.get(Job, job.id)
    assert row.state == "queued"
    assert row.error_code == "app_error"

    # 模拟任务进入待处理状态，补充信息后继续
    row.state = "needs_input"
    check.commit()
    assert q.resume_needs_input(check, job.id, {"answer": 42})
    check.close()

    worker.run(max_jobs=1, exit_when_empty=True)
    check = session_factory()
    row = check.get(Job, job.id)
    assert row.state == "succeeded"
    assert row.result_json == {"ok": 42}
    check.close()


def test_idempotency_key(session_factory):
    session = session_factory()
    a, created1 = q.enqueue(session, "echo_test", {}, idempotency_key="key-1")
    b, created2 = q.enqueue(session, "echo_test", {}, idempotency_key="key-1")
    assert created1 and not created2
    assert a.id == b.id
    session.close()
