"""Two-stage RSS and webpage synchronization orchestration."""

from __future__ import annotations

import asyncio
import errno
import logging
import time
from collections.abc import Awaitable, Callable, Collection, Hashable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from rssync.config import AppConfig, FeedConfig
from rssync.download_service import (
    DownloadConcurrency,
    DownloadedResource,
    DownloadService,
)
from rssync.downloaders.registry import DownloaderManager, DownloaderRegistry
from rssync.manifests import load_feed_records, load_page_records
from rssync.rss import RssDocument, rss_documents_equal
from rssync.storage import (
    manifest_path_relpath,
    rss_feed_local_url,
    rss_feed_relpath,
    webpage_manifest_path,
    webpage_relpath,
    write_json_atomic,
    write_rss_if_changed,
)
from rssync.webpage_refresh import (
    WebpageRefreshContext,
    WebpageRefreshRegistry,
    WebpageRefreshStrategy,
    default_webpage_refresh_registry,
)

logger = logging.getLogger(__name__)
RSS_FEED_NEW_PATH = ".new-feeds"
RSS_FEED_PATH = "feeds"
RSS_FEED_MANIFEST_PATH = "feeds.json"
WEBPAGE_MANIFEST_PATH = "pages.json"


@dataclass(slots=True)
class FeedOutcome:
    """Result of fetching and parsing one configured feed."""

    index: int
    feed: FeedConfig
    download: DownloadedResource | None = None
    document: RssDocument | None = None
    rss_changed: bool | None = None
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class PageTask:
    """One deduplicated webpage download selected in feed order."""

    canonical_url: str
    preset_name: str
    first_feed_url: str
    relpath: str
    refresh_policy: str


@dataclass(slots=True)
class PageOutcome:
    """Result of one webpage task."""

    task: PageTask
    download: DownloadedResource | None = None
    error: Exception | None = None


TaskKey = TypeVar("TaskKey", bound=Hashable)
TaskValue = TypeVar("TaskValue")
TaskResult = TypeVar("TaskResult")


