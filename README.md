# rssync

`rssync` downloads RSS 2.0 feeds into `feeds/` without rewriting their contents.
A feed can also archive the raw HTML response behind each `<item><link>`.
For feeds whose webpages are archived, rssync can generate an Atom view whose
entries point at those local files.
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
file and managed Atom output. Reconfiguring or disabling Atom output removes the
old managed Atom path. A webpage is removed from `pages.json` and storage after
it is no longer linked by any configured feed whose webpage downloads are
enabled. Shared links remain archived while at least one such feed still
references them. Empty archive subdirectories created by removed files are also
cleaned up.

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
- `webpage-refresh-policy`: optional override for the global webpage refresh
  policy.
- `change-detection`: optional per-feed override for RSS comparison rules.

Archived webpages are stored under `pages/` by default. The location can be
changed without configuring a publication host:

```json
{
  "webpages": {
    "storage-path": "pages",
    "refresh-policy": "on-rss-change",
    "atom": {
      "storage-path": "atoms",
      "missing-page-policy": "ignore"
    }
  }
}
```

`public-base-url` is not supported. RSS item links always retain their original
text, including relative links.

The optional `webpages.atom` object enables one derived Atom document for every
configured feed with `download-webpages: true`. Its `storage-path` defaults to
`atoms`; each output reuses the corresponding RSS path below that directory. For
example, `feeds/example.com/news/feed.xml` maps to
`atoms/example.com/news/feed.xml`.

Generated Atom documents do not contain `xml:base`. Their `self` links and local
entry `alternate` links are root-relative paths such as `/atoms/...` and
`/pages/...`, so a consumer resolves them against the origin from which the Atom
document was retrieved. Entry IDs remain absolute as required by Atom. A local
entry also includes its original webpage URL as an absolute `via` link.

`webpages.atom.missing-page-policy` controls entries whose webpage has no valid
local archive:

- `ignore`, the default, omits the entry.
- `source-url` retains the entry and uses the original absolute webpage URL as
  its `alternate` link.

RSS channel and item titles, descriptions, authors, categories, GUIDs, and valid
dates are mapped where Atom has an equivalent. Archived HTML remains in its own
file and is not embedded in the Atom document.

## Webpage refresh policies

The global `webpages.refresh-policy` setting controls when an existing webpage
archive is downloaded again. It supports three built-in strategies:

- `always` downloads every current webpage on every successful RSS fetch. This
  is the default and preserves the historical behavior.
- `on-rss-change` downloads existing webpages only when the RSS document has a
  meaningful change under its configured RSS change-detection rules.
- `missing-only` treats an existing webpage archive as immutable and never
  downloads it again.

All strategies download a page when its manifest record is missing or unsafe,
or when the recorded file no longer exists. An unrecorded file is not treated
as a cache entry. A feed can override the global strategy:

```json
{
  "webpages": {
    "refresh-policy": "on-rss-change"
  },
  "feeds": [
    {
      "url": "https://example.com/rss.xml",
      "download-webpages": true,
      "webpage-refresh-policy": "missing-only"
    }
  ]
}
```

When multiple feeds reference the same canonical URL, rssync downloads it once
if any referencing feed's strategy requests a refresh. The first such feed in
configuration order selects the webpage downloader. A policy-skipped page keeps
its original timestamps and download metadata and is reported with `skipped`
status in `pages.json`. If `storage-path` changes, a skipped archive remains at
its recorded path; newly downloaded pages use the new storage path.

Refresh decisions use the `WebpageRefreshStrategy` protocol and
`WebpageRefreshRegistry` in `rssync.webpage_refresh`. Applications can register
and inject another strategy into both configuration parsing and `SyncEngine`;
the synchronization orchestration does not need strategy-specific branches.

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

The `default` preset always exists and uses the built-in asynchronous `httpx`
backend. It does not need to be declared. A partial declaration overrides its
defaults:

```json
{
  "downloaders": {
    "default": {
      "options": {
        "timeout": 45,
        "http2": true,
        "user-agent": {
          "strategy": "per-run"
        }
      }
    },
    "rotating": {
      "backend": "httpx",
      "options": {
        "http2": false,
        "user-agent": {
          "strategy": "per-request"
        }
      }
    }
  }
}
```

The `httpx` options are:

- `http2`: enable HTTP/2 when supported by the server; defaults to `true` and
  falls back to HTTP/1.1 when necessary.
- `timeout`: connection, read, write, and connection-pool inactivity timeout in
  seconds, not a total response deadline.
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
Python entry-point group. The public asynchronous protocols are exported from
`rssync.downloaders`; a factory validates preset options and creates one shared
downloader instance per preset. Response streaming and backend cleanup are
awaitable operations.

Python integrations call `await SyncEngine(config).run()`. The exported
`fetch_rss_xml` and `rss_update_worker` compatibility helpers are also
coroutines; the `rssync` command itself manages the asyncio event loop.

## Concurrency

```json
{
  "concurrency": {
    "rss-downloads": 2,
    "webpage-downloads": 8,
    "per-domain-downloads": 2,
    "request-interval": 0.5
  }
}
```

`rss-downloads` and `webpage-downloads` limit all active attempts in their
respective synchronization stages. RSS downloads finish before webpage downloads
begin. Their defaults are 2 and 8.

`per-domain-downloads` optionally limits active attempts for each hostname. The
limit is shared by all downloader presets and both stages; protocol and port do
not create separate buckets. Omitting it leaves per-domain concurrency
unlimited. Tasks waiting for one busy hostname do not reserve a stage-wide
download slot, so other hostnames can continue making progress.

`request-interval` is the minimum number of seconds between the actual start
times of two attempts for the same hostname. It accepts zero and fractional
values, defaults to 0, and applies to retries as well as initial attempts.
Different hostnames are paced independently. Retry backoff and `Retry-After`
remain in effect in addition to this interval.

All concurrency settings are top-level. Downloader presets do not accept a
`concurrency` field.

## Archived data and manifests

Only `text/html` and `application/xhtml+xml` webpage responses are archived.
The response body is stored as bytes after HTTP transfer decoding, without
charset conversion, content extraction, HTML rewriting, or linked-asset
downloads. Relative images, stylesheets, and scripts may therefore not render
correctly from the archive.

`feeds.json` describes original archived RSS files and their resolved downloader
presets. When Atom output is enabled, each applicable feed record also reports
`atom_path`, `atom_updated_at`, `atom_changed`, and `atom_status`; the sync record
lists changed Atom paths in `changed_atoms`. `pages.json` records archived paths,
canonical source and final URLs, response headers, SHA-256 hashes, downloader
metadata, timestamps, and current status.

Manifest paths are root-relative and never contain a URL scheme or authority. RSS paths
look like `/feeds/example.com/feed.xml`; webpage paths look like
`/pages/example.com/article--012345abcdef.html`; default Atom paths look like
`/atoms/example.com/feed.xml`. On disk, these are relative to the synchronization
root after removing the leading `/`.

A downstream generator can still resolve an item link against the corresponding
`feeds[].final_url`, canonicalize it, and find the matching
`pages[].source_url`. The optional generated Atom provides this mapping directly
without requiring a publication origin in the rssync configuration.

If an RSS fetch or parse fails, the previous original feed is retained. If a
webpage refresh fails, a valid existing archive is reported with `cached`
status; without a cache, it is reported as `failed`. RSS links are unaffected.
An existing Atom document is regenerated from the last usable RSS when possible,
or retained unchanged if that RSS can no longer be parsed.
Historical webpage and Atom files are not automatically deleted unless
`archive-current-only` is enabled.
