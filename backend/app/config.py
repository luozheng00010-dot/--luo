"""应用配置。

所有运行数据放在项目根 data/ 目录（不提交 Git），API 默认只监听 127.0.0.1。
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AUTOEDITOR_", env_file=None)

    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = PROJECT_ROOT / "data"
    # 任务队列默认单渲染并发；AI 分析并发另行受限（计划 5.2）
    max_render_concurrency: int = 1
    max_analysis_concurrency: int = 2
    lease_seconds: int = 120
    heartbeat_seconds: int = 20
    provider_timeout_seconds: float = 60.0
    provider_max_retries: int = 3

    @property
    def db_path(self) -> Path:
        return self.data_dir / "autoeditor.sqlite3"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def session_token_file(self) -> Path:
        return self.data_dir / "session_token"

    def ensure_dirs(self) -> None:
        for sub in (
            "library",
            "thumbnails",
            "vectors",
            "audio",
            "previews",
            "exports",
            "temp",
            "backups",
        ):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)


_config: AppConfig | None = None


def get_config() -> AppConfig:
    global _config
    if _config is None:
        _config = AppConfig()
    return _config


def set_config(config: AppConfig) -> None:
    global _config
    _config = config


def default_data_dir_for_tests(tmp: Path) -> AppConfig:
    """测试专用：独立数据目录，避免污染开发数据。"""
    return AppConfig(data_dir=tmp, _env_file=None)  # type: ignore[call-arg]


if sys.platform == "darwin":  # noqa: F401  首发系统为 macOS（计划 1.2）
    pass
