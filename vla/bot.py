"""Conversación con el dueño por Telegram: comandos, botones y respuestas de texto.

Solo atiende a VLA_TELEGRAM_CHAT_ID en su chat privado con el bot: el chat y el remitente
tienen que ser ese id (en un chat privado coinciden). Así, aunque el bot acabe en un grupo,
nadie más puede publicar en tu LinkedIn. El resto se ignora.
Lo rápido se responde aquí; lo que llama a Claude, a LinkedIn o a las fuentes va a la cola
del trabajador para no bloquear el bot.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from typing import TYPE_CHECKING, Any, Dict, Optional

from .linkedin import parse_callback
from .sources import KIND_LABEL
from .telegram import esc
from .util import fingerprint, shorten

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("vla.bot")

HELP = """🤖 <b>Agente de LinkedIn</b>

Busco novedades (modelos de IA, repos de GitHub que despegan, noticias tech y tus repos open source), redacto un post con Claude y te lo mando aquí. En tu LinkedIn solo se publica lo que apruebas con «Publicar».

/borrador · preparar un post ahora
/temas · lo que tengo para los próximos posts
/repos · escribir sobre uno de tus repos
/recoger · buscar novedades ahora
/linkedin · conectar o renovar el permiso de LinkedIn
/estado · cómo va todo
/cancelar · olvidar la respuesta que estoy esperando
/ayuda · este mensaje"""

COMMANDS = [
    ("borrador", "Preparar un post ahora"),
    ("temas", "Temas para los próximos posts"),
    ("repos", "Escribir sobre uno de tus repos"),
    ("recoger", "Buscar novedades ahora"),
    ("linkedin", "Conectar o renovar LinkedIn"),
    ("estado", "Cómo va todo"),
    ("ayuda", "Qué puedo hacer"),
]

ASK_CHANGES = ("✏️ ¿Qué cambio? Escríbemelo y lo rehago.\n"
               "Si lo prefieres, pégame el texto entero ya corregido y lo publico tal cual.")
# Una respuesta así de larga es el post reescrito, no una indicación.
REPLACE_MIN_CHARS = 300
REPLACE_MIN_RATIO = 0.6
# Lo que espera cada botón: con otro estado, el botón es antiguo y no hace nada.
EXPECTED = {
    "publish": ("review", "failed"),
    "changes": ("review", "failed"),
    "other": ("review", "failed"),
    "discard": ("review", "failed"),
}


class Bot:
    def __init__(self, app: "App"):
        self.app = app
        self.db = app.db
        self.tg = app.tg
        self.chat_id = str(app.cfg.telegram_chat_id)

    # --- bucle --------------------------------------------------------------

    def poll_forever(self) -> None:
        if not self.app.cfg.telegram_token:
            log.warning("Sin VLA_TELEGRAM_TOKEN: el bot no escucha")
            return
        self.tg.set_commands(COMMANDS)
        offset = self.db.get_meta("tg_offset")
        backoff = 5
        while not self.app.stop.is_set():
            try:
                updates = self.tg.updates(offset)
                backoff = 5
            except Exception as exc:  # TelegramError, JSON roto…: el hilo del bot no puede morir
                log.warning("Telegram: %s", exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                self.db.set_meta("tg_offset", offset)  # antes de actuar: un fallo a medias no se repite
                try:
                    self.handle(upd)
                except Exception:
                    log.exception("Error atendiendo a Telegram")

    # --- despacho -----------------------------------------------------------

    def authorized(self, chat: Dict[str, Any], sender: Dict[str, Any]) -> bool:
        return str(chat.get("id", "")) == self.chat_id and str(sender.get("id", "")) == self.chat_id

    def handle(self, upd: Dict[str, Any]) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            if not self.authorized(cq.get("message", {}).get("chat", {}), cq.get("from", {})):
                log.warning("Botón no autorizado: chat %s, usuario %s",
                            cq.get("message", {}).get("chat", {}).get("id"), cq.get("from", {}).get("id"))
                return
            self.on_button(cq)
            return
        msg = upd.get("message") or {}
        if not self.authorized(msg.get("chat", {}), msg.get("from", {})):
            log.warning("Mensaje no autorizado: chat %s, usuario %s",
                        msg.get("chat", {}).get("id"), msg.get("from", {}).get("id"))
            return
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if text.startswith("/"):
            self.on_command(text.split()[0].split("@")[0].lower())
        else:
            self.on_text(text, msg.get("message_id"))

    def on_command(self, command: str) -> None:
        app = self.app
        if command in ("/start", "/ayuda", "/help"):
            self.tg.send(HELP)
        elif command == "/estado":
            self.tg.send(app.status_text())
        elif command == "/temas":
            self.tg.send(app.topics_text())
        elif command == "/repos":
            self.show_repos()
        elif command == "/borrador":
            self.tg.send("✍️ Preparo un borrador. Tardo un par de minutos.")
            app.enqueue("borrador", app.draft, True)
        elif command == "/recoger":
            self.tg.send("🔎 Busco novedades.")
            app.enqueue("recoger", self.collect_and_report)
        elif command == "/linkedin":
            self.connect_linkedin()
        elif command in ("/cancelar", "/saltar"):
            self.db.set_meta("awaiting", None)
            self.tg.send("Hecho.")
        else:
            self.tg.send("No conozco ese comando. /ayuda")

    def collect_and_report(self) -> None:
        new = self.app.collect()
        if not new:
            self.tg.send("No hay temas nuevos desde la última búsqueda. /temas para ver lo que tengo.")
            return
        detail = " · ".join(f"{n} {KIND_LABEL.get(k, k).lower()}" for k, n in sorted(new.items()))
        self.tg.send(f"📥 {sum(new.values())} temas nuevos: {esc(detail)}. /temas para verlos.")

    def connect_linkedin(self) -> None:
        try:
            url = self.app.start_auth()
        except RuntimeError as exc:
            self.tg.send(f"❌ {esc(exc)}")
            return
        host = urllib.parse.urlsplit(self.app.cfg.linkedin_redirect_uri).netloc
        self.tg.send(
            "🔐 <b>Conectar LinkedIn</b>\n"
            f"1. Abre <a href=\"{esc(url)}\">este enlace</a> y acepta.\n"
            f"2. LinkedIn te llevará a una página de {esc(host)}. Puede salir un error 404: es normal.\n"
            "3. Copia la dirección completa de esa página y pégamela aquí.\n"
            "El enlace vale 30 minutos y solo sirve una vez."
        )

    def show_repos(self) -> None:
        repos = self.db.own_items()
        if not repos:
            self.tg.send("No tengo repos tuyos. Revisa VLA_GITHUB_OWNERS y busca con /recoger.")
            return
        lines = ["📦 <b>Tus repos</b> · elige uno y redacto el post"]
        buttons = []
        for item in repos[:10]:
            mark = " (ya publicado)" if item["status"] == "used" else ""
            lines.append(f"• <a href=\"{esc(item['url'])}\">{esc(item['title'])}</a>{mark}")
            buttons.append([(f"✍️ {shorten(item['title'].split('/')[-1], 40)}", f"own:{item['id']}")])
        self.tg.send("\n".join(lines), buttons)

    # --- botones ------------------------------------------------------------

    def on_button(self, cq: Dict[str, Any]) -> None:
        action, _, rest = (cq.get("data") or "").partition(":")
        raw_id, _, version = rest.partition(":")
        message_id = cq.get("message", {}).get("message_id")
        try:
            target = int(raw_id)
        except ValueError:
            self.tg.answer(cq["id"], "Botón no válido")
            return
        app = self.app
        if action == "own":
            self.tg.answer(cq["id"])
            self.tg.send("✍️ Preparo el post sobre ese repo.")
            app.enqueue("borrador de repo", app.draft, True, True, target)
            return
        if action not in EXPECTED:
            self.tg.answer(cq["id"], "Acción desconocida")
            return
        post = self.db.post(target)
        if not post:
            self.tg.answer(cq["id"], "Ese borrador ya no existe")
            return
        if post["status"] not in EXPECTED[action]:
            self.tg.answer(cq["id"], f"Ese borrador ya está {STATUS_LABEL.get(post['status'], post['status'])}")
            self.tg.edit_buttons(message_id)
            return
        if action == "publish" and version != fingerprint(post["text"]):
            self.tg.answer(cq["id"], "Ese botón es de una versión anterior: usa el del último borrador")
            self.tg.edit_buttons(message_id)
            return
        self.tg.answer(cq["id"])
        self.tg.edit_buttons(message_id)

        if action == "publish":
            # El estado cambia aquí, antes de encolar: un segundo clic ya no publica dos veces.
            self.db.update_post(target, app.now(), status="publishing")
            self.tg.send("🚀 Publicando en LinkedIn…")
            app.enqueue(f"publicar #{target}", app.publish, target)
        elif action == "changes":
            self.db.set_meta("awaiting", {"mode": "changes", "id": target})
            self.tg.send(ASK_CHANGES)
        elif action == "other":
            self.tg.send("🔀 Busco otro tema.")
            app.enqueue(f"otro tema #{target}", app.other_topic, target)
        elif action == "discard":
            app.discard(target)

    # --- texto libre --------------------------------------------------------

    def on_text(self, text: str, message_id: Optional[int]) -> None:
        app = self.app
        code, _, error = parse_callback(text)
        if (code or error) and self.db.get_meta("li_auth"):
            # El código de LinkedIn no se queda en el chat.
            self.tg.delete(message_id)
            self.tg.send("🔐 Conectando con LinkedIn…")
            app.enqueue("conectar LinkedIn", app.finish_auth, text)
            return
        awaiting: Optional[Dict[str, Any]] = self.db.get_meta("awaiting")
        if not awaiting:
            self.tg.send("No estoy esperando ninguna respuesta. /ayuda")
            return
        self.db.set_meta("awaiting", None)
        post = self.db.post(int(awaiting["id"]))
        if not post or post["status"] not in EXPECTED["changes"]:
            self.tg.send("Ese borrador ya no está esperando cambios.")
            return
        if len(text) >= REPLACE_MIN_CHARS and len(text) >= REPLACE_MIN_RATIO * len(post["text"]):
            app.replace_text(post["id"], text)
        else:
            self.tg.send("✏️ Rehago el borrador con tus cambios.")
            app.enqueue(f"rehacer #{post['id']}", app.redo, post["id"], text)


STATUS_LABEL = {
    "publishing": "publicándose",
    "published": "publicado",
    "discarded": "descartado",
    "expired": "caducado",
    "review": "esperando tu revisión",
    "failed": "con error",
}
