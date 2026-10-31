"""La imagen de los posts: lo que propone Claude se recorta, se escapa y se pinta sin scripts ni red."""

import os
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from vla import card
from vla.db import DB
from vla.telegram import multipart
from tests.helpers import CARD, FAKE_PNG


def page(spec, **kw):
    args = {"eyebrow": "Modelo de IA", "source": "huggingface.co/acme/Acme-7B", "brand": "VérticeDev",
            "site": "verticedev.es", "day": date(2026, 10, 31)}
    args.update(kw)
    return card.html_for(spec, **args)


class CardSpecTest(unittest.TestCase):
    def test_keeps_only_the_fields_of_its_layout(self):
        spec, problems = card.clean_spec({**CARD, "body": "sobra", "stat": {"value": "9", "label": "sobra"}})
        self.assertEqual(problems, [])
        self.assertEqual(set(spec), {"layout", "theme", "title", "highlight", "points", "alt"})

    def test_reports_what_does_not_fit(self):
        _, problems = card.clean_spec({"layout": "lista", "title": "Corto", "points": ["uno"]})
        self.assertTrue(any("titular" in p for p in problems))
        self.assertTrue(any("puntos" in p for p in problems))
        _, problems = card.clean_spec({"layout": "dato", "title": "Un titular que vale",
                                       "stat": {"value": "1.234.567.890.123", "label": ""}})
        self.assertEqual(len(problems), 2)
        _, problems = card.clean_spec({"layout": "collage", "title": "Un titular que vale"})
        self.assertIn("collage", problems[0])

    def test_highlight_must_be_part_of_the_title_and_emojis_go_away(self):
        spec, _ = card.clean_spec({**CARD, "title": "🚀 Un **agente** que publica", "highlight": "otra cosa"})
        self.assertEqual(spec["title"], "Un agente que publica")
        self.assertEqual(spec["highlight"], "")

    def test_unknown_theme_is_dark(self):
        spec, _ = card.clean_spec({**CARD, "theme": "neón"})
        self.assertEqual(spec["theme"], "oscuro")


class CardPageTest(unittest.TestCase):
    def test_everything_is_escaped_and_the_page_cannot_run_scripts(self):
        spec, _ = card.clean_spec({
            "layout": "novedad", "theme": "oscuro", "title": "Ataque <script>alert(1)</script> en el titular",
            "highlight": "<script>alert(1)</script>",
            "body": "Texto con <img src=x onerror=alert(1)> y \"comillas\" que no rompen nada del todo.",
        })
        html = page(spec, source="evil.example.com/<b>")
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("default-src 'none'", html)

    def test_highlight_layouts_and_footer(self):
        html = page(card.clean_spec(CARD)[0])
        self.assertIn('<span class="hl">solo si tú quieres</span>', html)
        self.assertIn('<body class="claro">', html)
        self.assertEqual(html.count("<li>"), 3)
        self.assertIn("31 oct 2026", html)
        self.assertIn("huggingface.co/acme/Acme-7B", html)
        dato = page(card.clean_spec({"layout": "dato", "title": "Un router que despega",
                                     "stat": {"value": "4.200", "label": "estrellas en cinco días"}})[0])
        self.assertIn('class="value v2">4.200<', dato)

    def test_footer_does_not_repeat_your_own_site(self):
        html = page(card.clean_spec(CARD)[0], source="verticedev.es")
        self.assertIn('<span class="source"></span><span class="site">verticedev.es</span>', html)

    def test_short_source(self):
        self.assertEqual(card.short_source("https://github.com/acme/tool/releases/tag/v1"), "github.com/acme/tool")
        self.assertEqual(card.short_source("https://www.example.com/news/a?b=1"), "example.com")

    def test_png_size_and_missing_chromium(self):
        self.assertEqual(card.png_size(FAKE_PNG), (1080, 1350))
        self.assertEqual(card.png_size(b"<html>"), (0, 0))
        self.assertIsNone(card.find_chromium("/no/existe/chromium"))


class StorageAndUploadTest(unittest.TestCase):
    def test_old_databases_get_the_card_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.sqlite3"
            old = sqlite3.connect(str(path))
            old.execute("CREATE TABLE posts (id INTEGER PRIMARY KEY, item_id INTEGER, created TEXT, updated TEXT, "
                        "status TEXT, text TEXT, why TEXT, feedback TEXT, message_id INTEGER, linkedin_urn TEXT, "
                        "published_at TEXT, last_error TEXT)")
            old.commit()
            old.close()
            db = DB(path)
            db.init()
            db.init()  # dos veces no falla
            columns = {r["name"] for r in db.query("PRAGMA table_info(posts)")}
            db.conn().close()
        self.assertIn("card", columns)

    def test_multipart_body(self):
        body, ctype = multipart({"chat_id": "42", "caption": "Imagen «lista»"}, "photo", "post.png", FAKE_PNG,
                                "image/png")
        boundary = ctype.split("boundary=", 1)[1]
        self.assertTrue(body.startswith(f"--{boundary}\r\n".encode()))
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))
        self.assertIn('name="caption"\r\n\r\nImagen «lista»\r\n'.encode("utf-8"), body)
        self.assertIn(b'filename="post.png"\r\nContent-Type: image/png\r\n\r\n' + FAKE_PNG + b"\r\n", body)


@unittest.skipUnless(os.environ.get("VLA_TEST_CHROMIUM"), "pon VLA_TEST_CHROMIUM=<ruta> para pintar de verdad")
class RealChromiumTest(unittest.TestCase):
    def test_renders_every_layout(self):
        samples = [
            CARD,
            {"layout": "novedad", "title": "Un modelo que cabe en un portátil", "highlight": "cabe",
             "body": "Corre en local: código asistido sin mandar nada a la nube."},
            {"layout": "dato", "title": "Un router que despega", "stat": {"value": "4.200", "label": "estrellas"}},
            {"layout": "frase", "theme": "claro", "title": "Primero los límites, después la IA", "highlight": "límites"},
        ]
        for raw in samples:
            png = card.render(page(card.clean_spec(raw)[0]), os.environ["VLA_TEST_CHROMIUM"])
            self.assertEqual(card.png_size(png), (1080, 1350))


if __name__ == "__main__":
    unittest.main()
