"""结构化 JSON 日志与脱敏（计划 3.2 日志行 / P1-05）。

- 以 job_id / project_id 等字段追踪。
- 含密钥样式的字段值一律替换为 ***；文案默认只记录哈希摘要。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

SENSITIVE_KEY_PATTERN = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|credential)", re.IGNORECASE
)


def redact_value(key: str, value) -> object:
    if SENSITIVE_KEY_PATTERN.search(key or ""):
        return "***"
    return value


def redact_obj(obj):
    if isinstance(obj, dict):
        return {k: redact_value(k, redact_obj(v)) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj


def script_digest(text: str, length: int = 12) -> str:
    """文案只记哈希摘要，不落原文（计划 10：文案日志只记摘要或哈希）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # extra 字段合并（做脱敏）
        reserved = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
            "asctime", "message", "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in reserved:
                payload[key] = redact_value(key, value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
