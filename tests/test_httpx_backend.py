import gzip
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from rssync.config import DEFAULT_USER_AGENT
from rssync.downloaders.base import (
    DownloaderRuntimeContext,
    DownloadRequest,
    PreparedDownloadRequest,
)
from rssync.downloaders.httpx_backend import (
    HttpxDownloader,
    parse_httpx_options,
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


def options(strategy, *, fallback=DEFAULT_USER_AGENT, http2=True):
    return parse_httpx_options(
        {
            "http2": http2,
            "user-agent": {
                "strategy": strategy,
                "fallback": fallback,
            },
        }
    )


class HttpxBackendTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        SequenceUserAgent.calls = 0

    def test_http2_defaults_on_and_removed_session_option_is_rejected(self):
        self.assertTrue(parse_httpx_options({}).http2)
        self.assertFalse(parse_httpx_options({"http2": False}).http2)
        with self.assertRaisesRegex(ValueError, "use-session"):
            parse_httpx_options({"use-session": True})
        with self.assertRaisesRegex(TypeError, "http2 must be a boolean"):
            parse_httpx_options({"http2": "true"})

    async def test_per_run_user_agent_is_shared_across_instances(self):
        runtime = DownloaderRuntimeContext()
        with patch(
            "rssync.downloaders.httpx_backend.UserAgent",
            SequenceUserAgent,
        ):
            first = HttpxDownloader(options("per-run"), runtime)
            second = HttpxDownloader(options("per-run"), runtime)
            request = DownloadRequest("https://example.com", "rss")

            self.assertEqual(first.prepare(request).headers["User-Agent"], "agent-1")
            self.assertEqual(second.prepare(request).headers["User-Agent"], "agent-1")
            self.assertEqual(SequenceUserAgent.calls, 1)

        await first.close()
        await second.close()

    async def test_per_request_user_agent_changes_per_logical_request(self):
        runtime = DownloaderRuntimeContext()
        with patch(
            "rssync.downloaders.httpx_backend.UserAgent",
            SequenceUserAgent,
        ):
            downloader = HttpxDownloader(options("per-request"), runtime)
            request = DownloadRequest("https://example.com", "webpage")

            self.assertEqual(
                downloader.prepare(request).headers["User-Agent"], "agent-1"
            )
            self.assertEqual(
                downloader.prepare(request).headers["User-Agent"], "agent-2"
            )

        await downloader.close()

    async def test_user_agent_initialization_failure_uses_fallback(self):
        runtime = DownloaderRuntimeContext()
        with patch(
            "rssync.downloaders.httpx_backend.UserAgent",
            BrokenUserAgent,
        ):
            downloader = HttpxDownloader(
                options("per-request", fallback="fallback-agent"), runtime
            )
            prepared = downloader.prepare(
                DownloadRequest("https://example.com", "rss")
            )

        self.assertEqual(prepared.headers["User-Agent"], "fallback-agent")
        await downloader.close()

    async def test_client_enables_http2_and_uses_validated_transport_options(self):
        client = Mock()
        client.aclose = AsyncMock()
        configured = parse_httpx_options(
            {"http2": True, "timeout": 12, "verify-tls": False}
        )

        with patch(
            "rssync.downloaders.httpx_backend.httpx.AsyncClient",
            return_value=client,
        ) as client_type:
            downloader = HttpxDownloader(configured, DownloaderRuntimeContext())
            await downloader.close()

        client_type.assert_called_once_with(
            http2=True,
            timeout=12.0,
            verify=False,
            follow_redirects=True,
        )
        client.aclose.assert_awaited_once()

    async def test_open_attempt_uses_manual_streaming(self):
        response = Mock()
        client = Mock()
        client.build_request.return_value = Mock()
        client.send = AsyncMock(return_value=response)
        client.aclose = AsyncMock()
        prepared = PreparedDownloadRequest("https://example.com", "rss", {}, {})

        with patch(
            "rssync.downloaders.httpx_backend.httpx.AsyncClient",
            return_value=client,
        ):
            downloader = HttpxDownloader(options("per-run"), DownloaderRuntimeContext())
            wrapped = await downloader.open_attempt(prepared)
            await downloader.close()

        self.assertIs(wrapped._response, response)
        client.send.assert_awaited_once_with(
            client.build_request.return_value,
            stream=True,
        )

    async def test_redirect_and_gzip_stream_preserve_decoded_bytes(self):
        body = b"\xff<html><body>compressed</body></html>"

        async def handler(request):
            if request.url.path == "/start":
                return httpx.Response(302, headers={"Location": "/final"})
            return httpx.Response(
                200,
                content=gzip.compress(body),
                headers={
                    "Content-Type": "text/html; charset=latin-1",
                    "Content-Encoding": "gzip",
                },
            )

        real_async_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)

        def client_factory(**kwargs):
            return real_async_client(transport=transport, **kwargs)

        with patch(
            "rssync.downloaders.httpx_backend.httpx.AsyncClient",
            side_effect=client_factory,
        ):
            downloader = HttpxDownloader(options("per-run"), DownloaderRuntimeContext())
            prepared = downloader.prepare(
                DownloadRequest("https://example.com/start", "webpage")
            )
            wrapped = await downloader.open_attempt(prepared)
            received = b"".join([chunk async for chunk in wrapped.iter_bytes()])
            await wrapped.close()
            await downloader.close()

        self.assertEqual(received, body)
        self.assertEqual(wrapped.final_url, "https://example.com/final")

    async def test_httpx_request_errors_are_retryable(self):
        downloader = HttpxDownloader(options("per-run"), DownloaderRuntimeContext())
        request = httpx.Request("GET", "https://example.com")

        self.assertTrue(
            downloader.is_retryable_exception(
                httpx.ConnectError("connection failed", request=request)
            )
        )
        self.assertFalse(downloader.is_retryable_exception(ValueError("invalid")))
        await downloader.close()


if __name__ == "__main__":
    unittest.main()
