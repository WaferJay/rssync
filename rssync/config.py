"""Configuration parsing and validation for rssync."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from rssync.rss import DEFAULT_RSS_IGNORE_TAGS
from rssync.webpage_refresh import (
    WebpageRefreshRegistry,
    default_webpage_refresh_registry,
)

DEFAULT_USER_AGENT = "Mozilla/5.0 +https://podnews.net/bot PodnewsBot/1.0"
DEFAULT_HTTPX_OPTIONS: dict[str, Any] = {
    "http2": True,
    "timeout": 30,
    "retries": 3,
    "backoff-factor": 0.5,
    "verify-tls": True,
    "headers": {},
    "user-agent": {
        "strategy": "per-run",
        "fallback": DEFAULT_USER_AGENT,
    },
}


class ConfigError(ValueError):
    """Raised when a configuration document is invalid."""


@dataclass(frozen=True, slots=True)
class ConcurrencyConfig:
    """Global concurrency and request pacing settings."""

    rss_downloads: int = 2
    webpage_downloads: int = 8
    per_domain_downloads: int | None = None
    request_interval: float = 0.0


@dataclass(frozen=True, slots=True)
class RssChangeDetectionConfig:
    """Rules used to decide whether an archived RSS document changed."""

    ignore_tags: tuple[str, ...] = DEFAULT_RSS_IGNORE_TAGS


@dataclass(frozen=True, slots=True)
class RssConfig:
    """Global RSS processing settings."""

    change_detection: RssChangeDetectionConfig = field(
        default_factory=RssChangeDetectionConfig
    )


@dataclass(frozen=True, slots=True)
class AtomConfig:
    """Output settings for Atom feeds that point at archived webpages."""

    storage_path: str = "atoms"
    missing_page_policy: str = "ignore"


@dataclass(frozen=True, slots=True)
class WebpageConfig:
    """Storage, refresh, and derived-feed settings for archived webpages."""

    storage_path: str = "pages"
    refresh_policy: str = "always"
    atom: AtomConfig | None = None


@dataclass(frozen=True, slots=True)
class DownloaderPresetConfig:
    """A named downloader backend and its effective options."""

    name: str
    backend: str
    options: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class FeedConfig:
    """Configuration for one RSS source."""

    url: str
    rss_downloader: str = "default"
    download_webpages: bool = False
    webpage_downloader: str = "default"
    webpage_refresh_policy: str = "always"
    change_detection: RssChangeDetectionConfig = field(
        default_factory=RssChangeDetectionConfig
    )
    webpage_ignore_query: bool = False


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Fully validated rssync configuration."""

    concurrency: ConcurrencyConfig
    rss: RssConfig
    webpages: WebpageConfig
    downloaders: Mapping[str, DownloaderPresetConfig]
    feeds: tuple[FeedConfig, ...]
    archive_current_only: bool = False

    def downloader(self, name: str) -> DownloaderPresetConfig:
        """Return a named preset from the validated configuration."""

        return self.downloaders[name]


