"""Manifest compatibility helpers and persisted-record loading."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from rssync.rss import canonicalize_http_url
from rssync.storage import (
    manifest_path_relpath,
    root_relative_manifest_path,
    rss_feed_local_url,
    rss_feed_relpath,
)

logger = logging.getLogger(__name__)


def page_record_source_urls(record: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a page record's primary source URL followed by valid aliases."""

    source_url = record.get("source_url")
    if not isinstance(source_url, str):
        return ()
    urls = [source_url]
    aliases = record.get("aliases")
    if isinstance(aliases, list):
        urls.extend(alias for alias in aliases if isinstance(alias, str) and alias)
    return tuple(dict.fromkeys(urls))


def page_record_identity_url(record: Mapping[str, Any]) -> str | None:
    """Return the URL identity used for a page record's archive path."""

    identity_url = record.get("identity_url")
    if (
        isinstance(identity_url, str)
        and canonicalize_http_url(identity_url) is not None
    ):
        return identity_url
    source_url = record.get("source_url")
    return source_url if isinstance(source_url, str) else None


def index_page_records(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Index page records by every normalized primary URL and alias."""

    indexed: dict[str, Mapping[str, Any]] = {}
    for record in records:
        for source_url in page_record_source_urls(record):
            canonical_url = canonicalize_http_url(source_url)
            if canonical_url is None:
                continue
            existing = indexed.setdefault(canonical_url, record)
            if existing is not record:
                logger.warning(
                    "Ignoring conflicting webpage alias in manifest: %s",
                    source_url,
                )
    return indexed


def _read_manifest(path: str | Path, label: str) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as file:
            manifest = json.load(file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning("Unable to read %s manifest %s", label, path, exc_info=True)
        return {}
    if not isinstance(manifest, dict):
        logger.warning("Ignoring non-object %s manifest %s", label, path)
        return {}
    return manifest


def load_feed_records(path: str | Path = "feeds.json") -> dict[str, dict[str, Any]]:
    """Load complete feed records indexed by source URL."""

    manifest = _read_manifest(path, "feed")
    records = manifest.get("feeds", [])
    if not isinstance(records, list):
        return {}
    return {
        record["source_url"]: dict(record)
        for record in records
        if isinstance(record, dict) and isinstance(record.get("source_url"), str)
    }


def load_page_records(path: str | Path = "pages.json") -> dict[str, dict[str, Any]]:
    """Load and migrate webpage records indexed by their URL identity."""

    manifest = _read_manifest(path, "webpage")
    records = manifest.get("pages", [])
    if not isinstance(records, list):
        return {}
    migrated: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(
            record.get("source_url"), str
        ):
            continue
        normalized = dict(record)
        normalized.pop("local_url", None)
        stored_identity = normalized.get("identity_url")
        identity_url = page_record_identity_url(normalized)
        if identity_url is None:
            continue
        if (
            identity_url == normalized["source_url"]
            or not isinstance(stored_identity, str)
            or canonicalize_http_url(stored_identity) is None
        ):
            normalized.pop("identity_url", None)
        aliases = normalized.get("aliases")
        if isinstance(aliases, list):
            normalized_aliases: list[str] = []
            for alias in aliases:
                if (
                    isinstance(alias, str)
                    and alias
                    and alias != normalized["source_url"]
                    and alias not in normalized_aliases
                ):
                    normalized_aliases.append(alias)
            if normalized_aliases:
                normalized["aliases"] = normalized_aliases
            else:
                normalized.pop("aliases", None)
        else:
            normalized.pop("aliases", None)
        path_value = normalized.get("path")
        if isinstance(path_value, str):
            relative = manifest_path_relpath(path_value)
            if relative is None:
                normalized.pop("path", None)
            else:
                normalized["path"] = root_relative_manifest_path(relative.as_posix())
        migrated[identity_url] = normalized
    return migrated


def load_feed_metadata(
    manifest_path: str | Path = "feeds.json",
) -> dict[str, dict[str, Any]]:
    """Load timestamps while supporting the historical manifest format."""

    manifest = _read_manifest(manifest_path, "feed")

    metadata: dict[str, dict[str, Any]] = {}
    feeds = manifest.get("feeds", [])
    if not isinstance(feeds, list):
        feeds = []
    for feed in feeds:
        if not isinstance(feed, dict):
            continue
        source_url = feed.get("source_url")
        if source_url:
            metadata[source_url] = {
                "updated_at": feed.get("updated_at"),
                "fetched_at": feed.get("fetched_at"),
            }

    legacy_updated_at = manifest.get("update_time")
    if isinstance(legacy_updated_at, (int, float)):
        legacy_updated_at = int(legacy_updated_at // 1000)
    else:
        legacy_updated_at = None
    legacy_feeds = manifest.get("last_updated_feeds", [])
    if not isinstance(legacy_feeds, list):
        legacy_feeds = []
    for feed in legacy_feeds:
        if not isinstance(feed, dict):
            continue
        source_url = feed.get("url")
        if source_url and source_url not in metadata:
            metadata[source_url] = {
                "updated_at": legacy_updated_at,
                "fetched_at": None,
            }
    return metadata


def load_feed_updated_at(manifest_path: str | Path = "feeds.json") -> dict[str, Any]:
    """Return legacy updated-at metadata for callers using the old helper."""

    return {
        source_url: feed_metadata.get("updated_at")
        for source_url, feed_metadata in load_feed_metadata(manifest_path).items()
    }


def build_feed_manifest(
    feed_urls: Iterable[str],
    available_feed_urls: Iterable[str],
    feed_results: Iterable[Mapping[str, Any]],
    previous_feed_metadata: Mapping[str, Mapping[str, Any]],
    sync_time: int,
) -> dict[str, Any]:
    """Build the historical feed manifest shape for API compatibility."""

    available = set(available_feed_urls)
    results_by_url = {result["source_url"]: result for result in feed_results}
    feeds = []
    changed_paths = []

    for source_url in feed_urls:
        if source_url not in available:
            continue
        path = rss_feed_local_url(rss_feed_relpath(source_url))
        previous = previous_feed_metadata.get(source_url, {})
        result = results_by_url.get(source_url)
        changed = bool(result and result["changed"])
        fetched_at = result["fetched_at"] if result else previous.get("fetched_at")
        if changed:
            updated_at = result["fetched_at"]
            changed_paths.append(path)
        else:
            updated_at = previous.get("updated_at")
        feeds.append(
            {
                "path": path,
                "source_url": source_url,
                "updated_at": updated_at,
                "fetched_at": fetched_at,
                "changed": changed,
            }
        )
    return {
        "feeds": feeds,
        "sync": {"completed_at": sync_time, "changed": changed_paths},
    }
