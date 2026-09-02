import unittest

from rssync.rss import canonicalize_http_url, rss_documents_equal


class UrlCanonicalizationTest(unittest.TestCase):
    def test_normalizes_equivalent_http_url_spelling(self):
        self.assertEqual(
            canonicalize_http_url(
                "HTTPS://Example.COM:443/a/./b/../%7euser?q=%41%2f#section"
            ),
            "https://example.com/a/~user?q=A%2F",
        )

    def test_decodes_only_unreserved_percent_escapes(self):
        self.assertEqual(
            canonicalize_http_url("https://example.com/%7e/a%2fb?x=%2e%2f"),
            "https://example.com/~/a%2Fb?x=.%2F",
        )

    def test_preserves_semantically_distinct_url_components(self):
        self.assertNotEqual(
            canonicalize_http_url("https://example.com/article"),
            canonicalize_http_url("https://example.com/article/"),
        )
        self.assertNotEqual(
            canonicalize_http_url("https://example.com/?a=1&b=2"),
            canonicalize_http_url("https://example.com/?b=2&a=1"),
        )
        self.assertEqual(
            canonicalize_http_url("https://example.com//article"),
            "https://example.com//article",
        )


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
