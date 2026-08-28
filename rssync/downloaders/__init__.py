"""Extensible resource downloader API."""

from rssync.downloaders.base import (
    Downloader,
    DownloaderBackendFactory,
    DownloaderRuntimeContext,
    DownloadRequest,
    DownloadResponse,
    PreparedDownloadRequest,
    ResourceKind,
    RetryPolicy,
)
from rssync.downloaders.registry import (
    ENTRY_POINT_GROUP,
    DownloaderManager,
    DownloaderRegistry,
    DownloaderRegistryError,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "DownloadRequest",
    "DownloadResponse",
    "Downloader",
    "DownloaderBackendFactory",
    "DownloaderManager",
    "DownloaderRegistry",
    "DownloaderRegistryError",
    "DownloaderRuntimeContext",
    "PreparedDownloadRequest",
    "ResourceKind",
    "RetryPolicy",
]
