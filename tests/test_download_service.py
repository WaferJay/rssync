import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from rssync.config import parse_config
from rssync.download_service import DownloadConcurrency, DownloadService
from rssync.downloaders.base import (
    DownloaderRuntimeContext,
    DownloadRequest,
    PreparedDownloadRequest,
    RetryPolicy,
)
from rssync.downloaders.registry import DownloaderManager, DownloaderRegistry


class BrokenResponse:
    requested_url = "https://example.com/page"
    final_url = requested_url
    status_code = 200
    headers: ClassVar[dict[str, str]] = {"Content-Type": "text/html"}

    def iter_bytes(self):
        yield b"partial"
        raise OSError("stream interrupted")

    def close(self):
        return None


class SuccessfulResponse(BrokenResponse):
    def iter_bytes(self):
        yield b"complete-body"


class RetryAfterResponse(BrokenResponse):
    status_code = 503
    headers: ClassVar[dict[str, str]] = {
        "Content-Type": "text/html",
        "Retry-After": "1.5",
    }


class RetryDownloader:
    retry_policy = RetryPolicy(retries=1, backoff_factor=0.25)

    def __init__(self):
        self.attempts = 0

    def prepare(self, request: DownloadRequest):
        return PreparedDownloadRequest(request.url, request.resource_kind, {}, {})

    def open_attempt(self, request):
        del request
        self.attempts += 1
        return BrokenResponse() if self.attempts == 1 else SuccessfulResponse()

    def is_retryable_exception(self, error):
        return isinstance(error, OSError)

    def close(self):
        return None


class RetryFactory:
    def __init__(self, instance=None):
        self.instance = instance or RetryDownloader()

    def validate_options(self, options):
        return options

    def create(self, options, runtime: DownloaderRuntimeContext):
        del options, runtime
        return self.instance


class DownloadServiceTest(unittest.TestCase):
    def test_stream_retry_discards_partial_file_and_restarts(self):
        config = parse_config(
            {
                "downloaders": {"default": {"backend": "retry"}},
                "feeds": [{"url": "https://example.com/rss.xml"}],
            }
        )
        registry = DownloaderRegistry(load_plugins=False)
        factory = RetryFactory()
        registry.register("retry", factory)
        manager = DownloaderManager(config.downloaders, registry)
        delays = []
        service = DownloadService(
            manager,
            DownloadConcurrency(1, {"default": 1}),
            sleep=delays.append,
            clock=lambda: 123,
        )

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "page.html")
            result = service.download(
                url="https://example.com/page",
                resource_kind="webpage",
                preset_name="default",
                target_path=target,
            )

            self.assertEqual(target.read_bytes(), b"complete-body")
            self.assertEqual(result.byte_count, len(b"complete-body"))
            self.assertEqual(factory.instance.attempts, 2)
            self.assertEqual(delays, [0.25])
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])
        manager.close()

    def test_retry_after_longer_than_backoff_is_honored(self):
        class StatusRetryDownloader(RetryDownloader):
            def open_attempt(self, request):
                del request
                self.attempts += 1
                if self.attempts == 1:
                    return RetryAfterResponse()
                return SuccessfulResponse()

        config = parse_config(
            {
                "downloaders": {"default": {"backend": "retry"}},
                "feeds": [{"url": "https://example.com/rss.xml"}],
            }
        )
        registry = DownloaderRegistry(load_plugins=False)
        registry.register("retry", RetryFactory(StatusRetryDownloader()))
        manager = DownloaderManager(config.downloaders, registry)
        delays = []
        service = DownloadService(
            manager,
            DownloadConcurrency(1, {"default": 1}),
            sleep=delays.append,
        )

        with tempfile.TemporaryDirectory() as directory:
            service.download(
                url="https://example.com/page",
                resource_kind="webpage",
                preset_name="default",
                target_path=Path(directory, "page.html"),
            )

        self.assertEqual(delays, [1.5])
        manager.close()


if __name__ == "__main__":
    unittest.main()
