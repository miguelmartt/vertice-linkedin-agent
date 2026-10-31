"""Dobles de Telegram, Claude Code, LinkedIn y las fuentes, para probar sin red."""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from vla import linkedin, sources
from vla.app import App
from vla.bot import Bot
from vla.config import Config
from vla.http import HttpError, Response

CHAT = "42"
FEED_URL = "https://feeds.example.com/ai.xml"
OWN_URL = "https://github.com/miguelmartt/demo-agent"

POST_TEXT = (
    "Un agente que redacta tus posts y no publica nada sin tu visto bueno (eso es lo importante).\n\n"
    "Busca novedades en Hugging Face, GitHub y Hacker News, elige una y escribe el borrador con Claude. "
    "Te lo manda a Telegram y solo lo publica si pulsas el botón.\n\n"
    "→ Sin dependencias: librería estándar de Python.\n"
    "→ Sin puertos abiertos: long polling.\n\n"
    "¿Lo usarías para tu empresa o prefieres escribir cada post a mano?"
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def rss() -> bytes:
    fresh = format_datetime(now_utc() - timedelta(hours=5))
    old = format_datetime(now_utc() - timedelta(days=20))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Lab News</title>
<item><title>Nuevo modelo Lab-2</title><link>https://lab.example.com/lab-2</link><pubDate>{fresh}</pubDate>
<description>&lt;p&gt;Un modelo &lt;b&gt;más rápido&lt;/b&gt;.&lt;/p&gt;</description>
<media:content url="https://img.example.com/lab2.png" medium="image"/></item>
<item><title>Algo antiguo</title><link>https://lab.example.com/old</link><pubDate>{old}</pubDate></item>
</channel></rss>""".encode("utf-8")


PAGE = (b'<html><head><title>Demo</title><meta property="og:image" content="https://img.example.com/card.png">'
        b'<meta property="og:description" content="La descripcion de la pagina"></head>'
        b'<body><nav>Menu</nav><article><p>Texto del articulo.</p></article><script>var x=1;</script></body></html>')


def fake_get_json(method: str, url: str, params: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
    created = (now_utc() - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    if url == sources.HF_API:
        return [
            {"id": "acme/Acme-7B", "createdAt": created, "likes": 500, "downloads": 1234,
             "trendingScore": 400, "pipeline_tag": "text-generation", "tags": ["license:apache-2.0"]},
            {"id": "old/Old-Model", "createdAt": "2025-01-01T00:00:00Z", "likes": 9000, "trendingScore": 900},
        ]
    if url.endswith("/search/repositories"):
        return {"items": [
            {"full_name": "dev/fast-db", "owner": {"login": "dev"}, "html_url": "https://github.com/dev/fast-db",
             "description": "A fast embedded database in Rust", "stargazers_count": 1200, "created_at": created,
             "language": "Rust", "topics": ["database"]},
            {"full_name": "miguelmartt/demo-agent", "owner": {"login": "miguelmartt"},
             "html_url": OWN_URL, "description": "mío", "stargazers_count": 500, "created_at": created},
        ]}
    if url == sources.HN_API:
        return {"hits": [
            {"objectID": "1", "title": "Show HN: An open-source LLM router", "url": "https://router.example.com/",
             "points": 420, "num_comments": 90, "created_at": created},
            {"objectID": "2", "title": "Ask HN: anything", "url": None, "points": 900},
        ]}
    if "/users/miguelmartt/repos" in url:
        return [
            {"name": "demo-agent", "full_name": "miguelmartt/demo-agent", "html_url": OWN_URL,
             "description": "Agente de demostración", "stargazers_count": 3, "language": "Python",
             "created_at": created, "pushed_at": created},
            {"name": "miguelmartt", "full_name": "miguelmartt/miguelmartt", "html_url": "https://github.com/miguelmartt/miguelmartt"},
            {"name": "a-fork", "full_name": "miguelmartt/a-fork", "fork": True},
        ]
    if url.endswith("/releases"):
        return []
    raise AssertionError(f"URL inesperada: {url}")


def fake_fetch(url: str, max_bytes: int = 0, accept: str = "") -> Response:
    if url.startswith("https://img.example.com/"):
        return Response(200, {"content-type": "image/png"}, b"\x89PNG\r\n\x1a\nfake")
    if url == FEED_URL:
        return Response(200, {"content-type": "application/rss+xml"}, rss())
    return Response(200, {"content-type": "text/html; charset=utf-8"}, PAGE)


def fake_request(method: str, url: str, **kwargs: Any) -> Response:
    if url.endswith("/contents/README.es.md"):
        return Response(200, {"content-type": "text/plain"}, "# demo-agent\n\nQué hace, en castellano.".encode("utf-8"))
    return Response(200, {"content-type": "text/plain"}, b"# README\n\nWhat it does.")


class FakeTG:
    def __init__(self) -> None:
        self.messages: List[tuple] = []
        self.deleted: List[int] = []
        self.edited: List[Optional[int]] = []

    def send(self, text, buttons=None):
        self.messages.append((text, buttons))
        return len(self.messages)

    def edit_buttons(self, message_id, buttons=None):
        self.edited.append(message_id)

    def answer(self, *args, **kwargs):
        pass

    def delete(self, message_id):
        self.deleted.append(message_id)
        return True

    def set_commands(self, *args, **kwargs):
        pass

    def text(self) -> str:
        return "\n".join(m for m, _ in self.messages)

    def last_buttons(self):
        for _, buttons in reversed(self.messages):
            if buttons:
                return buttons
        return None

    def button(self, prefix: str) -> str:
        for row in self.last_buttons() or []:
            for _, data in row:
                if data.startswith(prefix):
                    return data
        raise AssertionError(f"no hay botón {prefix}")


class FakeClaude:
    """Elige el primer candidato y devuelve los textos que le pongas (o POST_TEXT)."""

    def __init__(self, texts: Optional[List[str]] = None, pick: Optional[str] = None) -> None:
        self.prompts: List[str] = []
        self.texts = list(texts or [])
        self.pick = pick

    def __call__(self, cmd, input, **kwargs):  # noqa: A002 - firma de subprocess.run
        self.prompts.append(input)
        payload = json.loads(input.split("\n\nTu respuesta anterior")[0])
        cands = payload["candidatos"]
        chosen = next((c for c in cands if self.pick and self.pick in c["titulo"]), cands[0])
        out = {"item_id": chosen["id"], "text": self.texts.pop(0) if self.texts else POST_TEXT,
               "hashtags": ["IA", "#Open Source", "Automatización"], "why": "Es útil para empresas"}
        stdout = json.dumps({"subtype": "success", "is_error": False, "structured_output": out, "total_cost_usd": 0})
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


class FakeLinkedIn:
    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.reject_article = False
        self.status = 0

    def __call__(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append((method, url, kwargs))
        if self.status:
            raise HttpError(self.status, '{"message": "Acceso revocado"}', url)
        if url == linkedin.TOKEN_URL:
            return Response(200, {}, json.dumps({"access_token": "AQ" + "x" * 60, "expires_in": 5184000,
                                                 "scope": "openid,profile,w_member_social"}).encode())
        if url == linkedin.USERINFO_URL:
            return Response(200, {}, b'{"sub": "abc123", "name": "Miguel"}')
        if url == linkedin.IMAGES_URL:
            return Response(200, {}, json.dumps({"value": {"uploadUrl": "https://www.linkedin.com/dms-uploads/up1",
                                                           "image": "urn:li:image:IMG1"}}).encode())
        if url.startswith("https://www.linkedin.com/dms-uploads/"):
            return Response(201, {}, b"")
        if url == linkedin.POSTS_URL:
            if self.reject_article and "content" in kwargs["json_body"]:
                raise HttpError(400, '{"message": "Invalid thumbnail"}', url)
            return Response(201, {"x-restli-id": "urn:li:share:777"}, b"")
        raise AssertionError(f"URL inesperada: {url}")

    def posts(self) -> List[Dict[str, Any]]:
        return [kw["json_body"] for m, url, kw in self.calls if url == linkedin.POSTS_URL]


def make_app(owners: str = "miguelmartt", **overrides: str) -> App:
    state = tempfile.mkdtemp(prefix="vla-test-")
    env = {
        "VLA_STATE_DIR": state,
        "VLA_TELEGRAM_TOKEN": "",
        "VLA_TELEGRAM_CHAT_ID": CHAT,
        "VLA_LINKEDIN_CLIENT_ID": "client-id",
        "VLA_LINKEDIN_CLIENT_SECRET": "client-secret-value",
        "VLA_LINKEDIN_REDIRECT_URI": "https://verticedev.es/linkedin/callback",
        "VLA_OWNER": "Miguel",
        "VLA_ABOUT": "Desarrollador",
        "VLA_GITHUB_OWNERS": owners,
        "VLA_FEEDS": FEED_URL,
        **overrides,
    }
    app = App(Config.from_env(env))
    app.tg = FakeTG()  # type: ignore[assignment]
    app.llm.runner = FakeClaude()
    app.li.request = FakeLinkedIn()
    app.get_json = fake_get_json
    app.fetch = fake_fetch
    app.request = fake_request
    app._state = state  # type: ignore[attr-defined]
    return app


def cleanup(app: App) -> None:
    app.db.conn().close()
    shutil.rmtree(app._state, ignore_errors=True)  # type: ignore[attr-defined]


def connect(app: App, days: int = 60) -> None:
    app.db.set_meta("li_token", {"access_token": "AQ" + "t" * 60,
                                 "expires_at": (app.now() + timedelta(days=days)).isoformat()})
    app.db.set_meta("li_profile", {"sub": "abc123", "name": "Miguel"})


def bot_for(app: App) -> Bot:
    return Bot(app)


def click(bot: Bot, data: str, user: str = CHAT, message_id: int = 1) -> None:
    bot.handle({"update_id": 1, "callback_query": {
        "id": "cb", "data": data, "from": {"id": int(user)},
        "message": {"message_id": message_id, "chat": {"id": int(user), "type": "private"}}}})


def say(bot: Bot, text: str, user: str = CHAT, chat: Optional[str] = None, message_id: int = 9) -> None:
    bot.handle({"update_id": 2, "message": {
        "message_id": message_id, "text": text, "from": {"id": int(user)},
        "chat": {"id": int(chat or user), "type": "private"}}})


def drain(app: App) -> None:
    while not app.tasks.empty():
        name, fn, args = app.tasks.get()
        app.run_task(name, fn, *args)
