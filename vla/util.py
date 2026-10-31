"""Utilidades de fechas, texto y entorno."""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from datetime import date, datetime, timezone, tzinfo
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# Lo que necesita cualquier proceso hijo para funcionar (las de Windows, para desarrollo local).
BASE_ENV = {
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PATHEXT", "COMSPEC",
}


def child_env(*extra: str, prefixes: Tuple[str, ...] = ()) -> Dict[str, str]:
    """Entorno mínimo para un subproceso: sin los tokens del agente que no le hacen falta."""
    keys = BASE_ENV | set(extra)
    return {k: v for k, v in os.environ.items() if k.upper() in keys or k.startswith(prefixes)}


def zone(tz: str) -> tzinfo:
    """Zona horaria; si el sistema no trae la base de datos (Windows), la local."""
    try:
        return ZoneInfo(tz)
    except Exception:
        return datetime.now().astimezone().tzinfo or timezone.utc


def now(tz: str) -> datetime:
    return datetime.now(zone(tz))


def today(tz: str) -> date:
    return now(tz).date()


def parse_date(value: Optional[str]) -> Optional[datetime]:
    """Fecha ISO 8601 (con «Z») o RFC 822 de un feed, siempre con zona (UTC si no trae)."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def shorten(text: Optional[str], limit: int) -> str:
    """Espacios colapsados y, si no cabe, cortado en la última palabra con «…»."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= limit:
        return clean
    cut = clean[: limit - 1].rsplit(" ", 1)[0]
    return (cut or clean[: limit - 1]) + "…"


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def fingerprint(text: Optional[str]) -> str:
    """Huella corta de un texto: el «Publicar» de un borrador anterior no publica el que lo sustituye."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:12]


def fmt_num(value: float) -> str:
    """Número con formato español: 1.234."""
    return f"{int(round(value)):,}".replace(",", ".")


def split_message(text: str, limit: int = 3900) -> List[str]:
    """Trocea un mensaje largo por párrafos para no pasar del límite de Telegram."""
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    current = ""
    for block in text.split("\n"):
        candidate = f"{current}\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:
            parts.append(block[:limit])
            block = block[limit:]
        current = block
    if current:
        parts.append(current)
    return parts
