"""RSS 2.0 parsing, link normalization, and rendering."""

from __future__ import annotations

import ipaddress
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit


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


@dataclass(slots=True)
class RssLink:
    """A mutable RSS item link and its normalized download identity."""

    element: ET.Element
    original: str
    resolved_url: str
    canonical_url: str


@dataclass(slots=True)
class RssDocument:
    """A parsed RSS document retaining its original source bytes."""

    source: bytes
    root: ET.Element
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
                        element=child,
                        original=original,
                        resolved_url=resolved,
                        canonical_url=canonical,
                    )
                )
        return cls(source=source, root=root, links=tuple(links))

    def render(
        self,
        replacements: Mapping[str, str],
        *,
        absolute_fallback: bool = False,
    ) -> bytes:
        """Render selected local links and optionally absolutize fallbacks."""

        changed = False
        for link in self.links:
            replacement = replacements.get(link.canonical_url)
            if replacement is None and absolute_fallback:
                replacement = link.resolved_url
            if replacement is not None and link.element.text != replacement:
                link.element.text = replacement
                changed = True
        if not changed:
            return self.source
        return ET.tostring(self.root, encoding="utf-8", xml_declaration=True)
