"""Cliente HTTP mínimo con la librería estándar: reintentos, límite de tamaño y errores sin secretos.

`fetch_public` es para las URL que llegan de fuera (Hacker News, feeds, la og:image de una página):
solo http(s) y solo hosts con IP pública, también tras cada redirección. Así un enlace no puede
hacer que el agente pida algo a la red interna del VPS.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, NamedTuple, Optional

USER_AGENT = "vertice-linkedin-agent (+https://github.com/miguelmartt/vertice-linkedin-agent)"
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_BYTES = 10_000_000

SECRET_PATTERNS = [
    (re.compile(r"bot\d+:[A-Za-z0-9_-]+"), "bot***"),
    (re.compile(r"(?i)(bearer|token|basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 ***"),
    (re.compile(r"(?i)\b(code|client_secret|access_token|refresh_token|state)=[^&\s\"']+"), r"\1=***"),
    (re.compile(r"\bAQ[A-Za-z0-9_-]{40,}"), "***"),  # tokens de LinkedIn
    (re.compile(r"github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]+"), "***"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "***"),
]


def redact(text: str) -> str:
    """Quita tokens (Telegram, LinkedIn, GitHub, Claude), códigos OAuth y cabeceras de autorización."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class Response(NamedTuple):
    status: int
    headers: Dict[str, str]
    body: bytes


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        self.status = status
        self.body = body
        self.url = redact(url.split("?", 1)[0])
        super().__init__(f"HTTP {status} en {self.url}: {redact(body[:300])}")


def is_public_url(url: str) -> bool:
    """http(s) a un host cuyas IP son todas públicas (ni loopback, ni red privada, ni enlace local)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            return False
        if not ip.is_global:
            return False
    return bool(infos)


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Si una redirección cambia de host o de esquema, no le pasa la cabecera Authorization.
    Con `public_only`, además, solo sigue redirecciones a hosts públicos."""

    def __init__(self, public_only: bool = False):
        super().__init__()
        self.public_only = public_only

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if self.public_only and not is_public_url(newurl):
            raise HttpError(0, "redirección a un host no público", newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        old, target = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if new is not None and (old.scheme, old.netloc) != (target.scheme, target.netloc):
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_Redirects())
_public_opener = urllib.request.build_opener(_Redirects(public_only=True))


def request(
    method: str,
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    data: Optional[Mapping[str, Any]] = None,
    json_body: Any = None,
    body: Optional[bytes] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 30,
    retries: int = 2,
    backoff: float = 2.0,
    max_bytes: int = MAX_BYTES,
    truncate: bool = False,
    public_only: bool = False,
) -> Response:
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    if public_only and not is_public_url(url):
        raise HttpError(0, "solo se piden URL públicas", url)
    hdrs: Dict[str, str] = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json; charset=utf-8")
    elif data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    opener = _public_opener if public_only else _opener

    attempt = 0
    while True:
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with opener.open(req, timeout=timeout) as resp:
                payload = resp.read(max_bytes + 1)
                if len(payload) > max_bytes:
                    if not truncate:
                        raise HttpError(resp.status, f"respuesta de más de {max_bytes} bytes", url)
                    payload = payload[:max_bytes]
                return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, payload)
        except urllib.error.HTTPError as exc:
            text = exc.read(64_000).decode("utf-8", errors="replace")
            if exc.code in RETRY_STATUS and attempt < retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = float(retry_after) if retry_after and retry_after.isdigit() else backoff * (2 ** attempt)
                time.sleep(min(delay, 60))
                attempt += 1
                continue
            raise HttpError(exc.code, text, url) from None
        except HttpError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                attempt += 1
                continue
            raise HttpError(0, str(getattr(exc, "reason", exc)), url) from None


def request_json(method: str, url: str, **kwargs: Any) -> Any:
    payload = request(method, url, **kwargs).body
    if not payload:
        return None
    return json.loads(payload.decode("utf-8"))


def fetch_public(url: str, max_bytes: int = 2_000_000, accept: str = "*/*") -> Response:
    """GET de una URL que viene de fuera: solo hosts públicos, recortada a `max_bytes`."""
    return request("GET", url, headers={"Accept": accept}, timeout=20, retries=1,
                   max_bytes=max_bytes, truncate=True, public_only=True)
