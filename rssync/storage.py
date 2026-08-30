"""Filesystem paths, hashing, and atomic persistence helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Collection
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from rssync.rss import DEFAULT_RSS_IGNORE_TAGS, rss_documents_equal


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
    """Return the root-relative manifest path of a local RSS file."""

    return root_relative_manifest_path("feeds", relpath.replace(os.sep, "/"))


def atom_feed_local_url(storage_path: str, relpath: str) -> str:
    """Return the root-relative path of a derived Atom feed."""

    return root_relative_manifest_path(
        storage_path,
        relpath.replace(os.sep, "/"),
    )


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


def root_relative_manifest_path(*parts: str) -> str:
    """Join safe path components as a root-relative POSIX manifest path."""

    path = PurePosixPath(*parts)
    return f"/{path.as_posix().lstrip('/')}"


def manifest_path_relpath(path: str) -> PurePosixPath | None:
    """Validate a current or legacy manifest path and return its disk path."""

    if not path or path.startswith("//"):
        return None
    value = path.removeprefix("/")
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        return None
    return relative


def webpage_manifest_path(storage_path: str, relpath: str) -> str:
    """Return the root-relative manifest path of an archived webpage."""

    return root_relative_manifest_path(storage_path, relpath)


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
    rss_file1: str | os.PathLike[str],
    rss_file2: str | os.PathLike[str],
    ignore_tags: Collection[str] = DEFAULT_RSS_IGNORE_TAGS,
) -> bool:
    """Compare RSS files while omitting configured XML elements."""

    data1 = Path(rss_file1).read_bytes()
    data2 = Path(rss_file2).read_bytes()
    return rss_documents_equal(data1, data2, ignore_tags)


def write_rss_if_changed(
    path: str | os.PathLike[str],
    data: bytes,
    ignore_tags: Collection[str] = DEFAULT_RSS_IGNORE_TAGS,
) -> bool:
    """Atomically write original RSS unless meaningful content is unchanged."""

    target = Path(path)
    temporary = temporary_sibling(target)
    try:
        temporary.write_bytes(data)
        if target.is_file() and is_duplicate_rss_file(temporary, target, ignore_tags):
            temporary.unlink(missing_ok=True)
            return False
        os.replace(temporary, target)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def write_bytes_if_changed(path: str | os.PathLike[str], data: bytes) -> bool:
    """Atomically write bytes only when the target content changed."""

    target = Path(path)
    if target.is_file() and target.read_bytes() == data:
        return False
    temporary = temporary_sibling(target)
    try:
        temporary.write_bytes(data)
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
