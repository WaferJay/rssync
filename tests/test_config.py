import unittest

from rssync.config import ConfigError, parse_config
from rssync.webpage_refresh import default_webpage_refresh_registry


class CustomRefreshStrategy:
    name = "custom"

    def should_fetch(self, context):
        return False


class ConfigTest(unittest.TestCase):
    def test_implicit_default_and_feed_defaults(self):
        config = parse_config({"feeds": [{"url": "https://example.com/rss.xml"}]})

        self.assertEqual(config.feeds[0].rss_downloader, "default")
        self.assertEqual(config.feeds[0].webpage_downloader, "default")
        self.assertFalse(config.feeds[0].download_webpages)
        self.assertEqual(config.webpages.refresh_policy, "always")
        self.assertIsNone(config.webpages.atom)
        self.assertEqual(config.feeds[0].webpage_refresh_policy, "always")
        self.assertFalse(config.archive_current_only)
        self.assertEqual(config.downloaders["default"].backend, "httpx")
        self.assertTrue(config.downloaders["default"].options["http2"])
        self.assertEqual(config.rss.change_detection.ignore_tags, ("lastBuildDate",))
        self.assertEqual(config.concurrency.rss_downloads, 2)
        self.assertEqual(config.concurrency.webpage_downloads, 8)
        self.assertIsNone(config.concurrency.per_domain_downloads)
        self.assertEqual(config.concurrency.request_interval, 0)
        self.assertEqual(
            config.feeds[0].change_detection.ignore_tags,
            ("lastBuildDate",),
        )

    def test_global_concurrency_and_request_interval_are_parsed(self):
        config = parse_config(
            {
                "concurrency": {
                    "rss-downloads": 3,
                    "webpage-downloads": 12,
                    "per-domain-downloads": 2,
                    "request-interval": 0.75,
                },
                "feeds": [{"url": "https://example.com/rss.xml"}],
            }
        )

        self.assertEqual(config.concurrency.rss_downloads, 3)
        self.assertEqual(config.concurrency.webpage_downloads, 12)
        self.assertEqual(config.concurrency.per_domain_downloads, 2)
        self.assertEqual(config.concurrency.request_interval, 0.75)

    def test_invalid_domain_concurrency_and_request_intervals_are_rejected(self):
        invalid_fields = [
            ("per-domain-downloads", 0),
            ("per-domain-downloads", None),
            ("per-domain-downloads", True),
            ("request-interval", -0.1),
            ("request-interval", True),
            ("request-interval", float("nan")),
            ("request-interval", float("inf")),
        ]
        for field, value in invalid_fields:
            with self.subTest(field=field, value=value), self.assertRaises(
                ConfigError
            ):
                parse_config(
                    {
                        "concurrency": {field: value},
                        "feeds": [{"url": "https://example.com/rss.xml"}],
                    }
                )

    def test_downloader_preset_concurrency_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "unknown downloaders.default field"):
            parse_config(
                {
                    "downloaders": {
                        "default": {"concurrency": {"rss-downloads": 1}}
                    },
                    "feeds": [{"url": "https://example.com/rss.xml"}],
                }
            )

    def test_archive_current_only_requires_a_boolean(self):
        config = parse_config(
            {
                "archive-current-only": True,
                "feeds": [{"url": "https://example.com/rss.xml"}],
            }
        )
        self.assertTrue(config.archive_current_only)

        with self.assertRaisesRegex(
            ConfigError, "archive-current-only must be a boolean"
        ):
            parse_config(
                {
                    "archive-current-only": "true",
                    "feeds": [{"url": "https://example.com/rss.xml"}],
                }
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

    def test_atom_output_is_enabled_by_its_configuration_object(self):
        config = parse_config(
            {
                "webpages": {"atom": {}},
                "feeds": [
                    {
                        "url": "https://example.com/rss.xml",
                        "download-webpages": True,
                    }
                ],
            }
        )

        self.assertIsNotNone(config.webpages.atom)
        self.assertEqual(config.webpages.atom.storage_path, "atoms")
        self.assertEqual(config.webpages.atom.missing_page_policy, "ignore")

        configured = parse_config(
            {
                "webpages": {
                    "atom": {
                        "storage-path": "public/feeds",
                        "missing-page-policy": "source-url",
                    }
                },
                "feeds": [{"url": "https://example.com/rss.xml"}],
            }
        )

        self.assertEqual(configured.webpages.atom.storage_path, "public/feeds")
        self.assertEqual(
            configured.webpages.atom.missing_page_policy,
            "source-url",
        )

    def test_invalid_atom_settings_are_rejected(self):
        invalid_atom_values = [
            None,
            {"storage-path": ""},
            {"storage-path": "/atoms"},
            {"storage-path": "../atoms"},
            {"storage-path": "."},
            {"storage-path": "feeds"},
            {"storage-path": "feeds/atoms"},
            {"storage-path": ".new-feeds"},
            {"storage-path": ".new-feeds/atoms"},
            {"storage-path": "feeds.json"},
            {"storage-path": "pages.json/atoms"},
            {"missing-page-policy": "local-path"},
            {"unknown": True},
        ]
        for atom in invalid_atom_values:
            with self.subTest(atom=atom), self.assertRaises(ConfigError):
                parse_config(
                    {
                        "webpages": {"atom": atom},
                        "feeds": [{"url": "https://example.com/rss.xml"}],
                    }
                )

    def test_feed_refresh_policy_overrides_the_global_policy(self):
        config = parse_config(
            {
                "webpages": {"refresh-policy": "on-rss-change"},
                "feeds": [
                    {"url": "https://example.com/inherited.xml"},
                    {
                        "url": "https://example.com/immutable.xml",
                        "webpage-refresh-policy": "missing-only",
                    },
                ],
            }
        )

        self.assertEqual(config.webpages.refresh_policy, "on-rss-change")
        self.assertEqual(
            config.feeds[0].webpage_refresh_policy,
            "on-rss-change",
        )
        self.assertEqual(
            config.feeds[1].webpage_refresh_policy,
            "missing-only",
        )

    def test_unknown_refresh_policies_are_rejected(self):
        invalid_configs = [
            {
                "webpages": {"refresh-policy": "unknown"},
                "feeds": [{"url": "https://example.com/feed.xml"}],
            },
            {
                "feeds": [
                    {
                        "url": "https://example.com/feed.xml",
                        "webpage-refresh-policy": "unknown",
                    }
                ]
            },
        ]
        for config in invalid_configs:
            with self.subTest(config=config), self.assertRaisesRegex(
                ConfigError,
                "unknown strategy",
            ):
                parse_config(config)

    def test_custom_refresh_policy_can_be_injected(self):
        registry = default_webpage_refresh_registry()
        registry.register(CustomRefreshStrategy())

        config = parse_config(
            {
                "webpages": {"refresh-policy": "custom"},
                "feeds": [{"url": "https://example.com/feed.xml"}],
            },
            refresh_registry=registry,
        )

        self.assertEqual(config.feeds[0].webpage_refresh_policy, "custom")

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
