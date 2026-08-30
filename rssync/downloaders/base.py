"""Public downloader interfaces shared by built-in and plugin backends."""

from __future__ import annotations

from collections.abc import AsyncIterable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ResourceKind = Literal["rss", "webpage"]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry settings applied around complete streaming attempts."""

    retries: int
    backoff_factor: float
    status_codes: frozenset[int] = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """One logical resource download before backend preparation."""

    url: str
    resource_kind: ResourceKind
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreparedDownloadRequest:
    """A logical request whose identity stays stable across retries."""

    url: str
    resource_kind: ResourceKind
    headers: Mapping[str, str]
    metadata: Mapping[str, Any] = field(default_factory=dict)


class DownloadResponse(Protocol):
    """A streaming response returned by one network attempt."""

    @property
    def requested_url(self) -> str: ...

    @property
    def final_url(self) -> str: ...

    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def iter_bytes(self) -> AsyncIterable[bytes]: ...

    async def close(self) -> None: ...


class Downloader(Protocol):
    """An asynchronous backend shared by concurrent tasks for one preset."""

    @property
    def retry_policy(self) -> RetryPolicy: ...

    def prepare(self, request: DownloadRequest) -> PreparedDownloadRequest: ...

    async def open_attempt(
        self, request: PreparedDownloadRequest
    ) -> DownloadResponse: ...

    def is_retryable_exception(self, error: BaseException) -> bool: ...

    async def close(self) -> None: ...


class DownloaderRuntimeContext:
    """Process-local shared state made available to backend factories."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def get_or_create(self, key: str, factory: Callable[[], Any]) -> Any:
        """Return one lazily-created value shared by all downloader presets."""

        if key not in self._values:
            self._values[key] = factory()
        return self._values[key]


class DownloaderBackendFactory(Protocol):
    """Factory contract exposed through the ``rssync.downloaders`` group."""

    def validate_options(self, options: Mapping[str, Any]) -> Any: ...

    def create(self, options: Any, runtime: DownloaderRuntimeContext) -> Downloader: ...


class DownloaderError(RuntimeError):
    """Base class for resource download failures."""


class DownloadHTTPError(DownloaderError):
    """Raised when the final HTTP status is not successful."""

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"HTTP {status_code} while downloading {url}")
        self.status_code = status_code
        self.url = url


class UnsupportedContentType(DownloaderError):
    """Raised when a webpage response is not HTML or XHTML."""

    def __init__(self, content_type: str | None, url: str) -> None:
        label = content_type or "missing Content-Type"
        super().__init__(f"unsupported content type {label!r} for {url}")
        self.content_type = content_type
        self.url = url
