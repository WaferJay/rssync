import unittest
import xml.etree.ElementTree as ET

from rssync.atom import ATOM_NAMESPACE, build_atom_feed
from rssync.rss import RssDocument


ATOM = f"{{{ATOM_NAMESPACE}}}"


class AtomGenerationTest(unittest.TestCase):
    def test_maps_rss_metadata_and_uses_root_relative_local_links(self):
        source = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example News</title>
    <description>&lt;b&gt;Latest news&lt;/b&gt;</description>
    <link>https://example.com/</link>
    <managingEditor>editor@example.com</managingEditor>
    <lastBuildDate>Tue, 31 Dec 2024 20:00:00 GMT</lastBuildDate>
    <item>
      <title>Archived story</title>
      <guid>story-1</guid>
      <pubDate>Tue, 31 Dec 2024 12:00:00 GMT</pubDate>
      <description>&lt;strong&gt;Summary&lt;/strong&gt;</description>
      <author>writer@example.com</author>
      <category domain="https://example.com/topics">Testing</category>
      <link>/story</link>
    </item>
  </channel>
</rss>
"""
        document = RssDocument.parse(
            source,
            "https://example.com/news/feed.xml",
        )
        canonical_url = document.links[0].canonical_url
        page_path = "/pages/example.com/story--0123456789ab.html"

        output = build_atom_feed(
            document,
            source_feed_url="https://example.com/news/feed.xml",
            self_path="/atoms/example.com/news/feed.xml",
            page_records={
                canonical_url: {
                    "path": page_path,
                    "content_type": "text/html; charset=utf-8",
                    "updated_at": 1735689600,
                }
            },
            available_pages={canonical_url},
            missing_page_policy="ignore",
            fallback_updated_at=100,
        )

        root = ET.fromstring(output)
        self.assertEqual(root.tag, f"{ATOM}feed")
        self.assertNotIn("{http://www.w3.org/XML/1998/namespace}base", root.attrib)
        self.assertEqual(root.findtext(f"{ATOM}title"), "Example News")
        self.assertEqual(
            root.findtext(f"{ATOM}subtitle"),
            "<b>Latest news</b>",
        )
        self.assertEqual(
            root.findtext(f"{ATOM}updated"),
            "2025-01-01T00:00:00Z",
        )
        self.assertEqual(
            root.findtext(f"{ATOM}author/{ATOM}name"),
            "editor@example.com",
        )

        feed_links = {
            link.attrib["rel"]: link.attrib
            for link in root.findall(f"{ATOM}link")
        }
        self.assertEqual(
            feed_links["self"]["href"],
            "/atoms/example.com/news/feed.xml",
        )
        self.assertEqual(
            feed_links["via"]["href"],
            "https://example.com/news/feed.xml",
        )

        entry = root.find(f"{ATOM}entry")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.findtext(f"{ATOM}title"), "Archived story")
        self.assertTrue(
            entry.findtext(f"{ATOM}id").startswith("urn:rssync:entry:")
        )
        self.assertEqual(
            entry.findtext(f"{ATOM}published"),
            "2024-12-31T12:00:00Z",
        )
        self.assertEqual(
            entry.findtext(f"{ATOM}summary"),
            "<strong>Summary</strong>",
        )
        self.assertEqual(
            entry.findtext(f"{ATOM}author/{ATOM}name"),
            "writer@example.com",
        )
        category = entry.find(f"{ATOM}category")
        self.assertEqual(category.attrib["term"], "Testing")
        self.assertEqual(category.attrib["scheme"], "https://example.com/topics")

        entry_links = {
            link.attrib["rel"]: link.attrib
            for link in entry.findall(f"{ATOM}link")
        }
        self.assertEqual(entry_links["alternate"]["href"], page_path)
        self.assertEqual(
            entry_links["alternate"]["type"],
            "text/html; charset=utf-8",
        )
        self.assertEqual(
            entry_links["via"]["href"],
            "https://example.com/story",
        )

    def test_missing_page_policy_controls_entry_inclusion(self):
        document = RssDocument.parse(
            b"<rss><channel><item><link>/missing</link></item></channel></rss>",
            "https://example.com/feed.xml",
        )
        common = {
            "source_feed_url": "https://example.com/feed.xml",
            "self_path": "/atoms/example.com/feed.xml",
            "page_records": {},
            "available_pages": set(),
            "fallback_updated_at": 100,
        }

        ignored = ET.fromstring(
            build_atom_feed(document, missing_page_policy="ignore", **common)
        )
        self.assertEqual(ignored.findall(f"{ATOM}entry"), [])

        fallback = ET.fromstring(
            build_atom_feed(document, missing_page_policy="source-url", **common)
        )
        entry = fallback.find(f"{ATOM}entry")
        links = entry.findall(f"{ATOM}link")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].attrib["rel"], "alternate")
        self.assertEqual(links[0].attrib["href"], "https://example.com/missing")


if __name__ == "__main__":
    unittest.main()
