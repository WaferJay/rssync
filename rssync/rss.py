"""RSS 2.0 validation, link normalization, and change detection."""

from __future__ import annotations

import ipaddress
import re
import xml.etree.ElementTree as ET
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

DEFAULT_RSS_IGNORE_TAGS = ("lastBuildDate",)
_UNRESERVED_URL_CHARACTERS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_PERCENT_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")


class RssParseError(ValueError):
    """Raised when downloaded data is not a usable RSS document."""


def _normalize_percent_encoding(value: str) -> str:
    """Normalize valid percent escapes without decoding reserved characters."""

    def replace(match: re.Match[str]) -> str:
        character = chr(int(match.group(1), 16))
        if character in _UNRESERVED_URL_CHARACTERS:
            return character
        return f"%{match.group(1).upper()}"

    return _PERCENT_ESCAPE.sub(replace, value)


def _remove_dot_segments(path: str) -> str:
    """Remove RFC 3986 dot segments while preserving other slash semantics."""

    remaining = path
    output = ""
    while remaining:
        if remaining.startswith("../"):
            remaining = remaining[3:]
        elif remaining.startswith("./"):
            remaining = remaining[2:]
        elif remaining.startswith("/./"):
            remaining = remaining[2:]
        elif remaining == "/.":
            remaining = "/"
        elif remaining.startswith("/../"):
            remaining = remaining[3:]
            output = output.rsplit("/", 1)[0]
        elif remaining == "/..":
            remaining = "/"
            output = output.rsplit("/", 1)[0]
        elif remaining in {".", ".."}:
            remaining = ""
        else:
            separator = remaining.find("/", 1 if remaining.startswith("/") else 0)
            if separator == -1:
                output += remaining
                remaining = ""
            else:
                output += remaining[:separator]
                remaining = remaining[separator:]
    return output


def canonicalize_http_url(
    url: str,
    *,
    ignore_query: bool = False,
) -> str | None:
    """Normalize an HTTP(S) URL for download identity and deduplication.

    ``ignore_query`` affects only the returned identity. Callers can therefore
    deduplicate requests without removing the query from the URL they fetch.
    """

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
        path = _remove_dot_segments(_normalize_percent_encoding(parsed.path or "/"))
        query = "" if ignore_query else _normalize_percent_encoding(parsed.query)
        return urlunsplit((scheme, hostname, path, query, ""))
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
class RssCategory:
    """One RSS item category and its optional taxonomy domain."""

    term: str
    domain: str | None = None


@dataclass(frozen=True, slots=True)
class RssEntry:
    """RSS item metadata used for derived Atom entries."""

    links: tuple[RssLink, ...]
    title: str | None = None
    guid: str | None = None
    pub_date: str | None = None
    description: str | None = None
    author: str | None = None
    categories: tuple[RssCategory, ...] = ()


@dataclass(frozen=True, slots=True)
class RssChannel:
    """RSS channel metadata used for a derived Atom feed."""

    title: str | None = None
    description: str | None = None
    link: str | None = None
    pub_date: str | None = None
    last_build_date: str | None = None
    author: str | None = None


def _direct_children(element: ET.Element, name: str) -> list[ET.Element]:
    expected = name.lower()
    return [
        child
        for child in element
        if _local_name(child.tag).lower() == expected
    ]


def _plain_text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    value = "".join(element.itertext()).strip()
    return value or None


def _inner_xml(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    if not list(element):
        value = (element.text or "").strip()
        return value or None
    parts = [element.text or ""]
    parts.extend(ET.tostring(child, encoding="unicode") for child in element)
    value = "".join(parts).strip()
    return value or None


def _first_child(element: ET.Element, name: str) -> ET.Element | None:
    children = _direct_children(element, name)
    return children[0] if children else None


def _http_link(value: str | None, base_url: str) -> RssLink | None:
    if value is None:
        return None
    original = value.strip()
    if not original:
        return None
    resolved = urljoin(base_url, original)
    canonical = canonicalize_http_url(resolved)
    if canonical is None:
        return None
    return RssLink(
        original=original,
        resolved_url=resolved,
        canonical_url=canonical,
    )


@dataclass(frozen=True, slots=True)
class RssDocument:
    """A validated RSS document retaining its exact source bytes."""

    source: bytes
    links: tuple[RssLink, ...]
    channel: RssChannel = field(default_factory=RssChannel)
    entries: tuple[RssEntry, ...] = ()

    @classmethod
    def parse(cls, source: bytes, base_url: str) -> RssDocument:
        """Parse RSS bytes and resolve direct ``item/link`` elements."""

        try:
            root = ET.fromstring(source)
        except ET.ParseError as error:
            raise RssParseError(f"invalid RSS XML: {error}") from error
        if _local_name(root.tag).lower() != "rss":
            raise RssParseError("document root is not an RSS element")

        channel_element = next(
            (
                child
                for child in root
                if _local_name(child.tag).lower() == "channel"
            ),
            root,
        )
        channel_link = _http_link(
            _plain_text(_first_child(channel_element, "link")),
            base_url,
        )
        channel = RssChannel(
            title=_plain_text(_first_child(channel_element, "title")),
            description=_inner_xml(
                _first_child(channel_element, "description")
            ),
            link=channel_link.resolved_url if channel_link is not None else None,
            pub_date=_plain_text(_first_child(channel_element, "pubDate")),
            last_build_date=_plain_text(
                _first_child(channel_element, "lastBuildDate")
            ),
            author=(
                _plain_text(_first_child(channel_element, "managingEditor"))
                or _plain_text(_first_child(channel_element, "webMaster"))
            ),
        )

        links: list[RssLink] = []
        entries: list[RssEntry] = []
        for item in root.iter():
            if _local_name(item.tag).lower() != "item":
                continue
            item_links: list[RssLink] = []
            for child in item:
                if _local_name(child.tag).lower() != "link":
                    continue
                link = _http_link(_plain_text(child), base_url)
                if link is not None:
                    item_links.append(link)
                    links.append(link)
            categories = tuple(
                RssCategory(
                    term=term,
                    domain=(child.get("domain") or "").strip() or None,
                )
                for child in _direct_children(item, "category")
                if (term := _plain_text(child)) is not None
            )
            entries.append(
                RssEntry(
                    links=tuple(item_links),
                    title=_plain_text(_first_child(item, "title")),
                    guid=_plain_text(_first_child(item, "guid")),
                    pub_date=_plain_text(_first_child(item, "pubDate")),
                    description=_inner_xml(
                        _first_child(item, "description")
                    ),
                    author=_plain_text(_first_child(item, "author")),
                    categories=categories,
                )
            )
        return cls(
            source=source,
            links=tuple(links),
            channel=channel,
            entries=tuple(entries),
        )
