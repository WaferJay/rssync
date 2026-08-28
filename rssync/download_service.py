"""Retry-aware streaming downloads independent of concrete HTTP backends."""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from threading import Semaphore
from types import MappingProxyType
from typing import Any

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
    """Apply one stage-wide and one per-preset active-attempt limit."""

    def __init__(self, global_limit: int, preset_limits: Mapping[str, int]) -> None:
        self._global = Semaphore(global_limit)
        self._presets = {
            name: Semaphore(limit) for name, limit in preset_limits.items()
        }

    @contextmanager
    def slot(self, preset_name: str) -> Iterator[None]:
        """Reserve active network capacity for one complete attempt."""

        preset = self._presets[preset_name]
        preset.acquire()
        try:
            self._global.acquire()
            try:
                yield
            finally:
                self._global.release()
        finally:
            preset.release()


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
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.manager = manager
        self.concurrency = concurrency
        self.sleep = sleep
        self.clock = clock

    def download(
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
                with self.concurrency.slot(preset_name):
                    response = downloader.open_attempt(prepared)
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
                        for chunk in response.iter_bytes():
                            file.write(chunk)
                            digest.update(chunk)
                            byte_count += len(chunk)

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
                    response.close()
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
                self.sleep(delay)

        raise AssertionError("unreachable download retry state")
