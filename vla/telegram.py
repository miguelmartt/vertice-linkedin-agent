"""Bot API de Telegram por long polling, sin puertos abiertos ni librerías externas."""

from __future__ import annotations

import html
import json
import secrets
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import http
from .util import split_message

API = "https://api.telegram.org/bot{token}/{method}"

Button = Tuple[str, str]  # (texto, callback_data)


def esc(text: Any) -> str:
    """Escapa para el modo HTML de Telegram, también dentro de atributos (href="…")."""
    return html.escape(str(text), quote=True)


def keyboard(rows: Sequence[Sequence[Button]]) -> Dict[str, Any]:
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


class TelegramError(Exception):
    pass


def multipart(fields: Dict[str, str], name: str, filename: str, content: bytes,
              content_type: str) -> Tuple[bytes, str]:
    """Cuerpo multipart/form-data con unos campos de texto y un archivo. Devuelve (cuerpo, Content-Type)."""
    boundary = "vla-" + secrets.token_hex(16)
    crlf = "\r\n"
    parts = [f'--{boundary}{crlf}Content-Disposition: form-data; name="{key}"{crlf}{crlf}{value}{crlf}'.encode("utf-8")
             for key, value in fields.items()]
    parts.append(f'--{boundary}{crlf}Content-Disposition: form-data; name="{name}"; filename="{filename}"{crlf}'
                 f"Content-Type: {content_type}{crlf}{crlf}".encode("utf-8") + content + crlf.encode("ascii"))
    parts.append(f"--{boundary}--{crlf}".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False, printer: Callable[[str], None] = print):
        self.token = token
        self.chat_id = str(chat_id)
        self.dry_run = dry_run
        self.printer = printer

    def call(self, method: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 30,
             upload: Optional[Tuple[bytes, str]] = None) -> Any:
        """Llama a la Bot API. Con `upload` (cuerpo, Content-Type), envía un archivo en vez de JSON."""
        url = API.format(token=self.token, method=method)
        try:
            if upload:
                raw = http.request("POST", url, body=upload[0], headers={"Content-Type": upload[1]},
                                   timeout=timeout).body
            else:
                raw = http.request("POST", url, json_body=payload or {}, timeout=timeout).body
        except http.HttpError as exc:
            raise TelegramError(str(exc)) from None
        data = json.loads(raw.decode("utf-8"))
        if not data.get("ok"):
            raise TelegramError(data.get("description", "respuesta no válida de Telegram"))
        return data.get("result")

    def send(self, text: str, buttons: Optional[Sequence[Sequence[Button]]] = None) -> Optional[int]:
        """Envía un mensaje HTML (troceado si es largo). Devuelve el id del último trozo."""
        parts = split_message(text)
        if self.dry_run:
            self.printer("[telegram] " + text + (f"\n[botones] {buttons}" if buttons else ""))
            return None
        message_id = None
        for i, part in enumerate(parts):
            payload: Dict[str, Any] = {
                "chat_id": self.chat_id,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if buttons and i == len(parts) - 1:
                payload["reply_markup"] = keyboard(buttons)
            message_id = self.call("sendMessage", payload)["message_id"]
        return message_id

    def send_photo(self, photo: bytes, caption: str = "") -> Optional[int]:
        """Envía una imagen (PNG o JPG) con un pie corto en HTML. Devuelve el id del mensaje."""
        if self.dry_run:
            self.printer(f"[telegram] imagen de {len(photo)} bytes: {caption}")
            return None
        fields = {"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML"}
        result = self.call("sendPhoto", timeout=60, upload=multipart(fields, "photo", "post.png", photo, "image/png"))
        return result["message_id"]

    def edit_buttons(self, message_id: Optional[int], buttons: Optional[Sequence[Sequence[Button]]] = None) -> None:
        if self.dry_run or not message_id:
            return
        try:
            self.call("editMessageReplyMarkup", {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "reply_markup": keyboard(buttons or []),
            })
        except TelegramError:
            pass  # el mensaje puede ser antiguo o no haber cambiado

    def delete(self, message_id: Optional[int]) -> bool:
        """Borra un mensaje del chat (el tuyo con el código de LinkedIn). True si se ha borrado."""
        if self.dry_run or not message_id:
            return False
        try:
            self.call("deleteMessage", {"chat_id": self.chat_id, "message_id": message_id})
            return True
        except TelegramError:
            return False

    def answer(self, callback_id: str, text: str = "") -> None:
        if self.dry_run:
            return
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})
        except TelegramError:
            pass

    def set_commands(self, commands: Sequence[Tuple[str, str]]) -> None:
        if self.dry_run:
            return
        try:
            self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})
        except TelegramError:
            pass

    def updates(self, offset: Optional[int], timeout: int = 50) -> List[Dict[str, Any]]:
        payload: Dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 15) or []

    def me(self) -> Dict[str, Any]:
        return self.call("getMe")

    def chat(self) -> Dict[str, Any]:
        """Datos del chat configurado. Falla con «chat not found» si aún no has escrito al bot."""
        return self.call("getChat", {"chat_id": self.chat_id})
