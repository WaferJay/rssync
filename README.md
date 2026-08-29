# rssync

`rssync` downloads RSS 2.0 feeds into `feeds/` without rewriting their contents.
A feed can also archive the raw HTML response behind each `<item><link>`.
`feeds.json` and `pages.json` expose the relationship between the original RSS
and archived pages so publication tooling can choose its own URL prefix.

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

By default, rssync keeps files from feeds and webpage links that later disappear.
Set the top-level `archive-current-only` option to keep only the archive that can
be attributed to the current configuration and latest usable RSS documents:

```json
{
  "archive-current-only": true,
  "feeds": [
    {
      "url": "https://example.com/rss.xml",
      "download-webpages": true
    }
  ]
}
```

In this mode, removing a configured feed removes its previously recorded RSS
file. A webpage is removed from `pages.json` and storage after it is no longer
linked by any configured feed whose webpage downloads are enabled. Shared links
remain archived while at least one such feed still references them. Empty
archive subdirectories created by removed files are also cleaned up.

RSS download or parsing failures retain the previous RSS and protect the pages
referenced by that last usable document. If those references cannot be recovered
safely, webpage cleanup is skipped for that run. For deletion safety, rssync only
removes files whose ownership can be verified from the previous manifests; it
does not sweep unrecorded files from archive directories. The option defaults to
`false` for backward compatibility.

Feeds must be objects. The historical string-only `feeds` array is no longer
accepted. Each feed supports:

- `rss-downloader`: preset used for RSS XML; defaults to `default`.
- `download-webpages`: whether item webpages are archived; defaults to `false`.
- `webpage-downloader`: webpage preset; defaults to `default`.
- `change-detection`: optional per-feed override for RSS comparison rules.

Archived webpages are stored under `pages/` by default. The location can be
changed without configuring a publication host:

```json
{
  "webpages": {
    "storage-path": "pages"
  }
}
```

`public-base-url` is not supported. RSS item links always retain their original
text, including relative links.

## RSS change detection

RSS responses are persisted as their original bytes. By default, changes that
only affect elements named `lastBuildDate` do not replace the archived RSS:

```json
{
  "rss": {
    "change-detection": {
      "ignore-tags": ["lastBuildDate"]
    }
  }
}
```

Tag names are case-sensitive XML local names, so namespace prefixes do not need
to be configured. A feed can replace the global list; an empty list enables
strict byte comparison:

```json
{
  "url": "https://example.com/rss.xml",
  "change-detection": {
    "ignore-tags": []
  }
}
```

Ignored tags only affect change detection. RSS content is never normalized or
serialized before it is written. If only ignored elements changed, the previous
original response remains in `feeds/`.

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

`feeds.json` describes original archived RSS files and their resolved downloader
presets. `pages.json` records archived paths, canonical source and final URLs,
response headers, SHA-256 hashes, downloader metadata, timestamps, and current
status.

Manifest paths are root-relative and never contain a schema or host. RSS paths
look like `/feeds/example.com/feed.xml`; webpage paths look like
`/pages/example.com/article--012345abcdef.html`. On disk, these are relative to
the synchronization root after removing the leading `/`.

A downstream RSS generator can resolve an item link against the corresponding
`feeds[].final_url`, canonicalize it, find the matching `pages[].source_url`, and
prepend its own origin or deployment prefix to `pages[].path`. rssync itself does
not generate that derived RSS.

If an RSS fetch or parse fails, the previous original feed is retained. If a
webpage refresh fails, a valid existing archive is reported with `cached`
status; without a cache, it is reported as `failed`. RSS links are unaffected.
Historical webpage files are not automatically deleted unless
`archive-current-only` is enabled.
