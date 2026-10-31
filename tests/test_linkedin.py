import unittest

from vla import linkedin
from vla.linkedin import LinkedIn, LinkedInError, escape_little, parse_callback, to_little
from tests.helpers import FakeLinkedIn


class LittleTextTest(unittest.TestCase):
    def test_every_reserved_character_is_escaped(self):
        self.assertEqual(escape_little("|{}@[]()<>#\\*_~"), "\\|\\{\\}\\@\\[\\]\\(\\)\\<\\>\\#\\\\\\*\\_\\~")
        self.assertEqual(escape_little("Hola, ¿qué tal? 100% → sí"), "Hola, ¿qué tal? 100% → sí")

    def test_hashtags_become_templates_and_the_rest_is_plain_text(self):
        text = "Mira esto (de verdad): snake_case @alguien\n\nhttps://github.com/a/b_c#readme\n\n#IA #OpenSource #2026"
        self.assertEqual(
            to_little(text),
            "Mira esto \\(de verdad\\): snake\\_case \\@alguien\n\nhttps://github.com/a/b\\_c\\#readme\n\n"
            "{hashtag|\\#|IA} {hashtag|\\#|OpenSource} \\#2026",
        )

    def test_hashtag_with_underscore_is_escaped_inside_the_template(self):
        self.assertEqual(to_little("#Machine_Learning"), "{hashtag|\\#|Machine\\_Learning}")


class CallbackTest(unittest.TestCase):
    def test_full_url(self):
        self.assertEqual(parse_callback("https://verticedev.es/linkedin/callback?code=AQT1&state=s-1"),
                         ("AQT1", "s-1", None))

    def test_url_inside_a_sentence(self):
        self.assertEqual(parse_callback("aquí va: https://x.es/cb?state=s&code=c2 gracias"), ("c2", "s", None))

    def test_error(self):
        code, _, error = parse_callback("https://x.es/cb?error=user_cancelled_authorize&error_description=Cancelled+by+user&state=s")
        self.assertIsNone(code)
        self.assertEqual(error, "Cancelled by user")

    def test_no_code(self):
        self.assertEqual(parse_callback("hola"), (None, None, None))


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeLinkedIn()
        self.li = LinkedIn("cid", "secret", "https://verticedev.es/linkedin/callback", "202610", request=self.fake)

    def test_auth_url(self):
        url = self.li.auth_url("st4te")
        self.assertTrue(url.startswith(linkedin.AUTH_URL + "?response_type=code&client_id=cid"))
        self.assertIn("redirect_uri=https%3A%2F%2Fverticedev.es%2Flinkedin%2Fcallback", url)
        self.assertIn("state=st4te", url)

    def test_post_headers_and_body(self):
        urn = self.li.create_post("tok", "urn:li:person:p1", "Hola", {"source": "https://a.es", "title": "T",
                                                                       "description": "", "thumbnail": None})
        self.assertEqual(urn, "urn:li:share:777")
        _, _, kw = self.fake.calls[0]
        self.assertEqual(kw["headers"]["Linkedin-Version"], "202610")
        self.assertEqual(kw["headers"]["X-Restli-Protocol-Version"], "2.0.0")
        body = kw["json_body"]
        self.assertEqual(body["lifecycleState"], "PUBLISHED")
        self.assertEqual(body["visibility"], "PUBLIC")
        self.assertEqual(body["content"], {"article": {"source": "https://a.es", "title": "T"}})

    def test_image_upload_returns_the_urn(self):
        self.assertEqual(self.li.upload_image("tok", "urn:li:person:p1", b"png"), "urn:li:image:IMG1")
        init, put = self.fake.calls
        self.assertEqual(init[2]["json_body"], {"initializeUploadRequest": {"owner": "urn:li:person:p1"}})
        self.assertEqual((put[0], put[2]["body"]), ("PUT", b"png"))

    def test_errors_keep_the_status_and_hide_secrets(self):
        self.fake.status = 401
        with self.assertRaises(LinkedInError) as ctx:
            self.li.create_post("tok", "urn:li:person:p1", "Hola")
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("Acceso revocado", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
