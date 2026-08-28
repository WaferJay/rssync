import unittest

from rssync.rss import rss_documents_equal


class RssComparisonTest(unittest.TestCase):
    def test_ignored_tags_match_exact_local_name_in_any_namespace(self):
        first = (
            b'<rss xmlns:a="urn:test"><channel><a:volatile>one</a:volatile>'
            b"<title>Feed</title></channel></rss>"
        )
        second = (
            b'<rss xmlns:b="urn:test"><channel><b:volatile>two</b:volatile>'
            b"<title>Feed</title></channel></rss>"
        )

        self.assertTrue(rss_documents_equal(first, second, ["volatile"]))

    def test_ignored_tag_matching_is_case_sensitive(self):
        first = b"<rss><channel><lastBuildDate>one</lastBuildDate></channel></rss>"
        second = b"<rss><channel><lastBuildDate>two</lastBuildDate></channel></rss>"

        self.assertFalse(rss_documents_equal(first, second, ["lastbuilddate"]))

    def test_non_ignored_content_still_changes_comparison(self):
        first = (
            b"<rss><channel><lastBuildDate>one</lastBuildDate>"
            b"<title>First</title></channel></rss>"
        )
        second = (
            b"<rss><channel><lastBuildDate>two</lastBuildDate>"
            b"<title>Second</title></channel></rss>"
        )

        self.assertFalse(rss_documents_equal(first, second, ["lastBuildDate"]))


if __name__ == "__main__":
    unittest.main()
