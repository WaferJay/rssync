"""The built-in downloader implemented with :mod:`requests`."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import requests
from fake_useragent import UserAgent

from rssync.config import DEFAULT_USER_AGENT
from rssync.downloaders.base import (
    DownloaderRuntimeContext,
    DownloadRequest,
    PreparedDownloadRequest,
    RetryPolicy,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UserAgentOptions:
    """User-Agent selection settings for the requests backend."""

    strategy: str = "per-run"
    fallback: str = DEFAULT_USER_AGENT


@dataclass(frozen=True, slots=True)
class RequestsOptions:
    """Validated options for one requests downloader preset."""

    use_session: bool
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


def parse_requests_options(options: Mapping[str, Any]) -> RequestsOptions:
    """Validate requests-specific preset options."""

    allowed = {
        "use-session",
        "timeout",
        "retries",
        "backoff-factor",
        "verify-tls",
        "headers",
        "user-agent",
    }
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise ValueError(f"unknown requests option(s): {', '.join(unknown)}")

    use_session = options.get("use-session", True)
    verify_tls = options.get("verify-tls", True)
    if not isinstance(use_session, bool):
        raise TypeError("requests option use-session must be a boolean")
    if not isinstance(verify_tls, bool):
        raise TypeError("requests option verify-tls must be a boolean")

    retries = options.get("retries", 3)
    if isinstance(retries, bool) or not isinstance(retries, int):
        raise TypeError("requests option retries must be an integer")
    if retries < 0:
        raise ValueError("requests option retries must be a non-negative integer")

    headers_value = options.get("headers", {})
    if not isinstance(headers_value, Mapping):
        raise TypeError("requests option headers must be an object")
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in headers_value.items()
    ):
        raise ValueError("requests option headers must map strings to strings")
    if any(key.casefold() == "user-agent" for key in headers_value):
        raise ValueError("headers.User-Agent is reserved; use user-agent strategy")

    user_agent_value = options.get("user-agent", {})
    if not isinstance(user_agent_value, Mapping):
        raise TypeError("requests option user-agent must be an object")
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

    return RequestsOptions(
        use_session=use_session,
        timeout=_number(
            options.get("timeout", 30),
            "requests option timeout",
            minimum=0.001,
        ),
        retries=retries,
        backoff_factor=_number(
            options.get("backoff-factor", 0.5),
            "requests option backoff-factor",
            minimum=0,
        ),
        verify_tls=verify_tls,
        headers=MappingProxyType(dict(headers_value)),
        user_agent=UserAgentOptions(strategy=strategy, fallback=fallback),
    )


class RequestsDownloadResponse:
    """Streaming adapter around a requests response."""

    def __init__(self, response: requests.Response, requested_url: str) -> None:
        self._response = response
        self._requested_url = requested_url

    @property
    def requested_url(self) -> str:
        return self._requested_url

    @property
    def final_url(self) -> str:
        return self._response.url

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    def iter_bytes(self) -> Iterable[bytes]:
        for chunk in self._response.iter_content(chunk_size=64 * 1024):
            if chunk:
                yield chunk

    def close(self) -> None:
        self._response.close()


class RequestsDownloader:
    """One worker-local requests transport."""

    def __init__(
        self, options: RequestsOptions, runtime: DownloaderRuntimeContext
    ) -> None:
        self.options = options
        self.runtime = runtime
        self._session = requests.Session() if options.use_session else None
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
        # Random generation is non-critical once a fallback is configured;
        # any provider/data failure must degrade to that static value.
        except Exception as error:  # noqa: BLE001
            self._user_agent_unavailable = True
            logger.warning("Unable to generate a random User-Agent: %s", error)
        return self.options.user_agent.fallback

    def _select_user_agent(self) -> str:
        if self.options.user_agent.strategy == "per-run":
            return self.runtime.get_or_create(
                "requests.user-agent.per-run", self._random_user_agent
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
                    "use_session": self.options.use_session,
                    "user_agent": user_agent,
                    "user_agent_strategy": self.options.user_agent.strategy,
                }
            ),
        )

    def open_attempt(
        self, request: PreparedDownloadRequest
    ) -> RequestsDownloadResponse:
        request_method = self._session.request if self._session else requests.request
        response = request_method(
            "GET",
            request.url,
            headers=dict(request.headers),
            timeout=self.options.timeout,
            verify=self.options.verify_tls,
            allow_redirects=True,
            stream=True,
        )
        return RequestsDownloadResponse(response, request.url)

    def is_retryable_exception(self, error: BaseException) -> bool:
        return isinstance(error, requests.RequestException)

    def close(self) -> None:
        if self._session is not None:
            self._session.close()


class RequestsBackendFactory:
    """Factory registered under the built-in ``requests`` backend key."""

    def validate_options(self, options: Mapping[str, Any]) -> RequestsOptions:
        return parse_requests_options(options)

    def create(
        self, options: RequestsOptions, runtime: DownloaderRuntimeContext
    ) -> RequestsDownloader:
        return RequestsDownloader(options, runtime)
