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
    if not os.path.exists(d):
        os.makedirs(d)


def md5sum(data: bytes):
    md5 = hashlib.md5(data)
    return md5.hexdigest()


def fetch_rss_xml(url, basepath='.'):
    resp = se.get(url)
    resp.raise_for_status()
    parse_result = urlparse(url)
    urlpath = unquote(parse_result.path.removeprefix('/'))
    relpath = os.path.join(basepath, parse_result.netloc, urlpath)
    ensure_file_directory(relpath)
    with open(relpath, 'wb') as fp:
        fp.write(resp.content)
    return relpath


def is_duplicate_rss_file(rss_file1, rss_file2):
    with open(rss_file1, 'rb') as fp1, open(rss_file2, 'rb') as fp2:
        data1 = fp1.read()
        data2 = fp2.read()
    for p in P_IGNORE_TAGS:
        data1 = p.sub(data1, b'')
        data2 = p.sub(data2, b'')
    return md5sum(data1) == md5sum(data2)


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

    new_rss_files = []
    err_rss_urls = []
    random.shuffle(feed_urls)

    for u in feed_urls:
        try:
            relpath = fetch_rss_xml(u, RSS_FEED_NEW_PATH)
            new_rss_files.append(relpath)
        except Exception as e:
            logger.error('Fetch failed to %s [%s]', relpath, u, exc_info=True)
            err_rss_urls.append(u)
        time.sleep(3)

    for f in new_rss_files:
        target_file = os.path.join(
                RSS_FEED_PATH, f.removeprefix(RSS_FEED_NEW_PATH).removeprefix('/'))
        if os.path.exists(target_file) and is_duplicate_rss_file(f, target_file):
            continue
        ensure_file_directory(target_file)
        shutil.copyfile(f, target_file)
    if err_rss_urls:
        logger.warning('Failed to retrieve URLs: %s', pprint.pformat(err_rss_urls))


if __name__ == '__main__':
    main()
