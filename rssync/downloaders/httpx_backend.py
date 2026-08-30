"""The built-in asynchronous downloader implemented with :mod:`httpx`."""

from __future__ import annotations

import logging
import math
from collections.abc import AsyncIterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import httpx
from fake_useragent import UserAgent

from rssync.config import DEFAULT_USER_AGENT
from rssync.downloaders.base import (
    DownloaderRuntimeContext,
    DownloadRequest,
    PreparedDownloadRequest,
    RetryPolicy,
)

logger = logging.getLogger(__name__)


DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


@dataclass(frozen=True, slots=True)
class UserAgentOptions:
    """User-Agent selection settings for the HTTPX backend."""

    strategy: str = "per-run"
    fallback: str = DEFAULT_USER_AGENT


@dataclass(frozen=True, slots=True)
class HttpxOptions:
    """Validated options for one HTTPX downloader preset."""

    http2: bool
    timeout: float
    retries: int
    backoff_factor: float
    verify_tls: bool
    headers: Mapping[str, str]
    user_agent: UserAgentOptions


def _number(value: object, location: str, *, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{location} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{location} must be at least {minimum}")
    return result


def parse_httpx_options(options: Mapping[str, Any]) -> HttpxOptions:
    """Validate HTTPX-specific preset options."""

    allowed = {
        "http2",
        "timeout",
        "retries",
        "backoff-factor",
        "verify-tls",
        "headers",
        "user-agent",
    }
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise ValueError(f"unknown httpx option(s): {', '.join(unknown)}")

    http2 = options.get("http2", True)
    verify_tls = options.get("verify-tls", True)
    if not isinstance(http2, bool):
        raise TypeError("httpx option http2 must be a boolean")
    if not isinstance(verify_tls, bool):
        raise TypeError("httpx option verify-tls must be a boolean")

    retries = options.get("retries", 3)
    if isinstance(retries, bool) or not isinstance(retries, int):
        raise TypeError("httpx option retries must be an integer")
    if retries < 0:
        raise ValueError("httpx option retries must be a non-negative integer")

    headers_value = options.get("headers", {})
    if not isinstance(headers_value, Mapping):
        raise TypeError("httpx option headers must be an object")
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in headers_value.items()
    ):
        raise ValueError("httpx option headers must map strings to strings")
    if any(key.casefold() == "user-agent" for key in headers_value):
        raise ValueError("headers.User-Agent is reserved; use user-agent strategy")

    user_agent_value = options.get("user-agent", {})
    if not isinstance(user_agent_value, Mapping):
        raise TypeError("httpx option user-agent must be an object")
    unknown_user_agent = sorted(set(user_agent_value) - {"strategy", "fallback"})
    if unknown_user_agent:
        raise ValueError(
            "unknown user-agent option(s): " + ", ".join(unknown_user_agent)
        )
    strategy = user_agent_value.get("strategy", "per-run")
    if strategy not in {"per-run", "per-request"}:
        raise ValueError("user-agent.strategy must be per-run or per-request")
    fallback = user_agent_value.get("fallback", DEFAULT_USER_AGENT)
    if not isinstance(fallback, str):
        raise TypeError("user-agent.fallback must be a string")
    if not fallback:
        raise ValueError("user-agent.fallback must be a non-empty string")

    return HttpxOptions(
        http2=http2,
        timeout=_number(
            options.get("timeout", 30),
            "httpx option timeout",
            minimum=0.001,
        ),
        retries=retries,
        backoff_factor=_number(
            options.get("backoff-factor", 0.5),
            "httpx option backoff-factor",
            minimum=0,
        ),
        verify_tls=verify_tls,
        headers=MappingProxyType(dict(headers_value)),
        user_agent=UserAgentOptions(strategy=strategy, fallback=fallback),
    )


class HttpxDownloadResponse:
    """Streaming adapter around an HTTPX response."""

    def __init__(self, response: httpx.Response, requested_url: str) -> None:
        self._response = response
        self._requested_url = requested_url

    @property
    def requested_url(self) -> str:
        return self._requested_url

    @property
    def final_url(self) -> str:
        return str(self._response.url)

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    async def iter_bytes(self) -> AsyncIterable[bytes]:
        async for chunk in self._response.aiter_bytes(64 * 1024):
            if chunk:
                yield chunk

    async def close(self) -> None:
        await self._response.aclose()


class HttpxDownloader:
    """One shared asynchronous HTTPX transport."""

    def __init__(
        self, options: HttpxOptions, runtime: DownloaderRuntimeContext
    ) -> None:
        self.options = options
        self.runtime = runtime
        self._client = httpx.AsyncClient(
            http2=options.http2,
            timeout=options.timeout,
            verify=options.verify_tls,
            follow_redirects=True,
        )
        self._user_agent: UserAgent | None = None
        self._user_agent_unavailable = False

    @property
    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            retries=self.options.retries,
            backoff_factor=self.options.backoff_factor,
        )

    def _random_user_agent(self) -> str:
        if self._user_agent_unavailable:
            return self.options.user_agent.fallback
        try:
            if self._user_agent is None:
                self._user_agent = UserAgent(fallback=self.options.user_agent.fallback)
            value = self._user_agent.random
            if isinstance(value, str) and value:
                return value
        except Exception as error:  # noqa: BLE001
            self._user_agent_unavailable = True
            logger.warning("Unable to generate a random User-Agent: %s", error)
        return self.options.user_agent.fallback

    def _select_user_agent(self) -> str:
        if self.options.user_agent.strategy == "per-run":
            return self.runtime.get_or_create(
                "httpx.user-agent.per-run", self._random_user_agent
            )
        return self._random_user_agent()

    def prepare(self, request: DownloadRequest) -> PreparedDownloadRequest:
        headers = dict(self.options.headers)
        headers.update(request.headers)
        user_agent = self._select_user_agent()
        headers["User-Agent"] = user_agent
        return PreparedDownloadRequest(
            url=request.url,
            resource_kind=request.resource_kind,
            headers=MappingProxyType(headers),
            metadata=MappingProxyType(
                {
                    "http2": self.options.http2,
                    "user_agent": user_agent,
                    "user_agent_strategy": self.options.user_agent.strategy,
                }
            ),
        )

    async def open_attempt(
        self, request: PreparedDownloadRequest
    ) -> HttpxDownloadResponse:
        headers = dict(request.headers)
        headers.update(DEFAULT_HEADERS)
        outbound = self._client.build_request("GET", request.url, headers=headers)
        response = await self._client.send(outbound, stream=True)
        return HttpxDownloadResponse(response, request.url)

    def is_retryable_exception(self, error: BaseException) -> bool:
        return isinstance(error, httpx.RequestError)

    async def close(self) -> None:
        await self._client.aclose()


class HttpxBackendFactory:
    """Factory registered under the built-in ``httpx`` backend key."""

    def validate_options(self, options: Mapping[str, Any]) -> HttpxOptions:
        return parse_httpx_options(options)

    def create(
        self, options: HttpxOptions, runtime: DownloaderRuntimeContext
    ) -> HttpxDownloader:
        return HttpxDownloader(options, runtime)
