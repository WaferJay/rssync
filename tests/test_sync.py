import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from rssync.config import parse_config
from rssync.downloaders.registry import DownloaderRegistry
from rssync.sync import SyncEngine
from tests.fakes import FakeBackendFactory, FakeReply


def registry_with(factory):
    registry = DownloaderRegistry(load_plugins=False)
    registry.register("fake", factory)
    return registry


class SyncEngineTest(unittest.TestCase):
    def test_webpages_are_disabled_by_default(self):
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
            SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
                clock=lambda: 100,
            ).run()

            output = Path(directory, "feeds/example.com/feed.xml")
            self.assertEqual(output.read_bytes(), feed_body)
            self.assertEqual(factory.calls, [("rss", feed_url, "default")])
            self.assertFalse(Path(directory, "pages.json").exists())

    def test_enabled_webpage_is_stored_raw_and_linked_absolutely(self):
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
                "webpages": {
                    "public-base-url": "https://archive.example/rssync/",
                    "storage-path": "pages",
                },
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            manifests = SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
                clock=lambda: 200,
            ).run()

            page_record = manifests["pages"]["pages"][0]
            archived = Path(directory, page_record["path"])
            self.assertEqual(archived.read_bytes(), page_body)
            self.assertTrue(
                page_record["local_url"].startswith(
                    "https://archive.example/rssync/pages/"
                )
            )
            generated = ET.parse(Path(directory, "feeds/example.com/news/feed.xml"))
            self.assertEqual(
                generated.findtext("./channel/item/link"),
                page_record["local_url"],
            )
            self.assertEqual(
                factory.calls,
                [
                    ("rss", feed_url, "default"),
                    ("webpage", page_url, "default"),
                ],
            )

    def test_first_enabled_feed_selects_the_page_preset(self):
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
                "webpages": {"public-base-url": "https://archive.example/"},
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
            SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
            ).run()

        page_calls = [call for call in factory.calls if call[0] == "webpage"]
        self.assertEqual(page_calls, [("webpage", page_url, "first")])

    def test_rss_stage_honors_global_and_per_preset_concurrency(self):
        feed_urls = [f"https://example.com/feed-{index}.xml" for index in range(6)]
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
                        "concurrency": {"rss-downloads": 1},
                        "options": {"label": "one"},
                    },
                    "two": {
                        "backend": "fake",
                        "concurrency": {"rss-downloads": 2},
                        "options": {"label": "two"},
                    },
                },
                "feeds": [
                    {
                        "url": url,
                        "rss-downloader": "default" if index < 3 else "two",
                    }
                    for index, url in enumerate(feed_urls)
                ],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
            ).run()

        self.assertLessEqual(factory.max_active, 2)
        self.assertLessEqual(factory.max_active_by_label["one"], 1)
        self.assertLessEqual(factory.max_active_by_label["two"], 2)

    def test_failed_webpage_refresh_uses_existing_cache(self):
        feed_url = "https://example.com/feed.xml"
        page_url = "https://example.com/page"
        rss = (
            b'<rss version="2.0"><channel><item><link>'
            + page_url.encode()
            + b"</link></item></channel></rss>"
        )
        config = parse_config(
            {
                "webpages": {"public-base-url": "https://archive.example/"},
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
            first = SyncEngine(
                config,
                root=directory,
                registry=registry_with(first_factory),
            ).run()
            local_url = first["pages"]["pages"][0]["local_url"]

            second_factory = FakeBackendFactory(
                {
                    feed_url: FakeReply(rss),
                    page_url: FakeReply(b"unavailable", "text/html", status=500),
                }
            )
            second = SyncEngine(
                config,
                root=directory,
                registry=registry_with(second_factory),
            ).run()

            generated = ET.parse(Path(directory, "feeds/example.com/feed.xml"))
            self.assertEqual(generated.findtext("./channel/item/link"), local_url)
            self.assertEqual(second["pages"]["pages"][0]["status"], "cached")

    def test_failed_relative_webpage_falls_back_to_absolute_external_url(self):
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
                "webpages": {"public-base-url": "https://archive.example/"},
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": feed_url, "download-webpages": True}],
            }
        )

        with tempfile.TemporaryDirectory() as directory:
            SyncEngine(
                config,
                root=directory,
                registry=registry_with(factory),
            ).run()
            generated = ET.parse(Path(directory, "feeds/example.com/news/feed.xml"))

        self.assertEqual(generated.findtext("./channel/item/link"), page_url)


if __name__ == "__main__":
    unittest.main()
