import concurrent.futures
import io
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
P_IGNORE_TAGS = [
    re.compile(b'<lastBuildDate>.*?</lastBuildDate>', re.I)
]


def ensure_file_directory(file):
    d, _ = os.path.split(file)
    os.makedirs(d, exist_ok=True)


def md5sum(data: bytes):
    md5 = hashlib.md5(data)
    return md5.hexdigest()


def fetch_rss_xml(url, basepath='.'):
    resp = se.get(url)
    resp.raise_for_status()
    logger.info("Fetched %d bytes from %s", len(resp.content), url)
    parse_result = urlparse(url)
    urlpath = unquote(parse_result.path.removeprefix('/'))
    relpath = os.path.join(parse_result.netloc, urlpath)
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
    try:
        temp_file, relpath = fetch_rss_xml(url, temp_dir)
    except Exception as e:
        logger.error('Fetch failed: %s', url, exc_info=True)
        return

    target_file = os.path.join(target_dir, relpath)
    if os.path.exists(target_file) and is_duplicate_rss_file(temp_file, target_file):
        return
    ensure_file_directory(target_file)
    shutil.copyfile(temp_file, target_file)
    logger.info('Update RSS feed %s -> %s', temp_file, target_file)
    return target_file


def main(args=None):
    args = args or sys.argv
    match len(args):
        case 0 | 1:
            config_file = os.path.relpath('rssync-config.json')
        case _:
            config_file = args[1]

    with open(config_file, 'r') as fp:
        config_data = json.load(fp)
    feed_urls = config_data['feeds']
    max_concurrency = int(config_data.get('max-concurrency', 2))

    err_rss_urls = []
    random.shuffle(feed_urls)
    update_feeds = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        results = executor.map(rss_update_worker, feed_urls, [RSS_FEED_NEW_PATH] * len(feed_urls), [RSS_FEED_PATH] * len(feed_urls))

        for url, file in zip(feed_urls, results):
            if file:
                logger.info('Updated %s [feed: %s]', file, url)
                update_feeds.append({'url': url, 'path': file})
            else:
                logger.debug('Skipped %s [feed: %s]', file, url)

    if err_rss_urls:
        logger.warning('Failed to retrieve URLs: %s', pprint.pformat(err_rss_urls))

    summary = {'last_updated_feeds': update_feeds, 'update_time': int(time.time() * 1000)}
    if update_feeds or not os.path.exists('feeds.json'):
        with open('feeds.json', 'w') as fp:
            json.dump(summary, fp, indent=2)
    logger.info('Updated %d feeds (Total: %d): %s', len(update_feeds), len(feed_urls), pprint.pformat(summary))


if __name__ == '__main__':
    main()
