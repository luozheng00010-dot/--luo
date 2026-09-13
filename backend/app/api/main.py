"""FastAPI 应用工厂与本地安全中间件（计划 P1-05 / T24）。

- 默认只监听 127.0.0.1。
- 除 /healthz 外都要求 X-Session-Token；令牌在首次启动时生成并写入
  data/session_token（权限 0600）。恶意网页拿不到令牌也无法伪造 Host/Origin。
"""

from __future__ import annotations

import logging
import secrets
import stat

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import AppConfig, get_config
from app.db import make_engine, make_session_factory, run_migrations
from app.errors import AppError

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def ensure_session_token(config: AppConfig) -> str:
    """首次启动生成会话令牌文件；返回当前令牌。"""
    path = config.session_token_file
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    config.ensure_dirs()
    path.write_text(token, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return token


class LocalSecurityMiddleware:
    """来源/令牌校验：阻断恶意网页对本机 API 的跨站操作（T24）。"""

    def __init__(self, app: FastAPI, config: AppConfig) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = headers.get("host", "").split(":")[0]
        if host not in ALLOWED_HOSTS:
            await _reject(send, "forbidden_host", "仅允许本机访问")
            return
        origin = headers.get("origin")
        if origin is not None:
            origin_host = origin.split("://", 1)[-1].split(":")[0]
            if origin_host not in ALLOWED_HOSTS:
                await _reject(send, "forbidden_origin", "来源不在允许列表")
                return
        path = scope["path"]
        if path == "/healthz":
            await self.app(scope, receive, send)
            return
        token = headers.get("x-session-token", "")
        if not token or token != _expected_token(self.config):
            await _reject(send, "unauthorized", "缺少或无效的会话令牌")
            return
        await self.app(scope, receive, send)


def _expected_token(config: AppConfig) -> str:
    return ensure_session_token(config)


async def _reject(send, code: str, message: str) -> None:
    import json

    body = json.dumps({"error": code, "detail": message}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or get_config()
    config.ensure_dirs()
    run_migrations(config)
    # 首次启动幂等初始化预置品类（计划 4.6.3：通用/文胸/内裤）
    from app.services.categories import init_builtin_categories

    session = make_session_factory(make_engine(config))()
    try:
        init_builtin_categories(session)
    finally:
        session.close()
    app = FastAPI(title="自动剪辑软件", version=__version__, docs_url=None, redoc_url=None)
    engine = make_engine(config)
    session_factory = make_session_factory(engine)
    app.state.config = config
    app.state.engine = engine
    app.state.session_factory = session_factory

    app.add_middleware(LocalSecurityMiddleware, config=config)

    from app.api.routes import router as api_router

    app.include_router(api_router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.exception_handler(AppError)
    async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "detail": exc.detail}, status_code=exc.http_status)

    return app
