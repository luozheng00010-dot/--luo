"""统一错误码（计划 P1-04：服务超时、429、认证失败、无效 JSON 各有明确分支）。"""

from __future__ import annotations


class AppError(Exception):
    code = "app_error"
    http_status = 500

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.code)
        self.detail = detail


class ValidationError(AppError):
    code = "validation_error"
    http_status = 422


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404


class ConflictError(AppError):
    code = "conflict"
    http_status = 409


class VersionConflictError(ConflictError):
    code = "version_conflict"


# ---- 供应商调用错误（T14：云端超时、429、错误 JSON） ----


class ProviderError(AppError):
    code = "provider_error"
    http_status = 502


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"


class ProviderRateLimitedError(ProviderError):
    code = "provider_rate_limited"


class ProviderAuthError(ProviderError):
    code = "provider_auth_failed"


class ProviderInvalidResponseError(ProviderError):
    code = "provider_invalid_json"
