# rssync

A GitHub Action workflow to automatically fetch and archive RSS feeds in Git.

After synchronization, `feeds.json` describes the local RSS files:

```json
{
  "feeds": [
    {
      "path": "/feeds/example.com/rss.xml",
      "source_url": "https://example.com/rss.xml",
      "updated_at": 1787369300,
      "fetched_at": 1787369300,
      "changed": true
    }
  ],
  "sync": {
    "completed_at": 1787369400,
    "changed": [
      "/feeds/example.com/rss.xml"
    ]
  }
}
```

`feeds` contains all configured feeds that currently have a local archive. The
per-feed `updated_at`, `fetched_at`, and `sync.completed_at` values are Unix
timestamps in seconds. `updated_at` changes only when the local RSS content
changes; `fetched_at` records when that feed's worker finishes successfully.
The per-feed `changed` value and `sync.changed` identify feeds changed during
the most recent synchronization.
