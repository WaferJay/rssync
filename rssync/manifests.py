"""Manifest compatibility helpers and persisted-record loading."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from rssync.storage import rss_feed_local_url, rss_feed_relpath

logger = logging.getLogger(__name__)


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
    """Load complete webpage records indexed by canonical source URL."""

    manifest = _read_manifest(path, "webpage")
    records = manifest.get("pages", [])
    if not isinstance(records, list):
        return {}
    return {
        record["source_url"]: dict(record)
        for record in records
        if isinstance(record, dict) and isinstance(record.get("source_url"), str)
    }


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
