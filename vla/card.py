"""La imagen de cada post: una plantilla HTML con tu estilo que Chromium convierte en un PNG de 1080 × 1350.

Claude no dibuja: elige el diseño y escribe lo que va en la imagen (titular, la parte que se resalta,
la explicación, los puntos o la cifra). Aquí se recorta a lo que cabe, se escapa y se pinta.
La página no tiene JavaScript ni red: Chromium solo hace una captura.
"""

from __future__ import annotations

import base64
import html
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unicodedata
import urllib.parse
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .util import child_env

HERE = Path(__file__).parent / "cards"
WIDTH, HEIGHT = 1080, 1350
LAYOUTS = ("novedad", "lista", "dato", "frase")
THEMES = ("oscuro", "claro")
MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
PNG = b"\x89PNG\r\n\x1a\n"

# Chromium sin interfaz: el paquete chromium-headless de EPEL instala headless_shell.
CHROMIUM_CANDIDATES = (
    "/usr/lib64/chromium-browser/headless_shell",
    "chromium-headless-shell",
    "headless_shell",
    "chromium-browser",
    "chromium",
    "google-chrome-stable",
    "google-chrome",
)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "layout": {"type": "string", "enum": list(LAYOUTS)},
        "theme": {"type": "string", "enum": list(THEMES)},
        "title": {"type": "string"},
        "highlight": {"type": "string"},
        "body": {"type": "string"},
        "points": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "stat": {
            "type": "object",
            "properties": {"value": {"type": "string"}, "label": {"type": "string"}},
            "required": ["value", "label"],
            "additionalProperties": False,
        },
        "alt": {"type": "string"},
    },
    "required": ["layout", "theme", "title", "highlight", "body", "points", "stat", "alt"],
    "additionalProperties": False,
}


class CardError(Exception):
    pass


