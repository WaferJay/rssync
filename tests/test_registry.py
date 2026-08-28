import unittest
from unittest.mock import patch

from rssync.downloaders.registry import DownloaderRegistry
from tests.fakes import FakeBackendFactory


class FakeEntryPoint:
    name = "external"

    def __init__(self, factory):
        self.factory = factory

    def load(self):
        return self.factory


class DownloaderRegistryTest(unittest.TestCase):
    def test_loads_external_backend_from_entry_point_group(self):
        factory = FakeBackendFactory({})
        with patch(
            "rssync.downloaders.registry.metadata.entry_points",
            return_value=[FakeEntryPoint(factory)],
        ) as entry_points:
            registry = DownloaderRegistry()

        self.assertIs(registry.factory("external"), factory)
        entry_points.assert_called_once_with(group="rssync.downloaders")


if __name__ == "__main__":
    unittest.main()
