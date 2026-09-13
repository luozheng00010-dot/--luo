"""任务处理器注册表：把 job_type 绑定到服务实现。"""

from __future__ import annotations

from app.workers.registry import register_handler


def _import_assets_handler(session, payload, worker, job_id):

    from app.config import get_config
    from app.models import Job
    from app.services.ingest.import_service import run_import_job

    job = session.get(Job, job_id)
    return run_import_job(session, job, get_config())


def register_all() -> None:
    register_handler("import_assets", _import_assets_handler)


register_all()
