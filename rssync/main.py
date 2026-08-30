"""Command-line entry point and compatibility exports for rssync."""

from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from rssync.config import load_config, parse_config
from rssync.download_service import DownloadConcurrency, DownloadService
from rssync.downloaders.registry import DownloaderManager
from rssync.manifests import (
    build_feed_manifest,
    load_feed_metadata,
    load_feed_updated_at,
)
from rssync.storage import (
    ensure_file_directory,
    is_duplicate_rss_file,
    md5sum,
    rss_feed_local_url,
    rss_feed_relpath,
    unique_feed_urls,
)
from rssync.sync import (
    RSS_FEED_MANIFEST_PATH,
    RSS_FEED_NEW_PATH,
    RSS_FEED_PATH,
    SyncEngine,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def fetch_rss_xml(url: str, basepath: str = ".") -> tuple[str, str]:
    """Fetch one RSS file using the implicit default downloader preset."""

    config = parse_config({"feeds": [{"url": url}]})
    manager = DownloaderManager(config.downloaders)
    relpath = rss_feed_relpath(url)
    target_path = Path(basepath, relpath)
    limit = config.concurrency.rss_downloads
    service = DownloadService(
        manager,
        DownloadConcurrency(
            limit,
            config.concurrency.webpage_downloads,
            config.concurrency.per_domain_downloads,
            config.concurrency.request_interval,
        ),
    )
    try:
        service.download(
            url=url,
            resource_kind="rss",
            preset_name="default",
            target_path=target_path,
        )
    finally:
        manager.close()
    return str(target_path), relpath


def rss_update_worker(
    url: str, temp_dir: str, target_dir: str
) -> dict[str, Any] | None:
    """Compatibility wrapper for the historical single-feed worker."""

    relpath = rss_feed_relpath(url)
    target_file = Path(target_dir, relpath)
    try:
        temp_file, _ = fetch_rss_xml(url, temp_dir)
        changed = not (
            target_file.exists() and is_duplicate_rss_file(temp_file, target_file)
        )
        if changed:
            ensure_file_directory(target_file)
            shutil.copyfile(temp_file, target_file)
            logger.info("Update RSS feed %s -> %s", temp_file, target_file)
        return {
            "source_url": url,
            "target_path": str(target_file),
            "changed": changed,
            "fetched_at": int(time.time()),
        }
    except Exception:
        logger.exception("Fetch failed: %s", url)
        return None


def main(args: list[str] | None = None) -> None:
    """Run rssync using a JSON configuration path from the command line."""

    arguments = sys.argv if args is None else args
    config_file = (
        Path("rssync-config.json") if len(arguments) <= 1 else Path(arguments[1])
    )
    config = load_config(config_file)
    SyncEngine(config).run()


__all__ = [
    "RSS_FEED_MANIFEST_PATH",
    "RSS_FEED_NEW_PATH",
    "RSS_FEED_PATH",
    "build_feed_manifest",
    "ensure_file_directory",
    "fetch_rss_xml",
    "is_duplicate_rss_file",
    "load_feed_metadata",
    "load_feed_updated_at",
    "main",
    "md5sum",
    "rss_feed_local_url",
    "rss_feed_relpath",
    "rss_update_worker",
    "unique_feed_urls",
]


if __name__ == "__main__":
    main()
