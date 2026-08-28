import unittest

from rssync.config import ConfigError, parse_config


class ConfigTest(unittest.TestCase):
    def test_implicit_default_and_feed_defaults(self):
        config = parse_config({"feeds": [{"url": "https://example.com/rss.xml"}]})

        self.assertEqual(config.feeds[0].rss_downloader, "default")
        self.assertEqual(config.feeds[0].webpage_downloader, "default")
        self.assertFalse(config.feeds[0].download_webpages)
        self.assertEqual(config.downloaders["default"].backend, "requests")
        self.assertTrue(config.downloaders["default"].options["use-session"])

    def test_default_preset_is_partially_overridden(self):
        config = parse_config(
            {
                "downloaders": {"default": {"options": {"timeout": 45}}},
                "feeds": [{"url": "https://example.com/rss.xml"}],
            }
        )

        options = config.downloaders["default"].options
        self.assertEqual(options["timeout"], 45)
        self.assertEqual(options["retries"], 3)
        self.assertEqual(options["user-agent"]["strategy"], "per-run")

    def test_enabled_webpages_require_absolute_public_base_url(self):
        with self.assertRaisesRegex(ConfigError, "public-base-url"):
            parse_config(
                {
                    "feeds": [
                        {
                            "url": "https://example.com/rss.xml",
                            "download-webpages": True,
                        }
                    ]
                }
            )

    def test_public_base_url_rejects_query_and_fragment(self):
        with self.assertRaisesRegex(ConfigError, "query or fragment"):
            parse_config(
                {
                    "webpages": {
                        "public-base-url": "https://archive.example/?tenant=one"
                    },
                    "feeds": [
                        {
                            "url": "https://example.com/rss.xml",
                            "download-webpages": True,
                        }
                    ],
                }
            )

    def test_old_string_feed_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "must be an object"):
            parse_config({"feeds": ["https://example.com/rss.xml"]})

    def test_explicit_unknown_preset_is_rejected_even_when_pages_disabled(self):
        with self.assertRaisesRegex(ConfigError, "unknown preset"):
            parse_config(
                {
                    "feeds": [
                        {
                            "url": "https://example.com/rss.xml",
                            "webpage-downloader": "missing",
                        }
                    ]
                }
            )


if __name__ == "__main__":
    unittest.main()
