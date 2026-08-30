"""Generate deterministic Atom feeds for locally archived webpages."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

from rssync.rss import RssDocument, RssEntry, RssLink

ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
_ABSOLUTE_IRI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

ET.register_namespace("", ATOM_NAMESPACE)


def _atom(name: str) -> str:
    return f"{{{ATOM_NAMESPACE}}}{name}"


def _rss_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(microsecond=0)


def _unix_datetime(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None


def _rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _is_absolute_iri(value: str | None) -> bool:
    return bool(
        value
        and _ABSOLUTE_IRI.match(value)
        and not any(character.isspace() for character in value)
    )


def _entry_id(feed_url: str, entry: RssEntry, link: RssLink) -> str:
    if _is_absolute_iri(entry.guid):
        return entry.guid or link.canonical_url
    if entry.guid:
        identity = f"{feed_url}\0{entry.guid}".encode("utf-8")
        return f"urn:rssync:entry:{hashlib.sha256(identity).hexdigest()}"
    return link.canonical_url


def _valid_media_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(character in value for character in "\r\n"):
        return None
    return value


def _author(parent: ET.Element, value: str) -> None:
    author = ET.SubElement(parent, _atom("author"))
    ET.SubElement(author, _atom("name")).text = value


def _link(
    parent: ET.Element,
    relation: str,
    href: str,
    *,
    media_type: str | None = None,
) -> None:
    attributes = {"rel": relation, "href": href}
    if media_type is not None:
        attributes["type"] = media_type
    ET.SubElement(parent, _atom("link"), attributes)


def _entry_candidates(
    document: RssDocument,
    page_records: Mapping[str, Mapping[str, Any]],
    available_pages: Collection[str],
    missing_page_policy: str,
) -> list[tuple[RssEntry, RssLink, Mapping[str, Any] | None, datetime | None]]:
    included = []
    for entry in document.entries:
        published = _rss_datetime(entry.pub_date)
        for link in entry.links:
            record = page_records.get(link.canonical_url)
            has_local_page = (
                link.canonical_url in available_pages and record is not None
            )
            if not has_local_page and missing_page_policy == "ignore":
                continue
            page_updated = _unix_datetime(record.get("updated_at")) if record else None
            updated = max(
                (candidate for candidate in (published, page_updated) if candidate),
                default=None,
            )
            included.append((entry, link, record if has_local_page else None, updated))
    return included


def build_atom_feed(
    document: RssDocument,
    *,
    source_feed_url: str,
    self_path: str,
    page_records: Mapping[str, Mapping[str, Any]],
    available_pages: Collection[str],
    missing_page_policy: str,
    fallback_updated_at: int | float,
) -> bytes:
    """Return one Atom document whose alternate links target archived pages."""

    if missing_page_policy not in {"ignore", "source-url"}:
        raise ValueError(f"unknown Atom missing-page policy: {missing_page_policy}")
    candidates = _entry_candidates(
        document,
        page_records,
        available_pages,
        missing_page_policy,
    )
    feed_fallback = _unix_datetime(fallback_updated_at) or datetime.fromtimestamp(
        0, UTC
    )
    dated_candidates = [
        candidate
        for candidate in (
            _rss_datetime(document.channel.last_build_date),
            _rss_datetime(document.channel.pub_date),
            feed_fallback,
            *(updated for _, _, _, updated in candidates),
        )
        if candidate is not None
    ]
    feed_updated = max(dated_candidates, default=feed_fallback)

    root = ET.Element(_atom("feed"))
    ET.SubElement(root, _atom("title")).text = (
        document.channel.title or source_feed_url
    )
    if document.channel.description:
        subtitle = ET.SubElement(root, _atom("subtitle"), {"type": "html"})
        subtitle.text = document.channel.description
    ET.SubElement(root, _atom("id")).text = source_feed_url
    ET.SubElement(root, _atom("updated")).text = _rfc3339(feed_updated)
    _author(root, document.channel.author or "rssync")
    _link(root, "self", self_path, media_type="application/atom+xml")
    if document.channel.link:
        _link(root, "alternate", document.channel.link)
    _link(root, "via", source_feed_url, media_type="application/rss+xml")

    for rss_entry, source_link, page_record, entry_updated in candidates:
        entry = ET.SubElement(root, _atom("entry"))
        ET.SubElement(entry, _atom("title")).text = (
            rss_entry.title or source_link.resolved_url
        )
        ET.SubElement(entry, _atom("id")).text = _entry_id(
            source_feed_url,
            rss_entry,
            source_link,
        )
        updated = max(
            (
                candidate
                for candidate in (entry_updated, feed_fallback)
                if candidate is not None
            ),
            default=feed_updated,
        )
        ET.SubElement(entry, _atom("updated")).text = _rfc3339(updated)
        published = _rss_datetime(rss_entry.pub_date)
        if published is not None:
            ET.SubElement(entry, _atom("published")).text = _rfc3339(published)

        if page_record is not None:
            path = str(page_record["path"])
            _link(
                entry,
                "alternate",
                path,
                media_type=_valid_media_type(page_record.get("content_type")),
            )
            _link(entry, "via", source_link.resolved_url)
        else:
            _link(entry, "alternate", source_link.resolved_url)

        if rss_entry.description:
            summary = ET.SubElement(entry, _atom("summary"), {"type": "html"})
            summary.text = rss_entry.description
        if rss_entry.author:
            _author(entry, rss_entry.author)
        for category in rss_entry.categories:
            attributes = {"term": category.term}
            if _is_absolute_iri(category.domain):
                attributes["scheme"] = category.domain or ""
            ET.SubElement(entry, _atom("category"), attributes)

    return ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    ) + b"\n"
