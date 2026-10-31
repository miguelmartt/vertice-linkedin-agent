"""LinkedIn: permiso del miembro (OAuth), publicación con la Posts API e imagen de la tarjeta.

La app de developers necesita dos productos de autoservicio: «Share on LinkedIn» (w_member_social)
y «Sign In with LinkedIn using OpenID Connect» (openid, profile). LinkedIn no da refresh token a
estas apps: el permiso dura 60 días y hay que renovarlo en el navegador.

El texto del post (`commentary`) va en «little text format»: los caracteres reservados se escapan
con una barra invertida y los hashtags van como plantilla, {hashtag|\\#|Tag}. Si no se escapan,
LinkedIn corta el post o lo rechaza.

La Posts API no genera la vista previa de un enlace: título, descripción e imagen los pone el
agente. La imagen se sube antes con la Images API.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any, Callable, Dict, Optional, Tuple

from . import http

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
POSTS_URL = "https://api.linkedin.com/rest/posts"
IMAGES_URL = "https://api.linkedin.com/rest/images?action=initializeUpload"
SCOPES = "openid profile w_member_social"
MAX_COMMENTARY = 3000
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif"}

RESERVED = set("|{}@[]()<>#\\*_~")
# Un hashtag: # pegado a una palabra que tenga al menos una letra, y que no siga a una letra,
# una barra o un & (así no se confunde con el #fragmento de una URL ni con &#123;).
HASHTAG_RE = re.compile(r"(?<![\w/&#])#(\w*[^\W\d_]\w*)")


def escape_little(text: str) -> str:
    """Texto plano en little text format: cada carácter reservado, con su barra invertida."""
    return "".join("\\" + ch if ch in RESERVED else ch for ch in text)


def to_little(text: str) -> str:
    """El post listo para `commentary`: todo escapado salvo los hashtags, que van como plantilla."""
    out = []
    last = 0
    for m in HASHTAG_RE.finditer(text):
        out.append(escape_little(text[last:m.start()]))
        out.append("{hashtag|\\#|" + escape_little(m.group(1)) + "}")
        last = m.end()
    out.append(escape_little(text[last:]))
    return "".join(out)


def post_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}/"


def parse_callback(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(code, state, error) de la URL a la que LinkedIn te devuelve, pegada tal cual."""
    match = re.search(r"\S*(?:code|error)=\S+", text or "")
    if not match:
        return None, None, None
    raw = match.group(0)
    query = urllib.parse.urlsplit(raw).query if "?" in raw else raw
    params = urllib.parse.parse_qs(query)
    first = lambda key: (params.get(key) or [None])[0]  # noqa: E731
    error = first("error_description") or first("error")
    return first("code"), first("state"), error


class LinkedInError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


class LinkedIn:
    def __init__(self, client_id: str, client_secret: str, redirect_uri: str, version: str,
                 request: Callable[..., http.Response] = http.request):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.version = version
        self.request = request

    # --- permiso --------------------------------------------------------------

    def auth_url(self, state: str) -> str:
        return AUTH_URL + "?" + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "scope": SCOPES,
        }, quote_via=urllib.parse.quote)

    def exchange(self, code: str) -> Dict[str, Any]:
        """Código de un solo uso → access token (60 días). El secreto va en el cuerpo, nunca en la URL."""
        return self._json("POST", TOKEN_URL, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
        })

    def userinfo(self, token: str) -> Dict[str, Any]:
        """Quién ha dado el permiso: `sub` (el id del miembro para esta app) y su nombre."""
        return self._json("GET", USERINFO_URL, headers={"Authorization": f"Bearer {token}"})

    # --- publicar -------------------------------------------------------------

    def _api_headers(self, token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Linkedin-Version": self.version,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    def upload_image(self, token: str, owner: str, data: bytes) -> str:
        """Sube una imagen (JPG, PNG o GIF) y devuelve su URN: para la miniatura de la tarjeta o para un post con imagen."""
        init = self._json("POST", IMAGES_URL, headers=self._api_headers(token),
                          json_body={"initializeUploadRequest": {"owner": owner}})
        value = (init or {}).get("value") or {}
        upload_url, image = value.get("uploadUrl"), value.get("image")
        if not upload_url or not image:
            raise LinkedInError(0, "LinkedIn no ha devuelto dónde subir la imagen")
        self._call("PUT", upload_url, body=data, headers={"Authorization": f"Bearer {token}",
                                                          "Content-Type": "application/octet-stream"})
        return str(image)

    def create_post(self, token: str, author: str, commentary: str,
                    article: Optional[Dict[str, Any]] = None, image: Optional[Dict[str, str]] = None) -> str:
        """Publica en el perfil de `author` (urn:li:person:…) y devuelve la URN del post.

        Con `article`, el post lleva la tarjeta del enlace; con `image` ({id, altText}), una imagen grande.
        """
        body: Dict[str, Any] = {
            "author": author,
            "commentary": commentary,
            "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if image:
            body["content"] = {"media": {"id": image["id"], "altText": image.get("altText") or ""}}
        elif article:
            body["content"] = {"article": {k: v for k, v in article.items() if v}}
        resp = self._call("POST", POSTS_URL, headers=self._api_headers(token), json_body=body)
        urn = resp.headers.get("x-restli-id") or resp.headers.get("x-linkedin-id")
        if not urn and resp.body:
            try:
                urn = json.loads(resp.body.decode("utf-8")).get("id")
            except (ValueError, AttributeError):
                urn = None
        if not urn:
            raise LinkedInError(resp.status, "LinkedIn ha aceptado el post pero no ha devuelto su id")
        return str(urn)

    # --- HTTP -----------------------------------------------------------------

    def _call(self, method: str, url: str, **kwargs: Any) -> http.Response:
        try:
            return self.request(method, url, timeout=60, retries=1, **kwargs)
        except http.HttpError as exc:
            raise LinkedInError(exc.status, _error_message(exc)) from None

    def _json(self, method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
        resp = self._call(method, url, **kwargs)
        try:
            return json.loads(resp.body.decode("utf-8")) if resp.body else {}
        except ValueError:
            raise LinkedInError(resp.status, "LinkedIn ha respondido algo que no es JSON") from None


def _error_message(exc: http.HttpError) -> str:
    """El mensaje de error de LinkedIn, sin tokens ni códigos."""
    detail = exc.body
    try:
        data = json.loads(exc.body)
        detail = data.get("message") or data.get("error_description") or data.get("error") or exc.body
    except (ValueError, AttributeError):
        pass
    return http.redact(f"LinkedIn respondió {exc.status}: {str(detail)[:300]}")
