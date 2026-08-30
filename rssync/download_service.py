"""Retry-aware streaming downloads independent of concrete HTTP backends."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from rssync.downloaders.base import (
    DownloadHTTPError,
    DownloadRequest,
    ResourceKind,
    UnsupportedContentType,
)
from rssync.downloaders.registry import DownloaderManager
from rssync.storage import commit_download, temporary_sibling

logger = logging.getLogger(__name__)
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})


@dataclass(frozen=True, slots=True)
class DownloadedResource:
    """Metadata produced after a resource is fully persisted."""

    requested_url: str
    final_url: str
    target_path: Path
    status_code: int
    response_headers: Mapping[str, str]
    content_type: str | None
    sha256: str
    byte_count: int
    changed: bool
    fetched_at: int
    preset_name: str
    backend_name: str
    metadata: Mapping[str, Any]


class DownloadConcurrency:
    """Apply stage-wide and cross-preset per-host request limits."""

    @dataclass(slots=True)
    class _DomainState:
        semaphore: asyncio.Semaphore | None
        start_lock: asyncio.Lock
        last_started: float | None = None

    def __init__(
        self,
        rss_limit: int,
        webpage_limit: int,
        per_domain_limit: int | None = None,
        request_interval: float = 0.0,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._stages = {
            "rss": asyncio.Semaphore(rss_limit),
            "webpage": asyncio.Semaphore(webpage_limit),
        }
        self._per_domain_limit = per_domain_limit
        self._request_interval = request_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._domains: dict[str, DownloadConcurrency._DomainState] = {}
        self._domains_lock = asyncio.Lock()

    async def _domain_state(self, url: str) -> _DomainState:
        hostname = urlsplit(url).hostname
        if hostname is None:
            raise ValueError(f"download URL does not contain a hostname: {url}")
        hostname = hostname.casefold()
        async with self._domains_lock:
            state = self._domains.get(hostname)
            if state is None:
                state = self._DomainState(
                    semaphore=(
                        asyncio.Semaphore(self._per_domain_limit)
                        if self._per_domain_limit is not None
                        else None
                    ),
                    start_lock=asyncio.Lock(),
                )
                self._domains[hostname] = state
            return state

    @asynccontextmanager
    async def slot(
        self, resource_kind: ResourceKind, url: str
    ) -> AsyncIterator[None]:
        """Reserve stage and hostname capacity for one complete attempt."""

        stage = self._stages[resource_kind]
        domain = await self._domain_state(url)
        if domain.semaphore is not None:
            await domain.semaphore.acquire()
        stage_acquired = False
        try:
            async with domain.start_lock:
                if domain.last_started is not None:
                    earliest_start = domain.last_started + self._request_interval
                    while (delay := earliest_start - self._monotonic()) > 0:
                        await self._sleep(delay)
                await stage.acquire()
                stage_acquired = True
                domain.last_started = self._monotonic()
            try:
                yield
            finally:
                stage.release()
                stage_acquired = False
        finally:
            if stage_acquired:
                stage.release()
            if domain.semaphore is not None:
                domain.semaphore.release()


def _media_type(headers: Mapping[str, str]) -> str | None:
    value = next(
        (
            header_value
            for key, header_value in headers.items()
            if key.casefold() == "content-type"
        ),
        None,
    )
    if not value:
        return None
    return value.partition(";")[0].strip().lower() or None


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip())
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        pass
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=UTC)
        return max(0.0, (target - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    return next(
        (value for key, value in headers.items() if key.casefold() == name.casefold()),
        None,
    )


class DownloadService:
    """Run logical downloads with retries and atomic streaming persistence."""

    def __init__(
        self,
        manager: DownloaderManager,
        concurrency: DownloadConcurrency,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.manager = manager
        self.concurrency = concurrency
        self.sleep = sleep
        self.clock = clock

    async def download(
        self,
        *,
        url: str,
        resource_kind: ResourceKind,
        preset_name: str,
        target_path: str | Path,
    ) -> DownloadedResource:
        """Download one logical resource to an atomic target path."""

        downloader = self.manager.get(preset_name)
        prepared = downloader.prepare(
            DownloadRequest(url=url, resource_kind=resource_kind)
        )
        policy = downloader.retry_policy

        for attempt_index in range(policy.retries + 1):
            temporary: Path | None = None
            response = None
            retry_after: float | None = None
            retryable = False
            try:
                async with self.concurrency.slot(
                    prepared.resource_kind,
                    prepared.url,
                ):
                    response = await downloader.open_attempt(prepared)
                    try:
                        headers = dict(response.headers)
                        status_code = response.status_code
                        final_url = response.final_url
                        if not 200 <= status_code < 300:
                            retryable = status_code in policy.status_codes
                            retry_after = _retry_after_seconds(
                                _header(headers, "Retry-After")
                            )
                            raise DownloadHTTPError(status_code, final_url)

                        content_type = _media_type(headers)
                        if (
                            resource_kind == "webpage"
                            and content_type not in HTML_CONTENT_TYPES
                        ):
                            raise UnsupportedContentType(content_type, final_url)

                        temporary = temporary_sibling(target_path)
                        digest = hashlib.sha256()
                        byte_count = 0
                        with temporary.open("wb") as file:
                            async for chunk in response.iter_bytes():
                                file.write(chunk)
                                digest.update(chunk)
                                byte_count += len(chunk)
                    finally:
                        try:
                            await response.close()
                        finally:
                            response = None

                digest_value = digest.hexdigest()
                changed = commit_download(temporary, target_path, digest_value)
                temporary = None
                fetched_at = int(self.clock())
                logger.info(
                    "Downloaded %d bytes from %s with preset %s",
                    byte_count,
                    url,
                    preset_name,
                )
                return DownloadedResource(
                    requested_url=url,
                    final_url=final_url,
                    target_path=Path(target_path),
                    status_code=status_code,
                    response_headers=MappingProxyType(headers),
                    content_type=content_type,
                    sha256=digest_value,
                    byte_count=byte_count,
                    changed=changed,
                    fetched_at=fetched_at,
                    preset_name=preset_name,
                    backend_name=self.manager.backend_name(preset_name),
                    metadata=MappingProxyType(dict(prepared.metadata)),
                )
            except Exception as error:
                if not isinstance(error, DownloadHTTPError):
                    retryable = downloader.is_retryable_exception(error)
                if not retryable or attempt_index >= policy.retries:
                    raise
            finally:
                if response is not None:
                    await response.close()
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

            retry_number = attempt_index + 1
            delay = policy.backoff_factor * (2 ** (retry_number - 1))
            if retry_after is not None:
                delay = max(delay, retry_after)
            logger.warning(
                "Download attempt failed for %s; retrying in %.3f seconds",
                url,
                delay,
            )
            if delay > 0:
                await self.sleep(delay)

        raise AssertionError("unreachable download retry state")
