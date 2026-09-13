"""任务处理器注册表。

独立模块：避免 `python -m app.workers.worker` 把 worker.py 同时加载为
__main__ 与 app.workers.worker 导致注册/查找命中两个不同的字典。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.workers.worker import Worker

Handler = Callable[["Session", dict, "Worker", str], "dict | None"]
"""处理器签名：(session, payload, worker, job_id) -> result_json。"""

_REGISTRY: dict[str, Handler] = {}


def register_handler(job_type: str, handler: Handler) -> None:
    _REGISTRY[job_type] = handler


def get_handler(job_type: str) -> Handler | None:
    return _REGISTRY.get(job_type)
