"""Secretos fuera de logs y subprocesos, configuración leída como datos."""

import os
import tempfile
import unittest
from unittest import mock

from vla import http
from vla.config import load_env_file
from vla.llm import claude_env
from vla.telegram import TelegramError
from tests.helpers import cleanup, make_app


class HardeningTest(unittest.TestCase):
    def test_redact_tokens_and_oauth_codes(self):
        text = ("https://api.telegram.org/bot123456:ABC-def/getUpdates Bearer AQXyz "
                "AQ" + "a" * 50 + " ?code=AQabc&state=s1 client_secret=shh ghp_abcdef sk-ant-api03-xyz")
        clean = http.redact(text)
        for secret in ("123456:ABC", "AQXyz", "a" * 50, "AQabc", "s1", "shh", "ghp_abcdef", "sk-ant-api03"):
            self.assertNotIn(secret, clean)

    def test_claude_gets_its_token_and_nothing_else(self):
        env = {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "c", "VLA_TELEGRAM_TOKEN": "t",
               "VLA_LINKEDIN_CLIENT_SECRET": "s", "ANTHROPIC_API_KEY": "k", "VLA_GITHUB_TOKEN": "g"}
        with mock.patch.dict(os.environ, env, clear=True):
            child = claude_env()
        self.assertEqual(set(child), {"PATH", "CLAUDE_CODE_OAUTH_TOKEN"})

    def test_env_file_is_data_not_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "env")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# comentario\nVLA_ABOUT=Dev $(rm -r /) `id`\nVLA_OWNER=\"Miguel Martínez\"\nBAD-KEY=1\n")
            target = {}
            load_env_file(path, target)
        self.assertEqual(target, {"VLA_ABOUT": "Dev $(rm -r /) `id`", "VLA_OWNER": "Miguel Martínez"})

    def test_service_starts_before_you_open_the_chat_with_the_bot(self):
        app = make_app()
        try:
            app.tg.send = mock.Mock(side_effect=TelegramError("Bad Request: chat not found"))
            app.stop.set()
            with self.assertLogs("vla", level="WARNING") as logs:
                self.assertTrue(app.run_forever())
            self.assertIn("pulsa Iniciar", "\n".join(logs.output))
        finally:
            cleanup(app)


if __name__ == "__main__":
    unittest.main()
