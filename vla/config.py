"""Configuración desde variables de entorno (en el VPS, /etc/vertice-linkedin-agent/env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional


class ConfigError(Exception):
    pass


def _get(env: Mapping[str, str], name: str, default: Optional[str] = None) -> Optional[str]:
    value = (env.get(name) or "").strip()
    return value if value else default


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} debe ser un número entero: {raw!r}") from None


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = _get(env, name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "si", "sí", "on")


def _list(env: Mapping[str, str], name: str, default: str = "") -> List[str]:
    return [part.strip() for part in (_get(env, name) or default).split(",") if part.strip()]


# Blogs de los laboratorios de IA (RSS o Atom). Anthropic no publica feed: sus lanzamientos
# llegan por Hacker News.
DEFAULT_FEEDS = ",".join([
    "https://openai.com/news/rss.xml",
    "https://deepmind.google/blog/rss.xml",
    "https://blog.google/technology/ai/rss/",
    "https://huggingface.co/blog/feed.xml",
    "https://mistral.ai/rss.xml",
])

# Variables obligatorias por funcionalidad. `check` y `run` las comprueban.
REQUIRED = {
    "telegram": ("VLA_TELEGRAM_TOKEN", "VLA_TELEGRAM_CHAT_ID"),
    "linkedin": ("VLA_LINKEDIN_CLIENT_ID", "VLA_LINKEDIN_CLIENT_SECRET", "VLA_LINKEDIN_REDIRECT_URI"),
    "profile": ("VLA_OWNER", "VLA_ABOUT"),
}


def load_env_file(path: str, environ: Optional[Dict[str, str]] = None) -> None:
    """Carga un archivo KEY=valor como datos, igual que EnvironmentFile= de systemd.

    No lo interpreta un shell: un valor con $, comillas invertidas o $(…) se queda tal cual.
    """
    target = os.environ if environ is None else environ
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key.replace("_", "").isalnum() or key[0].isdigit():
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            target[key] = value


@dataclass
class Config:
    state_dir: Path
    timezone: str
    dry_run: bool
    telegram_token: str
    telegram_chat_id: str
    linkedin_client_id: str
    linkedin_client_secret: str
    linkedin_redirect_uri: str
    linkedin_version: str
    claude_bin: str
    model: str
    llm_max_calls_per_day: int
    llm_timeout: int
    collect_schedule: str
    draft_schedule: str
    daily_schedule: str
    github_owners: List[str]
    github_token: str
    own_exclude: List[str]
    own_per_week: int
    feeds: List[str]
    hn_min_points: int
    github_min_stars: int
    hf_min_likes: int
    owner: str
    about: str
    site_url: str
    card_brand: str
    images: bool
    chromium: str
    env: Mapping[str, str]

    @property
    def db_path(self) -> Path:
        return self.state_dir / "vla.sqlite3"

    @property
    def sandbox_dir(self) -> Path:
        return self.state_dir / "llm-sandbox"

    @property
    def cards_dir(self) -> Path:
        return self.state_dir / "cards"

    @property
    def site_host(self) -> str:
        """El dominio de VLA_SITE_URL, para el pie de las imágenes."""
        host = self.site_url.split("://", 1)[-1].split("/", 1)[0].lower()
        return host[4:] if host.startswith("www.") else host

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        env = dict(os.environ if env is None else env)
        return cls(
            state_dir=Path(_get(env, "VLA_STATE_DIR", "/var/lib/vertice-linkedin-agent")),
            timezone=_get(env, "VLA_TIMEZONE", "Europe/Madrid"),
            dry_run=_bool(env, "VLA_DRY_RUN"),
            telegram_token=_get(env, "VLA_TELEGRAM_TOKEN", ""),
            telegram_chat_id=_get(env, "VLA_TELEGRAM_CHAT_ID", ""),
            linkedin_client_id=_get(env, "VLA_LINKEDIN_CLIENT_ID", ""),
            linkedin_client_secret=_get(env, "VLA_LINKEDIN_CLIENT_SECRET", ""),
            linkedin_redirect_uri=_get(env, "VLA_LINKEDIN_REDIRECT_URI", ""),
            linkedin_version=_get(env, "VLA_LINKEDIN_VERSION", "202610"),
            claude_bin=_get(env, "VLA_CLAUDE_BIN", "claude"),
            model=_get(env, "VLA_MODEL", "opus"),
            llm_max_calls_per_day=_int(env, "VLA_LLM_MAX_CALLS_PER_DAY", 15),
            llm_timeout=_int(env, "VLA_LLM_TIMEOUT", 600),
            collect_schedule=_get(env, "VLA_COLLECT_SCHEDULE", "every 3h"),
            draft_schedule=_get(env, "VLA_DRAFT_SCHEDULE", "mon,wed,fri 08:30"),
            daily_schedule=_get(env, "VLA_DAILY_SCHEDULE", "09:00"),
            github_owners=_list(env, "VLA_GITHUB_OWNERS"),
            github_token=_get(env, "VLA_GITHUB_TOKEN", ""),
            own_exclude=[name.lower() for name in _list(env, "VLA_OWN_EXCLUDE")],
            own_per_week=_int(env, "VLA_OWN_PER_WEEK", 1),
            feeds=_list(env, "VLA_FEEDS", DEFAULT_FEEDS),
            hn_min_points=_int(env, "VLA_HN_MIN_POINTS", 250),
            github_min_stars=_int(env, "VLA_GITHUB_MIN_STARS", 150),
            hf_min_likes=_int(env, "VLA_HF_MIN_LIKES", 100),
            owner=_get(env, "VLA_OWNER", ""),
            about=_get(env, "VLA_ABOUT", ""),
            site_url=_get(env, "VLA_SITE_URL", "").rstrip("/"),
            card_brand=_get(env, "VLA_CARD_BRAND", _get(env, "VLA_OWNER", "")),
            images=_bool(env, "VLA_IMAGES", True),
            chromium=_get(env, "VLA_CHROMIUM", ""),
            env=env,
        )

    def missing(self, *features: str) -> List[str]:
        """Variables obligatorias sin valor para las funcionalidades pedidas."""
        return [
            name
            for feature in features
            for name in REQUIRED[feature]
            if not _get(self.env, name)
        ]
