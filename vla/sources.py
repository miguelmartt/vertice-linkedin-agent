"""De dónde salen los temas: modelos de Hugging Face, repos en auge de GitHub, Hacker News,
los blogs de los laboratorios de IA y tus propios repos.

Todo lo que llega de fuera es contenido no fiable: se guarda como datos, se recorta y nunca se
ejecuta. Las URL que no son de una API conocida se piden con `http.fetch_public` (solo hosts
públicos). Cada función recibe cómo pedir las cosas (`get`, `fetch`) para probarla sin red.

Cada tema lleva una puntuación de 0 a 1 que solo sirve para ordenar dentro de su tipo:
la tendencia en Hugging Face, las estrellas al día en GitHub o los puntos en Hacker News.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import http
from .util import fmt_num, parse_date, shorten

KIND_LABEL = {
    "model": "Modelo de IA",
    "ai": "Novedad de IA",
    "repo": "Repo en auge",
    "news": "Noticia tech",
    "own": "Repo propio",
}
KIND_PLURAL = {
    "model": "modelos de IA",
    "ai": "novedades de IA",
    "repo": "repos en auge",
    "news": "noticias",
    "own": "repos tuyos",
}
# Tipos de fuera, en el orden en que se reparten los huecos de la lista corta.
OUTSIDE_KINDS = ("model", "ai", "repo", "news")

GITHUB_API = "https://api.github.com"
HF_API = "https://huggingface.co/api/models"
HN_API = "https://hn.algolia.com/api/v1/search_by_date"
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5"

Item = Dict[str, Any]
GetJson = Callable[..., Any]
Fetch = Callable[..., http.Response]


# Lo que es de IA o de desarrollo pesa más al ordenar: la portada de Hacker News y las estrellas
# de GitHub también suben por cosas que no le interesan a quien te sigue en LinkedIn.
TECH_RE = re.compile(
    r"\b(ai|ia|llms?|gpt|agents?|agentic|mcp|rag|models?|ml|machine learning|deep learning|neural|transformers?|"
    r"inference|open[- ]?source|github|python|rust|typescript|javascript|golang|linux|kubernetes|docker|"
    r"databases?|postgres(?:ql)?|sqlite|apis?|sdks?|cli|devtools|frameworks?|compilers?|security|"
    r"vulnerabilit(?:y|ies)|backdoors?|exploits?|cve|claude|anthropic|openai|gemini|deepmind|mistral|llama|"
    r"qwen|deepseek|hugging ?face|copilot|cursor|vs ?code|automation|self-hosted|programming|developers?)\b",
    re.I,
)
TECH_BOOST = 1.5
OFF_TOPIC = 0.4  # en Hacker News, lo que no es tech casi nunca es material de LinkedIn


def is_tech(text: str) -> bool:
    return bool(TECH_RE.search(text or ""))


def github_headers(token: str = "") -> Dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def clean_url(url: str) -> str:
    """URL comparable: sin fragmento, sin parámetros utm_ y sin barra final."""
    parts = urllib.parse.urlsplit(url.strip())
    query = urllib.parse.urlencode(
        [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if not k.startswith("utm_")]
    )
    path = parts.path.rstrip("/") if parts.path not in ("", "/") else ""
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def _age_days(created: Optional[datetime], now: datetime) -> float:
    if not created:
        return 30.0
    return max(0.5, (now - created).total_seconds() / 86400)


# --- Hugging Face -------------------------------------------------------------

def huggingface(now: datetime, min_likes: int, max_age_days: int = 21, get: GetJson = http.request_json) -> List[Item]:
    """Modelos en tendencia publicados hace poco y con un mínimo de likes."""
    models = get("GET", HF_API, params={"sort": "trendingScore", "limit": 50}) or []
    items: List[Item] = []
    for m in models:
        model_id = m.get("id") or m.get("modelId")
        created = parse_date(m.get("createdAt"))
        likes = int(m.get("likes") or 0)
        if not model_id or not created or likes < min_likes or now - created > timedelta(days=max_age_days):
            continue
        tags = m.get("tags") or []
        license_ = next((t.split(":", 1)[1] for t in tags if isinstance(t, str) and t.startswith("license:")), None)
        trending = float(m.get("trendingScore") or 0)
        items.append({
            "key": f"hf:{model_id.lower()}",
            "kind": "model",
            "source": "Hugging Face",
            "title": model_id,
            "url": f"https://huggingface.co/{model_id}",
            "summary": " · ".join(p for p in (m.get("pipeline_tag"), f"licencia {license_}" if license_ else None) if p),
            "metrics": {"likes": likes, "downloads": int(m.get("downloads") or 0), "trending": trending,
                        "pipeline": m.get("pipeline_tag"), "license": license_},
            "score": round(trending / 800, 3),
            "published": created.isoformat(),
        })
    return items


# --- GitHub -------------------------------------------------------------------

def github_trending(now: datetime, min_stars: int, exclude_owners: Sequence[str] = (), token: str = "",
                    max_age_days: int = 10, get: GetJson = http.request_json) -> List[Item]:
    """Repos creados en los últimos días que ya acumulan estrellas; puntúa las estrellas al día."""
    since = (now - timedelta(days=max_age_days)).date().isoformat()
    data = get("GET", f"{GITHUB_API}/search/repositories",
               params={"q": f"created:>{since} stars:>{min_stars}", "sort": "stars", "order": "desc", "per_page": 30},
               headers=github_headers(token)) or {}
    skip = {o.lower() for o in exclude_owners}
    items: List[Item] = []
    for repo in data.get("items") or []:
        full = repo.get("full_name") or ""
        owner = (repo.get("owner") or {}).get("login") or full.split("/")[0]
        if not full or repo.get("fork") or repo.get("archived") or owner.lower() in skip:
            continue
        created = parse_date(repo.get("created_at"))
        stars = int(repo.get("stargazers_count") or 0)
        per_day = stars / _age_days(created, now)
        if not repo.get("description"):
            continue  # sin descripción no hay de qué escribir (y suele ser relleno)
        topical = is_tech(" ".join([full, repo.get("description") or "", " ".join(repo.get("topics") or [])]))
        items.append({
            "key": f"gh:{full.lower()}",
            "kind": "repo",
            "source": "GitHub",
            "title": full,
            "url": clean_url(repo.get("html_url") or f"https://github.com/{full}"),
            "summary": shorten(repo.get("description"), 300),
            "metrics": {"stars": stars, "stars_per_day": round(per_day), "language": repo.get("language"),
                        "topics": (repo.get("topics") or [])[:6],
                        "license": (repo.get("license") or {}).get("spdx_id")},
            "score": round(per_day / 400 * (TECH_BOOST if topical else 1), 3),
            "published": created.isoformat() if created else None,
        })
    return items


def own_repos(owners: Sequence[str], token: str = "", exclude: Sequence[str] = (), now: Optional[datetime] = None,
              release_days: int = 30, get: GetJson = http.request_json) -> List[Item]:
    """Tus repos públicos (sin forks, archivados ni el del perfil) y sus versiones recientes."""
    skip = {e.lower() for e in exclude}
    items: List[Item] = []
    for owner in owners:
        repos = get("GET", f"{GITHUB_API}/users/{urllib.parse.quote(owner)}/repos",
                    params={"type": "owner", "sort": "pushed", "per_page": 100}, headers=github_headers(token)) or []
        for repo in repos:
            name = repo.get("name") or ""
            full = repo.get("full_name") or f"{owner}/{name}"
            if (not name or repo.get("fork") or repo.get("archived") or repo.get("private")
                    or name.lower() == owner.lower() or name.lower() in skip or full.lower() in skip):
                continue
            metrics = {"stars": int(repo.get("stargazers_count") or 0), "language": repo.get("language"),
                       "topics": (repo.get("topics") or [])[:6],
                       "license": (repo.get("license") or {}).get("spdx_id")}
            items.append({
                "key": f"own:{full.lower()}",
                "kind": "own",
                "source": "GitHub (tuyo)",
                "title": full,
                "url": clean_url(repo.get("html_url") or f"https://github.com/{full}"),
                "summary": shorten(repo.get("description"), 300),
                "metrics": metrics,
                "score": 0.9,
                "published": repo.get("created_at"),
            })
            pushed = parse_date(repo.get("pushed_at"))
            if now and pushed and now - pushed > timedelta(days=release_days):
                continue  # sin cambios recientes no puede haber una versión nueva
            releases = get("GET", f"{GITHUB_API}/repos/{full}/releases", params={"per_page": 3},
                           headers=github_headers(token)) or []
            for rel in releases:
                published = parse_date(rel.get("published_at"))
                if rel.get("draft") or rel.get("prerelease") or not published:
                    continue
                if now and now - published > timedelta(days=release_days):
                    continue
                tag = rel.get("tag_name") or ""
                items.append({
                    "key": f"own:{full.lower()}@{tag}",
                    "kind": "own",
                    "source": "GitHub (tuyo)",
                    "title": f"{full} {rel.get('name') or tag}".strip(),
                    "url": clean_url(rel.get("html_url") or f"https://github.com/{full}/releases/tag/{tag}"),
                    "summary": shorten(rel.get("body"), 500),
                    "metrics": {**metrics, "tag": tag},
                    "score": 1.0,
                    "published": published.isoformat(),
                })
    return items


# --- Hacker News --------------------------------------------------------------

def hackernews(now: datetime, min_points: int, hours: int = 48, get: GetJson = http.request_json) -> List[Item]:
    """Historias de las últimas horas con muchos puntos (portada de Hacker News)."""
    since = int((now - timedelta(hours=hours)).timestamp())
    data = get("GET", HN_API, params={"tags": "story", "hitsPerPage": 50,
                                      "numericFilters": f"points>={min_points},created_at_i>={since}"}) or {}
    items: List[Item] = []
    for hit in data.get("hits") or []:
        url, title, story_id = hit.get("url"), hit.get("title"), hit.get("objectID")
        if not url or not title or not story_id or not str(url).startswith(("http://", "https://")):
            continue
        points = int(hit.get("points") or 0)
        items.append({
            "key": f"hn:{story_id}",
            "kind": "news",
            "source": "Hacker News",
            "title": shorten(title, 200),
            "url": clean_url(url),
            "summary": "",
            "metrics": {"points": points, "comments": int(hit.get("num_comments") or 0),
                        "hn_url": f"https://news.ycombinator.com/item?id={story_id}"},
            "score": round(points / 700 * (TECH_BOOST if is_tech(title) else OFF_TOPIC), 3),
            "published": hit.get("created_at"),
        })
    return items


# --- Feeds RSS y Atom ---------------------------------------------------------

def _text(el: Optional[ET.Element]) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_feed(raw: bytes) -> Tuple[str, List[Dict[str, Any]]]:
    """Título del feed y sus entradas (RSS 2.0 o Atom): título, enlace, fecha, resumen e imagen."""
    root = ET.fromstring(raw)
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    channel = root.find("channel")
    channel = channel if channel is not None else root
    entries = list(root.iter("item")) or list(root.iter("entry"))
    out: List[Dict[str, Any]] = []
    for entry in entries:
        link = ""
        for link_el in entry.findall("link"):
            if link_el.get("href") and link_el.get("rel", "alternate") == "alternate":
                link = link_el.get("href", "")
                break
            if (link_el.text or "").strip():
                link = link_el.text.strip()  # type: ignore[union-attr]
                break
        date = next((_text(entry.find(t)) for t in ("pubDate", "published", "updated", "date")
                     if _text(entry.find(t))), "")
        summary = next((_text(entry.find(t)) for t in ("description", "summary", "content", "encoded")
                        if _text(entry.find(t))), "")
        image = None
        for tag in ("content", "thumbnail", "enclosure"):
            for media in entry.findall(tag):
                url = media.get("url")
                kind = media.get("type") or media.get("medium") or ("image" if tag == "thumbnail" else "")
                if url and kind.startswith("image"):
                    image = url
                    break
            if image:
                break
        out.append({"title": _text(entry.find("title")), "link": link, "date": date,
                    "summary": html_to_text(summary), "image": image})
    return _text(channel.find("title")), out


def feed(url: str, now: datetime, days: int = 4, fetch: Fetch = http.fetch_public) -> List[Item]:
    """Entradas recientes del blog de un laboratorio de IA."""
    resp = fetch(url, max_bytes=3_000_000, accept=FEED_ACCEPT)
    channel, entries = parse_feed(resp.body)
    source = shorten(channel, 60) or urllib.parse.urlsplit(url).netloc
    items: List[Item] = []
    for entry in entries:
        published = parse_date(entry["date"])
        link = urllib.parse.urljoin(url, entry["link"]) if entry["link"] else ""
        if not entry["title"] or not link.startswith("https://") or not published:
            continue
        age = now - published
        if age > timedelta(days=days):
            continue
        items.append({
            "key": f"feed:{clean_url(link)}",
            "kind": "ai",
            "source": source,
            "title": shorten(entry["title"], 200),
            "url": clean_url(link),
            "summary": shorten(entry["summary"], 500),
            "image": entry["image"],
            "metrics": {"feed": source},
            "score": 0.75 if age <= timedelta(days=2) else 0.55,
            "published": published.isoformat(),
        })
    return items


# --- Páginas ------------------------------------------------------------------

class _PageParser(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "aside", "template", "button"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: Dict[str, str] = {}
        self.title = ""
        self._in_title = False
        self._skip = 0
        self._depth = {"article": 0, "main": 0}
        self.chunks: Dict[str, List[str]] = {"article": [], "main": [], "body": []}

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == "meta":
            a = dict(attrs)
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"] or ""
            return
        if tag == "title":
            self._in_title = True
        if tag in self.SKIP:
            self._skip += 1
        if tag in self._depth:
            self._depth[tag] += 1

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag == "meta":
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag in self._depth and self._depth[tag]:
            self._depth[tag] -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        text = data.strip()
        if self._skip or not text:
            return
        self.chunks["body"].append(text)
        for zone_name in ("article", "main"):
            if self._depth[zone_name]:
                self.chunks[zone_name].append(text)


def html_to_text(fragment: str) -> str:
    parser = _PageParser()
    try:
        parser.feed(fragment)
        parser.close()
    except Exception:  # HTML roto: mejor sin resumen que sin tema
        return ""
    return shorten(" ".join(parser.chunks["body"]), 5000)


def page_meta(html_text: str, base_url: str = "") -> Dict[str, str]:
    """Título, descripción, imagen (og:image) y texto principal de una página."""
    parser = _PageParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        pass
    meta = parser.meta
    image = meta.get("og:image") or meta.get("twitter:image") or ""
    body = parser.chunks["article"] or parser.chunks["main"] or parser.chunks["body"]
    return {
        "title": shorten(meta.get("og:title") or parser.title, 200),
        "description": shorten(meta.get("og:description") or meta.get("description"), 400),
        "image": urllib.parse.urljoin(base_url, image) if image else "",
        "text": shorten(" ".join(body), 5000),
    }


def _decode(resp: http.Response) -> str:
    m = re.search(r"charset=([\w-]+)", resp.headers.get("content-type", ""), re.I)
    try:
        return resp.body.decode(m.group(1) if m else "utf-8", errors="replace")
    except LookupError:
        return resp.body.decode("utf-8", errors="replace")


def details(item: Item, token: str = "", fetch: Fetch = http.fetch_public,
            request: Callable[..., http.Response] = http.request) -> Dict[str, str]:
    """Material para redactar: el texto de la página o el README, su descripción y su imagen."""
    url = item["url"]
    out = {"text": "", "description": "", "image": item.get("image") or ""}
    try:
        page = page_meta(_decode(fetch(url, max_bytes=1_500_000, accept="text/html")), url)
        out.update(text=page["text"], description=page["description"], image=out["image"] or page["image"])
    except http.HttpError:
        pass
    def raw(api_url: str, headers: Optional[Dict[str, str]] = None) -> str:
        try:
            return _decode(request("GET", api_url, headers=headers, timeout=20, retries=1,
                                   max_bytes=400_000, truncate=True))
        except http.HttpError:
            return ""

    readme = ""
    gh = re.match(r"https://github\.com/([\w.-]+/[\w.-]+)", url)
    hf = re.match(r"https://huggingface\.co/([\w.-]+/[\w.-]+)$", url)
    if gh:
        headers = {**github_headers(token), "Accept": "application/vnd.github.raw"}
        if item["kind"] == "own":  # tus repos tienen README en castellano: mejor material para el post
            readme = raw(f"{GITHUB_API}/repos/{gh.group(1)}/contents/README.es.md", headers)
        readme = readme or raw(f"{GITHUB_API}/repos/{gh.group(1)}/readme", headers)
    elif hf:
        readme = raw(f"https://huggingface.co/{hf.group(1)}/raw/main/README.md")
        readme = re.sub(r"\A---\n.*?\n---\n", "", readme, flags=re.S)  # metadatos YAML de la ficha
    if readme.strip():
        if item["kind"] == "own" and "@" in item["key"] and item.get("summary"):
            readme = f"Notas de la versión: {item['summary']}\n\nREADME:\n{readme}"
        out["text"] = readme.strip()[:6000]
    return out


def metrics_line(item: Item) -> str:
    m = item.get("metrics") or {}
    kind = item["kind"]
    if kind == "model":
        parts = [f"{fmt_num(m.get('likes') or 0)} likes", f"{fmt_num(m.get('downloads') or 0)} descargas",
                 m.get("pipeline")]
    elif kind in ("repo", "own"):
        parts = [f"★ {fmt_num(m.get('stars') or 0)}",
                 f"{fmt_num(m['stars_per_day'])} estrellas al día" if m.get("stars_per_day") else None,
                 m.get("language"), m.get("tag")]
    elif kind == "news":
        parts = [f"{fmt_num(m.get('points') or 0)} puntos y {fmt_num(m.get('comments') or 0)} comentarios en Hacker News"]
    else:
        parts = [item.get("source")]
    return " · ".join(str(p) for p in parts if p)
