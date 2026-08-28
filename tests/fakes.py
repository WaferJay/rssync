"""Test-only downloader backend implementations."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

from rssync.downloaders.base import (
    DownloaderRuntimeContext,
    DownloadRequest,
    PreparedDownloadRequest,
    RetryPolicy,
)


@dataclass(frozen=True)
class FakeReply:
    body: bytes
    content_type: str = "application/rss+xml"
    status: int = 200
    final_url: str | None = None
    delay: float = 0


class FakeResponse:
    def __init__(
        self,
        url: str,
        reply: FakeReply,
        factory: FakeBackendFactory,
        label: str,
    ) -> None:
        self.requested_url = url
        self.final_url = reply.final_url or url
        self.status_code = reply.status
        self.headers = {"Content-Type": reply.content_type}
        self._reply = reply
        self._factory = factory
        self._label = label

    def iter_bytes(self) -> Iterable[bytes]:
        self._factory.enter(self._label)
        try:
            if self._reply.delay:
                time.sleep(self._reply.delay)
            midpoint = len(self._reply.body) // 2
            yield self._reply.body[:midpoint]
            yield self._reply.body[midpoint:]
        finally:
            self._factory.exit(self._label)

    def close(self) -> None:
        return None


class FakeDownloader:
    def __init__(
        self,
        factory: FakeBackendFactory,
        options: Mapping[str, Any],
    ) -> None:
        self.factory = factory
        self.options = options

    @property
    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            retries=int(self.options.get("retries", 0)),
            backoff_factor=float(self.options.get("backoff-factor", 0)),
        )

    def prepare(self, request: DownloadRequest) -> PreparedDownloadRequest:
        return PreparedDownloadRequest(
            url=request.url,
            resource_kind=request.resource_kind,
            headers={},
            metadata={
                "label": self.options.get("label", "default"),
                "use_session": False,
                "user_agent": "fake-agent",
                "user_agent_strategy": "test",
            },
        )

    def open_attempt(self, request: PreparedDownloadRequest) -> FakeResponse:
        label = str(self.options.get("label", "default"))
        with self.factory.lock:
            self.factory.calls.append((request.resource_kind, request.url, label))
        reply = self.factory.replies[request.url]
        return FakeResponse(request.url, reply, self.factory, label)

    def is_retryable_exception(self, error: BaseException) -> bool:
        return isinstance(error, OSError)

    def close(self) -> None:
        return None


class FakeBackendFactory:
    def __init__(self, replies: Mapping[str, FakeReply]) -> None:
        self.replies = dict(replies)
        self.calls: list[tuple[str, str, str]] = []
        self.lock = Lock()
        self.active = 0
        self.max_active = 0
        self.active_by_label: dict[str, int] = {}
        self.max_active_by_label: dict[str, int] = {}

    def enter(self, label: str) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            active = self.active_by_label.get(label, 0) + 1
            self.active_by_label[label] = active
            self.max_active_by_label[label] = max(
                self.max_active_by_label.get(label, 0), active
            )

    def exit(self, label: str) -> None:
        with self.lock:
            self.active -= 1
            self.active_by_label[label] -= 1

    def validate_options(self, options: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(options)

    def create(
        self,
        options: Mapping[str, Any],
        runtime: DownloaderRuntimeContext,
    ) -> FakeDownloader:
        del runtime
        return FakeDownloader(self, options)
