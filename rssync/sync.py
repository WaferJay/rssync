"""Two-stage RSS and webpage synchronization orchestration."""

from __future__ import annotations

import asyncio
import errno
import logging
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable, Collection, Hashable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from rssync.atom import build_atom_feed
from rssync.config import AppConfig, FeedConfig
from rssync.download_service import (
    DownloadConcurrency,
    DownloadedResource,
    DownloadService,
)
from rssync.downloaders.registry import DownloaderManager, DownloaderRegistry
from rssync.manifests import (
    index_page_records,
    load_feed_records,
    load_page_records,
    page_record_identity_url,
    page_record_source_urls,
)
from rssync.rss import RssDocument, canonicalize_http_url, rss_documents_equal
from rssync.storage import (
    atom_feed_local_url,
    commit_download,
    manifest_path_relpath,
    rss_feed_local_url,
    rss_feed_relpath,
    temporary_sibling,
    webpage_manifest_path,
    webpage_relpath,
    write_bytes_if_changed,
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
ATOM_RECORD_FIELDS = {
    "atom_path",
    "atom_updated_at",
    "atom_changed",
    "atom_status",
}


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
class PageCandidate:
    """One page identity and all current source URLs known to reference it."""

    source_urls: list[str]
    first_task: PageTask


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
    ) -> tuple[dict[str, PageCandidate], dict[str, PageTask]]:
        candidates: dict[str, PageCandidate] = {}
        tasks: dict[str, PageTask] = {}
        cache_validity: dict[str, bool] = {}

        # Keep separate indexes because query significance is configurable per
        # feed. When several legacy records collapse to one queryless identity,
        # prefer an archive that still exists on disk.
        previous_by_url: dict[bool, dict[str, Mapping[str, Any]]] = {
            False: {},
            True: {},
        }
        for record in previous_pages.values():
            record_urls = list(page_record_source_urls(record))
            final_url = record.get("final_url")
            if isinstance(final_url, str):
                record_urls.append(final_url)
            for ignore_query, index_by_url in previous_by_url.items():
                for record_url in record_urls:
                    identity = canonicalize_http_url(
                        record_url,
                        ignore_query=ignore_query,
                    )
                    if identity is None:
                        continue
                    existing = index_by_url.get(identity)
                    if existing is None or (
                        not self._valid_page_cache(existing)
                        and self._valid_page_cache(record)
                    ):
                        index_by_url[identity] = record

        # An exact URL remains one page even when two feeds use different
        # query settings. If that exact URL occurs in an ignore-query feed, all
        # of its occurrences participate in that feed's queryless group.
        query_ignored_urls: set[str] = set()
        for index, feed in enumerate(self.config.feeds):
            document = feed_outcomes[index].document
            if (
                feed.download_webpages
                and feed.webpage_ignore_query
                and document is not None
            ):
                query_ignored_urls.update(
                    link.canonical_url for link in document.links
                )
        for index, feed in enumerate(self.config.feeds):
            outcome = feed_outcomes[index]
            if not feed.download_webpages or outcome.document is None:
                continue
            if outcome.rss_changed is None:
                raise AssertionError("parsed RSS has no change result")
            strategy = self.refresh_strategies[feed.webpage_refresh_policy]
            for link in outcome.document.links:
                ignore_query = (
                    feed.webpage_ignore_query
                    or link.canonical_url in query_ignored_urls
                )
                link_identity = canonicalize_http_url(
                    link.canonical_url,
                    ignore_query=ignore_query,
                )
                if link_identity is None:
                    raise AssertionError("parsed RSS link has no HTTP identity")
                prior = previous_by_url[ignore_query].get(link_identity)
                prior_identity = (
                    page_record_identity_url(prior) if prior is not None else None
                )
                identity_url = (
                    prior_identity
                    if prior_identity is not None
                    and canonicalize_http_url(prior_identity) is not None
                    else link_identity
                )
                task = PageTask(
                    canonical_url=link.canonical_url,
                    preset_name=feed.webpage_downloader,
                    first_feed_url=feed.url,
                    relpath=webpage_relpath(identity_url),
                    refresh_policy=feed.webpage_refresh_policy,
                )
                candidate = candidates.get(identity_url)
                if candidate is None:
                    candidate = PageCandidate([], task)
                    candidates[identity_url] = candidate
                if link.canonical_url not in candidate.source_urls:
                    candidate.source_urls.append(link.canonical_url)
                if identity_url not in cache_validity:
                    cache_validity[identity_url] = self._valid_page_cache(prior)
                cache_valid = cache_validity[identity_url]
                context = WebpageRefreshContext(
                    canonical_url=link.canonical_url,
                    feed_url=feed.url,
                    cache_valid=cache_valid,
                    rss_changed=outcome.rss_changed,
                )
                if (
                    identity_url not in tasks
                    and (not cache_valid or strategy.should_fetch(context))
                ):
                    tasks[identity_url] = task
        return candidates, tasks

    def _protected_page_urls(
        self,
        feed_outcomes: Mapping[int, FeedOutcome],
        previous_feeds: Mapping[str, Mapping[str, Any]],
    ) -> tuple[set[tuple[bool, str]], bool]:
        """Recover webpage references from retained RSS after fetch failures."""

        protected: set[tuple[bool, str]] = set()
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
            for link in document.links:
                identity = canonicalize_http_url(
                    link.canonical_url,
                    ignore_query=feed.webpage_ignore_query,
                )
                if identity is not None:
                    protected.add((feed.webpage_ignore_query, identity))
        return protected, True

    async def _fetch_pages(
        self,
        tasks: Mapping[str, PageTask],
        staging_root: Path,
    ) -> dict[str, PageOutcome]:
        if not tasks:
            return {}

        async def worker(task: PageTask) -> PageOutcome:
            try:
                target = staging_root / task.relpath
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

    @staticmethod
    def _add_page_aliases(
        record: dict[str, Any],
        source_urls: Iterable[str],
    ) -> None:
        """Add stable, unique aliases without changing the primary source URL."""

        primary = record.get("source_url")
        if not isinstance(primary, str):
            raise AssertionError("webpage record has no primary source URL")
        aliases = list(page_record_source_urls(record)[1:])
        for source_url in source_urls:
            if source_url != primary and source_url not in aliases:
                aliases.append(source_url)
        if aliases:
            record["aliases"] = aliases
        else:
            record.pop("aliases", None)

    @staticmethod
    def _page_record_matches(
        record: Mapping[str, Any],
        identities: Collection[tuple[bool, str]],
    ) -> bool:
        """Return whether a record matches any configured URL identity."""

        return any(
            canonicalize_http_url(source_url, ignore_query=ignore_query)
            == identity
            for source_url in page_record_source_urls(record)
            for ignore_query, identity in identities
        )

    def _atom_document(
        self,
        outcome: FeedOutcome,
        previous: Mapping[str, Any],
    ) -> RssDocument | None:
        """Return the current or last usable RSS document for Atom output."""

        target = self.root / RSS_FEED_PATH / rss_feed_relpath(outcome.feed.url)
        if not target.is_file():
            return outcome.document
        base_url = (
            outcome.download.final_url
            if outcome.download is not None
            else previous.get("final_url", outcome.feed.url)
        )
        if not isinstance(base_url, str) or not base_url:
            base_url = outcome.feed.url
        try:
            return RssDocument.parse(target.read_bytes(), base_url)
        except Exception:
            logger.warning(
                "Retained RSS cannot be used for Atom output: %s",
                outcome.feed.url,
                exc_info=True,
            )
            return None

    @staticmethod
    def _clear_atom_record(record: dict[str, Any]) -> None:
        for field in ATOM_RECORD_FIELDS:
            record.pop(field, None)

    def _valid_recorded_atom(
        self,
        source_url: str,
        record: Mapping[str, Any],
    ) -> bool:
        cleanup_target = self._atom_cleanup_target(source_url, record)
        return cleanup_target is not None and cleanup_target[1].is_file()

    def _write_atom_feeds(
        self,
        feed_outcomes: Mapping[int, FeedOutcome],
        feed_records: list[dict[str, Any]],
        previous_feeds: Mapping[str, Mapping[str, Any]],
        page_records: Mapping[str, Mapping[str, Any]],
        completed_at: int,
    ) -> list[str]:
        """Generate configured Atom feeds and return paths changed this run."""

        records_by_url = {
            record["source_url"]: record
            for record in feed_records
            if isinstance(record.get("source_url"), str)
        }
        atom_config = self.config.webpages.atom
        page_lookup = index_page_records(page_records.values())
        available_pages = {
            source_url
            for source_url, record in page_lookup.items()
            if self._valid_page_cache(record)
        }
        changed_paths: list[str] = []

        for index, feed in enumerate(self.config.feeds):
            record = records_by_url.get(feed.url)
            if record is None:
                continue
            previous = previous_feeds.get(feed.url, {})
            if atom_config is None or not feed.download_webpages:
                self._clear_atom_record(record)
                continue

            document = self._atom_document(feed_outcomes[index], previous)
            if document is None:
                if self._valid_recorded_atom(feed.url, record):
                    record["atom_changed"] = False
                    record["atom_status"] = "retained"
                else:
                    self._clear_atom_record(record)
                continue

            relpath = rss_feed_relpath(feed.url)
            public_path = atom_feed_local_url(atom_config.storage_path, relpath)
            fallback_updated_at = record.get("updated_at")
            if isinstance(fallback_updated_at, bool) or not isinstance(
                fallback_updated_at, (int, float)
            ):
                fallback_updated_at = record.get("fetched_at")
            if isinstance(fallback_updated_at, bool) or not isinstance(
                fallback_updated_at, (int, float)
            ):
                fallback_updated_at = completed_at

            data = build_atom_feed(
                document,
                source_feed_url=feed.url,
                self_path=public_path,
                page_records=page_lookup,
                available_pages=available_pages,
                missing_page_policy=atom_config.missing_page_policy,
                fallback_updated_at=fallback_updated_at,
            )
            target = self.root / atom_config.storage_path / relpath
            changed = write_bytes_if_changed(target, data)
            prior_updated_at = previous.get("atom_updated_at")
            has_prior_updated_at = not isinstance(
                prior_updated_at, bool
            ) and isinstance(prior_updated_at, (int, float))
            record.update(
                {
                    "atom_path": public_path,
                    "atom_updated_at": (
                        completed_at
                        if changed or not has_prior_updated_at
                        else prior_updated_at
                    ),
                    "atom_changed": changed,
                    "atom_status": (
                        "ok"
                        if feed_outcomes[index].document is not None
                        else "retained"
                    ),
                }
            )
            if changed:
                changed_paths.append(public_path)

        return changed_paths

    def _update_pages(
        self,
        candidates: Mapping[str, PageCandidate],
        outcomes: Mapping[str, PageOutcome],
        previous: Mapping[str, Mapping[str, Any]],
        protected_urls: Collection[tuple[bool, str]],
        cleanup_safe: bool,
    ) -> tuple[
        dict[str, dict[str, Any]],
        list[str],
        dict[str, dict[str, Any]],
    ]:
        protected = set(protected_urls)
        if self.config.archive_current_only and cleanup_safe:
            records = {
                url: dict(record)
                for url, record in previous.items()
                if self._page_record_matches(record, protected)
            }
        else:
            records = {url: dict(record) for url, record in previous.items()}
        changed_paths: list[str] = []

        previous_by_final: dict[str, list[str]] = {}
        for identity_url, record in previous.items():
            if canonicalize_http_url(identity_url) is None:
                continue
            final_url = record.get("final_url")
            canonical_final = (
                canonicalize_http_url(final_url)
                if isinstance(final_url, str)
                else None
            )
            if canonical_final is not None:
                previous_by_final.setdefault(canonical_final, []).append(identity_url)

        successful_final = {
            identity_url: canonicalize_http_url(outcome.download.final_url)
            for identity_url, outcome in outcomes.items()
            if outcome.download is not None
        }
        final_targets: dict[str, str] = {}
        target_members: dict[str, list[str]] = {}
        target_final_urls: dict[str, str | None] = {}
        representatives: dict[str, PageOutcome] = {}

        for identity_url, outcome in outcomes.items():
            if outcome.download is None:
                continue
            canonical_final = successful_final[identity_url]
            target_identity: str | None = None
            if canonical_final is not None:
                eligible_previous = [
                    previous_identity
                    for previous_identity in previous_by_final.get(canonical_final, [])
                    if successful_final.get(previous_identity, canonical_final)
                    == canonical_final
                ]
                if eligible_previous:
                    target_identity = next(
                        (
                            previous_identity
                            for previous_identity in eligible_previous
                            if self._valid_page_cache(
                                previous.get(previous_identity)
                            )
                        ),
                        eligible_previous[0],
                    )
                else:
                    target_identity = final_targets.setdefault(
                        canonical_final,
                        identity_url,
                    )
            if target_identity is None:
                target_identity = identity_url
            target_members.setdefault(target_identity, []).append(identity_url)
            target_final_urls[target_identity] = canonical_final
            representatives.setdefault(target_identity, outcome)

        covered_candidates: set[str] = set()
        for target_identity, member_ids in target_members.items():
            representative = representatives[target_identity]
            downloaded = representative.download
            if downloaded is None:
                raise AssertionError("successful webpage group has no download")

            group_final = target_final_urls[target_identity]
            source_urls: list[str] = []
            prior = previous.get(target_identity)
            if prior is not None:
                source_urls.extend(page_record_source_urls(prior))

            candidate_ids = list(member_ids)
            if (
                target_identity in candidates
                and target_identity not in candidate_ids
                and successful_final.get(target_identity, group_final) == group_final
            ):
                candidate_ids.append(target_identity)
            covered_candidates.update(candidate_ids)

            for member_id in member_ids:
                member_prior = previous.get(member_id)
                if member_prior is not None:
                    source_urls.extend(page_record_source_urls(member_prior))
                source_urls.extend(candidates[member_id].source_urls)
                if member_id != target_identity:
                    records.pop(member_id, None)
            if target_identity in candidates:
                source_urls.extend(candidates[target_identity].source_urls)

            relpath = webpage_relpath(target_identity)
            target = self.root / self.config.webpages.storage_path / relpath
            commit_candidate = temporary_sibling(target)
            try:
                shutil.copyfile(downloaded.target_path, commit_candidate)
                changed = commit_download(
                    commit_candidate,
                    target,
                    downloaded.sha256,
                )
            finally:
                commit_candidate.unlink(missing_ok=True)
                downloaded.target_path.unlink(missing_ok=True)
            for member_id in member_ids:
                member_download = outcomes[member_id].download
                if member_download is None or member_download is downloaded:
                    continue
                if member_download.sha256 != downloaded.sha256:
                    logger.warning(
                        "Webpage aliases returned different content for %s; "
                        "keeping the first successful response",
                        downloaded.final_url,
                    )
                member_download.target_path.unlink(missing_ok=True)

            path = webpage_manifest_path(
                self.config.webpages.storage_path,
                relpath,
            )
            previous_updated_at = (prior or {}).get("updated_at")
            updated_at = (
                downloaded.fetched_at
                if changed or previous_updated_at is None
                else previous_updated_at
            )
            primary_source = (prior or {}).get("source_url")
            if not isinstance(primary_source, str):
                primary_source = next(
                    (
                        source_url
                        for source_url in source_urls
                        if canonicalize_http_url(source_url) is not None
                    ),
                    target_identity,
                )
            record = {
                "source_url": primary_source,
                "final_url": downloaded.final_url,
                "path": path,
                "content_type": downloaded.content_type,
                "sha256": downloaded.sha256,
                "bytes": downloaded.byte_count,
                "response_headers": dict(downloaded.response_headers),
                "downloader": representative.task.preset_name,
                "backend": downloaded.backend_name,
                "user_agent": downloaded.metadata.get("user_agent"),
                "user_agent_strategy": downloaded.metadata.get(
                    "user_agent_strategy"
                ),
                "first_feed_url": representative.task.first_feed_url,
                "updated_at": updated_at,
                "fetched_at": downloaded.fetched_at,
                "changed": changed,
                "status": "ok",
            }
            if target_identity != primary_source:
                record["identity_url"] = target_identity
            self._add_page_aliases(record, source_urls)
            records[target_identity] = record
            if changed:
                changed_paths.append(path)

        for identity_url, candidate in candidates.items():
            if identity_url in covered_candidates:
                continue
            prior = previous.get(identity_url)
            outcome = outcomes.get(identity_url)
            if outcome is None:
                if not self._valid_page_cache(prior):
                    raise AssertionError("skipped webpage has no valid cache")
                record = dict(prior or {})
                record.pop("last_error", None)
                self._add_page_aliases(record, candidate.source_urls)
                record.update(
                    {
                        "changed": False,
                        "status": "skipped",
                        "skip_reason": candidate.first_task.refresh_policy,
                    }
                )
                records[identity_url] = record
                continue

            task = outcome.task
            if outcome.download is not None:
                raise AssertionError("successful webpage outcome was not grouped")
            if self._valid_page_cache(prior):
                record = dict(prior or {})
                record.pop("skip_reason", None)
                self._add_page_aliases(record, candidate.source_urls)
                record.update(
                    {
                        "changed": False,
                        "status": "cached",
                        "last_error": str(outcome.error),
                    }
                )
                records[identity_url] = record
            else:
                primary_source = next(
                    iter(candidate.source_urls),
                    identity_url,
                )
                record = {
                    "source_url": primary_source,
                    "downloader": task.preset_name,
                    "backend": self.manager.backend_name(task.preset_name),
                    "first_feed_url": task.first_feed_url,
                    "changed": False,
                    "status": "failed",
                    "last_error": str(outcome.error),
                }
                if identity_url != primary_source:
                    record["identity_url"] = identity_url
                self._add_page_aliases(record, candidate.source_urls)
                records[identity_url] = record
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
        identity_url: str,
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

        expected = PurePosixPath(webpage_relpath(identity_url))
        expected_length = len(expected.parts)
        if (
            len(relative.parts) < expected_length
            or relative.parts[-expected_length:] != expected.parts
        ):
            logger.warning(
                "Ignoring obsolete webpage path that does not match its identity: %s",
                path,
            )
            return None

        storage_parts = relative.parts[:-expected_length]
        managed_root = self.root.joinpath(*storage_parts)
        target = self.root.joinpath(*relative.parts)
        return relative, target, managed_root

    def _atom_cleanup_target(
        self,
        source_url: str,
        record: Mapping[str, Any],
    ) -> tuple[PurePosixPath, Path, Path] | None:
        """Validate a manifest-owned Atom path and recover its storage root."""

        path = record.get("atom_path")
        if not isinstance(path, str):
            return None
        relative = manifest_path_relpath(path)
        if relative is None:
            logger.warning("Ignoring unsafe obsolete Atom path: %s", path)
            return None

        expected = PurePosixPath(rss_feed_relpath(source_url))
        expected_length = len(expected.parts)
        if (
            len(relative.parts) <= expected_length
            or relative.parts[-expected_length:] != expected.parts
        ):
            logger.warning(
                "Ignoring obsolete Atom path that does not match its URL: %s",
                path,
            )
            return None

        storage_parts = relative.parts[:-expected_length]
        if storage_parts[0] in {
            RSS_FEED_PATH,
            RSS_FEED_NEW_PATH,
            RSS_FEED_MANIFEST_PATH,
            WEBPAGE_MANIFEST_PATH,
        }:
            logger.warning("Ignoring Atom path inside a reserved path: %s", path)
            return None
        managed_root = self.root.joinpath(*storage_parts)
        target = self.root.joinpath(*relative.parts)
        return relative, target, managed_root

    def _cleanup_obsolete_archives(
        self,
        obsolete_feeds: Mapping[str, Mapping[str, Any]],
        obsolete_atoms: Mapping[str, Mapping[str, Any]],
        obsolete_pages: Mapping[str, Mapping[str, Any]],
        current_feeds: Mapping[str, Mapping[str, Any]],
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

        current_atom_paths: set[PurePosixPath] = set()
        for source_url, record in current_feeds.items():
            cleanup_target = self._atom_cleanup_target(source_url, record)
            if cleanup_target is not None:
                current_atom_paths.add(cleanup_target[0])
        for source_url, record in obsolete_atoms.items():
            cleanup_target = self._atom_cleanup_target(source_url, record)
            if cleanup_target is None:
                continue
            relative, target, managed_root = cleanup_target
            if relative in current_atom_paths:
                continue
            self._remove_archive_file(target, managed_root)

        current_page_paths: set[PurePosixPath] = set()
        for identity_url, record in current_pages.items():
            cleanup_target = self._page_cleanup_target(identity_url, record)
            if cleanup_target is not None:
                current_page_paths.add(cleanup_target[0])
        for identity_url, record in obsolete_pages.items():
            cleanup_target = self._page_cleanup_target(identity_url, record)
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
            "webpage_ignore_query": feed.webpage_ignore_query,
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
            if self.config.archive_current_only:
                protected_pages, webpage_cleanup_safe = (
                    self._protected_page_urls(
                        feed_outcomes,
                        previous_feeds,
                    )
                )
            else:
                protected_pages, webpage_cleanup_safe = set(), True

            if page_tasks:
                with tempfile.TemporaryDirectory(
                    prefix=".rssync-pages-",
                    dir=self.root,
                ) as staging_directory:
                    page_outcomes = await self._fetch_pages(
                        page_tasks,
                        Path(staging_directory),
                    )
                    page_records, changed_pages, obsolete_pages = (
                        self._update_pages(
                            page_candidates,
                            page_outcomes,
                            previous_pages,
                            protected_pages,
                            webpage_cleanup_safe,
                        )
                    )
            else:
                page_outcomes = {}
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

            completed_at = int(self.clock())
            changed_atoms = self._write_atom_feeds(
                feed_outcomes,
                feed_records,
                previous_feeds,
                page_records,
                completed_at,
            )

            if self.config.archive_current_only:
                current_feed_records = {
                    record["source_url"]: record
                    for record in feed_records
                    if isinstance(record.get("source_url"), str)
                }
                configured_feed_urls = {feed.url for feed in self.config.feeds}
                atom_enabled_urls = {
                    feed.url
                    for feed in self.config.feeds
                    if self.config.webpages.atom is not None
                    and feed.download_webpages
                }
                obsolete_feeds = {
                    url: record
                    for url, record in previous_feeds.items()
                    if url not in configured_feed_urls
                }
                obsolete_atoms = {
                    url: record
                    for url, record in previous_feeds.items()
                    if isinstance(record.get("atom_path"), str)
                    and (
                        url not in atom_enabled_urls
                        or (
                            url in current_feed_records
                            and record.get("atom_path")
                            != current_feed_records[url].get("atom_path")
                        )
                    )
                }
                self._cleanup_obsolete_archives(
                    obsolete_feeds,
                    obsolete_atoms,
                    obsolete_pages,
                    current_feed_records,
                    page_records,
                )

            feed_sync = {
                "completed_at": completed_at,
                "changed": changed_feeds,
            }
            if self.config.webpages.atom is not None:
                feed_sync["changed_atoms"] = changed_atoms
            feed_manifest = {"feeds": feed_records, "sync": feed_sync}
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
