"""Recorrido completo con dobles: buscar → borrador → revisar en Telegram → publicar en LinkedIn."""

import json
import unittest
from datetime import timedelta

from vla import linkedin
from vla.linkedin import to_little
from tests.helpers import (
    CHAT, OWN_URL, POST_TEXT, FakeClaude, bot_for, cleanup, click, connect, drain, make_app, say,
)


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.bot = bot_for(self.app)
        self.tg = self.app.tg
        self.li = self.app.li.request
        self.app.collect()

    def tearDown(self):
        cleanup(self.app)

    def test_collect_stores_each_source_without_duplicates_or_own_repos_as_trending(self):
        rows = self.app.db.query("SELECT kind, title FROM items ORDER BY kind, title")
        self.assertEqual(
            [(r["kind"], r["title"]) for r in rows],
            [("ai", "Nuevo modelo Lab-2"), ("model", "acme/Acme-7B"), ("news", "Show HN: An open-source LLM router"),
             ("own", "miguelmartt/demo-agent"), ("repo", "dev/fast-db")],
        )
        self.app.collect()  # otra búsqueda no duplica nada
        self.assertEqual(self.app.db.one("SELECT COUNT(*) AS n FROM items")["n"], 5)

    def test_own_repo_first_then_published_with_card_and_escaped_text(self):
        connect(self.app)
        self.app.draft()
        post = self.app.db.open_post()
        self.assertTrue(post["text"].startswith(POST_TEXT))
        self.assertIn(OWN_URL, post["text"])
        self.assertTrue(post["text"].endswith("#IA #OpenSource #Automatizacion"))
        review = self.tg.messages[-1][0]
        self.assertIn("Repo propio", review)
        self.assertIn("<blockquote>", review)
        # Claude recibió el README en castellano de tu repo.
        self.assertIn("Qué hace, en castellano", self.app.llm.runner.prompts[0])

        click(self.bot, self.tg.button("publish:"))
        self.assertEqual(self.app.db.post(post["id"])["status"], "publishing")
        drain(self.app)

        body = self.li.posts()[0]
        self.assertEqual(body["author"], "urn:li:person:abc123")
        self.assertEqual(body["commentary"], to_little(post["text"]))
        self.assertIn("\\(eso es lo importante\\)", body["commentary"])
        self.assertIn("{hashtag|\\#|OpenSource}", body["commentary"])
        self.assertEqual(body["content"]["article"]["source"], OWN_URL)
        self.assertEqual(body["content"]["article"]["thumbnail"], "urn:li:image:IMG1")
        upload = next(kw for m, url, kw in self.li.calls if m == "PUT")
        self.assertEqual(upload["headers"]["Authorization"], "Bearer " + "AQ" + "t" * 60)
        done = self.app.db.post(post["id"])
        self.assertEqual((done["status"], done["linkedin_urn"]), ("published", "urn:li:share:777"))
        self.assertEqual(self.app.db.item(post["item_id"])["status"], "used")
        self.assertIn("https://www.linkedin.com/feed/update/urn:li:share:777/", self.tg.text())
        self.assertNotIn("⚠️", self.tg.text())

    def test_after_an_own_post_the_week_goes_to_outside_topics(self):
        connect(self.app)
        self.app.draft()
        click(self.bot, self.tg.button("publish:"))
        drain(self.app)
        shortlist = self.app.shortlist()
        self.assertEqual({c["kind"] for c in shortlist}, {"model", "ai", "repo", "news"})

    def test_double_click_on_publish_only_publishes_once(self):
        connect(self.app)
        self.app.draft()
        data = self.tg.button("publish:")
        click(self.bot, data)
        click(self.bot, data)
        drain(self.app)
        self.assertEqual(len(self.li.posts()), 1)

    def test_rejected_card_is_published_as_text_only(self):
        connect(self.app)
        self.li.reject_article = True
        self.app.draft()
        click(self.bot, self.tg.button("publish:"))
        drain(self.app)
        posts = self.li.posts()
        self.assertEqual(len(posts), 2)
        self.assertNotIn("content", posts[1])
        self.assertIn("sin tarjeta", self.tg.text())

    def test_revoked_token_fails_with_retry_and_renew_hint(self):
        connect(self.app)
        self.app.draft()
        self.li.status = 401
        click(self.bot, self.tg.button("publish:"))
        drain(self.app)
        post = self.app.db.open_post()
        self.assertEqual(post["status"], "failed")
        self.assertIn("/linkedin", self.tg.messages[-1][0])
        self.assertTrue(self.tg.button("publish:"))  # «Reintentar»
        self.li.status = 0
        click(self.bot, self.tg.button("publish:"))
        drain(self.app)
        self.assertEqual(self.app.db.post(post["id"])["status"], "published")

    def test_without_linkedin_it_does_not_publish(self):
        self.app.draft()
        click(self.bot, self.tg.button("publish:"))
        drain(self.app)
        self.assertEqual(self.li.posts(), [])
        self.assertIn("LinkedIn no está conectado", self.tg.text())

    def test_changes_are_applied_and_old_publish_button_stops_working(self):
        connect(self.app)
        self.app.llm.runner = FakeClaude(texts=[POST_TEXT, POST_TEXT.replace("Un agente", "Mi agente")])
        self.app.draft()
        old_publish = self.tg.button("publish:")
        click(self.bot, self.tg.button("changes:"))
        say(self.bot, "Más corto y sin pregunta final")
        drain(self.app)
        prompt = json.loads(self.app.llm.runner.prompts[-1])
        self.assertEqual(prompt["comentarios"], ["Más corto y sin pregunta final"])
        self.assertIn("borrador_anterior", prompt)
        post = self.app.db.open_post()
        self.assertTrue(post["text"].startswith("Mi agente"))

        click(self.bot, old_publish)  # el botón del primer borrador ya no publica
        drain(self.app)
        self.assertEqual(self.li.posts(), [])
        click(self.bot, self.tg.button("publish:"))
        drain(self.app)
        self.assertTrue(self.li.posts()[0]["commentary"].startswith("Mi agente"))

    def test_a_long_reply_replaces_the_text_as_is(self):
        self.app.draft()
        calls = len(self.app.llm.runner.prompts)
        click(self.bot, self.tg.button("changes:"))
        mine = "Lo escribo yo. " * 30 + "\n\nhttps://github.com/miguelmartt/demo-agent"
        say(self.bot, mine)
        drain(self.app)
        self.assertEqual(self.app.db.open_post()["text"], mine.strip())
        self.assertEqual(len(self.app.llm.runner.prompts), calls)  # sin llamar a Claude
        self.assertIn("tal cual", self.tg.text())

    def test_other_topic_skips_the_item_and_drafts_from_outside(self):
        self.app.draft()
        first = self.app.db.open_post()
        click(self.bot, self.tg.button("other:"))
        drain(self.app)
        second = self.app.db.open_post()
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.app.db.post(first["id"])["status"], "discarded")
        self.assertEqual(self.app.db.item(first["item_id"])["status"], "skipped")
        self.assertNotEqual(self.app.db.item(second["item_id"])["kind"], "own")

    def test_discard(self):
        self.app.draft()
        post = self.app.db.open_post()
        click(self.bot, self.tg.button("discard:"))
        self.assertEqual(self.app.db.post(post["id"])["status"], "discarded")
        self.assertIsNone(self.app.db.open_post())

    def test_manual_draft_with_one_open_shows_it_instead(self):
        self.app.draft()
        calls = len(self.app.llm.runner.prompts)
        say(self.bot, "/borrador")
        drain(self.app)
        self.assertEqual(len(self.app.llm.runner.prompts), calls)
        self.assertIn("borrador sin decidir", self.tg.text())

    def test_scheduled_draft_replaces_a_stale_one(self):
        self.app.draft()
        stale = self.app.db.open_post()
        self.app.draft()
        self.assertEqual(self.app.db.post(stale["id"])["status"], "expired")
        self.assertNotEqual(self.app.db.open_post()["id"], stale["id"])

    def test_repos_command_drafts_about_the_chosen_repo(self):
        say(self.bot, "/repos")
        click(self.bot, self.tg.button("own:"))
        drain(self.app)
        self.assertIn(OWN_URL, self.app.db.open_post()["text"])

    def test_invalid_draft_gets_a_second_chance(self):
        self.app.llm.runner = FakeClaude(texts=["Demasiado corto.", POST_TEXT])
        self.app.draft()
        self.assertEqual(len(self.app.llm.runner.prompts), 2)
        self.assertIn("Tu respuesta anterior no valía", self.app.llm.runner.prompts[1])
        self.assertIsNotNone(self.app.db.open_post())

    def test_strangers_cannot_use_the_bot(self):
        connect(self.app)
        self.app.draft()
        click(self.bot, self.tg.button("publish:"), user="666")
        say(self.bot, "/borrador", user="666")
        say(self.bot, "/linkedin", user=CHAT, chat="-100")  # tu usuario, pero en un grupo
        drain(self.app)
        self.assertEqual(self.app.db.open_post()["status"], "review")
        self.assertEqual(self.li.posts(), [])
        self.assertIsNone(self.app.db.get_meta("li_auth"))

    def test_dry_run_does_not_call_linkedin(self):
        cleanup(self.app)
        self.app = make_app(VLA_DRY_RUN="1")
        self.bot, self.tg, self.li = bot_for(self.app), self.app.tg, self.app.li.request
        self.app.collect()
        self.app.draft()
        click(self.bot, self.tg.button("publish:"))
        drain(self.app)
        self.assertEqual(self.li.calls, [])
        self.assertIn("simulado", self.tg.text())


class LinkedInAuthTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.bot = bot_for(self.app)
        self.tg = self.app.tg
        self.li = self.app.li.request

    def tearDown(self):
        cleanup(self.app)

    def auth_link(self) -> str:
        say(self.bot, "/linkedin")
        message = self.tg.messages[-1][0]
        url = message.split('href="', 1)[1].split('"', 1)[0].replace("&amp;", "&")
        self.assertTrue(url.startswith(linkedin.AUTH_URL))
        self.assertIn("scope=openid%20profile%20w_member_social", url)
        return url

    def test_connect_with_the_pasted_return_url(self):
        url = self.auth_link()
        state = url.split("state=", 1)[1].split("&", 1)[0]
        say(self.bot, f"https://verticedev.es/linkedin/callback?code=THE-CODE&state={state}", message_id=77)
        drain(self.app)
        self.assertEqual(self.tg.deleted, [77])  # el código no se queda en el chat
        method, token_url, kw = self.li.calls[0]
        self.assertEqual((method, token_url), ("POST", linkedin.TOKEN_URL))
        self.assertEqual(kw["data"]["code"], "THE-CODE")
        self.assertEqual(kw["data"]["client_secret"], "client-secret-value")
        self.assertNotIn("client-secret", token_url)
        self.assertEqual(self.app.linkedin_session()[1], "urn:li:person:abc123")
        self.assertIn("Conectado a LinkedIn como <b>Miguel</b>", self.tg.text())
        self.assertIsNone(self.app.db.get_meta("li_auth"))  # de un solo uso

    def test_wrong_or_reused_state_is_rejected(self):
        self.auth_link()
        say(self.bot, "https://verticedev.es/linkedin/callback?code=X&state=forged")
        drain(self.app)
        self.assertEqual(self.li.calls, [])
        self.assertIn("no es del último enlace", self.tg.text())

    def test_expired_link_is_rejected(self):
        url = self.auth_link()
        state = url.split("state=", 1)[1].split("&", 1)[0]
        pending = self.app.db.get_meta("li_auth")
        pending["at"] = (self.app.now() - timedelta(hours=1)).isoformat()
        self.app.db.set_meta("li_auth", pending)
        say(self.bot, f"https://verticedev.es/linkedin/callback?code=X&state={state}")
        drain(self.app)
        self.assertEqual(self.li.calls, [])
        self.assertIn("caducado", self.tg.text())

    def test_user_cancelled_on_linkedin(self):
        self.auth_link()
        say(self.bot, "https://verticedev.es/linkedin/callback?error=user_cancelled_login&error_description=The+user+cancelled")
        drain(self.app)
        self.assertIn("no ha dado el permiso", self.tg.text())

    def test_token_reminders_once_each(self):
        connect(self.app, days=5)
        self.app.daily()
        self.app.daily()
        self.assertEqual(self.tg.text().count("caduca el"), 1)
        connect(self.app, days=1)
        self.app.db.set_meta("li_warned", ["7"])
        self.app.daily()
        self.assertEqual(self.tg.text().count("caduca el"), 2)
        connect(self.app, days=-1)
        self.app.daily()
        self.app.daily()
        self.assertEqual(self.tg.text().count("ha caducado"), 1)

    def test_old_api_version_is_reported_once_a_month(self):
        cleanup(self.app)
        self.app = make_app(VLA_LINKEDIN_VERSION="202501")
        self.app.daily()
        self.app.daily()
        self.assertEqual(self.app.tg.text().count("VLA_LINKEDIN_VERSION"), 1)


if __name__ == "__main__":
    unittest.main()
