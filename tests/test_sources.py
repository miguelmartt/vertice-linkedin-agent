import unittest
from datetime import datetime, timedelta, timezone

from vla import http, sources
from vla.http import Response
from tests.helpers import fake_fetch, fake_get_json, fake_request, rss

NOW = datetime.now(timezone.utc)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


class SourcesTest(unittest.TestCase):
    def test_huggingface_only_recent_and_liked_models(self):
        items = sources.huggingface(NOW, min_likes=100, get=fake_get_json)
        self.assertEqual([i["title"] for i in items], ["acme/Acme-7B"])
        self.assertEqual(items[0]["metrics"]["license"], "apache-2.0")
        self.assertEqual(items[0]["url"], "https://huggingface.co/acme/Acme-7B")

    def test_github_trending_skips_your_repos_and_repos_without_description(self):
        def get(method, url, **kw):
            return {"items": [
                {"full_name": "a/tool", "owner": {"login": "a"}, "html_url": "https://github.com/a/tool",
                 "description": "CLI for LLM agents", "stargazers_count": 800, "created_at": iso(2)},
                {"full_name": "b/game", "owner": {"login": "b"}, "html_url": "https://github.com/b/game",
                 "description": "A cozy farming game", "stargazers_count": 800, "created_at": iso(2)},
                {"full_name": "c/empty", "owner": {"login": "c"}, "description": "", "stargazers_count": 5000,
                 "created_at": iso(1)},
                {"full_name": "Me/mine", "owner": {"login": "Me"}, "description": "x", "stargazers_count": 900,
                 "created_at": iso(1)},
            ]}
        items = sources.github_trending(NOW, 100, exclude_owners=["me"], get=get)
        self.assertEqual([i["title"] for i in items], ["a/tool", "b/game"])
        tool, game = items
        self.assertEqual(tool["metrics"]["stars_per_day"], 400)
        self.assertAlmostEqual(tool["score"], game["score"] * sources.TECH_BOOST, places=2)

    def test_hackernews_needs_a_link_and_weighs_off_topic_less(self):
        def get(method, url, **kw):
            return {"hits": [
                {"objectID": "1", "title": "Python 4 released", "url": "https://python.example/4", "points": 700},
                {"objectID": "2", "title": "A famous actor has died", "url": "https://news.example/x", "points": 700},
                {"objectID": "3", "title": "Ask HN: what?", "url": None, "points": 900},
            ]}
        items = sources.hackernews(NOW, 250, get=get)
        self.assertEqual([i["score"] for i in items], [1.5, 0.4])
        self.assertEqual(items[0]["metrics"]["hn_url"], "https://news.ycombinator.com/item?id=1")

    def test_rss_feed_recent_entries_with_text_summary_and_image(self):
        items = sources.feed("https://feeds.example.com/ai.xml", NOW, fetch=fake_fetch)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item["title"], item["source"], item["kind"]), ("Nuevo modelo Lab-2", "Lab News", "ai"))
        self.assertEqual(item["summary"], "Un modelo más rápido .")
        self.assertEqual(item["image"], "https://img.example.com/lab2.png")

    def test_atom_feed(self):
        atom = f"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Lab</title>
        <entry><title>Entrada</title><link rel="alternate" href="https://lab.example.com/e?utm_source=rss"/>
        <updated>{iso(1)}</updated><summary>Resumen</summary></entry></feed>""".encode()
        items = sources.feed("https://lab.example.com/atom", NOW,
                             fetch=lambda url, **kw: Response(200, {}, atom))
        self.assertEqual([(i["title"], i["url"]) for i in items], [("Entrada", "https://lab.example.com/e")])

    def test_own_repos(self):
        def get(method, url, params=None, **kw):
            if url.endswith("/users/me/repos"):
                return [
                    {"name": "tool", "full_name": "me/tool", "html_url": "https://github.com/me/tool",
                     "description": "Mi herramienta", "created_at": iso(100), "pushed_at": iso(1)},
                    {"name": "quiet", "full_name": "me/quiet", "html_url": "https://github.com/me/quiet",
                     "description": "Sin cambios", "created_at": iso(300), "pushed_at": iso(200)},
                    {"name": "me", "full_name": "me/me"},
                    {"name": "fork", "full_name": "me/fork", "fork": True},
                    {"name": "old", "full_name": "me/old", "archived": True},
                    {"name": "hidden", "full_name": "me/hidden"},
                ]
            if url.endswith("/repos/me/tool/releases"):
                return [
                    {"tag_name": "v2.0", "name": "v2.0", "html_url": "https://github.com/me/tool/releases/tag/v2.0",
                     "published_at": iso(2), "body": "Novedades"},
                    {"tag_name": "v2.1-rc", "prerelease": True, "published_at": iso(1)},
                    {"tag_name": "v1.0", "published_at": iso(90)},
                ]
            raise AssertionError(url)  # me/quiet no se consulta: no ha cambiado
        items = sources.own_repos(["me"], exclude=["hidden"], now=NOW, get=get)
        self.assertEqual([i["key"] for i in items], ["own:me/tool", "own:me/tool@v2.0", "own:me/quiet"])
        self.assertEqual(items[1]["score"], 1.0)

    def test_page_meta_prefers_the_article_and_ignores_scripts(self):
        html = ('<html><head><title>T</title><meta property="og:image" content="/img/card.png">'
                '<meta name="description" content="Desc"></head><body><nav>Menú</nav>'
                '<article><h1>Título</h1><p>Cuerpo &amp; más.</p><script>alert(1)</script></article></body></html>')
        meta = sources.page_meta(html, "https://site.example.com/post/1")
        self.assertEqual(meta["image"], "https://site.example.com/img/card.png")
        self.assertEqual(meta["description"], "Desc")
        self.assertEqual(meta["text"], "Título Cuerpo & más.")

    def test_details_of_your_repo_use_the_spanish_readme(self):
        item = {"kind": "own", "key": "own:miguelmartt/demo-agent", "url": "https://github.com/miguelmartt/demo-agent"}
        out = sources.details(item, fetch=fake_fetch, request=fake_request)
        self.assertIn("en castellano", out["text"])
        self.assertEqual(out["image"], "https://img.example.com/card.png")

    def test_clean_url(self):
        self.assertEqual(sources.clean_url("HTTPS://Example.com/a/?utm_source=x&id=2#top"), "https://example.com/a?id=2")


class PublicUrlTest(unittest.TestCase):
    def test_internal_addresses_are_not_fetched(self):
        for url in ("http://127.0.0.1/", "http://localhost:8080/", "http://10.0.0.5/", "http://169.254.169.254/latest",
                    "http://[::1]/", "file:///etc/passwd", "ftp://example.com/"):
            self.assertFalse(http.is_public_url(url), url)
            with self.assertRaises(http.HttpError):
                http.fetch_public(url)

    def test_public_ip_is_allowed(self):
        self.assertTrue(http.is_public_url("https://1.1.1.1/"))


class FeedParsingTest(unittest.TestCase):
    def test_parse_feed_returns_channel_title(self):
        title, entries = sources.parse_feed(rss())
        self.assertEqual(title, "Lab News")
        self.assertEqual(len(entries), 2)


if __name__ == "__main__":
    unittest.main()
