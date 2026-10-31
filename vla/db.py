"""Estado persistente en SQLite: temas encontrados, posts, uso de Claude y trabajos."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    summary TEXT,
    image TEXT,
    metrics TEXT,
    score REAL NOT NULL DEFAULT 0,
    published TEXT,
    found TEXT NOT NULL,
    seen TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS items_url ON items (url);
CREATE INDEX IF NOT EXISTS items_status ON items (status, kind);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    status TEXT NOT NULL,
    text TEXT NOT NULL,
    why TEXT,
    feedback TEXT,
    message_id INTEGER,
    linkedin_urn TEXT,
    published_at TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    task TEXT NOT NULL,
    ok INTEGER NOT NULL,
    duration_ms INTEGER,
    cost_usd REAL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    name TEXT PRIMARY KEY,
    last_run TEXT,
    ok INTEGER,
    error TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
"""

# Un post está abierto mientras espera tu decisión o se está publicando.
OPEN_STATUSES = ("review", "publishing", "failed")
JSON_FIELDS = ("metrics", "feedback")


def _encode(fields: Dict[str, Any]) -> Dict[str, Any]:
    return {k: json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and v is not None else v
            for k, v in fields.items()}


def _decode(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    for key in JSON_FIELDS:
        if row.get(key):
            row[key] = json.loads(row[key])
    return row


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def init(self) -> None:
        self.conn().executescript(SCHEMA)
        self.conn().commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        conn = self.conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.conn().execute(sql, params).fetchall()]

    def one(self, sql: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        row = self.conn().execute(sql, params).fetchone()
        return dict(row) if row else None

    # --- meta ---------------------------------------------------------------

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any) -> None:
        if value is None:
            self.execute("DELETE FROM meta WHERE key = ?", (key,))
            return
        self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def log_event(self, at: datetime, kind: str, detail: Any = None) -> None:
        text = detail if isinstance(detail, str) or detail is None else json.dumps(detail, ensure_ascii=False)
        self.execute("INSERT INTO events (at, kind, detail) VALUES (?, ?, ?)", (at.isoformat(), kind, text))

    # --- temas --------------------------------------------------------------

    def upsert_item(self, item: Dict[str, Any], at: datetime) -> Tuple[Optional[int], bool]:
        """Guarda un tema. Devuelve (id, nuevo). Si ya existe, actualiza sus cifras (no su estado).
        Si la misma URL ya llegó por otra fuente, no se duplica: (None, False)."""
        existing = self.one("SELECT id, image, summary FROM items WHERE key = ?", (item["key"],))
        if existing:
            self.execute(
                "UPDATE items SET metrics = ?, score = ?, seen = ?, image = ?, summary = ? WHERE id = ?",
                (json.dumps(item.get("metrics") or {}, ensure_ascii=False), float(item.get("score") or 0),
                 at.isoformat(), item.get("image") or existing["image"], item.get("summary") or existing["summary"],
                 existing["id"]),
            )
            return int(existing["id"]), False
        if self.one("SELECT id FROM items WHERE url = ?", (item["url"],)):
            return None, False
        data = _encode({
            "key": item["key"], "kind": item["kind"], "source": item["source"], "title": item["title"],
            "url": item["url"], "summary": item.get("summary"), "image": item.get("image"),
            "metrics": item.get("metrics") or {}, "score": float(item.get("score") or 0),
            "published": item.get("published"), "found": at.isoformat(), "seen": at.isoformat(),
            "status": item.get("status") or "new",
        })
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        cur = self.execute(f"INSERT INTO items ({cols}) VALUES ({marks})", list(data.values()))
        return int(cur.lastrowid), True

    def item(self, item_id: int) -> Optional[Dict[str, Any]]:
        return _decode(self.one("SELECT * FROM items WHERE id = ?", (item_id,)))

    def set_item_status(self, item_id: int, status: str) -> None:
        self.execute("UPDATE items SET status = ? WHERE id = ?", (status, item_id))

    def candidates(self, since: str) -> List[Dict[str, Any]]:
        """Temas sin usar: los de fuera, encontrados desde `since`; los tuyos, siempre."""
        rows = self.query(
            "SELECT * FROM items WHERE status = 'new' AND (kind = 'own' OR found >= ?) ORDER BY score DESC, id DESC",
            (since,),
        )
        return [_decode(r) for r in rows]  # type: ignore[misc]

    def set_item_image(self, item_id: int, image: str) -> None:
        self.execute("UPDATE items SET image = ? WHERE id = ?", (image, item_id))

    def own_items(self) -> List[Dict[str, Any]]:
        return [_decode(r) for r in self.query(  # type: ignore[misc]
            "SELECT * FROM items WHERE kind = 'own' AND status != 'old' ORDER BY published DESC, id DESC")]

    def retire_items(self, before: str) -> int:
        """Los temas de fuera que nadie ha usado en semanas ya no son novedad."""
        return self.execute(
            "UPDATE items SET status = 'old' WHERE status = 'new' AND kind != 'own' AND found < ?", (before,)
        ).rowcount

    def item_counts(self, since: str) -> Dict[str, int]:
        rows = self.query(
            "SELECT kind, COUNT(*) AS n FROM items WHERE status = 'new' AND (kind = 'own' OR found >= ?) GROUP BY kind",
            (since,),
        )
        return {r["kind"]: int(r["n"]) for r in rows}

    # --- posts --------------------------------------------------------------

    def add_post(self, at: datetime, item_id: int, text: str, why: str,
                 feedback: Optional[List[str]] = None) -> int:
        data = _encode({"item_id": item_id, "created": at.isoformat(), "updated": at.isoformat(),
                        "status": "review", "text": text, "why": why, "feedback": feedback or []})
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        return int(self.execute(f"INSERT INTO posts ({cols}) VALUES ({marks})", list(data.values())).lastrowid)

    def update_post(self, post_id: int, at: datetime, **fields: Any) -> None:
        data = _encode({**fields, "updated": at.isoformat()})
        sets = ", ".join(f"{k} = ?" for k in data)
        self.execute(f"UPDATE posts SET {sets} WHERE id = ?", [*data.values(), post_id])

    def post(self, post_id: int) -> Optional[Dict[str, Any]]:
        return _decode(self.one("SELECT * FROM posts WHERE id = ?", (post_id,)))

    def open_post(self) -> Optional[Dict[str, Any]]:
        marks = ", ".join("?" for _ in OPEN_STATUSES)
        return _decode(self.one(f"SELECT * FROM posts WHERE status IN ({marks}) ORDER BY id DESC LIMIT 1",
                                OPEN_STATUSES))

    def recent_posts(self, since: str, statuses: Iterable[str] = ("published",)) -> List[Dict[str, Any]]:
        """Posts (con su tema) creados desde `since`, del más nuevo al más antiguo."""
        statuses = tuple(statuses)
        marks = ", ".join("?" for _ in statuses)
        rows = self.query(
            f"SELECT p.*, i.kind, i.title AS item_title, i.url AS item_url FROM posts p "
            f"JOIN items i ON i.id = p.item_id WHERE p.created >= ? AND p.status IN ({marks}) ORDER BY p.id DESC",
            (since, *statuses),
        )
        return [_decode(r) for r in rows]  # type: ignore[misc]

    # --- uso de la IA -------------------------------------------------------

    def log_llm(self, at: datetime, task: str, ok: bool, duration_ms: int,
                cost_usd: Optional[float], error: Optional[str]) -> None:
        self.execute(
            "INSERT INTO llm_calls (at, task, ok, duration_ms, cost_usd, error) VALUES (?, ?, ?, ?, ?, ?)",
            (at.isoformat(), task, int(ok), duration_ms, cost_usd, error),
        )

    def llm_calls_on(self, day_iso: str) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM llm_calls WHERE substr(at, 1, 10) = ?", (day_iso,))
        return int(row["n"]) if row else 0

    # --- trabajos programados ----------------------------------------------

    def job(self, name: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM jobs WHERE name = ?", (name,))

    def set_job(self, name: str, last_run: datetime, ok: Optional[bool] = None, error: Optional[str] = None) -> None:
        self.execute(
            "INSERT INTO jobs (name, last_run, ok, error) VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
            "last_run = excluded.last_run, ok = excluded.ok, error = excluded.error",
            (name, last_run.isoformat(), None if ok is None else int(ok), error),
        )
