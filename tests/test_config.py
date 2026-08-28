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
        self.assertEqual(config.rss.change_detection.ignore_tags, ("lastBuildDate",))
        self.assertEqual(
            config.feeds[0].change_detection.ignore_tags,
            ("lastBuildDate",),
        )

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

    def test_enabled_webpages_use_default_storage_without_public_url(self):
        config = parse_config(
            {
                "feeds": [
                    {
                        "url": "https://example.com/rss.xml",
                        "download-webpages": True,
                    }
                ]
            }
        )

        self.assertEqual(config.webpages.storage_path, "pages")

    def test_removed_public_base_url_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown webpages field"):
            parse_config(
                {
                    "webpages": {"public-base-url": "https://archive.example/"},
                    "feeds": [
                        {
                            "url": "https://example.com/rss.xml",
                            "download-webpages": True,
                        }
                    ],
                }
            )

    def test_feed_ignore_tags_replace_global_list(self):
        config = parse_config(
            {
                "rss": {
                    "change-detection": {"ignore-tags": ["lastBuildDate", "pubDate"]}
                },
                "feeds": [
                    {"url": "https://example.com/inherited.xml"},
                    {
                        "url": "https://example.com/exact.xml",
                        "change-detection": {"ignore-tags": []},
                    },
                ],
            }
        )

        self.assertEqual(
            config.feeds[0].change_detection.ignore_tags,
            ("lastBuildDate", "pubDate"),
        )
        self.assertEqual(config.feeds[1].change_detection.ignore_tags, ())

    def test_ignore_tags_require_unique_non_empty_strings(self):
        invalid_lists = ["lastBuildDate", [""], ["pubDate", "pubDate"]]
        for ignore_tags in invalid_lists:
            with self.subTest(ignore_tags=ignore_tags), self.assertRaises(ConfigError):
                parse_config(
                    {
                        "rss": {"change-detection": {"ignore-tags": ignore_tags}},
                        "feeds": [{"url": "https://example.com/rss.xml"}],
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
