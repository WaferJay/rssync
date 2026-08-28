"""Filesystem paths, hashing, and atomic persistence helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

P_IGNORE_TAGS = [
    re.compile(b"<lastBuildDate>.*?</lastBuildDate>", re.IGNORECASE | re.DOTALL)
]


def ensure_file_directory(file: str | os.PathLike[str]) -> None:
    """Create the parent directory for a file when needed."""

    Path(file).parent.mkdir(parents=True, exist_ok=True)


def md5sum(data: bytes) -> str:
    """Return the hexadecimal MD5 digest used by the legacy API."""

    return hashlib.md5(data).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Hash a file without loading it into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rss_feed_relpath(url: str) -> str:
    """Return the established local path for an RSS URL."""

    parsed = urlsplit(url)
    url_path = unquote(parsed.path.removeprefix("/"))
    if not url_path or url_path.endswith("/"):
        url_path = f"{url_path}index.xml"
    safe_parts = [
        _safe_segment(part) for part in Path(url_path).parts if part not in {"", "."}
    ]
    return os.path.join(_safe_segment(parsed.netloc), *safe_parts)


def rss_feed_local_url(relpath: str) -> str:
    """Return the root-relative public URL of a local RSS file."""

    url_path = relpath.replace(os.sep, "/").lstrip("/")
    return f"/feeds/{url_path}"


def unique_feed_urls(feed_urls: list[str]) -> list[str]:
    """Return URLs in first-seen order, kept for API compatibility."""

    return list(dict.fromkeys(feed_urls))


def _safe_segment(value: str, *, fallback: str = "item") -> str:
    value = unquote(value).strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = value.strip("._")
    return value[:100] or fallback


def webpage_relpath(canonical_url: str) -> str:
    """Build a readable, stable local path from a canonical webpage URL."""

    parsed = urlsplit(canonical_url)
    host = _safe_segment(parsed.netloc, fallback="unknown-host")
    source_parts = [part for part in parsed.path.split("/") if part]
    directory_parts = [_safe_segment(part) for part in source_parts[:-1]]
    leaf = _safe_segment(
        source_parts[-1] if source_parts else "index", fallback="index"
    )
    leaf = leaf.rsplit(".", 1)[0] if "." in leaf else leaf
    digest = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()[:12]
    return Path(host, *directory_parts, f"{leaf}--{digest}.html").as_posix()


def public_webpage_url(public_base_url: str, storage_path: str, relpath: str) -> str:
    """Join the configured absolute base URL with one archived page path."""

    path = Path(storage_path, relpath).as_posix()
    encoded = "/".join(quote(part) for part in path.split("/"))
    return f"{public_base_url.rstrip('/')}/{encoded}"


def temporary_sibling(target: str | os.PathLike[str]) -> Path:
    """Create an empty temporary file next to an atomic-write target."""

    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, path = tempfile.mkstemp(
        prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent
    )
    os.close(descriptor)
    return Path(path)


def commit_download(
    temporary_path: str | os.PathLike[str],
    target_path: str | os.PathLike[str],
    digest: str,
) -> bool:
    """Atomically commit a download and report whether content changed."""

    temporary = Path(temporary_path)
    target = Path(target_path)
    if target.is_file() and sha256_file(target) == digest:
        temporary.unlink(missing_ok=True)
        return False
    os.replace(temporary, target)
    return True


def is_duplicate_rss_file(
    rss_file1: str | os.PathLike[str], rss_file2: str | os.PathLike[str]
) -> bool:
    """Compare RSS files while ignoring volatile ``lastBuildDate`` values."""

    data1 = Path(rss_file1).read_bytes()
    data2 = Path(rss_file2).read_bytes()
    for pattern in P_IGNORE_TAGS:
        data1 = pattern.sub(b"", data1)
        data2 = pattern.sub(b"", data2)
    return md5sum(data1) == md5sum(data2)


def write_rss_if_changed(path: str | os.PathLike[str], data: bytes) -> bool:
    """Atomically write generated RSS unless meaningful content is unchanged."""

    target = Path(path)
    temporary = temporary_sibling(target)
    try:
        temporary.write_bytes(data)
        if target.is_file() and is_duplicate_rss_file(temporary, target):
            temporary.unlink(missing_ok=True)
            return False
        os.replace(temporary, target)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def write_json_atomic(path: str | os.PathLike[str], value: Any) -> None:
    """Write a JSON document with an atomic same-filesystem replacement."""

    target = Path(path)
    temporary = temporary_sibling(target)
    try:
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(value, file, indent=2, ensure_ascii=False)
            file.write("\n")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: str | os.PathLike[str], default: Any) -> Any:
    """Read a JSON document, returning ``default`` when it is unavailable."""

    try:
        with Path(path).open("r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return default
