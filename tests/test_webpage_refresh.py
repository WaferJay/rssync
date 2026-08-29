import unittest

from rssync.webpage_refresh import (
    AlwaysRefreshStrategy,
    MissingOnlyStrategy,
    OnRssChangeStrategy,
    WebpageRefreshContext,
    WebpageRefreshRegistry,
    default_webpage_refresh_registry,
)


def context(*, cache_valid: bool, rss_changed: bool) -> WebpageRefreshContext:
    return WebpageRefreshContext(
        canonical_url="https://example.com/article",
        feed_url="https://example.com/feed.xml",
        cache_valid=cache_valid,
        rss_changed=rss_changed,
    )


class WebpageRefreshStrategyTest(unittest.TestCase):
    def test_always_refreshes_every_context(self):
        strategy = AlwaysRefreshStrategy()

        for cache_valid in (False, True):
            for rss_changed in (False, True):
                with self.subTest(
                    cache_valid=cache_valid,
                    rss_changed=rss_changed,
                ):
                    self.assertTrue(
                        strategy.should_fetch(
                            context(
                                cache_valid=cache_valid,
                                rss_changed=rss_changed,
                            )
                        )
                    )

    def test_on_rss_change_refreshes_changed_or_missing_pages(self):
        strategy = OnRssChangeStrategy()

        self.assertFalse(
            strategy.should_fetch(context(cache_valid=True, rss_changed=False))
        )
        self.assertTrue(
            strategy.should_fetch(context(cache_valid=True, rss_changed=True))
        )
        self.assertTrue(
            strategy.should_fetch(context(cache_valid=False, rss_changed=False))
        )

    def test_missing_only_never_refreshes_a_valid_cache(self):
        strategy = MissingOnlyStrategy()

        self.assertFalse(
            strategy.should_fetch(context(cache_valid=True, rss_changed=False))
        )
        self.assertFalse(
            strategy.should_fetch(context(cache_valid=True, rss_changed=True))
        )
        self.assertTrue(
            strategy.should_fetch(context(cache_valid=False, rss_changed=False))
        )


class WebpageRefreshRegistryTest(unittest.TestCase):
    def test_default_registry_resolves_all_builtin_strategies(self):
        registry = default_webpage_refresh_registry()

        self.assertIsInstance(registry.resolve("always"), AlwaysRefreshStrategy)
        self.assertIsInstance(
            registry.resolve("on-rss-change"), OnRssChangeStrategy
        )
        self.assertIsInstance(registry.resolve("missing-only"), MissingOnlyStrategy)

    def test_unknown_and_duplicate_strategies_are_rejected(self):
        registry = WebpageRefreshRegistry((AlwaysRefreshStrategy(),))

        with self.assertRaisesRegex(ValueError, "unknown webpage refresh strategy"):
            registry.resolve("missing")
        with self.assertRaisesRegex(ValueError, "duplicate webpage refresh strategy"):
            registry.register(AlwaysRefreshStrategy())

    def test_strategy_protocol_is_validated_during_registration(self):
        class InvalidStrategy:
            name = "invalid"

        with self.assertRaisesRegex(ValueError, "does not implement"):
            WebpageRefreshRegistry((InvalidStrategy(),))


if __name__ == "__main__":
    unittest.main()
