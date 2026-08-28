import json
import os
import tempfile
import unittest
from unittest.mock import patch

from rssync.main import (
    build_feed_manifest,
    load_feed_metadata,
    rss_feed_local_url,
    rss_feed_relpath,
    rss_update_worker,
)


class FeedManifestTest(unittest.TestCase):
    def test_feed_path_and_local_url(self):
        relpath = rss_feed_relpath('https://example.com/path/rss.xml?format=full')

        self.assertEqual(relpath, os.path.join('example.com', 'path', 'rss.xml'))
        self.assertEqual(rss_feed_local_url(relpath), '/feeds/example.com/path/rss.xml')

    def test_manifest_contains_all_available_feeds_and_changed_paths(self):
        feed_a = 'https://example.com/rss.xml'
        feed_b = 'https://news.example.org/feed.xml'
        manifest = build_feed_manifest(
            [feed_a, feed_b],
            [feed_a, feed_b],
            [
                {'source_url': feed_a, 'changed': True, 'fetched_at': 201},
                {'source_url': feed_b, 'changed': False, 'fetched_at': 202},
            ],
            {feed_b: {'updated_at': 100, 'fetched_at': 150}},
            300,
        )

        self.assertEqual(manifest['feeds'], [
            {
                'path': '/feeds/example.com/rss.xml',
                'source_url': feed_a,
                'updated_at': 201,
                'fetched_at': 201,
                'changed': True,
            },
            {
                'path': '/feeds/news.example.org/feed.xml',
                'source_url': feed_b,
                'updated_at': 100,
                'fetched_at': 202,
                'changed': False,
            },
        ])
        self.assertEqual(manifest['sync'], {
            'completed_at': 300,
            'changed': ['/feeds/example.com/rss.xml'],
        })

    def test_manifest_omits_unavailable_feeds(self):
        feed_a = 'https://example.com/rss.xml'
        feed_b = 'https://news.example.org/feed.xml'
        manifest = build_feed_manifest(
            [feed_a, feed_b],
            [feed_a],
            [],
            {feed_a: {'updated_at': 100, 'fetched_at': 150}},
            200,
        )

        self.assertEqual(len(manifest['feeds']), 1)
        self.assertEqual(manifest['feeds'][0]['source_url'], feed_a)
        self.assertEqual(manifest['feeds'][0]['updated_at'], 100)
        self.assertEqual(manifest['feeds'][0]['fetched_at'], 150)
        self.assertFalse(manifest['feeds'][0]['changed'])
        self.assertEqual(manifest['sync']['changed'], [])

    def test_worker_records_its_own_completion_time(self):
        source_url = 'https://example.com/rss.xml'
        with tempfile.TemporaryDirectory() as directory:
            downloaded_file = os.path.join(directory, 'downloaded.xml')
            with open(downloaded_file, 'wb') as fp:
                fp.write(b'<rss version="2.0" />')

            with patch(
                'rssync.main.fetch_rss_xml',
                return_value=(downloaded_file, 'example.com/rss.xml'),
            ), patch('rssync.main.time.time', return_value=123):
                result = rss_update_worker(
                    source_url,
                    os.path.join(directory, 'temp'),
                    os.path.join(directory, 'feeds'),
                )

            self.assertEqual(result['source_url'], source_url)
            self.assertTrue(result['changed'])
            self.assertEqual(result['fetched_at'], 123)

    def test_load_feed_metadata_migrates_legacy_update_time(self):
        feed_a = 'https://example.com/rss.xml'
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = os.path.join(directory, 'feeds.json')
            with open(manifest_path, 'w', encoding='utf-8') as fp:
                json.dump({
                    'last_updated_feeds': [
                        {'url': feed_a, 'path': 'feeds/example.com/rss.xml'}
                    ],
                    'update_time': 200000,
                }, fp)

            self.assertEqual(load_feed_metadata(manifest_path), {
                feed_a: {'updated_at': 200, 'fetched_at': None},
            })


if __name__ == '__main__':
    unittest.main()