class SyncEngine:
    """Synchronize configured RSS feeds and optionally archive item pages."""

    def __init__(
        self,
        config: AppConfig,
        *,
        root: str | Path = ".",
        registry: DownloaderRegistry | None = None,
        refresh_registry: WebpageRefreshRegistry | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.root = Path(root)
        self.clock = clock
        self.refresh_registry = (
            refresh_registry or default_webpage_refresh_registry()
        )
        policy_names = {
            feed.webpage_refresh_policy for feed in self.config.feeds
        }
        self.refresh_strategies: dict[str, WebpageRefreshStrategy] = {
            name: self.refresh_registry.resolve(name) for name in policy_names
        }
        self.manager = DownloaderManager(config.downloaders, registry)
        self.concurrency = DownloadConcurrency(
            config.concurrency.rss_downloads,
            config.concurrency.webpage_downloads,
            config.concurrency.per_domain_downloads,
            config.concurrency.request_interval,
        )
        self.service = DownloadService(
            self.manager,
            self.concurrency,
            clock=self.clock,
        )

    async def _run_parallel(
        self,
        tasks: Iterable[tuple[TaskKey, TaskValue]],
        worker: Callable[[TaskValue], Awaitable[TaskResult]],
    ) -> dict[TaskKey, TaskResult]:
        queued = list(tasks)
        if not queued:
            return {}
        scheduled: list[tuple[TaskKey, asyncio.Task[TaskResult]]] = []
        async with asyncio.TaskGroup() as group:
            for key, task in queued:
                scheduled.append((key, group.create_task(worker(task))))
        return {key: task.result() for key, task in scheduled}

    async def _fetch_feeds(self) -> dict[int, FeedOutcome]:
        async def worker(item: tuple[int, FeedConfig]) -> FeedOutcome:
            index, feed = item
            try:
                target = self.root / RSS_FEED_NEW_PATH / rss_feed_relpath(feed.url)
                downloaded = await self.service.download(
                    url=feed.url,
                    resource_kind="rss",
                    preset_name=feed.rss_downloader,
                    target_path=target,
                )
                document = RssDocument.parse(target.read_bytes(), downloaded.final_url)
                return FeedOutcome(index, feed, downloaded, document)
            except Exception as error:
                logger.exception("RSS fetch failed: %s", feed.url)
                return FeedOutcome(index, feed, error=error)

        tasks = [
            (index, (index, feed))
            for index, feed in enumerate(self.config.feeds)
        ]
        return await self._run_parallel(
            tasks,
            worker,
        )

    def _detect_feed_changes(
        self,
        feed_outcomes: Mapping[int, FeedOutcome],
    ) -> None:
        """Determine meaningful RSS changes before selecting webpage tasks."""

        for index, feed in enumerate(self.config.feeds):
            outcome = feed_outcomes[index]
            if outcome.document is None:
                continue
            target = self.root / RSS_FEED_PATH / rss_feed_relpath(feed.url)
            outcome.rss_changed = not target.is_file() or not rss_documents_equal(
                target.read_bytes(),
                outcome.document.source,
                feed.change_detection.ignore_tags,
            )

    def _select_pages(
        self,
        feed_outcomes: Mapping[int, FeedOutcome],
        previous_pages: Mapping[str, Mapping[str, Any]],
    ) -> tuple[dict[str, PageTask], dict[str, PageTask]]:
        candidates: dict[str, PageTask] = {}
        tasks: dict[str, PageTask] = {}
        cache_validity: dict[str, bool] = {}
        for index, feed in enumerate(self.config.feeds):
            outcome = feed_outcomes[index]
            if not feed.download_webpages or outcome.document is None:
                continue
            if outcome.rss_changed is None:
                raise AssertionError("parsed RSS has no change result")
            strategy = self.refresh_strategies[feed.webpage_refresh_policy]
            for link in outcome.document.links:
                task = PageTask(
                    canonical_url=link.canonical_url,
                    preset_name=feed.webpage_downloader,
                    first_feed_url=feed.url,
                    relpath=webpage_relpath(link.canonical_url),
                    refresh_policy=feed.webpage_refresh_policy,
                )
                candidates.setdefault(link.canonical_url, task)
                if link.canonical_url not in cache_validity:
                    cache_validity[link.canonical_url] = self._valid_page_cache(
                        previous_pages.get(link.canonical_url)
                    )
                cache_valid = cache_validity[link.canonical_url]
                context = WebpageRefreshContext(
                    canonical_url=link.canonical_url,
                    feed_url=feed.url,
                    cache_valid=cache_valid,
                    rss_changed=outcome.rss_changed,
                )
                if (
                    link.canonical_url not in tasks
                    and (not cache_valid or strategy.should_fetch(context))
                ):
                    tasks[link.canonical_url] = task
        return candidates, tasks

    def _protected_page_urls(
        self,
        feed_outcomes: Mapping[int, FeedOutcome],
        previous_feeds: Mapping[str, Mapping[str, Any]],
    ) -> tuple[set[str], bool]:
        """Recover webpage references from retained RSS after fetch failures."""

        protected: set[str] = set()
        for index, feed in enumerate(self.config.feeds):
            outcome = feed_outcomes[index]
            if not feed.download_webpages or outcome.document is not None:
                continue

            previous = previous_feeds.get(feed.url, {})
            target = self.root / RSS_FEED_PATH / rss_feed_relpath(feed.url)
            if not target.is_file():
                if previous:
                    logger.warning(
                        "Skipping webpage cleanup because retained RSS is missing: %s",
                        feed.url,
                    )
                    return set(), False
                continue

            base_url = previous.get("final_url", feed.url)
            if not isinstance(base_url, str) or not base_url:
                base_url = feed.url
            try:
                document = RssDocument.parse(target.read_bytes(), base_url)
            except Exception:
                logger.warning(
                    "Skipping webpage cleanup because retained RSS cannot be "
                    "parsed: %s",
                    feed.url,
                    exc_info=True,
                )
                return set(), False
            protected.update(link.canonical_url for link in document.links)
        return protected, True

    async def _fetch_pages(
        self, tasks: Mapping[str, PageTask]
    ) -> dict[str, PageOutcome]:
        if not tasks:
            return {}

        async def worker(task: PageTask) -> PageOutcome:
            try:
                target = self.root / self.config.webpages.storage_path / task.relpath
                downloaded = await self.service.download(
                    url=task.canonical_url,
                    resource_kind="webpage",
                    preset_name=task.preset_name,
                    target_path=target,
                )
                return PageOutcome(task, download=downloaded)
            except Exception as error:
                logger.exception(
                    "Webpage fetch failed: %s",
                    task.canonical_url,
                )
                return PageOutcome(task, error=error)

        queued_tasks = [(url, task) for url, task in tasks.items()]
        return await self._run_parallel(
            queued_tasks,
            worker,
        )

    def _valid_page_cache(self, record: Mapping[str, Any] | None) -> bool:
        if not record or not isinstance(record.get("path"), str):
            return False
        path = manifest_path_relpath(record["path"])
        if path is None:
            return False
        return self.root.joinpath(*path.parts).is_file()

    def _update_pages(
        self,
        candidates: Mapping[str, PageTask],
        outcomes: Mapping[str, PageOutcome],
        previous: Mapping[str, Mapping[str, Any]],
        protected_urls: Collection[str],
        cleanup_safe: bool,
    ) -> tuple[
        dict[str, dict[str, Any]],
        list[str],
        dict[str, dict[str, Any]],
    ]:
        if self.config.archive_current_only and cleanup_safe:
            records = {
                url: dict(record)
                for url, record in previous.items()
                if url in protected_urls
            }
        else:
            records = {url: dict(record) for url, record in previous.items()}
        changed_paths: list[str] = []

        for canonical_url, candidate in candidates.items():
            prior = previous.get(canonical_url)
            outcome = outcomes.get(canonical_url)
            if outcome is None:
                if not self._valid_page_cache(prior):
                    raise AssertionError("skipped webpage has no valid cache")
                record = dict(prior or {})
                record.pop("last_error", None)
                record.update(
                    {
                        "changed": False,
                        "status": "skipped",
                        "skip_reason": candidate.refresh_policy,
                    }
                )
                records[canonical_url] = record
                continue

            task = outcome.task
            if outcome.download is not None:
                downloaded = outcome.download
                path = webpage_manifest_path(
                    self.config.webpages.storage_path,
                    task.relpath,
                )
                previous_updated_at = (prior or {}).get("updated_at")
                updated_at = (
                    downloaded.fetched_at
                    if downloaded.changed or previous_updated_at is None
                    else previous_updated_at
                )
                record = {
                    "source_url": canonical_url,
                    "final_url": downloaded.final_url,
                    "path": path,
                    "content_type": downloaded.content_type,
                    "sha256": downloaded.sha256,
                    "bytes": downloaded.byte_count,
                    "response_headers": dict(downloaded.response_headers),
                    "downloader": task.preset_name,
                    "backend": downloaded.backend_name,
                    "user_agent": downloaded.metadata.get("user_agent"),
                    "user_agent_strategy": downloaded.metadata.get(
                        "user_agent_strategy"
                    ),
                    "first_feed_url": task.first_feed_url,
                    "updated_at": updated_at,
                    "fetched_at": downloaded.fetched_at,
                    "changed": downloaded.changed,
                    "status": "ok",
                }
                records[canonical_url] = record
                if downloaded.changed:
                    changed_paths.append(path)
            elif self._valid_page_cache(prior):
                record = dict(prior or {})
                record.pop("skip_reason", None)
                record.update(
                    {
                        "changed": False,
                        "status": "cached",
                        "last_error": str(outcome.error),
                    }
                )
                records[canonical_url] = record
            else:
                records[canonical_url] = {
                    "source_url": canonical_url,
                    "downloader": task.preset_name,
                    "backend": self.manager.backend_name(task.preset_name),
                    "first_feed_url": task.first_feed_url,
                    "changed": False,
                    "status": "failed",
                    "last_error": str(outcome.error),
                }
        obsolete = (
            {
                url: dict(record)
                for url, record in previous.items()
                if url not in records
                or record.get("path") != records[url].get("path")
            }
            if self.config.archive_current_only and cleanup_safe
            else {}
        )
        return records, changed_paths, obsolete

    def _remove_archive_file(self, target: Path, managed_root: Path) -> bool:
        """Remove one owned archive file and its now-empty parent directories."""

        resolved_root = managed_root.resolve()
        resolved_parent = target.parent.resolve()
        if not resolved_parent.is_relative_to(resolved_root):
            logger.warning(
                "Refusing to delete archive path outside its root: %s",
                target,
            )
            return False

        existed = target.is_file() or target.is_symlink()
        target.unlink(missing_ok=True)
        if existed:
            logger.info("Deleted obsolete archive: %s", target)

        parent = target.parent
        while parent != managed_root:
            try:
                parent.rmdir()
            except FileNotFoundError:
                pass
            except OSError as error:
                if error.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    break
                raise
            parent = parent.parent
        return existed

    def _page_cleanup_target(
        self,
        source_url: str,
        record: Mapping[str, Any],
    ) -> tuple[PurePosixPath, Path, Path] | None:
        """Validate a manifest-owned webpage path and recover its storage root."""

        path = record.get("path")
        if not isinstance(path, str):
            return None
        relative = manifest_path_relpath(path)
        if relative is None:
            logger.warning("Ignoring unsafe obsolete webpage path: %s", path)
            return None

        expected = PurePosixPath(webpage_relpath(source_url))
        expected_length = len(expected.parts)
        if (
            len(relative.parts) < expected_length
            or relative.parts[-expected_length:] != expected.parts
        ):
            logger.warning(
                "Ignoring obsolete webpage path that does not match its URL: %s",
                path,
            )
            return None

        storage_parts = relative.parts[:-expected_length]
        managed_root = self.root.joinpath(*storage_parts)
        target = self.root.joinpath(*relative.parts)
        return relative, target, managed_root

    def _cleanup_obsolete_archives(
        self,
        obsolete_feeds: Mapping[str, Mapping[str, Any]],
        obsolete_pages: Mapping[str, Mapping[str, Any]],
        current_pages: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """Delete only obsolete files whose ownership is proven by old manifests."""

        current_feed_relpaths = {
            PurePosixPath(rss_feed_relpath(feed.url)) for feed in self.config.feeds
        }
        for source_url, record in obsolete_feeds.items():
            expected = PurePosixPath(RSS_FEED_PATH) / PurePosixPath(
                rss_feed_relpath(source_url)
            )
            path = record.get("path")
            recorded = manifest_path_relpath(path) if isinstance(path, str) else None
            if recorded != expected:
                logger.warning(
                    "Ignoring obsolete RSS path that does not match its URL: %s",
                    path,
                )
                continue

            relpath = PurePosixPath(rss_feed_relpath(source_url))
            if relpath in current_feed_relpaths:
                continue
            for directory in (RSS_FEED_PATH, RSS_FEED_NEW_PATH):
                managed_root = self.root / directory
                self._remove_archive_file(
                    managed_root.joinpath(*relpath.parts),
                    managed_root,
                )

        current_page_paths: set[PurePosixPath] = set()
        for source_url, record in current_pages.items():
            cleanup_target = self._page_cleanup_target(source_url, record)
            if cleanup_target is not None:
                current_page_paths.add(cleanup_target[0])
        for source_url, record in obsolete_pages.items():
            cleanup_target = self._page_cleanup_target(source_url, record)
            if cleanup_target is None:
                continue
            relative, target, managed_root = cleanup_target
            if relative in current_page_paths:
                continue
            self._remove_archive_file(target, managed_root)

    def _feed_record(
        self,
        outcome: FeedOutcome,
        previous: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        feed = outcome.feed
        target = self.root / RSS_FEED_PATH / rss_feed_relpath(feed.url)
        public_path = rss_feed_local_url(rss_feed_relpath(feed.url))
        if outcome.download is None or outcome.document is None:
            if not target.is_file():
                return None, None
            record = dict(previous)
            if not record:
                record = {"path": public_path, "source_url": feed.url}
            record.update(
                {
                    "changed": False,
                    "status": "retained",
                    "last_error": str(outcome.error),
                }
            )
            return record, None

        changed = write_rss_if_changed(
            target,
            outcome.document.source,
            feed.change_detection.ignore_tags,
        )
        outcome.rss_changed = changed
        downloaded = outcome.download
        updated_at = downloaded.fetched_at if changed else previous.get("updated_at")
        record = {
            "path": public_path,
            "source_url": feed.url,
            "final_url": downloaded.final_url,
            "rss_downloader": feed.rss_downloader,
            "rss_backend": downloaded.backend_name,
            "webpage_downloader": feed.webpage_downloader,
            "webpage_backend": self.manager.backend_name(feed.webpage_downloader),
            "webpage_refresh_policy": feed.webpage_refresh_policy,
            "download_webpages": feed.download_webpages,
            "change_detection": {
                "ignore_tags": list(feed.change_detection.ignore_tags)
            },
            "user_agent": downloaded.metadata.get("user_agent"),
            "user_agent_strategy": downloaded.metadata.get("user_agent_strategy"),
            "updated_at": updated_at,
            "fetched_at": downloaded.fetched_at,
            "changed": changed,
            "status": "ok",
        }
        return record, public_path if changed else None

    async def run(self) -> dict[str, Any]:
        """Execute a complete synchronization and return both manifests."""

        try:
            previous_feeds = load_feed_records(self.root / RSS_FEED_MANIFEST_PATH)
            previous_pages = load_page_records(self.root / WEBPAGE_MANIFEST_PATH)
            feed_outcomes = await self._fetch_feeds()
            self._detect_feed_changes(feed_outcomes)

            page_candidates, page_tasks = self._select_pages(
                feed_outcomes,
                previous_pages,
            )
            page_outcomes = await self._fetch_pages(page_tasks)
            if self.config.archive_current_only:
                protected_pages, webpage_cleanup_safe = self._protected_page_urls(
                    feed_outcomes,
                    previous_feeds,
                )
            else:
                protected_pages, webpage_cleanup_safe = set(), True
            page_records, changed_pages, obsolete_pages = self._update_pages(
                page_candidates,
                page_outcomes,
                previous_pages,
                protected_pages,
                webpage_cleanup_safe,
            )

            feed_records: list[dict[str, Any]] = []
            changed_feeds: list[str] = []
            for index, feed in enumerate(self.config.feeds):
                record, changed_path = self._feed_record(
                    feed_outcomes[index],
                    previous_feeds.get(feed.url, {}),
                )
                if record is not None:
                    feed_records.append(record)
                if changed_path is not None:
                    changed_feeds.append(changed_path)

            if self.config.archive_current_only:
                configured_feed_urls = {feed.url for feed in self.config.feeds}
                obsolete_feeds = {
                    url: record
                    for url, record in previous_feeds.items()
                    if url not in configured_feed_urls
                }
                self._cleanup_obsolete_archives(
                    obsolete_feeds,
                    obsolete_pages,
                    page_records,
                )

            completed_at = int(self.clock())
            feed_manifest = {
                "feeds": feed_records,
                "sync": {
                    "completed_at": completed_at,
                    "changed": changed_feeds,
                },
            }
            write_json_atomic(self.root / RSS_FEED_MANIFEST_PATH, feed_manifest)

            page_manifest = {
                "pages": list(page_records.values()),
                "sync": {
                    "completed_at": completed_at,
                    "changed": changed_pages,
                },
            }
            if page_candidates or (self.root / WEBPAGE_MANIFEST_PATH).exists():
                write_json_atomic(self.root / WEBPAGE_MANIFEST_PATH, page_manifest)
            logger.info(
                "Synchronized %d/%d feeds and downloaded %d/%d webpages",
                len(feed_records),
                len(self.config.feeds),
                len(page_tasks),
                len(page_candidates),
            )
            return {"feeds": feed_manifest, "pages": page_manifest}
        finally:
            await self.manager.close()
