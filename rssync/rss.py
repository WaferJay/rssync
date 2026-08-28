"""RSS 2.0 validation, link normalization, and change detection."""

from __future__ import annotations

import ipaddress
import xml.etree.ElementTree as ET
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

DEFAULT_RSS_IGNORE_TAGS = ("lastBuildDate",)


class RssParseError(ValueError):
    """Raised when downloaded data is not a usable RSS document."""


def canonicalize_http_url(url: str) -> str | None:
    """Normalize an HTTP(S) URL for download identity and deduplication."""

    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname.lower().encode("idna").decode("ascii")
        try:
            if isinstance(ipaddress.ip_address(hostname), ipaddress.IPv6Address):
                hostname = f"[{hostname}]"
        except ValueError:
            pass
        port = parsed.port
        if port is not None and not (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        ):
            hostname = f"{hostname}:{port}"
        return urlunsplit((scheme, hostname, parsed.path or "/", parsed.query, ""))
    except (UnicodeError, ValueError):
        return None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _append_text(parts: list[tuple[str, Any]], value: str | None) -> None:
    """Append meaningful mixed-content text to an XML comparison key."""

    if value is None or not value.strip():
        return
    if parts and parts[-1][0] == "text":
        parts[-1] = ("text", parts[-1][1] + value)
    else:
        parts.append(("text", value))


def _element_comparison_key(
    element: ET.Element,
    ignored: frozenset[str],
) -> tuple[Any, ...] | None:
    """Return a namespace-aware structural key without ignored elements."""

    if _local_name(element.tag) in ignored:
        return None

    content: list[tuple[str, Any]] = []
    _append_text(content, element.text)
    for child in element:
        child_key = _element_comparison_key(child, ignored)
        if child_key is not None:
            content.append(("element", child_key))
        _append_text(content, child.tail)
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        tuple(content),
    )


def rss_documents_equal(
    first: bytes,
    second: bytes,
    ignore_tags: Collection[str] = (),
) -> bool:
    """Compare RSS bytes, omitting configured XML elements when requested."""

    if first == second:
        return True
    ignored = frozenset(ignore_tags)
    if not ignored:
        return False
    try:
        first_root = ET.fromstring(first)
        second_root = ET.fromstring(second)
    except ET.ParseError:
        return False
    return _element_comparison_key(first_root, ignored) == _element_comparison_key(
        second_root, ignored
    )


@dataclass(frozen=True, slots=True)
class RssLink:
    """An RSS item link and its normalized download identity."""

    original: str
    resolved_url: str
    canonical_url: str


@dataclass(frozen=True, slots=True)
class RssDocument:
    """A validated RSS document retaining its exact source bytes."""

    source: bytes
    links: tuple[RssLink, ...]

    @classmethod
    def parse(cls, source: bytes, base_url: str) -> RssDocument:
        """Parse RSS bytes and resolve direct ``item/link`` elements."""

        try:
            root = ET.fromstring(source)
        except ET.ParseError as error:
            raise RssParseError(f"invalid RSS XML: {error}") from error
        if _local_name(root.tag).lower() != "rss":
            raise RssParseError("document root is not an RSS element")

        links: list[RssLink] = []
        for item in root.iter():
            if _local_name(item.tag).lower() != "item":
                continue
            for child in item:
                if _local_name(child.tag).lower() != "link" or not child.text:
                    continue
                original = child.text.strip()
                if not original:
                    continue
                resolved = urljoin(base_url, original)
                canonical = canonicalize_http_url(resolved)
                if canonical is None:
                    continue
                links.append(
                    RssLink(
                        original=original,
                        resolved_url=resolved,
                        canonical_url=canonical,
                    )
                )
        return cls(source=source, links=tuple(links))
