import concurrent.futures
import re
import sys
import shutil
import json
import hashlib
import os.path
import logging
import time
import random
import pprint
from urllib.parse import urlparse, unquote
from urllib3.util import Retry

import requests
from requests.adapters import HTTPAdapter


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


se = requests.Session()
retries = Retry(
    total=6,
    backoff_factor=0.3,
    status_forcelist=[502, 503, 504],
    allowed_methods={'POST', 'GET'},
)
se.mount('https://', HTTPAdapter(max_retries=retries))
se.headers.update({
    'User-Agent': 'Mozilla/5.0 +https://podnews.net/bot PodnewsBot/1.0'
})

RSS_FEED_NEW_PATH = '.new-feeds'
RSS_FEED_PATH = 'feeds'
RSS_FEED_MANIFEST_PATH = 'feeds.json'
P_IGNORE_TAGS = [
    re.compile(b'<lastBuildDate>.*?</lastBuildDate>', re.I)
]


def ensure_file_directory(file):
    d, _ = os.path.split(file)
    os.makedirs(d, exist_ok=True)


def md5sum(data: bytes):
    md5 = hashlib.md5(data)
    return md5.hexdigest()


def rss_feed_relpath(url):
    parse_result = urlparse(url)
    urlpath = unquote(parse_result.path.removeprefix('/'))
    return os.path.join(parse_result.netloc, urlpath)


def rss_feed_local_url(relpath):
    urlpath = relpath.replace(os.sep, '/').lstrip('/')
    return f'/{RSS_FEED_PATH}/{urlpath}'


def unique_feed_urls(feed_urls):
    seen = set()
    unique_urls = []
    for url in feed_urls:
        if url in seen:
            continue
        seen.add(url)
        unique_urls.append(url)
    return unique_urls


def fetch_rss_xml(url, basepath='.'):
    resp = se.get(url)
    resp.raise_for_status()
    logger.info("Fetched %d bytes from %s", len(resp.content), url)
    relpath = rss_feed_relpath(url)
    target_path = os.path.join(basepath, relpath)
    ensure_file_directory(target_path)
    with open(target_path, 'wb') as fp:
        fp.write(resp.content)
    return target_path, relpath


def is_duplicate_rss_file(rss_file1, rss_file2):
    with open(rss_file1, 'rb') as fp1, open(rss_file2, 'rb') as fp2:
        data1 = fp1.read()
        data2 = fp2.read()
    for p in P_IGNORE_TAGS:
        data1 = p.sub(b'', data1)
        data2 = p.sub(b'', data2)
    dup = md5sum(data1) == md5sum(data2)
    return dup


def rss_update_worker(url, temp_dir, target_dir):
    relpath = rss_feed_relpath(url)
    target_file = os.path.join(target_dir, relpath)
    try:
        temp_file, _ = fetch_rss_xml(url, temp_dir)
        changed = not (
            os.path.exists(target_file)
            and is_duplicate_rss_file(temp_file, target_file)
        )
        if changed:
            ensure_file_directory(target_file)
            shutil.copyfile(temp_file, target_file)
            logger.info('Update RSS feed %s -> %s', temp_file, target_file)

        # Record the time after this feed has been fetched and its local
        # archive has been checked or updated. This is intentionally inside
        # the worker so feeds completed at different times get different
        # timestamps.
        fetched_at = int(time.time())
        return {
            'source_url': url,
            'target_path': target_file,
            'changed': changed,
            'fetched_at': fetched_at,
        }
    except Exception as e:
        logger.error('Fetch failed: %s', url, exc_info=True)
        return