def one_line(value: Any) -> str:
    """Texto de una línea: sin markdown, sin emojis (la fuente no los tiene) y sin espacios de sobra."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", str(value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("So", "Cs", "Co", "Cn")
                   and ch not in "️‍")
    return re.sub(r"\s+", " ", text).strip()


def clean_spec(raw: Any) -> Tuple[Dict[str, Any], List[str]]:
    """La imagen que propone Claude, con solo los campos de su diseño. Devuelve (imagen, problemas)."""
    raw = raw if isinstance(raw, dict) else {}
    problems: List[str] = []
    layout = one_line(raw.get("layout")).lower()
    if layout not in LAYOUTS:
        problems.append(f"layout {layout!r} no es uno de {', '.join(LAYOUTS)}")
        layout = "frase"
    theme = one_line(raw.get("theme")).lower()
    theme = theme if theme in THEMES else THEMES[0]
    title = one_line(raw.get("title"))
    if not 8 <= len(title) <= 90:
        problems.append(f"el titular tiene {len(title)} caracteres y va de 8 a 90")
    highlight = one_line(raw.get("highlight"))
    if highlight not in title:  # si no es un trozo literal del titular, no se resalta nada
        highlight = ""
    spec: Dict[str, Any] = {"layout": layout, "theme": theme, "title": title, "highlight": highlight}

    body = one_line(raw.get("body"))
    if layout == "novedad":
        if not 40 <= len(body) <= 230:
            problems.append(f"«body» tiene {len(body)} caracteres y en «novedad» va de 40 a 230")
        spec["body"] = body
    elif layout == "frase":
        if len(body) > 150:
            problems.append(f"«body» tiene {len(body)} caracteres y en «frase» son 150 como mucho")
        spec["body"] = body
    elif layout == "lista":
        points = [p for p in (one_line(p) for p in (raw.get("points") or [])) if p]
        if not 3 <= len(points) <= 4:
            problems.append(f"la lista tiene {len(points)} puntos y van de 3 a 4")
        if any(len(p) > 80 for p in points):
            problems.append("cada punto de la lista tiene 80 caracteres como mucho")
        spec["points"] = points[:4]
    elif layout == "dato":
        stat = raw.get("stat") if isinstance(raw.get("stat"), dict) else {}
        value, label = one_line(stat.get("value")), one_line(stat.get("label"))
        if not 1 <= len(value) <= 12:
            problems.append(f"la cifra tiene {len(value)} caracteres y va de 1 a 12")
        if not 3 <= len(label) <= 70:
            problems.append(f"la etiqueta de la cifra tiene {len(label)} caracteres y va de 3 a 70")
        spec["stat"] = {"value": value, "label": label}
    spec["alt"] = (one_line(raw.get("alt")) or title)[:300]
    return spec, problems


def short_source(url: str) -> str:
    """De dónde sale el tema, para el pie: el dominio y, en GitHub o Hugging Face, el repo."""
    parts = urllib.parse.urlsplit(url or "")
    host = parts.netloc.lower().removeprefix("www.")
    path = [p for p in parts.path.split("/") if p]
    if host in ("github.com", "huggingface.co") and len(path) >= 2:
        return f"{host}/{path[0]}/{path[1]}"
    return host


def human_date(day: date) -> str:
    return f"{day.day} {MONTHS[day.month - 1]} {day.year}"


def _size(text: str, steps: Sequence[Tuple[int, str]], last: str) -> str:
    for limit, cls in steps:
        if len(text) <= limit:
            return cls
    return last


def _title(spec: Dict[str, Any], cls: str) -> str:
    title, mark = spec["title"], spec.get("highlight") or ""
    if mark:
        before, _, after = title.partition(mark)
        inner = f"{html.escape(before)}<span class=\"hl\">{html.escape(mark)}</span>{html.escape(after)}"
    else:
        inner = html.escape(title)
    return f"<h1 class=\"{cls}\">{inner}</h1>"


@lru_cache(maxsize=1)
def _assets() -> Tuple[str, str]:
    fonts = "".join(
        f"@font-face{{font-family:\"{family}\";font-weight:100 900;font-display:block;"
        f"src:url(data:font/woff2;base64,{base64.b64encode((HERE / name).read_bytes()).decode('ascii')}) "
        f"format(\"woff2\")}}"
        for family, name in (("Geist", "geist-variable.woff2"), ("Geist Mono", "geist-mono-variable.woff2"))
    )
    return fonts + (HERE / "card.css").read_text(encoding="utf-8"), (HERE / "logo.svg").read_text(encoding="utf-8")


def html_for(spec: Dict[str, Any], *, eyebrow: str, source: str, brand: str, site: str, day: date) -> str:
    """La página de la imagen. Todo lo que viene de Claude o de la fuente va escapado."""
    css, logo = _assets()
    layout, e = spec["layout"], html.escape
    if layout == "novedad":
        content = (_title(spec, _size(spec["title"], ((28, "s2"), (48, "s3"), (68, "s4")), "s5"))
                   + f"<div class=\"note\"><div class=\"label\">Qué significa</div><p>{e(spec['body'])}</p></div>")
    elif layout == "lista":
        items = "".join(f"<li><span class=\"n\">{i:02d}</span><span>{e(p)}</span></li>"
                        for i, p in enumerate(spec.get("points") or [], 1))
        content = _title(spec, _size(spec["title"], ((36, "s4"), (60, "s5")), "s6")) + f"<ol class=\"points\">{items}</ol>"
    elif layout == "dato":
        value = spec["stat"]["value"]
        content = (f"<div><div class=\"value {_size(value, ((4, 'v1'), (6, 'v2'), (9, 'v3')), 'v4')}\">{e(value)}</div>"
                   f"<div class=\"value-label\">{e(spec['stat']['label'])}</div></div>"
                   + _title(spec, _size(spec["title"], ((40, "s5"),), "s6")))
    else:
        content = _title(spec, _size(spec["title"], ((30, "s1"), (52, "s2"), (72, "s3")), "s4"))
        if spec.get("body"):
            content += f"<p class=\"sub\">{e(spec['body'])}</p>"
    theme = "claro" if spec.get("theme") == "claro" else "oscuro"
    if source == site:  # un tema de tu propia web: el pie no repite el dominio
        source = ""
    return (
        "<!doctype html><html lang=\"es\"><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
        "style-src 'unsafe-inline'; font-src data:; img-src data:\">"
        f"<style>{css}</style></head><body class=\"{theme}\"><div class=\"card\">"
        f"<div class=\"top\"><div class=\"brand\">{logo}<span>{e(brand)}</span></div>"
        f"<div class=\"mono\">{e(human_date(day))}</div></div>"
        f"<div class=\"main\"><div class=\"eyebrow\">{e(eyebrow)}</div>{content}</div>"
        f"<div class=\"foot mono\"><span class=\"source\">{e(source)}</span><span class=\"site\">{e(site)}</span></div>"
        "</div></body></html>"
    )


def find_chromium(configured: str = "") -> Optional[str]:
    """El Chromium con el que se hacen las capturas: VLA_CHROMIUM o el primero que haya instalado."""
    for candidate in ((configured,) if configured else CHROMIUM_CANDIDATES):
        if os.path.isabs(candidate):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def png_size(data: bytes) -> Tuple[int, int]:
    if not data.startswith(PNG) or len(data) < 24:
        return 0, 0
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def render(page: str, chromium: str, timeout: int = 90) -> bytes:
    """Captura la página con Chromium sin interfaz. Sin red (todo va en la página) y sin JavaScript
    (la CSP de la página, default-src 'none', no deja ejecutar scripts).

    Va con --no-sandbox porque el servicio de systemd no deja crear los namespaces del sandbox de
    Chromium; lo que pinta es una plantilla propia con el texto escapado, sin scripts ni red.
    """
    with tempfile.TemporaryDirectory(prefix="vla-card-") as tmp:
        src = Path(tmp) / "card.html"
        out = Path(tmp) / "card.png"
        src.write_text(page, encoding="utf-8")
        cmd = [
            chromium, "--headless", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--mute-audio",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync",
            "--disable-background-networking", "--disable-component-update", "--disable-default-apps",
            "--disable-dev-shm-usage", "--host-resolver-rules=MAP * ~NOTFOUND", "--force-device-scale-factor=1",
            f"--window-size={WIDTH},{HEIGHT}", f"--user-data-dir={Path(tmp) / 'profile'}",
            f"--screenshot={out}", src.as_uri(),
        ]
        env = child_env()
        env["HOME"] = tmp
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env, cwd=tmp)
        except subprocess.TimeoutExpired:
            raise CardError(f"Chromium no ha terminado en {timeout} s") from None
        except OSError as exc:
            raise CardError(f"no puedo ejecutar Chromium: {exc}") from None
        data = out.read_bytes() if out.exists() else b""
        if png_size(data) != (WIDTH, HEIGHT):
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
            raise CardError(f"Chromium no ha hecho la captura ({png_size(data)}, código {proc.returncode}): "
                            + " | ".join(detail)[:400])
        return data
