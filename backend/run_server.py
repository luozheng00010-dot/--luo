"""本地服务入口：uvicorn app.api.main:create_app 工厂模式启动。"""

from __future__ import annotations

import logging

import uvicorn

from app.api.main import ensure_session_token
from app.config import get_config
from app.logging_setup import setup_logging


def main() -> None:
    config = get_config()
    setup_logging()
    config.ensure_dirs()
    ensure_session_token(config)
    logging.getLogger(__name__).info(
        "server_starting",
        extra={"host": config.host, "port": config.port, "data_dir": str(config.data_dir)},
    )
    uvicorn.run(
        "app.api.main:create_app",
        factory=True,
        host=config.host,
        port=config.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
