"""El agente: recoger novedades, redactar el post, revisarlo en Telegram y publicarlo en LinkedIn."""

from __future__ import annotations

import json
import logging
import queue
import re
import secrets
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import http, sources
from .config import Config
from .db import DB
from .linkedin import IMAGE_TYPES, MAX_COMMENTARY, LinkedIn, LinkedInError, parse_callback, post_url, to_little
from .llm import LLM, LLMError
from .sources import KIND_LABEL, KIND_PLURAL
from .telegram import Telegram, esc
from .util import fingerprint, now as tz_now, parse_date, shorten, strip_accents

log = logging.getLogger("vla")

PROMPT = (Path(__file__).parent / "prompts" / "post.md").read_text(encoding="utf-8")
POST_SCHEMA = {
    "type": "object",
    "properties": {
        "item_id": {"type": "integer"},
        "text": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "why": {"type": "string"},
    },
    "required": ["item_id", "text", "hashtags", "why"],
    "additionalProperties": False,
}

FRESH_DAYS = 5            # un tema de fuera vale como novedad durante 5 días
RETIRE_DAYS = 30          # después de un mes sin usarse, deja de ser candidato
PER_KIND = 3              # candidatos de cada tipo en la lista corta
MATERIAL_CHARS = 3500     # texto de cada candidato que recibe Claude
MIN_CHARS, MAX_CHARS = 300, 2600
MAX_IMAGE = 5_000_000
AUTH_TTL = timedelta(minutes=30)
TOKEN_WARNINGS = (7, 2)   # días antes de que caduque el permiso de LinkedIn
SOURCE_ALERT_AFTER = 3    # recogidas seguidas fallando antes de avisar
WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")


class NeedsAuth(Exception):
    """Falta el permiso de LinkedIn o ha caducado."""


def review_buttons(post: Dict[str, Any]) -> List[List[Tuple[str, str]]]:
    pid = post["id"]
    return [
        [("🚀 Publicar", f"publish:{pid}:{fingerprint(post['text'])}"), ("✏️ Cambios", f"changes:{pid}")],
        [("🔀 Otro tema", f"other:{pid}"), ("🗑 Descartar", f"discard:{pid}")],
    ]


def retry_buttons(post: Dict[str, Any]) -> List[List[Tuple[str, str]]]:
    pid = post["id"]
    return [[("🔁 Reintentar", f"publish:{pid}:{fingerprint(post['text'])}"), ("🗑 Descartar", f"discard:{pid}")]]


def clean_tag(tag: str) -> str:
    return re.sub(r"[^\w]", "", strip_accents(str(tag).lstrip("#")))[:40]