def _mapping(value: object, location: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{location} must be an object")
    return value


def _only_keys(data: Mapping[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"unknown {location} field(s): {', '.join(unknown)}")


def _positive_int(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{location} must be a positive integer")
    return value


def _non_negative_number(value: object, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{location} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ConfigError(f"{location} must be a non-negative finite number")
    return result


def _http_url(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{location} must be a non-empty string")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ConfigError(f"{location} must be an absolute HTTP(S) URL")
    try:
        _port = parsed.port
    except ValueError as error:
        raise ConfigError(f"{location} contains an invalid port") from error
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError(f"{location} must not contain credentials")
    return value


def _canonical_config_url(url: str) -> str:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, host, path, parsed.query, ""))


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _parse_concurrency(data: object) -> ConcurrencyConfig:
    if data is None:
        return ConcurrencyConfig()
    values = _mapping(data, "concurrency")
    _only_keys(
        values,
        {
            "rss-downloads",
            "webpage-downloads",
            "per-domain-downloads",
            "request-interval",
        },
        "concurrency",
    )
    return ConcurrencyConfig(
        rss_downloads=_positive_int(
            values.get("rss-downloads", 2), "concurrency.rss-downloads"
        ),
        webpage_downloads=_positive_int(
            values.get("webpage-downloads", 8),
            "concurrency.webpage-downloads",
        ),
        per_domain_downloads=(
            _positive_int(
                values["per-domain-downloads"],
                "concurrency.per-domain-downloads",
            )
            if "per-domain-downloads" in values
            else None
        ),
        request_interval=_non_negative_number(
            values.get("request-interval", 0),
            "concurrency.request-interval",
        ),
    )


def _parse_downloaders(data: object) -> Mapping[str, DownloaderPresetConfig]:
    raw_downloaders = {} if data is None else _mapping(data, "downloaders")
    if any(not isinstance(name, str) or not name for name in raw_downloaders):
        raise ConfigError("downloader preset names must be non-empty strings")

    presets: dict[str, DownloaderPresetConfig] = {}
    names = ["default", *(name for name in raw_downloaders if name != "default")]
    for name in names:
        location = f"downloaders.{name}"
        raw = _mapping(raw_downloaders.get(name, {}), location)
        _only_keys(raw, {"backend", "options"}, location)

        if name == "default":
            backend = raw.get("backend", "httpx")
        elif "backend" not in raw:
            raise ConfigError(f"{location}.backend is required")
        else:
            backend = raw["backend"]
        if not isinstance(backend, str) or not backend:
            raise ConfigError(f"{location}.backend must be a non-empty string")

        options = _mapping(raw.get("options", {}), f"{location}.options")
        if backend == "httpx":
            options = _deep_merge(DEFAULT_HTTPX_OPTIONS, options)
        else:
            options = deepcopy(dict(options))
        presets[name] = DownloaderPresetConfig(
            name=name,
            backend=backend,
            options=_freeze(options),
        )
    return MappingProxyType(presets)


def _parse_change_detection(
    data: object,
    location: str,
    default: RssChangeDetectionConfig,
) -> RssChangeDetectionConfig:
    if data is None:
        return default
    raw = _mapping(data, location)
    _only_keys(raw, {"ignore-tags"}, location)
    if "ignore-tags" not in raw:
        return default

    ignore_tags = raw["ignore-tags"]
    if not isinstance(ignore_tags, list):
        raise ConfigError(f"{location}.ignore-tags must be an array")

    parsed: list[str] = []
    seen: set[str] = set()
    for index, tag in enumerate(ignore_tags):
        tag_location = f"{location}.ignore-tags[{index}]"
        if not isinstance(tag, str) or not tag.strip():
            raise ConfigError(f"{tag_location} must be a non-empty string")
        if tag != tag.strip():
            raise ConfigError(f"{tag_location} must not have surrounding whitespace")
        if tag in seen:
            raise ConfigError(f"duplicate ignored RSS tag: {tag}")
        seen.add(tag)
        parsed.append(tag)
    return RssChangeDetectionConfig(ignore_tags=tuple(parsed))


def _parse_rss(data: object) -> RssConfig:
    if data is None:
        return RssConfig()
    raw = _mapping(data, "rss")
    _only_keys(raw, {"change-detection"}, "rss")
    return RssConfig(
        change_detection=_parse_change_detection(
            raw.get("change-detection"),
            "rss.change-detection",
            RssChangeDetectionConfig(),
        )
    )


def _parse_feeds(
    data: object,
    downloaders: Mapping[str, DownloaderPresetConfig],
    default_change_detection: RssChangeDetectionConfig,
    default_webpage_refresh_policy: str,
    refresh_registry: WebpageRefreshRegistry,
) -> tuple[FeedConfig, ...]:
    if not isinstance(data, list) or not data:
        raise ConfigError("feeds must be a non-empty array of objects")

    feeds: list[FeedConfig] = []
    seen: set[str] = set()
    for index, value in enumerate(data):
        location = f"feeds[{index}]"
        raw = _mapping(value, location)
        _only_keys(
            raw,
            {
                "url",
                "rss-downloader",
                "download-webpages",
                "webpage-downloader",
                "webpage-refresh-policy",
                "webpage-ignore-query",
                "change-detection",
            },
            location,
        )
        if "url" not in raw:
            raise ConfigError(f"{location}.url is required")
        url = _http_url(raw["url"], f"{location}.url")
        canonical_url = _canonical_config_url(url)
        if canonical_url in seen:
            raise ConfigError(f"duplicate RSS URL: {url}")
        seen.add(canonical_url)

        rss_downloader = raw.get("rss-downloader", "default")
        webpage_downloader = raw.get("webpage-downloader", "default")
        if not isinstance(rss_downloader, str) or rss_downloader not in downloaders:
            raise ConfigError(f"{location}.rss-downloader references an unknown preset")
        if (
            not isinstance(webpage_downloader, str)
            or webpage_downloader not in downloaders
        ):
            raise ConfigError(
                f"{location}.webpage-downloader references an unknown preset"
            )
        download_webpages = raw.get("download-webpages", False)
        if not isinstance(download_webpages, bool):
            raise ConfigError(f"{location}.download-webpages must be a boolean")
        webpage_ignore_query = raw.get("webpage-ignore-query", False)
        if not isinstance(webpage_ignore_query, bool):
            raise ConfigError(
                f"{location}.webpage-ignore-query must be a boolean"
            )
        feeds.append(
            FeedConfig(
                url=url,
                rss_downloader=rss_downloader,
                download_webpages=download_webpages,
                webpage_downloader=webpage_downloader,
                webpage_refresh_policy=_parse_refresh_policy(
                    raw.get(
                        "webpage-refresh-policy",
                        default_webpage_refresh_policy,
                    ),
                    f"{location}.webpage-refresh-policy",
                    refresh_registry,
                ),
                webpage_ignore_query=webpage_ignore_query,
                change_detection=_parse_change_detection(
                    raw.get("change-detection"),
                    f"{location}.change-detection",
                    default_change_detection,
                ),
            )
        )
    return tuple(feeds)


def _parse_refresh_policy(
    value: object,
    location: str,
    refresh_registry: WebpageRefreshRegistry,
) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{location} must be a non-empty string")
    try:
        refresh_registry.resolve(value)
    except ValueError as error:
        raise ConfigError(
            f"{location} references an unknown strategy: {value}"
        ) from error
    return value


def _parse_webpages(
    data: object,
    refresh_registry: WebpageRefreshRegistry,
) -> WebpageConfig:
    raw = {} if data is None else _mapping(data, "webpages")
    _only_keys(raw, {"storage-path", "refresh-policy", "atom"}, "webpages")

    storage_path = raw.get("storage-path", "pages")
    if not isinstance(storage_path, str) or not storage_path:
        raise ConfigError("webpages.storage-path must be a non-empty string")
    path = PurePath(storage_path)
    if path.is_absolute() or ".." in path.parts:
        raise ConfigError("webpages.storage-path must be a safe relative path")

    atom = None
    if "atom" in raw:
        atom_raw = _mapping(raw["atom"], "webpages.atom")
        _only_keys(
            atom_raw,
            {"storage-path", "missing-page-policy"},
            "webpages.atom",
        )
        atom_storage_path = atom_raw.get("storage-path", "atoms")
        if not isinstance(atom_storage_path, str) or not atom_storage_path:
            raise ConfigError(
                "webpages.atom.storage-path must be a non-empty string"
            )
        atom_path = PurePath(atom_storage_path)
        if (
            atom_path.is_absolute()
            or ".." in atom_path.parts
            or not atom_path.parts
        ):
            raise ConfigError(
                "webpages.atom.storage-path must be a safe relative path"
            )
        if atom_path.parts[0] in {
            "feeds",
            ".new-feeds",
            "feeds.json",
            "pages.json",
        }:
            raise ConfigError(
                "webpages.atom.storage-path must not overlap managed RSS "
                "or manifest paths"
            )
        missing_page_policy = atom_raw.get("missing-page-policy", "ignore")
        if missing_page_policy not in {"ignore", "source-url"}:
            raise ConfigError(
                "webpages.atom.missing-page-policy must be "
                "'ignore' or 'source-url'"
            )
        atom = AtomConfig(
            storage_path=atom_storage_path,
            missing_page_policy=missing_page_policy,
        )
    return WebpageConfig(
        storage_path=storage_path,
        refresh_policy=_parse_refresh_policy(
            raw.get("refresh-policy", "always"),
            "webpages.refresh-policy",
            refresh_registry,
        ),
        atom=atom,
    )


def parse_config(
    data: object,
    *,
    refresh_registry: WebpageRefreshRegistry | None = None,
) -> AppConfig:
    """Parse a JSON-compatible value into a validated configuration."""

    policy_registry = refresh_registry or default_webpage_refresh_registry()
    raw = _mapping(data, "configuration")
    _only_keys(
        raw,
        {
            "archive-current-only",
            "concurrency",
            "rss",
            "webpages",
            "downloaders",
            "feeds",
        },
        "top-level",
    )
    archive_current_only = raw.get("archive-current-only", False)
    if not isinstance(archive_current_only, bool):
        raise ConfigError("archive-current-only must be a boolean")
    downloaders = _parse_downloaders(raw.get("downloaders"))
    rss = _parse_rss(raw.get("rss"))
    webpages = _parse_webpages(raw.get("webpages"), policy_registry)
    feeds = _parse_feeds(
        raw.get("feeds"),
        downloaders,
        rss.change_detection,
        webpages.refresh_policy,
        policy_registry,
    )
    return AppConfig(
        archive_current_only=archive_current_only,
        concurrency=_parse_concurrency(raw.get("concurrency")),
        rss=rss,
        webpages=webpages,
        downloaders=downloaders,
        feeds=feeds,
    )


def load_config(
    path: str | Path,
    *,
    refresh_registry: WebpageRefreshRegistry | None = None,
) -> AppConfig:
    """Load and validate a JSON configuration file."""

    with Path(path).open("r", encoding="utf-8") as file:
        return parse_config(json.load(file), refresh_registry=refresh_registry)
