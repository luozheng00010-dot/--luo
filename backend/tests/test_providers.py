"""P1-04 验收：超时、429、认证失败、无效 JSON 各有明确分支；重试次数有上限。"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from app.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderInvalidResponseError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
)
from app.providers.base import BaseProvider, validate_structured_output


class Out(BaseModel):
    description: str


def make_provider(handler) -> BaseProvider:
    provider = BaseProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    provider.max_retries = 2
    provider.base_backoff_seconds = 0.001
    return provider


def test_timeout_branch():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectTimeout("timeout", request=request)

    p = make_provider(handler)
    with pytest.raises(ProviderTimeoutError):
        p.post_json("http://mock/x")
    assert calls["n"] == 1  # post_json 层不重试


def test_rate_limited_with_bounded_retry_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429)
        return httpx.Response(200, json={"description": "ok"})

    p = make_provider(handler)
    data, meta = p.call_structured("http://mock/x", Out)
    assert data.description == "ok"
    assert calls["n"] == 3
    assert meta.provider == "base"


def test_rate_limited_exceeds_retry_cap():
    def handler(request):
        return httpx.Response(429)

    p = make_provider(handler)
    with pytest.raises(ProviderRateLimitedError):
        p.call_structured("http://mock/x", Out)


def test_auth_failure_no_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(401)

    p = make_provider(handler)
    with pytest.raises(ProviderAuthError):
        p.call_structured("http://mock/x", Out)
    assert calls["n"] == 1, "认证失败不应重试"


def test_invalid_json_no_retry():
    def handler(request):
        return httpx.Response(200, text="not-json{{")

    p = make_provider(handler)
    with pytest.raises(ProviderInvalidResponseError):
        p.call_structured("http://mock/x", Out)


def test_schema_validation_of_model_output():
    assert validate_structured_output('{"description": "一只猫"}', Out).description == "一只猫"
    with pytest.raises(ProviderInvalidResponseError):
        validate_structured_output('{"wrong_field": 1}', Out)
    with pytest.raises(ProviderInvalidResponseError):
        validate_structured_output("[1,2,3]", Out)


def test_http_5xx_branch():
    def handler(request):
        return httpx.Response(500)

    p = make_provider(handler)
    with pytest.raises(ProviderError) as exc:
        p.call_structured("http://mock/x", Out)
    assert "HTTP 500" in str(exc.value)
