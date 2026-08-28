"""Two-stage RSS and webpage synchronization orchestration."""

from __future__ import annotations

import concurrent.futures
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Hashable, Iterable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, TypeVar

from rssync.config import AppConfig, FeedConfig
from rssync.download_service import (
    DownloadConcurrency,
    DownloadedResource,
    DownloadService,
)
from rssync.downloaders.registry import DownloaderManager, DownloaderRegistry
from rssync.manifests import load_feed_records, load_page_records
from rssync.rss import RssDocument
from rssync.storage import (
    public_webpage_url,
    rss_feed_local_url,
    rss_feed_relpath,
    webpage_relpath,
    write_json_atomic,
    write_rss_if_changed,
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
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class PageTask:
    """One deduplicated webpage download selected in feed order."""

    canonical_url: str
    preset_name: str
    first_feed_url: str
    relpath: str


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
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.root = Path(root)
        self.clock = clock
        self.manager = DownloaderManager(config.downloaders, registry)

    def _preset_limits(self, resource_kind: str) -> dict[str, int]:
        global_limit = (
            self.config.concurrency.rss_downloads
            if resource_kind == "rss"
            else self.config.concurrency.webpage_downloads
        )
        limits: dict[str, int] = {}
        for name, preset in self.config.downloaders.items():
            configured = (
                preset.rss_concurrency
                if resource_kind == "rss"
                else preset.webpage_concurrency
            )
            limits[name] = configured or global_limit
        return limits

    def _run_grouped(
        self,
        tasks: Iterable[tuple[TaskKey, str, TaskValue]],
        limits: Mapping[str, int],
        worker: Callable[[TaskValue], TaskResult],
    ) -> dict[TaskKey, TaskResult]:
        groups: dict[str, list[tuple[TaskKey, TaskValue]]] = defaultdict(list)
        for key, preset_name, task in tasks:
            groups[preset_name].append((key, task))
        results: dict[TaskKey, TaskResult] = {}
        with ExitStack() as stack:
            futures: dict[concurrent.futures.Future[TaskResult], TaskKey] = {}
            for preset_name, group in groups.items():
                executor = stack.enter_context(
                    concurrent.futures.ThreadPoolExecutor(
                        max_workers=min(limits[preset_name], len(group)),
                        thread_name_prefix=f"rssync-{preset_name}",
                    )
                )
                for key, task in group:
                    futures[executor.submit(worker, task)] = key
            for future in concurrent.futures.as_completed(futures):
                results[futures[future]] = future.result()
        return results

    def _fetch_feeds(self) -> dict[int, FeedOutcome]:
        limits = self._preset_limits("rss")
        concurrency = DownloadConcurrency(self.config.concurrency.rss_downloads, limits)
        service = DownloadService(self.manager, concurrency, clock=self.clock)

        def worker(item: tuple[int, FeedConfig]) -> FeedOutcome:
            index, feed = item
            try:
                target = self.root / RSS_FEED_NEW_PATH / rss_feed_relpath(feed.url)
                downloaded = service.download(
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
            (index, feed.rss_downloader, (index, feed))
            for index, feed in enumerate(self.config.feeds)
        ]
        return self._run_grouped(tasks, limits, worker)

    def _select_page_tasks(
        self, feed_outcomes: Mapping[int, FeedOutcome]
    ) -> dict[str, PageTask]:
        tasks: dict[str, PageTask] = {}
        for index, feed in enumerate(self.config.feeds):
            outcome = feed_outcomes[index]
            if not feed.download_webpages or outcome.document is None:
                continue
            for link in outcome.document.links:
                tasks.setdefault(
                    link.canonical_url,
                    PageTask(
                        canonical_url=link.canonical_url,
                        preset_name=feed.webpage_downloader,
                        first_feed_url=feed.url,
                        relpath=webpage_relpath(link.canonical_url),
                    ),
                )
        return tasks

    def _fetch_pages(self, tasks: Mapping[str, PageTask]) -> dict[str, PageOutcome]:
        if not tasks:
            return {}
        limits = self._preset_limits("webpage")
        concurrency = DownloadConcurrency(
            self.config.concurrency.webpage_downloads, limits
        )
        service = DownloadService(self.manager, concurrency, clock=self.clock)

        def worker(task: PageTask) -> PageOutcome:
            try:
                target = self.root / self.config.webpages.storage_path / task.relpath
                downloaded = service.download(
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

        grouped_tasks = [(url, task.preset_name, task) for url, task in tasks.items()]
        return self._run_grouped(grouped_tasks, limits, worker)

    def _valid_page_cache(self, record: Mapping[str, Any] | None) -> bool:
        if not record or not isinstance(record.get("path"), str):
            return False
        path = PurePath(record["path"])
        if path.is_absolute() or ".." in path.parts:
            return False
        return (self.root / path).is_file()

    def _update_pages(
        self,
        tasks: Mapping[str, PageTask],
        outcomes: Mapping[str, PageOutcome],
    ) -> tuple[dict[str, str], dict[str, dict[str, Any]], list[str]]:
        manifest_path = self.root / WEBPAGE_MANIFEST_PATH
        previous = load_page_records(manifest_path)
        records = dict(previous)
        replacements: dict[str, str] = {}
        changed_paths: list[str] = []
        public_base_url = self.config.webpages.public_base_url
        if tasks and public_base_url is None:
            raise AssertionError("validated webpage configuration has no public URL")

        for canonical_url, task in tasks.items():
            outcome = outcomes[canonical_url]
            prior = previous.get(canonical_url)
            if outcome.download is not None:
                downloaded = outcome.download
                path = Path(self.config.webpages.storage_path, task.relpath).as_posix()
                local_url = public_webpage_url(
                    public_base_url or "",
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
                    "local_url": local_url,
                    "content_type": downloaded.content_type,
                    "sha256": downloaded.sha256,
                    "bytes": downloaded.byte_count,
                    "response_headers": dict(downloaded.response_headers),
                    "downloader": task.preset_name,
                    "backend": downloaded.backend_name,
                    "use_session": downloaded.metadata.get("use_session"),
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
                replacements[canonical_url] = local_url
                if downloaded.changed:
                    changed_paths.append(path)
            elif self._valid_page_cache(prior):
                record = dict(prior or {})
                path = record["path"]
                local_url = public_webpage_url(public_base_url or "", "", path)
                record.update(
                    {
                        "local_url": local_url,
                        "changed": False,
                        "status": "cached",
                        "last_error": str(outcome.error),
                    }
                )
                records[canonical_url] = record
                replacements[canonical_url] = local_url
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
        return replacements, records, changed_paths

    def _feed_record(
        self,
        outcome: FeedOutcome,
        replacements: Mapping[str, str],
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

        rendered = outcome.document.render(
            replacements if feed.download_webpages else {},
            absolute_fallback=feed.download_webpages,
        )
        changed = write_rss_if_changed(target, rendered)
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
            "download_webpages": feed.download_webpages,
            "use_session": downloaded.metadata.get("use_session"),
            "user_agent": downloaded.metadata.get("user_agent"),
            "user_agent_strategy": downloaded.metadata.get("user_agent_strategy"),
            "updated_at": updated_at,
            "fetched_at": downloaded.fetched_at,
            "changed": changed,
            "status": "ok",
        }
        return record, public_path if changed else None

    def run(self) -> dict[str, Any]:
        """Execute a complete synchronization and return both manifests."""

        try:
            feed_outcomes = self._fetch_feeds()
            page_tasks = self._select_page_tasks(feed_outcomes)
            page_outcomes = self._fetch_pages(page_tasks)
            replacements, page_records, changed_pages = self._update_pages(
                page_tasks, page_outcomes
            )

            previous_feeds = load_feed_records(self.root / RSS_FEED_MANIFEST_PATH)
            feed_records: list[dict[str, Any]] = []
            changed_feeds: list[str] = []
            for index, feed in enumerate(self.config.feeds):
                record, changed_path = self._feed_record(
                    feed_outcomes[index],
                    replacements,
                    previous_feeds.get(feed.url, {}),
                )
                if record is not None:
                    feed_records.append(record)
                if changed_path is not None:
                    changed_feeds.append(changed_path)

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
            if page_tasks or (self.root / WEBPAGE_MANIFEST_PATH).exists():
                write_json_atomic(self.root / WEBPAGE_MANIFEST_PATH, page_manifest)
            logger.info(
                "Synchronized %d/%d feeds and %d webpages",
                len(feed_records),
                len(self.config.feeds),
                len(page_tasks),
            )
            return {"feeds": feed_manifest, "pages": page_manifest}
        finally:
            self.manager.close()
