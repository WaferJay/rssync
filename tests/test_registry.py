import unittest
from unittest.mock import patch

from rssync.config import parse_config
from rssync.downloaders.registry import (
    DownloaderManager,
    DownloaderRegistry,
    DownloaderRegistryError,
)
from tests.fakes import FakeBackendFactory


class FakeEntryPoint:
    name = "external"

    def __init__(self, factory):
        self.factory = factory

    def load(self):
        return self.factory


class DownloaderRegistryTest(unittest.IsolatedAsyncioTestCase):
    def test_removed_requests_backend_is_rejected(self):
        config = parse_config(
            {
                "downloaders": {"default": {"backend": "requests"}},
                "feeds": [{"url": "https://example.com/rss.xml"}],
            }
        )

        with self.assertRaisesRegex(
            DownloaderRegistryError,
            "unknown downloader backend: requests",
        ):
            registry = DownloaderRegistry(load_plugins=False)
            DownloaderManager(config.downloaders, registry)

    def test_loads_external_backend_from_entry_point_group(self):
        factory = FakeBackendFactory({})
        with patch(
            "rssync.downloaders.registry.metadata.entry_points",
            return_value=[FakeEntryPoint(factory)],
        ) as entry_points:
            registry = DownloaderRegistry()

        self.assertIs(registry.factory("external"), factory)
        entry_points.assert_called_once_with(group="rssync.downloaders")

    async def test_manager_shares_one_instance_per_preset_and_closes_it(self):
        factory = FakeBackendFactory({})
        registry = DownloaderRegistry(load_plugins=False)
        registry.register("fake", factory)
        config = parse_config(
            {
                "downloaders": {"default": {"backend": "fake"}},
                "feeds": [{"url": "https://example.com/rss.xml"}],
            }
        )
        manager = DownloaderManager(config.downloaders, registry)

        first = manager.get("default")
        second = manager.get("default")
        await manager.close()

        self.assertIs(first, second)
        self.assertEqual(factory.created, 1)
        self.assertEqual(factory.closed, 1)


if __name__ == "__main__":
    unittest.main()
