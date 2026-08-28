import gzip
import io
import unittest
from unittest.mock import Mock, patch

import requests
from urllib3.response import HTTPResponse

from rssync.config import DEFAULT_USER_AGENT
from rssync.downloaders.base import (
    DownloaderRuntimeContext,
    DownloadRequest,
    PreparedDownloadRequest,
)
from rssync.downloaders.requests_backend import (
    RequestsDownloader,
    RequestsDownloadResponse,
    parse_requests_options,
)


class SequenceUserAgent:
    calls = 0

    def __init__(self, fallback):
        self.fallback = fallback

    @property
    def random(self):
        type(self).calls += 1
        return f"agent-{type(self).calls}"


class BrokenUserAgent:
    def __init__(self, fallback):
        del fallback
        raise RuntimeError("no user-agent data")


def options(strategy, *, fallback=DEFAULT_USER_AGENT):
    return parse_requests_options(
        {
            "use-session": False,
            "user-agent": {
                "strategy": strategy,
                "fallback": fallback,
            },
        }
    )


class RequestsBackendTest(unittest.TestCase):
    def setUp(self):
        SequenceUserAgent.calls = 0

    def test_per_run_user_agent_is_shared_across_instances(self):
        runtime = DownloaderRuntimeContext()
        with patch(
            "rssync.downloaders.requests_backend.UserAgent",
            SequenceUserAgent,
        ):
            first = RequestsDownloader(options("per-run"), runtime)
            second = RequestsDownloader(options("per-run"), runtime)
            request = DownloadRequest("https://example.com", "rss")

            self.assertEqual(first.prepare(request).headers["User-Agent"], "agent-1")
            self.assertEqual(second.prepare(request).headers["User-Agent"], "agent-1")
            self.assertEqual(SequenceUserAgent.calls, 1)

    def test_per_request_user_agent_changes_per_logical_request(self):
        runtime = DownloaderRuntimeContext()
        with patch(
            "rssync.downloaders.requests_backend.UserAgent",
            SequenceUserAgent,
        ):
            downloader = RequestsDownloader(options("per-request"), runtime)
            request = DownloadRequest("https://example.com", "webpage")

            self.assertEqual(
                downloader.prepare(request).headers["User-Agent"], "agent-1"
            )
            self.assertEqual(
                downloader.prepare(request).headers["User-Agent"], "agent-2"
            )

    def test_user_agent_initialization_failure_uses_fallback(self):
        runtime = DownloaderRuntimeContext()
        with patch(
            "rssync.downloaders.requests_backend.UserAgent",
            BrokenUserAgent,
        ):
            downloader = RequestsDownloader(
                options("per-request", fallback="fallback-agent"), runtime
            )
            prepared = downloader.prepare(DownloadRequest("https://example.com", "rss"))

        self.assertEqual(prepared.headers["User-Agent"], "fallback-agent")

    def test_use_session_selects_worker_local_session_transport(self):
        response = Mock()
        session = Mock()
        session.request.return_value = response
        configured = parse_requests_options({"use-session": True})
        prepared = PreparedDownloadRequest("https://example.com", "rss", {}, {})

        with patch(
            "rssync.downloaders.requests_backend.requests.Session",
            return_value=session,
        ):
            downloader = RequestsDownloader(configured, DownloaderRuntimeContext())
            downloader.open_attempt(prepared)
            downloader.close()

        session.request.assert_called_once()
        session.close.assert_called_once()

    def test_disabled_session_uses_module_level_request(self):
        response = Mock()
        configured = parse_requests_options({"use-session": False})
        prepared = PreparedDownloadRequest("https://example.com", "rss", {}, {})

        with patch(
            "rssync.downloaders.requests_backend.requests.request",
            return_value=response,
        ) as request:
            downloader = RequestsDownloader(configured, DownloaderRuntimeContext())
            downloader.open_attempt(prepared)
            downloader.close()

        request.assert_called_once()

    def test_response_stream_decodes_gzip_without_text_conversion(self):
        body = b"\xff<html><body>compressed</body></html>"
        response = requests.Response()
        response.status_code = 200
        response.url = "https://example.com/page"
        response.headers["Content-Type"] = "text/html; charset=latin-1"
        response.headers["Content-Encoding"] = "gzip"
        response.raw = HTTPResponse(
            body=io.BytesIO(gzip.compress(body)),
            headers={"Content-Encoding": "gzip"},
            preload_content=False,
        )
        wrapped = RequestsDownloadResponse(response, response.url)

        self.assertEqual(b"".join(wrapped.iter_bytes()), body)


if __name__ == "__main__":
    unittest.main()
