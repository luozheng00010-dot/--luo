"""模型适配层（计划 3.3：视觉、文本、配音适配器）。

统一接口 + 结构化校验 + 明确错误分支 + 有界重试。
禁止模型输出直接进入 shell（计划 3.3 合成引擎边界），适配器只返回 Pydantic 结构。
"""

from __future__ import annotations

import json
import time
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderInvalidResponseError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
)

T = TypeVar("T", bound=BaseModel)


class ProviderResult(BaseModel):
    """一次供应商调用的记录要素（写入 provider_calls，不含密钥）。"""

    provider: str
    model: str
    usage_json: dict = {}
    estimated_cost: float | None = None
    currency: str | None = None


class ProviderResponse:
    def __init__(self, data: dict, meta: ProviderResult) -> None:
        self.data = data
        self.meta = meta


class BaseProvider:
    """HTTP 供应商基类：超时/429/认证失败/无效 JSON 分支 + 指数退避有界重试。"""

    name = "base"
    model_id = "unknown"
    max_retries = 3
    base_backoff_seconds = 0.2

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def _client_or_default(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=60.0)
        return self._client

    def post_json(self, url: str, *, headers: dict | None = None, json_body: dict | None = None):
        try:
            resp = self._client_or_default().post(url, headers=headers, json=json_body)
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"{self.name} 超时: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name} 网络错误: {exc}") from exc

        if resp.status_code == 401 or resp.status_code == 403:
            raise ProviderAuthError(f"{self.name} 认证失败 (HTTP {resp.status_code})")
        if resp.status_code == 429:
            raise ProviderRateLimitedError(f"{self.name} 限流 (HTTP 429)")
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name} HTTP {resp.status_code}")
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise ProviderInvalidResponseError(f"{self.name} 返回非 JSON: {exc}") from exc

    def call_structured(
        self,
        url: str,
        schema: type[T],
        *,
        headers: dict | None = None,
        json_body: dict | None = None,
        usage_extractor=None,
    ) -> tuple[T, ProviderResult]:
        """带重试地调用并把返回体按 schema 校验；重试次数有上限（计划 P1-04）。"""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                raw = self.post_json(url, headers=headers, json_body=json_body)
                data = schema.model_validate(raw)
                usage = usage_extractor(raw) if usage_extractor else {}
                meta = ProviderResult(provider=self.name, model=self.model_id, usage_json=usage)
                return data, meta
            except (ProviderTimeoutError, ProviderRateLimitedError, ProviderError) as exc:
                last_exc = exc
                # 认证失败与无效 JSON 不重试：重试也不会成功
                if isinstance(exc, ProviderAuthError):
                    raise
                if isinstance(exc, ProviderInvalidResponseError):
                    raise
                if attempt >= self.max_retries:
                    raise
                time.sleep(self.base_backoff_seconds * (2**attempt))
        raise last_exc or ProviderError("供应商调用失败")


def validate_structured_output(raw: str | bytes | dict, schema: type[T]) -> T:
    """把任意输出（含模型文本）按 Schema 校验；失败抛 ProviderInvalidResponseError。"""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProviderInvalidResponseError(f"模型输出不是合法 JSON: {exc}") from exc
    try:
        return schema.model_validate(raw)
    except ValidationError as exc:
        raise ProviderInvalidResponseError(f"模型输出未通过 Schema 校验: {exc}") from exc


# ---------------------------------------------------------------------------
# 离线 Mock 供应商：用于测试与未配置服务时的确定性替代
# ---------------------------------------------------------------------------


class ScriptedProvider(BaseProvider):
    """按脚本依次返回响应或异常，供测试编排各错误分支。"""

    def __init__(self, script: list) -> None:
        super().__init__()
        self.script = list(script)
        self.calls = 0

    def step(self) -> object:
        self.calls += 1
        if not self.script:
            raise ProviderError("脚本耗尽")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