def load_feed_metadata(manifest_path=RSS_FEED_MANIFEST_PATH):
    try:
        with open(manifest_path, 'r', encoding='utf-8') as fp:
            manifest = json.load(fp)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        logger.warning('Unable to read feed manifest %s', manifest_path, exc_info=True)
        return {}

    metadata = {}
    for feed in manifest.get('feeds', []):
        if not isinstance(feed, dict):
            continue
        source_url = feed.get('source_url')
        if source_url:
            metadata[source_url] = {
                'updated_at': feed.get('updated_at'),
                'fetched_at': feed.get('fetched_at'),
            }

    # Migrate timestamps that can be recovered from the previous manifest
    # format. Feeds not present in last_updated_feeds remain unknown (None).
    legacy_updated_at = manifest.get('update_time')
    if isinstance(legacy_updated_at, (int, float)):
        legacy_updated_at = int(legacy_updated_at // 1000)
    else:
        legacy_updated_at = None
    for feed in manifest.get('last_updated_feeds', []):
        if not isinstance(feed, dict):
            continue
        source_url = feed.get('url')
        if source_url and source_url not in metadata:
            metadata[source_url] = {
                'updated_at': legacy_updated_at,
                'fetched_at': None,
            }

    return metadata


def load_feed_updated_at(manifest_path=RSS_FEED_MANIFEST_PATH):
    """Return legacy updated-at metadata for callers using the old helper."""
    return {
        source_url: feed_metadata.get('updated_at')
        for source_url, feed_metadata in load_feed_metadata(manifest_path).items()
    }


def build_feed_manifest(
    feed_urls,
    available_feed_urls,
    feed_results,
    previous_feed_metadata,
    sync_time,
):
    available_feed_urls = set(available_feed_urls)
    results_by_url = {
        result['source_url']: result
        for result in feed_results
    }
    feeds = []
    changed_paths = []

    for source_url in feed_urls:
        if source_url not in available_feed_urls:
            continue

        path = rss_feed_local_url(rss_feed_relpath(source_url))
        previous = previous_feed_metadata.get(source_url, {})
        result = results_by_url.get(source_url)
        changed = bool(result and result['changed'])
        if result:
            fetched_at = result['fetched_at']
        else:
            fetched_at = previous.get('fetched_at')

        if changed:
            updated_at = result['fetched_at']
            changed_paths.append(path)
        else:
            updated_at = previous.get('updated_at')

        feeds.append({
            'path': path,
            'source_url': source_url,
            'updated_at': updated_at,
            'fetched_at': fetched_at,
            'changed': changed,
        })

    return {
        'feeds': feeds,
        'sync': {
            'completed_at': sync_time,
            'changed': changed_paths,
        },
    }


def main(args=None):
    args = args or sys.argv
    match len(args):
        case 0 | 1:
            config_file = os.path.relpath('rssync-config.json')
        case _:
            config_file = args[1]

    with open(config_file, 'r') as fp:
        config_data = json.load(fp)
    feed_urls = unique_feed_urls(config_data['feeds'])
    max_concurrency = int(config_data.get('max-concurrency', 2))

    worker_feed_urls = list(feed_urls)
    random.shuffle(worker_feed_urls)
    feed_results = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        results = executor.map(
            rss_update_worker,
            worker_feed_urls,
            [RSS_FEED_NEW_PATH] * len(worker_feed_urls),
            [RSS_FEED_PATH] * len(worker_feed_urls),
        )

        for url, result in zip(worker_feed_urls, results):
            if result:
                feed_results.append(result)
                if result['changed']:
                    logger.info('Updated %s [feed: %s]', result['target_path'], url)
                else:
                    logger.debug('Unchanged %s [feed: %s]', result['target_path'], url)
            else:
                logger.debug('Skipped %s [feed: %s]', result, url)

    available_feed_urls = [
        url for url in feed_urls
        if os.path.isfile(os.path.join(RSS_FEED_PATH, rss_feed_relpath(url)))
    ]
    sync_time = int(time.time())
    manifest = build_feed_manifest(
        feed_urls,
        available_feed_urls,
        feed_results,
        load_feed_metadata(),
        sync_time,
    )
    with open(RSS_FEED_MANIFEST_PATH, 'w', encoding='utf-8') as fp:
        json.dump(manifest, fp, indent=2, ensure_ascii=False)
        fp.write('\n')

    logger.info(
        'Updated %d feeds, changed %d feeds (Total: %d): %s',
        len(available_feed_urls),
        len(manifest['sync']['changed']),
        len(feed_urls),
        pprint.pformat(manifest),
    )


if __name__ == '__main__':
    main()