def clean_body(text: str) -> str:
    """Lo que devuelve Claude, en texto plano: sin markdown ni URL (el enlace lo pone el agente)."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?m)^#+\s+", "", text)
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compose(body: str, link: str, hashtags: Sequence[str]) -> str:
    """El post final: texto, enlace y hashtags, en párrafos separados."""
    tags = " ".join("#" + t for t in (clean_tag(h) for h in list(hashtags)[:3]) if t)
    return "\n\n".join(part for part in (body.strip(), link, tags) if part)


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.db.init()
        self.tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id, dry_run=not cfg.telegram_token)
        self.llm = LLM(cfg.claude_bin, cfg.sandbox_dir, self.db, cfg.timezone,
                       cfg.llm_max_calls_per_day, cfg.llm_timeout)
        self.li = LinkedIn(cfg.linkedin_client_id, cfg.linkedin_client_secret, cfg.linkedin_redirect_uri,
                           cfg.linkedin_version)
        # Cómo se piden las cosas de fuera (los tests las sustituyen por dobles).
        self.get_json: Callable[..., Any] = http.request_json
        self.fetch: Callable[..., http.Response] = http.fetch_public
        self.request: Callable[..., http.Response] = http.request
        self.tasks: "queue.Queue[Tuple[str, Callable[..., Any], tuple]]" = queue.Queue()
        self.stop = threading.Event()
        self.busy: Optional[str] = None

    # --- utilidades ---------------------------------------------------------

    def now(self) -> datetime:
        return tz_now(self.cfg.timezone)

    def today(self) -> date:
        return self.now().date()

    def enqueue(self, name: str, fn: Callable[..., Any], *args: Any) -> None:
        log.info("En cola: %s", name)
        self.tasks.put((name, fn, args))

    def run_task(self, name: str, fn: Callable[..., Any], *args: Any) -> None:
        self.busy = name
        try:
            fn(*args)
        except Exception as exc:  # el servicio no se cae por un trabajo
            log.exception("Error en %s", name)
            try:
                self.db.log_event(self.now(), "error", {"task": name, "error": http.redact(str(exc))[:500]})
                self.tg.send(f"⚠️ Error en <b>{esc(name)}</b>: {esc(http.redact(str(exc))[:800])}")
            except Exception:  # si Telegram tampoco responde, queda en el log y se sigue
                log.exception("No he podido avisar del error en %s", name)
        finally:
            self.busy = None

    def worker_loop(self) -> None:
        while not self.stop.is_set():
            try:
                name, fn, args = self.tasks.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.run_task(name, fn, *args)
            except Exception:
                log.exception("Fallo inesperado del trabajador en %s", name)

    # --- recoger ------------------------------------------------------------

    def collect(self) -> Dict[str, int]:
        """Pasa por todas las fuentes y guarda los temas nuevos. Una fuente caída no para a las demás."""
        cfg = self.cfg
        current = self.now()
        steps: List[Tuple[str, Callable[[], List[Dict[str, Any]]]]] = [
            ("Hugging Face", partial(sources.huggingface, current, cfg.hf_min_likes, get=self.get_json)),
            ("GitHub", partial(sources.github_trending, current, cfg.github_min_stars, cfg.github_owners,
                               cfg.github_token, get=self.get_json)),
            ("Hacker News", partial(sources.hackernews, current, cfg.hn_min_points, get=self.get_json)),
        ]
        for url in cfg.feeds:
            steps.append((urllib.parse.urlsplit(url).netloc, partial(sources.feed, url, current, fetch=self.fetch)))
        if cfg.github_owners:
            steps.append(("tus repos", partial(sources.own_repos, cfg.github_owners, cfg.github_token,
                                               cfg.own_exclude, current, get=self.get_json)))

        failures: Dict[str, int] = dict(self.db.get_meta("source_failures") or {})
        alerted = set(self.db.get_meta("source_alerted") or [])
        new: Dict[str, int] = {}
        errors: Dict[str, str] = {}
        for name, fn in steps:
            try:
                items = fn()
            except Exception as exc:
                errors[name] = http.redact(str(exc))[:200]
                failures[name] = failures.get(name, 0) + 1
                log.warning("Fuente %s: %s", name, errors[name])
                if failures[name] >= SOURCE_ALERT_AFTER and name not in alerted:
                    alerted.add(name)
                    self.tg.send(f"⚠️ La fuente <b>{esc(name)}</b> lleva {failures[name]} búsquedas fallando: "
                                 f"{esc(errors[name])}\nSigo con las demás.")
                continue
            failures.pop(name, None)
            if name in alerted:
                alerted.discard(name)
                self.tg.send(f"✅ La fuente <b>{esc(name)}</b> vuelve a funcionar.")
            for item in items:
                _, is_new = self.db.upsert_item(item, current)
                if is_new:
                    new[item["kind"]] = new.get(item["kind"], 0) + 1
        self.db.set_meta("source_failures", failures)
        self.db.set_meta("source_alerted", sorted(alerted))
        self.db.set_meta("last_collect", {"at": current.isoformat(), "new": new, "errors": errors})
        log.info("Búsqueda: %s temas nuevos, %s fuentes con error", sum(new.values()), len(errors))
        return new

    # --- elegir -------------------------------------------------------------

    def own_allowed(self) -> bool:
        """Como mucho VLA_OWN_PER_WEEK posts de tus repos en 7 días: promoción sí, monotema no."""
        since = (self.now() - timedelta(days=7)).isoformat()
        recent = self.db.recent_posts(since, ("review", "publishing", "published"))
        return sum(1 for p in recent if p["kind"] == "own") < self.cfg.own_per_week

    def shortlist(self, allow_own: bool = True) -> List[Dict[str, Any]]:
        """Los candidatos del próximo post. Si toca un repo tuyo, solo ese; si no, los mejores de cada tipo."""
        cands = self.db.candidates((self.now() - timedelta(days=FRESH_DAYS)).isoformat())
        own = [c for c in cands if c["kind"] == "own"]
        if allow_own and own and self.own_allowed():
            # Primero una versión nueva (puntúa 1.0); entre repos, el más reciente.
            return [max(own, key=lambda c: (c["score"], c.get("published") or ""))]
        picked: List[Dict[str, Any]] = []
        for kind in sources.OUTSIDE_KINDS:
            picked += [c for c in cands if c["kind"] == kind][:PER_KIND]
        return picked

    # --- redactar -----------------------------------------------------------

    def system_prompt(self) -> str:
        return PROMPT.replace("{{owner}}", self.cfg.owner).replace("{{about}}", self.cfg.about)

    def material(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """Lo que Claude sabe de un candidato: sus datos y el texto de su página o su README."""
        try:
            extra = sources.details(item, self.cfg.github_token, fetch=self.fetch, request=self.request)
        except Exception as exc:  # sin material extra se puede redactar igual con el resumen
            log.warning("Sin detalles de %s: %s", item["url"], http.redact(str(exc))[:200])
            extra = {"text": "", "description": "", "image": item.get("image") or ""}
        if extra.get("image") and not item.get("image"):
            self.db.set_item_image(item["id"], extra["image"])
            item["image"] = extra["image"]
        return {
            "id": item["id"],
            "tipo": KIND_LABEL.get(item["kind"], item["kind"]),
            "fuente": item["source"],
            "titulo": item["title"],
            "url": item["url"],
            "fecha": (item.get("published") or "")[:10],
            "cifras": sources.metrics_line(item),
            "resumen": item.get("summary") or "",
            "descripcion": extra.get("description") or "",
            "material": (extra.get("text") or "")[:MATERIAL_CHARS],
        }

    def validate(self, result: Dict[str, Any], items: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
        problems: List[str] = []
        ids = [i["id"] for i in items]
        try:
            item_id: Optional[int] = int(result.get("item_id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            item_id = None
        if item_id not in ids:
            if len(ids) == 1:
                item_id = ids[0]
            else:
                problems.append(f"item_id {result.get('item_id')!r} no es ninguno de los candidatos")
        body = clean_body(str(result.get("text") or ""))
        if len(body) < MIN_CHARS:
            problems.append(f"el texto tiene {len(body)} caracteres y el mínimo es {MIN_CHARS}")
        if len(body) > MAX_CHARS:
            problems.append(f"el texto tiene {len(body)} caracteres y el máximo es {MAX_CHARS}")
        hashtags = [t for t in (clean_tag(h) for h in (result.get("hashtags") or [])) if t][:3]
        why = shorten(str(result.get("why") or ""), 200)
        return {"item_id": item_id, "text": body, "hashtags": hashtags, "why": why}, problems

    def write(self, items: Sequence[Dict[str, Any]], feedback: Optional[List[str]] = None,
              previous: Optional[str] = None) -> Dict[str, Any]:
        """Claude elige un candidato y escribe el post. Si no cumple las reglas, una segunda oportunidad."""
        with ThreadPoolExecutor(max_workers=6) as pool:
            material = list(pool.map(self.material, items))
        since = (self.now() - timedelta(days=30)).isoformat()
        recent = [
            {"fecha": p["created"][:10], "tipo": KIND_LABEL.get(p["kind"], p["kind"]), "tema": p["item_title"],
             "inicio": shorten(p["text"].split("\n", 1)[0], 140)}
            for p in self.db.recent_posts(since)[:10]
        ]
        payload: Dict[str, Any] = {"hoy": self.today().isoformat(), "candidatos": material, "posts_recientes": recent}
        if previous:
            payload["borrador_anterior"] = previous
        if feedback:
            payload["comentarios"] = feedback
        prompt = json.dumps(payload, ensure_ascii=False, indent=1)
        problems: List[str] = []
        for _ in range(2):
            ask = prompt if not problems else (
                prompt + "\n\nTu respuesta anterior no valía: " + "; ".join(problems) + ". Corrígelo.")
            result, problems = self.validate(self.llm.run("post", self.system_prompt(), ask, POST_SCHEMA,
                                                          self.cfg.model), items)
            if not problems:
                return result
        raise LLMError("el borrador de Claude no cumple las reglas: " + "; ".join(problems))

    def draft(self, manual: bool = False, allow_own: bool = True, item_id: Optional[int] = None) -> None:
        """Prepara el post del día y te lo manda para revisar."""
        current = self.db.open_post()
        if current:
            if manual or current["status"] == "publishing":
                if manual:
                    self.tg.send("Tienes un borrador sin decidir: publícalo, cámbialo o descártalo antes de pedir otro.")
                    self.send_review(current)
                return
            # Un borrador que lleva días sin respuesta ya no es novedad: deja paso al nuevo.
            self.db.update_post(current["id"], self.now(), status="expired")
            self.db.set_item_status(current["item_id"], "skipped")
            self.tg.edit_buttons(current.get("message_id"))
        if item_id is not None:
            item = self.db.item(item_id)
            items = [item] if item and item["status"] != "old" else []
        else:
            items = self.shortlist(allow_own)
        if not items:
            self.tg.send("🤷 No tengo temas nuevos que merezcan un post. Lo vuelvo a intentar en el próximo turno "
                         "(o /recoger para buscar ahora).")
            return
        result = self.write(items)
        item = next(i for i in items if i["id"] == result["item_id"])
        text = compose(result["text"], item["url"], result["hashtags"])
        pid = self.db.add_post(self.now(), item["id"], text, result["why"])
        self.db.set_item_status(item["id"], "drafted")
        self.send_review(self.db.post(pid))  # type: ignore[arg-type]

    def send_review(self, post: Dict[str, Any]) -> None:
        item = self.db.item(post["item_id"]) or {}
        lines = [
            f"📝 <b>Borrador para LinkedIn</b> · {esc(KIND_LABEL.get(item.get('kind', ''), ''))}",
            f"Fuente: <a href=\"{esc(item.get('url', ''))}\">{esc(shorten(item.get('title', ''), 120))}</a>",
        ]
        metrics = sources.metrics_line(item) if item else ""
        if metrics:
            lines.append(esc(metrics))
        if post.get("why"):
            lines.append(f"Por qué: {esc(post['why'])}")
        card = "con tarjeta e imagen del enlace" if item.get("image") else "con tarjeta del enlace, sin imagen"
        lines += ["", f"<blockquote>{esc(post['text'])}</blockquote>", f"{len(post['text'])} caracteres · {card}"]
        message_id = self.tg.send("\n".join(lines), review_buttons(post))
        self.db.update_post(post["id"], self.now(), message_id=message_id)

    def redo(self, pid: int, feedback: str) -> None:
        """Rehace el borrador con tus comentarios (el mismo tema)."""
        post = self.db.post(pid)
        item = self.db.item(post["item_id"]) if post else None
        if not post or not item:
            return
        notes = list(post.get("feedback") or []) + [feedback]
        result = self.write([item], feedback=notes, previous=post["text"])
        text = compose(result["text"], item["url"], result["hashtags"])
        self.db.update_post(pid, self.now(), text=text, feedback=notes, status="review", last_error=None)
        self.send_review(self.db.post(pid))  # type: ignore[arg-type]

    def replace_text(self, pid: int, text: str) -> None:
        """Tu texto, tal cual, en lugar del borrador."""
        post = self.db.post(pid)
        if not post:
            return
        notes = list(post.get("feedback") or []) + ["(texto reescrito a mano)"]
        self.db.update_post(pid, self.now(), text=text.strip()[:MAX_COMMENTARY], feedback=notes,
                            status="review", last_error=None)
        self.tg.send("✍️ Uso tu texto tal cual.")
        self.send_review(self.db.post(pid))  # type: ignore[arg-type]

    def other_topic(self, pid: int) -> None:
        post = self.db.post(pid)
        if post:
            self.db.update_post(pid, self.now(), status="discarded")
            self.db.set_item_status(post["item_id"], "skipped")
        self.draft(manual=True, allow_own=False)

    def discard(self, pid: int) -> None:
        post = self.db.post(pid)
        if not post:
            return
        self.db.update_post(pid, self.now(), status="discarded")
        self.db.set_item_status(post["item_id"], "skipped")
        self.tg.send("🗑 Descartado. El siguiente llega en el próximo turno (o pídelo con /borrador).")

    # --- publicar -----------------------------------------------------------

    def linkedin_session(self) -> Tuple[str, str]:
        """(token, URN del autor) o NeedsAuth si no hay permiso o ha caducado."""
        token = self.db.get_meta("li_token") or {}
        profile = self.db.get_meta("li_profile") or {}
        if not token.get("access_token") or not profile.get("sub"):
            raise NeedsAuth("LinkedIn no está conectado")
        expires = parse_date(token.get("expires_at"))
        if expires and expires <= self.now():
            raise NeedsAuth("el permiso de LinkedIn ha caducado")
        return token["access_token"], f"urn:li:person:{profile['sub']}"

    def article(self, token: str, author: str, item: Dict[str, Any]) -> Dict[str, Optional[str]]:
        """La tarjeta del enlace. LinkedIn no la genera por API: título, descripción e imagen van aquí."""
        thumbnail: Optional[str] = None
        if item.get("image"):
            try:
                resp = self.fetch(item["image"], max_bytes=MAX_IMAGE, accept="image/png, image/jpeg, image/gif")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype in IMAGE_TYPES and len(resp.body) < MAX_IMAGE:
                    thumbnail = self.li.upload_image(token, author, resp.body)
                else:
                    log.info("Imagen de %s descartada (%s, %s bytes)", item["url"], ctype, len(resp.body))
            except Exception as exc:  # sin imagen la tarjeta sale igual
                log.warning("Sin imagen para la tarjeta: %s", http.redact(str(exc))[:200])
        return {
            "source": item["url"],
            "title": shorten(item["title"], 200),
            "description": shorten(item.get("summary") or "", 250),
            "thumbnail": thumbnail,
        }

    def publish(self, pid: int) -> None:
        post = self.db.post(pid)
        item = self.db.item(post["item_id"]) if post else None
        if not post or not item:
            return
        try:
            urn, note = self._publish(post, item)
        except Exception as exc:
            error = http.redact(str(exc))[:500]
            log.warning("No he podido publicar el post %s: %s", pid, error)
            self.db.update_post(pid, self.now(), status="failed", last_error=error)
            auth = isinstance(exc, NeedsAuth) or (isinstance(exc, LinkedInError) and exc.status == 401)
            hint = "\nRenueva el permiso con /linkedin y pulsa «Reintentar»." if auth else ""
            self.tg.send(f"❌ No he podido publicar el post: {esc(error)}{hint}", retry_buttons(post))
            return
        self.db.update_post(pid, self.now(), status="published", linkedin_urn=urn,
                            published_at=self.now().isoformat(), last_error=None)
        self.db.set_item_status(item["id"], "used")
        self.tg.send(f"✅ Publicado en LinkedIn{note}.\n{esc(post_url(urn))}")

    def _publish(self, post: Dict[str, Any], item: Dict[str, Any]) -> Tuple[str, str]:
        if self.cfg.dry_run:
            return "urn:li:share:dry-run", " (simulado: VLA_DRY_RUN está activado)"
        token, author = self.linkedin_session()
        commentary = to_little(post["text"])
        article = self.article(token, author, item)
        try:
            return self.li.create_post(token, author, commentary, article), ""
        except LinkedInError as exc:
            if exc.status in (401, 403, 429) or exc.status >= 500:
                raise
            log.warning("LinkedIn ha rechazado la tarjeta (%s); publico sin ella", exc)
            return self.li.create_post(token, author, commentary), " (sin tarjeta del enlace: LinkedIn la ha rechazado)"

    # --- permiso de LinkedIn -------------------------------------------------

    def start_auth(self) -> str:
        """URL para dar permiso. El `state` aleatorio ata la respuesta a esta petición."""
        missing = self.cfg.missing("linkedin")
        if missing:
            raise RuntimeError("faltan variables de LinkedIn: " + ", ".join(missing))
        state = secrets.token_urlsafe(24)
        self.db.set_meta("li_auth", {"state": state, "at": self.now().isoformat()})
        return self.li.auth_url(state)

    def finish_auth(self, text: str) -> None:
        self.tg.send(self.connect(text))

    def connect(self, text: str) -> str:
        """Cambia el código de la URL de vuelta por el token y guarda quién eres. Devuelve el mensaje."""
        pending = self.db.get_meta("li_auth")
        code, state, error = parse_callback(text)
        if error:
            self.db.set_meta("li_auth", None)
            return f"❌ LinkedIn no ha dado el permiso: {esc(error)}. Vuelve a intentarlo con /linkedin."
        if not code:
            return "No veo el código en lo que me has pegado: copia la dirección entera de la página a la que te ha llevado LinkedIn."
        if not pending or not state or not secrets.compare_digest(str(state), str(pending.get("state"))):
            return "❌ Esa respuesta no es del último enlace que te he dado (o ya la has usado). Pide otro con /linkedin."
        started = parse_date(pending.get("at"))
        self.db.set_meta("li_auth", None)  # de un solo uso
        if not started or self.now() - started > AUTH_TTL:
            return "⌛ Ese enlace ha caducado (dura 30 minutos). Pide otro con /linkedin."
        token = self.li.exchange(code)
        access = token.get("access_token")
        if not access:
            raise LinkedInError(0, "LinkedIn no ha devuelto el token")
        info = self.li.userinfo(access)
        if not info.get("sub"):
            raise LinkedInError(0, "LinkedIn no ha dicho quién eres (falta el producto «Sign In with LinkedIn»)")
        expires_at = self.now() + timedelta(seconds=int(token.get("expires_in") or 60 * 86400))
        self.db.set_meta("li_token", {"access_token": access, "expires_at": expires_at.isoformat(),
                                      "scope": token.get("scope")})
        self.db.set_meta("li_profile", {"sub": info["sub"], "name": info.get("name")})
        self.db.set_meta("li_warned", [])
        self.db.log_event(self.now(), "linkedin_connected", {"expires_at": expires_at.isoformat()})
        return (f"✅ Conectado a LinkedIn como <b>{esc(info.get('name') or info['sub'])}</b>. "
                f"El permiso dura hasta el {expires_at:%d/%m/%Y}; te aviso antes de que caduque.")

    # --- mantenimiento diario -----------------------------------------------

    def daily(self) -> None:
        current = self.now()
        self.check_token(current)
        self.check_version(current)
        retired = self.db.retire_items((current - timedelta(days=RETIRE_DAYS)).isoformat())
        if retired:
            log.info("%s temas antiguos retirados", retired)

    def check_token(self, current: datetime) -> None:
        """Avisa a 7 y a 2 días de que caduque el permiso de LinkedIn, y cuando caduca."""
        expires = parse_date((self.db.get_meta("li_token") or {}).get("expires_at"))
        if not expires:
            return
        warned = set(self.db.get_meta("li_warned") or [])
        days = (expires - current).total_seconds() / 86400
        if days <= 0:
            if "expired" not in warned:
                self.tg.send("⚠️ El permiso de LinkedIn ha caducado: no puedo publicar hasta que lo renueves con /linkedin.")
                warned.add("expired")
        else:
            due = [limit for limit in TOKEN_WARNINGS if days <= limit and str(limit) not in warned]
            if due:
                self.tg.send(f"⏳ El permiso de LinkedIn caduca el {expires:%d/%m} "
                             f"(en {max(1, round(days))} días). Renuévalo con /linkedin: es un minuto.")
                warned |= {str(limit) for limit in TOKEN_WARNINGS if days <= limit}
        self.db.set_meta("li_warned", sorted(warned))

    def check_version(self, current: datetime) -> None:
        """LinkedIn retira cada versión de su API al año: avisa cuando la configurada se acerca."""
        m = re.fullmatch(r"(\d{4})(\d{2})", self.cfg.linkedin_version)
        if not m:
            return
        months = current.year * 12 + current.month - (int(m.group(1)) * 12 + int(m.group(2)))
        stamp = f"{current:%Y%m}"
        if months >= 10 and self.db.get_meta("version_warned") != stamp:
            self.db.set_meta("version_warned", stamp)
            self.tg.send(f"⚠️ La versión de la API de LinkedIn que uso ({esc(self.cfg.linkedin_version)}) tiene "
                         f"{months} meses y LinkedIn retira cada versión al año. Cambia VLA_LINKEDIN_VERSION a "
                         f"{stamp} en /etc/vertice-linkedin-agent/env y reinicia el servicio.")

    # --- textos -------------------------------------------------------------

    def status_text(self) -> str:
        from .scheduler import next_due, parse

        current = self.now()
        token = self.db.get_meta("li_token") or {}
        profile = self.db.get_meta("li_profile") or {}
        expires = parse_date(token.get("expires_at"))
        if profile.get("sub") and expires and expires > current:
            days = int((expires - current).total_seconds() // 86400)
            linkedin = f"✅ {esc(profile.get('name') or profile['sub'])}, permiso hasta el {expires:%d/%m} ({days} días)"
        elif profile.get("sub"):
            linkedin = "❌ permiso caducado: /linkedin"
        else:
            linkedin = "❌ sin conectar: /linkedin"
        lines = ["📊 <b>Estado</b>", f"LinkedIn: {linkedin}"]
        if self.cfg.dry_run:
            lines.append("🧪 Modo de pruebas: no publico de verdad (VLA_DRY_RUN)")
        upcoming = next_due(parse(self.cfg.draft_schedule), current)
        lines.append(f"Próximo borrador: {WEEKDAYS[upcoming.weekday()]} {upcoming:%d/%m %H:%M}")
        counts = self.db.item_counts((current - timedelta(days=FRESH_DAYS)).isoformat())
        if counts:
            lines.append("Temas: " + " · ".join(f"{counts[k]} {KIND_PLURAL[k]}" for k in KIND_PLURAL if counts.get(k)))
        last = self.db.get_meta("last_collect")
        if last:
            at = parse_date(last.get("at"))
            when = f"{at:%d/%m %H:%M}" if at else "?"
            lines.append(f"Última búsqueda: {when}, {sum((last.get('new') or {}).values())} temas nuevos")
            if last.get("errors"):
                lines.append("Fuentes con error: " + esc(", ".join(last["errors"])))
        open_post = self.db.open_post()
        if open_post:
            lines.append(f"Borrador abierto: #{open_post['id']} ({open_post['status']})")
        published = self.db.recent_posts((current - timedelta(days=30)).isoformat())
        lines.append(f"Publicados en 30 días: {len(published)}")
        lines.append(f"Claude: {self.llm.calls_left_today()} llamadas disponibles hoy")
        if self.busy:
            lines.append(f"Ahora mismo: {esc(self.busy)}")
        return "\n".join(lines)

    def topics_text(self) -> str:
        """Lo que tengo ahora para los próximos posts, por tipo."""
        cands = self.db.candidates((self.now() - timedelta(days=FRESH_DAYS)).isoformat())
        if not cands:
            return "No tengo temas ahora mismo. /recoger para buscar."
        lines = ["📰 <b>Temas para los próximos posts</b>"]
        for kind in (*sources.OUTSIDE_KINDS, "own"):
            group = [c for c in cands if c["kind"] == kind][:PER_KIND if kind != "own" else 5]
            if not group:
                continue
            lines += ["", f"<b>{esc(KIND_LABEL[kind])}</b>"]
            for c in group:
                metrics = sources.metrics_line(c)
                lines.append(f"• <a href=\"{esc(c['url'])}\">{esc(shorten(c['title'], 90))}</a>"
                             + (f" · {esc(metrics)}" if metrics and kind != "ai" else ""))
        return "\n".join(lines)

    # --- bucle principal ----------------------------------------------------

    def run_forever(self) -> bool:
        """Bucle del servicio. Devuelve False si un hilo ha muerto, para que systemd lo reinicie."""
        from .bot import Bot
        from .scheduler import is_due, parse

        jobs = [
            ("collect", parse(self.cfg.collect_schedule), self.collect),
            ("draft", parse(self.cfg.draft_schedule), self.draft),
            ("daily", parse(self.cfg.daily_schedule), self.daily),
        ]
        bot = Bot(self)
        threads = [
            threading.Thread(target=self.worker_loop, name="worker", daemon=True),
            threading.Thread(target=bot.poll_forever, name="telegram", daemon=True),
        ]
        for t in threads:
            t.start()
        log.info("vertice-linkedin-agent en marcha")
        hello = "🟢 Agente de LinkedIn en marcha. /ayuda para ver qué puedo hacer."
        if not (self.db.get_meta("li_profile") or {}).get("sub") and not self.cfg.dry_run:
            hello += "\nAntes de nada, conéctame a tu LinkedIn con /linkedin."
        self.tg.send(hello)
        while not self.stop.is_set():
            dead = [t.name for t in threads if not t.is_alive()]
            if dead and self.cfg.telegram_token:
                log.error("Hilos parados: %s. Salgo para que systemd me reinicie", ", ".join(dead))
                self.stop.set()
                return False
            current = self.now()
            for name, schedule, fn in jobs:
                job = self.db.job(name)
                last = datetime.fromisoformat(job["last_run"]) if job and job.get("last_run") else None
                if last is None and schedule.kind != "interval":
                    self.db.set_job(name, current)
                    continue
                if is_due(schedule, current, last):
                    self.db.set_job(name, current)
                    self.enqueue(name, self.tracked(name, fn))
            self.stop.wait(30)
        log.info("Parando")
        return True

    def tracked(self, name: str, fn: Callable[[], Any]) -> Callable[[], None]:
        def wrapper() -> None:
            started = self.now()
            try:
                fn()
            except Exception as exc:
                self.db.set_job(name, started, ok=False, error=http.redact(str(exc))[:500])
                raise
            self.db.set_job(name, started, ok=True)
        return wrapper
