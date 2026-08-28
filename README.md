# rssync

`rssync` downloads RSS 2.0 feeds into `feeds/`. A feed can also archive the raw
HTML response behind each `<item><link>` and regenerate that RSS link as an
absolute URL pointing at the local archive.

## Configuration

Run `rssync [config-path]`. The default path is `rssync-config.json`; a complete
example is available in `rssync-config.example.json`.

The smallest valid configuration is:

```json
{
  "feeds": [
    {
      "url": "https://example.com/rss.xml"
    }
  ]
}
```

Feeds must be objects. The historical string-only `feeds` array is no longer
accepted. Each feed supports:

- `rss-downloader`: preset used for RSS XML; defaults to `default`.
- `download-webpages`: whether item webpages are archived; defaults to `false`.
- `webpage-downloader`: webpage preset; defaults to `default`.

When any feed enables webpage downloads, configure an absolute publication URL:

```json
{
  "webpages": {
    "public-base-url": "https://archive.example.com/rssync/",
    "storage-path": "pages"
  }
}
```

The generated RSS uses URLs such as
`https://archive.example.com/rssync/pages/example.com/article--012345abcdef.html`.
A feed with webpage downloads disabled always keeps its original links, even if
another feed archives the same page.

## Downloader presets

The `default` preset always exists and uses the built-in `requests` backend. It
does not need to be declared. A partial declaration overrides its defaults:

```json
{
  "downloaders": {
    "default": {
      "options": {
        "timeout": 45,
        "use-session": true,
        "user-agent": {
          "strategy": "per-run"
        }
      }
    },
    "rotating": {
      "backend": "requests",
      "options": {
        "use-session": false,
        "user-agent": {
          "strategy": "per-request"
        }
      }
    }
  }
}
```

The `requests` options are:

- `use-session`: reuse one Session per worker thread; defaults to `true`.
- `timeout`: connection and individual read timeout in seconds, not a total
  response deadline.
- `retries`: additional attempts after the initial request.
- `backoff-factor`: retry delay factor; retry `n` waits
  `factor * 2^(n-1)` seconds, unless a longer valid `Retry-After` is returned.
- `verify-tls`: TLS certificate verification.
- `headers`: additional request headers. `User-Agent` is reserved.
- `user-agent.strategy`: `per-run` or `per-request`. Redirects and retries keep
  the value selected for the logical request.
- `user-agent.fallback`: static value used if `fake-useragent` cannot select one.

The built-in retryable statuses are 429, 500, 502, 503, and 504. Downloads are
streamed to same-filesystem temporary files and atomically committed. There is
no application-level response size limit.

Third-party backends can register a factory under the `rssync.downloaders`
Python entry-point group. The public protocols are exported from
`rssync.downloaders`; a factory validates preset options and creates one
downloader instance per worker thread.

## Concurrency

```json
{
  "concurrency": {
    "rss-downloads": 2,
    "webpage-downloads": 8
  },
  "downloaders": {
    "default": {
      "concurrency": {
        "rss-downloads": 2,
        "webpage-downloads": 4
      }
    }
  }
}
```

The top-level values limit all active requests in each synchronization stage.
Preset values limit that preset within the corresponding stage and inherit the
global value when omitted. RSS downloads finish before webpage downloads begin.

## Archived data and manifests

Only `text/html` and `application/xhtml+xml` webpage responses are archived.
The response body is stored as bytes after HTTP transfer decoding, without
charset conversion, content extraction, HTML rewriting, or linked-asset
downloads. Relative images, stylesheets, and scripts may therefore not render
correctly from the archive.

`feeds.json` describes generated RSS files and their resolved downloader
presets. `pages.json` records archived paths, source and final URLs, response
headers, SHA-256 hashes, downloader metadata, timestamps, and current status.

If an RSS fetch or parse fails, the previous generated feed is retained. If a
webpage refresh fails, a valid existing archive remains linked; without a cache,
the original external target is retained. Relative source links are resolved
against the final RSS response URL so regenerated links remain absolute.
Historical webpage files are not automatically deleted.
