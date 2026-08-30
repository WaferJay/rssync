import json
import tempfile
import unittest
from pathlib import Path

from rssync.config import parse_config
from rssync.downloaders.registry import DownloaderRegistry
from rssync.sync import SyncEngine
from rssync.webpage_refresh import default_webpage_refresh_registry
from tests.fakes import FakeBackendFactory, FakeReply


def registry_with(factory):
    registry = DownloaderRegistry(load_plugins=False)
    registry.register("fake", factory)
    return registry


def rss_with_links(*urls: str) -> bytes:
    items = b"".join(
        b"<item><link>" + url.encode() + b"</link></item>" for url in urls
    )
    return b'<rss version="2.0"><channel>' + items + b"</channel></rss>"


class NeverRefreshStrategy:
    name = "never"

    def should_fetch(self, context):
        return False


class SyncEngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_webpages_are_disabled_by_default(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/article"
        feed_body = (
            b'<rss version="2.0"><channel><item><link>'
            + page_url.encode()
            + b"</link></item></channel></rss>"
        )
        factory = FakeBackendFactory({feed_url: FakeReply(feed_body)})
        config = parse_config(
            {
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            await SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
                clock=lambda: 100,
            ).run()

            output = Path(directory, "feeds/example.com/feed.xml")
            self.assertEqual(output.read_bytes(), feed_body)
            self.assertEqual(factory.calls, [("rss", feed_url, "default")])
            self.assertFalse(Path(directory, "pages.json").exists())

    async def test_enabled_webpage_and_rss_are_both_stored_raw(self):
        feed_url = "https://example.com/news/feed.xml"
        page_url = "https://example.com/article?id=1"
        page_body = b"\xff<html><body>raw bytes</body></html>"
        feed_body = (
            b'<rss version="2.0"><channel><item><link>'
            b"/article?id=1"
            b"</link></item></channel></rss>"
        )
        factory = FakeBackendFactory(
            {
                feed_url: FakeReply(feed_body),
                page_url: FakeReply(
                    page_body, content_type="text/html; charset=latin-1"
                ),
            }
        )
        config = parse_config(
            {
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            manifests = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
                clock=lambda: 200,
            ).run()

            page_record = manifests["pages"]["pages"][0]
            archived = Path(directory, page_record["path"].removeprefix("/"))
            self.assertEqual(archived.read_bytes(), page_body)
            self.assertTrue(page_record["path"].startswith("/pages/"))
            self.assertNotIn("local_url", page_record)
            self.assertEqual(
                Path(directory, "feeds/example.com/news/feed.xml").read_bytes(),
                feed_body,
            )
            self.assertEqual(
                manifests["pages"]["sync"]["changed"], [page_record["path"]]
            )
            self.assertEqual(
                factory.calls,
                [
                    ("rss", feed_url, "default"),
                    ("webpage", page_url, "default"),
                ],
            )

    async def test_on_rss_change_skips_when_only_ignored_tags_change(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/article"
        first_rss = (
            b'<rss version="2.0"><channel><lastBuildDate>one</lastBuildDate>'
            b"<item><link>"
            + page_url.encode()
            + b"</link></item></channel></rss>"
        )
        second_rss = first_rss.replace(b">one<", b">two<")
        config = parse_config(
            {
                "webpages": {"refresh-policy": "on-rss-change"},
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            first = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(first_rss),
                            page_url: FakeReply(b"<html>original</html>", "text/html"),
                        }
                    )
                ),
                clock=lambda: 100,
            ).run()
            page_path = Path(
                directory,
                first["pages"]["pages"][0]["path"].removeprefix("/"),
            )
            second_factory = FakeBackendFactory({feed_url: FakeReply(second_rss)})

            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(second_factory),
                clock=lambda: 200,
            ).run()

            page_record = second["pages"]["pages"][0]
            self.assertEqual(second_factory.calls, [("rss", feed_url, "default")])
            self.assertEqual(page_path.read_bytes(), b"<html>original</html>")
            self.assertEqual(page_record["status"], "skipped")
            self.assertEqual(page_record["skip_reason"], "on-rss-change")
            self.assertEqual(page_record["updated_at"], 100)
            self.assertEqual(page_record["fetched_at"], 100)
            self.assertFalse(page_record["changed"])
            self.assertEqual(second["pages"]["sync"]["changed"], [])
            self.assertEqual(
                second["feeds"]["feeds"][0]["webpage_refresh_policy"],
                "on-rss-change",
            )

    async def test_on_rss_change_refreshes_after_a_meaningful_change(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/article"
        first_rss = rss_with_links(page_url)
        second_rss = first_rss.replace(b"<channel>", b"<channel><title>new</title>")
        config = parse_config(
            {
                "webpages": {"refresh-policy": "on-rss-change"},
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(first_rss),
                            page_url: FakeReply(b"<html>old</html>", "text/html"),
                        }
                    )
                ),
                clock=lambda: 100,
            ).run()
            second_factory = FakeBackendFactory(
                {
                    feed_url: FakeReply(second_rss),
                    page_url: FakeReply(b"<html>new</html>", "text/html"),
                }
            )

            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(second_factory),
                clock=lambda: 200,
            ).run()

            page_record = second["pages"]["pages"][0]
            self.assertIn(("webpage", page_url, "default"), second_factory.calls)
            self.assertEqual(page_record["status"], "ok")
            self.assertEqual(page_record["updated_at"], 200)
            self.assertEqual(page_record["fetched_at"], 200)

    async def test_missing_only_preserves_cache_and_repairs_invalid_cache(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/article"
        first_rss = rss_with_links(page_url)
        changed_rss = first_rss.replace(b"<channel>", b"<channel><title>new</title>")
        config = parse_config(
            {
                "webpages": {"refresh-policy": "missing-only"},
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            first = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(first_rss),
                            page_url: FakeReply(b"<html>original</html>", "text/html"),
                        }
                    )
                ),
                clock=lambda: 100,
            ).run()
            page_path = Path(
                directory,
                first["pages"]["pages"][0]["path"].removeprefix("/"),
            )
            second_factory = FakeBackendFactory({feed_url: FakeReply(changed_rss)})

            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(second_factory),
                clock=lambda: 200,
            ).run()

            self.assertEqual(second_factory.calls, [("rss", feed_url, "default")])
            self.assertEqual(page_path.read_bytes(), b"<html>original</html>")
            self.assertEqual(second["pages"]["pages"][0]["status"], "skipped")
            page_path.unlink()
            repair_factory = FakeBackendFactory(
                {
                    feed_url: FakeReply(changed_rss),
                    page_url: FakeReply(b"<html>repaired</html>", "text/html"),
                }
            )

            repaired = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(repair_factory),
                clock=lambda: 300,
            ).run()

            self.assertIn(("webpage", page_url, "default"), repair_factory.calls)
            self.assertEqual(page_path.read_bytes(), b"<html>repaired</html>")
            self.assertEqual(repaired["pages"]["pages"][0]["status"], "ok")
            self.assertEqual(repaired["pages"]["pages"][0]["fetched_at"], 300)

            unsafe_manifest = repaired["pages"]
            unsafe_manifest["pages"][0]["path"] = "/../outside.html"
            Path(directory, "pages.json").write_text(
                json.dumps(unsafe_manifest),
                encoding="utf-8",
            )
            reindex_factory = FakeBackendFactory(
                {
                    feed_url: FakeReply(changed_rss),
                    page_url: FakeReply(b"<html>reindexed</html>", "text/html"),
                }
            )

            reindexed = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(reindex_factory),
                clock=lambda: 400,
            ).run()

            self.assertIn(("webpage", page_url, "default"), reindex_factory.calls)
            self.assertEqual(page_path.read_bytes(), b"<html>reindexed</html>")
            self.assertEqual(reindexed["pages"]["pages"][0]["status"], "ok")
            self.assertEqual(reindexed["pages"]["pages"][0]["fetched_at"], 400)

    async def test_shared_page_uses_first_feed_whose_policy_requests_refresh(self):
        first_feed = "https://one.example/feed.xml"
        second_feed = "https://two.example/feed.xml"
        page_url = "https://shared.example/article"
        first_rss = rss_with_links(page_url)
        second_rss = rss_with_links(page_url)
        changed_second_rss = second_rss.replace(
            b"<channel>",
            b"<channel><title>changed</title>",
        )
        config = parse_config(
            {
                "downloaders": {
                    "default": {"backend": "fake"},
                    "first": {"backend": "fake", "options": {"label": "first"}},
                    "second": {
                        "backend": "fake",
                        "options": {"label": "second"},
                    },
                },
                "feeds": [
                    {
                        "url": first_feed,
                        "download-webpages": True,
                        "webpage-downloader": "first",
                        "webpage-refresh-policy": "missing-only",
                    },
                    {
                        "url": second_feed,
                        "download-webpages": True,
                        "webpage-downloader": "second",
                        "webpage-refresh-policy": "on-rss-change",
                    },
                ],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            first_feed: FakeReply(first_rss),
                            second_feed: FakeReply(second_rss),
                            page_url: FakeReply(b"<html>first</html>", "text/html"),
                        }
                    )
                ),
            ).run()
            second_factory = FakeBackendFactory(
                {
                    first_feed: FakeReply(first_rss),
                    second_feed: FakeReply(changed_second_rss),
                    page_url: FakeReply(b"<html>second</html>", "text/html"),
                }
            )

            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(second_factory),
            ).run()

            page_calls = [call for call in second_factory.calls if call[0] == "webpage"]
            self.assertEqual(page_calls, [("webpage", page_url, "second")])
            self.assertEqual(
                second["pages"]["pages"][0]["first_feed_url"],
                second_feed,
            )

    async def test_custom_refresh_strategy_is_used_without_engine_changes(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/article"
        rss = rss_with_links(page_url)
        refresh_registry = default_webpage_refresh_registry()
        refresh_registry.register(NeverRefreshStrategy())
        config = parse_config(
            {
                "webpages": {"refresh-policy": "never"},
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            },
            refresh_registry=refresh_registry,
        )

        with tempfile.TemporaryDirectory() as directory:
            await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(rss),
                            page_url: FakeReply(b"<html>cached</html>", "text/html"),
                        }
                    )
                ),
                refresh_registry=refresh_registry,
            ).run()
            second_factory = FakeBackendFactory({feed_url: FakeReply(rss)})

            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(second_factory),
                refresh_registry=refresh_registry,
            ).run()

            self.assertEqual(second_factory.calls, [("rss", feed_url, "default")])
            self.assertEqual(second["pages"]["pages"][0]["status"], "skipped")
            self.assertEqual(second["pages"]["pages"][0]["skip_reason"], "never")

    async def test_first_enabled_feed_selects_the_page_preset(self):
        first_feed = "https://one.example/feed.xml"
        second_feed = "https://two.example/feed.xml"
        page_url = "https://shared.example/article"
        rss = (
            b'<rss version="2.0"><channel><item><link>'
            + page_url.encode()
            + b"</link></item></channel></rss>"
        )
        factory = FakeBackendFactory(
            {
                first_feed: FakeReply(rss, delay=0.02),
                second_feed: FakeReply(rss),
                page_url: FakeReply(b"<html>shared</html>", "text/html"),
            }
        )
        config = parse_config(
            {
                "downloaders": {
                    "default": {"backend": "fake"},
                    "first": {
                        "backend": "fake",
                        "options": {"label": "first"},
                    },
                    "second": {
                        "backend": "fake",
                        "options": {"label": "second"},
                    },
                },
                "feeds": [
                    {
                        "url": first_feed,
                        "download-webpages": True,
                        "webpage-downloader": "first",
                    },
                    {
                        "url": second_feed,
                        "download-webpages": True,
                        "webpage-downloader": "second",
                    },
                ],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            await SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
            ).run()

        page_calls = [call for call in factory.calls if call[0] == "webpage"]
        self.assertEqual(page_calls, [("webpage", page_url, "first")])

    async def test_rss_stage_honors_global_and_cross_preset_domain_concurrency(self):
        feeds = [
            (f"https://{hostname}/feed-{index}.xml", index)
            for index in range(3)
            for hostname in ("example.com", "example.org")
        ]
        rss = b'<rss version="2.0"><channel /></rss>'
        factory = FakeBackendFactory(
            {url: FakeReply(rss, delay=0.03) for url, _ in feeds}
        )
        config = parse_config(
            {
                "concurrency": {
                    "rss-downloads": 4,
                    "webpage-downloads": 2,
                    "per-domain-downloads": 1,
                },
                "downloaders": {
                    "default": {
                        "backend": "fake",
                        "options": {"label": "one"},
                    },
                    "two": {
                        "backend": "fake",
                        "options": {"label": "two"},
                    },
                },
                "feeds": [
                    {
                        "url": url,
                        "rss-downloader": "default" if index % 2 == 0 else "two",
                    }
                    for url, index in feeds
                ],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            await SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
            ).run()

        self.assertLessEqual(factory.max_active, 4)
        self.assertEqual(factory.max_active, 2)
        self.assertEqual(factory.max_active_by_hostname["example.com"], 1)
        self.assertEqual(factory.max_active_by_hostname["example.org"], 1)
        labels_by_hostname = {
            hostname: {
                label
                for _, url, label in factory.calls
                if url.startswith(f"https://{hostname}/")
            }
            for hostname in ("example.com", "example.org")
        }
        self.assertEqual(labels_by_hostname["example.com"], {"one", "two"})
        self.assertEqual(labels_by_hostname["example.org"], {"one", "two"})

    async def test_rss_stage_honors_global_concurrency_across_presets(self):
        feed_urls = [
            f"https://example.net/feed-{index}.xml" for index in range(6)
        ]
        rss = b'<rss version="2.0"><channel /></rss>'
        factory = FakeBackendFactory(
            {url: FakeReply(rss, delay=0.03) for url in feed_urls}
        )
        config = parse_config(
            {
                "concurrency": {
                    "rss-downloads": 2,
                    "webpage-downloads": 2,
                },
                "downloaders": {
                    "default": {
                        "backend": "fake",
                        "options": {"label": "one"},
                    },
                    "two": {
                        "backend": "fake",
                        "options": {"label": "two"},
                    },
                },
                "feeds": [
                    {
                        "url": url,
                        "rss-downloader": "default" if index % 2 == 0 else "two",
                    }
                    for index, url in enumerate(feed_urls)
                ],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            await SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
            ).run()

        self.assertEqual(factory.max_active, 2)
        self.assertEqual(set(factory.max_active_by_label), {"one", "two"})

    async def test_webpage_stage_does_not_starve_later_hostnames(self):
        hostnames = [f"site-{index}.example" for index in range(8)]
        feed_urls = [f"https://{hostname}/rss.xml" for hostname in hostnames]
        page_urls = {
            feed_url: [f"https://{hostname}/article-{index}" for index in range(8)]
            for feed_url, hostname in zip(feed_urls, hostnames, strict=True)
        }
        replies = {
            feed_url: FakeReply(rss_with_links(*page_urls[feed_url]))
            for feed_url in feed_urls
        }
        replies.update(
            {
                page_url: FakeReply(
                    b"<html>page</html>",
                    "text/html",
                    delay=0.03,
                )
                for urls in page_urls.values()
                for page_url in urls
            }
        )
        factory = FakeBackendFactory(replies)
        config = parse_config(
            {
                "concurrency": {
                    "rss-downloads": 8,
                    "webpage-downloads": 8,
                    "per-domain-downloads": 1,
                },
                "downloaders": {
                    "default": {
                        "backend": "fake",
                        "options": {"label": "rss"},
                    },
                    "pages": {
                        "backend": "fake",
                        "options": {"label": "pages"},
                    },
                },
                "feeds": [
                    {
                        "url": feed_url,
                        "download-webpages": True,
                        "webpage-downloader": "pages",
                    }
                    for feed_url in feed_urls
                ],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            await SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
            ).run()

        first_page_calls = [
            url for kind, url, _ in factory.calls if kind == "webpage"
        ][:8]
        self.assertEqual(
            set(first_page_calls),
            {urls[0] for urls in page_urls.values()},
        )
        self.assertEqual(factory.max_active_by_label["pages"], 8)
        for hostname in hostnames:
            self.assertEqual(factory.max_active_by_hostname[hostname], 1)

    async def test_failed_webpage_refresh_uses_existing_cache(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/page"
        rss = (
            b'<rss version="2.0"><channel><item><link>'
            + page_url.encode()
            + b"</link></item></channel></rss>"
        )
        config = parse_config(
            {
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            first_factory = FakeBackendFactory(
                {
                    feed_url: FakeReply(rss),
                    page_url: FakeReply(b"<html>cached</html>", "text/html"),
                }
            )
            first = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(first_factory),
            ).run()
            page_record = first["pages"]["pages"][0]
            page_path = page_record["path"]
            legacy_manifest = first["pages"]
            legacy_manifest["pages"][0]["path"] = page_path.removeprefix("/")
            legacy_manifest["pages"][0]["local_url"] = (
                f"https://archive.example{page_path}"
            )
            Path(directory, "pages.json").write_text(
                json.dumps(legacy_manifest), encoding="utf-8"
            )

            second_factory = FakeBackendFactory(
                {
                    feed_url: FakeReply(rss),
                    page_url: FakeReply(b"unavailable", "text/html", status=500),
                }
            )
            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(second_factory),
            ).run()

            migrated = second["pages"]["pages"][0]
            self.assertEqual(migrated["status"], "cached")
            self.assertEqual(migrated["path"], page_path)
            self.assertNotIn("local_url", migrated)
            self.assertEqual(
                Path(directory, "feeds/example.com/feed.xml").read_bytes(), rss
            )

    async def test_failed_relative_webpage_does_not_modify_rss(self):
        feed_url = "https://example.com/news/feed.xml"
        page_url = "https://example.com/article"
        rss = (
            b'<rss version="2.0"><channel><item><link>'
            b"/article"
            b"</link></item></channel></rss>"
        )
        factory = FakeBackendFactory(
            {
                feed_url: FakeReply(rss),
                page_url: FakeReply(b"unavailable", "text/html", status=500),
            }
        )
        config = parse_config(
            {
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            manifests = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
            ).run()
            archived_rss = Path(directory, "feeds/example.com/news/feed.xml")

            self.assertEqual(archived_rss.read_bytes(), rss)
            self.assertEqual(manifests["pages"]["pages"][0]["status"], "failed")

    async def test_current_only_removes_feed_and_its_unreferenced_page(self):
        removed_feed = "https://removed.example/feed.xml"
        retained_feed = "https://retained.example/feed.xml"
        page_url = "https://pages.example/old/article"
        first_config = parse_config(
            {
                "archive-current-only": True,
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [
                    {"url": removed_feed, "download-webpages": True},
                    {"url": retained_feed},
                ],
            }
        )
        second_config = parse_config(
            {
                "archive-current-only": True,
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": retained_feed}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            first = await SyncEngine(
                first_config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            removed_feed: FakeReply(rss_with_links(page_url)),
                            retained_feed: FakeReply(rss_with_links()),
                            page_url: FakeReply(b"<html>old</html>", "text/html"),
                        }
                    )
                ),
            ).run()
            page_path = Path(
                directory,
                first["pages"]["pages"][0]["path"].removeprefix("/"),
            )
            removed_rss = Path(directory, "feeds/removed.example/feed.xml")
            removed_download = Path(
                directory, ".new-feeds/removed.example/feed.xml"
            )
            self.assertTrue(page_path.is_file())
            self.assertTrue(removed_rss.is_file())
            self.assertTrue(removed_download.is_file())

            second = await SyncEngine(
                second_config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {retained_feed: FakeReply(rss_with_links())}
                    )
                ),
            ).run()

            self.assertEqual(second["pages"]["pages"], [])
            self.assertFalse(page_path.exists())
            self.assertFalse(page_path.parent.exists())
            self.assertFalse(removed_rss.exists())
            self.assertFalse(removed_rss.parent.exists())
            self.assertFalse(removed_download.exists())
            self.assertFalse(removed_download.parent.exists())

    async def test_current_only_removes_page_missing_from_latest_rss(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/articles/old"
        config = parse_config(
            {
                "archive-current-only": True,
                "webpages": {"refresh-policy": "missing-only"},
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            first = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(rss_with_links(page_url)),
                            page_url: FakeReply(b"<html>old</html>", "text/html"),
                        }
                    )
                ),
            ).run()
            page_path = Path(
                directory,
                first["pages"]["pages"][0]["path"].removeprefix("/"),
            )

            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory({feed_url: FakeReply(rss_with_links())})
                ),
            ).run()

            self.assertEqual(second["pages"]["pages"], [])
            self.assertFalse(page_path.exists())

    async def test_current_only_preserves_last_pages_when_rss_parse_fails(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/current"
        config = parse_config(
            {
                "archive-current-only": True,
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            first = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(rss_with_links(page_url)),
                            page_url: FakeReply(
                                b"<html>current</html>", "text/html"
                            ),
                        }
                    )
                ),
            ).run()
            page_record = first["pages"]["pages"][0]
            page_path = Path(directory, page_record["path"].removeprefix("/"))
            failed_factory = FakeBackendFactory(
                {feed_url: FakeReply(b"not valid RSS")}
            )

            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(failed_factory),
            ).run()

            self.assertEqual(second["pages"]["pages"], [page_record])
            self.assertTrue(page_path.is_file())
            self.assertEqual(failed_factory.calls, [("rss", feed_url, "default")])
            self.assertEqual(second["feeds"]["feeds"][0]["status"], "retained")

    async def test_current_only_skips_page_cleanup_when_retained_rss_is_invalid(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/current"
        config = parse_config(
            {
                "archive-current-only": True,
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            first = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(rss_with_links(page_url)),
                            page_url: FakeReply(
                                b"<html>current</html>", "text/html"
                            ),
                        }
                    )
                ),
            ).run()
            archived_rss = Path(directory, "feeds/example.com/feed.xml")
            archived_rss.write_bytes(b"corrupt retained RSS")
            page_record = first["pages"]["pages"][0]
            page_path = Path(directory, page_record["path"].removeprefix("/"))

            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory({feed_url: FakeReply(b"invalid new RSS")})
                ),
            ).run()

            self.assertEqual(second["pages"]["pages"], [page_record])
            self.assertTrue(page_path.is_file())

    async def test_current_only_keeps_a_page_still_referenced_by_another_feed(self):
        first_feed = "https://one.example/feed.xml"
        second_feed = "https://two.example/feed.xml"
        page_url = "https://shared.example/article"
        config = parse_config(
            {
                "archive-current-only": True,
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [
                    {"url": first_feed, "download-webpages": True},
                    {"url": second_feed, "download-webpages": True},
                ],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            first_feed: FakeReply(rss_with_links(page_url)),
                            second_feed: FakeReply(rss_with_links(page_url)),
                            page_url: FakeReply(b"<html>shared</html>", "text/html"),
                        }
                    )
                ),
            ).run()
            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            first_feed: FakeReply(rss_with_links()),
                            second_feed: FakeReply(rss_with_links(page_url)),
                            page_url: FakeReply(b"<html>shared</html>", "text/html"),
                        }
                    )
                ),
            ).run()

            self.assertEqual(len(second["pages"]["pages"]), 1)
            page_path = Path(
                directory,
                second["pages"]["pages"][0]["path"].removeprefix("/"),
            )
            self.assertTrue(page_path.is_file())

    async def test_current_only_removes_page_from_previous_storage_path(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/article"

        def config(storage_path):
            return parse_config(
                {
                    "archive-current-only": True,
                    "webpages": {"storage-path": storage_path},
                    "downloaders": {"default": {"backend": "fake"}},
                    "feeds": [{"url": feed_url, "download-webpages": True}],
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            first = await SyncEngine(
                config("old-pages"),
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(rss_with_links(page_url)),
                            page_url: FakeReply(b"<html>page</html>", "text/html"),
                        }
                    )
                ),
            ).run()
            old_path = Path(
                directory,
                first["pages"]["pages"][0]["path"].removeprefix("/"),
            )

            second = await SyncEngine(
                config("new-pages"),
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(rss_with_links(page_url)),
                            page_url: FakeReply(b"<html>page</html>", "text/html"),
                        }
                    )
                ),
            ).run()
            new_path = Path(
                directory,
                second["pages"]["pages"][0]["path"].removeprefix("/"),
            )

            self.assertFalse(old_path.exists())
            self.assertFalse(old_path.parent.exists())
            self.assertTrue(new_path.is_file())

    async def test_skipped_page_stays_in_its_recorded_storage_path(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/article"
        rss = rss_with_links(page_url)

        def config(storage_path):
            return parse_config(
                {
                    "archive-current-only": True,
                    "webpages": {
                        "storage-path": storage_path,
                        "refresh-policy": "missing-only",
                    },
                    "downloaders": {"default": {"backend": "fake"}},
                    "feeds": [{"url": feed_url, "download-webpages": True}],
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            first = await SyncEngine(
                config("old-pages"),
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(rss),
                            page_url: FakeReply(b"<html>page</html>", "text/html"),
                        }
                    )
                ),
            ).run()
            old_record = first["pages"]["pages"][0]
            old_path = Path(directory, old_record["path"].removeprefix("/"))
            second_factory = FakeBackendFactory({feed_url: FakeReply(rss)})

            second = await SyncEngine(
                config("new-pages"),
                root=directory,
                registry=registry_with(second_factory),
            ).run()

            page_record = second["pages"]["pages"][0]
            self.assertEqual(second_factory.calls, [("rss", feed_url, "default")])
            self.assertEqual(page_record["path"], old_record["path"])
            self.assertEqual(page_record["status"], "skipped")
            self.assertTrue(old_path.is_file())
            self.assertFalse(Path(directory, "new-pages").exists())

    async def test_current_only_does_not_delete_a_manifest_path_mismatched_to_url(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/article"
        config = parse_config(
            {
                "archive-current-only": True,
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            first = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(rss_with_links(page_url)),
                            page_url: FakeReply(b"<html>page</html>", "text/html"),
                        }
                    )
                ),
            ).run()
            original_page = Path(
                directory,
                first["pages"]["pages"][0]["path"].removeprefix("/"),
            )
            sentinel = Path(directory, "sentinel.html")
            sentinel.write_text("keep", encoding="utf-8")
            manifest = first["pages"]
            manifest["pages"][0]["path"] = "/sentinel.html"
            Path(directory, "pages.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory({feed_url: FakeReply(rss_with_links())})
                ),
            ).run()

            self.assertEqual(second["pages"]["pages"], [])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertTrue(original_page.is_file())

    async def test_current_only_does_not_sweep_unrecorded_feed_files(self):
        feed_url = "https://current.example/feed.xml"
        config = parse_config(
            {
                "archive-current-only": True,
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            unrecorded = Path(directory, "feeds/unrecorded.example/feed.xml")
            unrecorded.parent.mkdir(parents=True)
            unrecorded.write_bytes(b"not owned by the current manifest")

            await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory({feed_url: FakeReply(rss_with_links())})
                ),
            ).run()

            self.assertEqual(
                unrecorded.read_bytes(), b"not owned by the current manifest"
            )

    async def test_default_archive_mode_keeps_pages_missing_from_latest_rss(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/history"
        config = parse_config(
            {
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            first = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory(
                        {
                            feed_url: FakeReply(rss_with_links(page_url)),
                            page_url: FakeReply(b"<html>history</html>", "text/html"),
                        }
                    )
                ),
            ).run()
            page_path = Path(
                directory,
                first["pages"]["pages"][0]["path"].removeprefix("/"),
            )
            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory({feed_url: FakeReply(rss_with_links())})
                ),
            ).run()

            self.assertEqual(len(second["pages"]["pages"]), 1)
            self.assertTrue(page_path.is_file())

    async def test_default_change_detection_ignores_last_build_date(self):
        feed_url = "https://example.com/feed.xml"
        first_body = (
            b'<rss version="2.0"><channel><lastBuildDate>one</lastBuildDate>'
            b"<title>Feed</title></channel></rss>"
        )
        second_body = first_body.replace(b">one<", b">two<")
        config = parse_config(
            {
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            first = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory({feed_url: FakeReply(first_body)})
                ),
                clock=lambda: 100,
            ).run()
            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory({feed_url: FakeReply(second_body)})
                ),
                clock=lambda: 200,
            ).run()

            output = Path(directory, "feeds/example.com/feed.xml")
            self.assertEqual(output.read_bytes(), first_body)
            self.assertTrue(first["feeds"]["feeds"][0]["changed"])
            self.assertFalse(second["feeds"]["feeds"][0]["changed"])
            self.assertEqual(second["feeds"]["feeds"][0]["updated_at"], 100)
            self.assertEqual(second["feeds"]["feeds"][0]["fetched_at"], 200)

    async def test_feed_override_can_enable_exact_byte_detection(self):
        feed_url = "https://example.com/feed.xml"
        first_body = (
            b'<rss version="2.0"><channel><lastBuildDate>one</lastBuildDate>'
            b"</channel></rss>"
        )
        second_body = first_body.replace(b">one<", b">two<")
        config = parse_config(
            {
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [
                    {
                        "url": feed_url,
                        "change-detection": {"ignore-tags": []},
                    }
                ],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory({feed_url: FakeReply(first_body)})
                ),
                clock=lambda: 100,
            ).run()
            second = await SyncEngine(
                config,
                root=directory,
                registry=registry_with(
                    FakeBackendFactory({feed_url: FakeReply(second_body)})
                ),
                clock=lambda: 200,
            ).run()

            output = Path(directory, "feeds/example.com/feed.xml")
            self.assertEqual(output.read_bytes(), second_body)
            self.assertTrue(second["feeds"]["feeds"][0]["changed"])
            self.assertEqual(
                second["feeds"]["feeds"][0]["change_detection"],
                {"ignore_tags": []},
            )


if __name__ == "__main__":
    unittest.main()
