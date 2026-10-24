import gzip
import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

RAW_KEEP_DAYS = 30
HISTORY_DAYS = 60
DAY = 86400
GOOD_OUTCOMES = ("ok", "nothing_new")
HASHED_FIELDS = ("text", "title", "price", "images", "city")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
    place TEXT NOT NULL, category TEXT, native_id TEXT, name TEXT, added_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS items (
    source_id INTEGER NOT NULL, item_id TEXT NOT NULL, created_at REAL, content_hash TEXT NOT NULL,
    sold INTEGER, expired INTEGER NOT NULL DEFAULT 0, first_seen REAL NOT NULL, last_seen REAL NOT NULL,
    data TEXT NOT NULL, PRIMARY KEY (source_id, item_id));
CREATE TABLE IF NOT EXISTS comments (
    source_id INTEGER NOT NULL, item_id TEXT NOT NULL, comment_id TEXT NOT NULL, created_at REAL,
    data TEXT NOT NULL, PRIMARY KEY (source_id, comment_id));
CREATE INDEX IF NOT EXISTS comments_by_item ON comments (source_id, item_id);
CREATE TABLE IF NOT EXISTS comment_reads (
    source_id INTEGER NOT NULL, item_id TEXT NOT NULL, comment_count INTEGER, PRIMARY KEY (source_id, item_id));
CREATE TABLE IF NOT EXISTS fetch_state (
    source_id INTEGER PRIMARY KEY, last_outcome TEXT NOT NULL, attempted_at REAL NOT NULL,
    fetched_at REAL, newest_created_at REAL, detail TEXT);
"""


@dataclass(frozen=True)
class Source:
    id: int
    url: str
    kind: str
    place: str
    category: str | None
    native_id: str | None
    name: str | None


@dataclass(frozen=True)
class FetchState:
    last_outcome: str
    attempted_at: float
    fetched_at: float | None
    newest_created_at: float | None
    detail: str | None


def content_hash(item: dict) -> str:
    picked = {k: item.get(k) for k in HASHED_FIELDS}
    return hashlib.sha256(json.dumps(picked, sort_keys=True, default=str).encode()).hexdigest()


class Store:
    """SQLite for sources/items/comments/fetch state, plus gzipped raw replies kept 30 days."""

    def __init__(self, data_dir: Path, clock=time.time):
        self.data_dir = Path(data_dir)
        self.raw_dir = self.data_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.Lock()
        self._raw_seq = 0
        self._db = sqlite3.connect(self.data_dir / "crawler.sqlite3", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def _write(self, sql: str, params=()):
        with self._lock, self._db:
            return self._db.execute(sql, params)

    def _read(self, sql: str, params=()):
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    def add_source(self, url, kind, place, category, native_id=None, name=None) -> Source:
        self._write(
            "INSERT INTO sources (url, kind, place, category, native_id, name, added_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET kind=excluded.kind, place=excluded.place, category=excluded.category",
            (url, kind, place, category, native_id, name, self._clock()),
        )
        return self.get_source(url)

    def get_source(self, url) -> Source | None:
        rows = self._read("SELECT * FROM sources WHERE url = ?", (url,))
        return Source(**{k: rows[0][k] for k in Source.__annotations__}) if rows else None

    def update_source(self, source_id, native_id) -> None:
        self._write("UPDATE sources SET native_id = ? WHERE id = ?", (native_id, source_id))

    def list_sources(self, place=None, category=None) -> list[Source]:
        sql, params = "SELECT * FROM sources WHERE 1=1", []
        if place:
            sql += " AND lower(place) = lower(?)"
            params.append(place)
        if category:
            sql += " AND lower(category) = lower(?)"
            params.append(category)
        rows = self._read(sql + " ORDER BY id", params)
        return [Source(**{k: r[k] for k in Source.__annotations__}) for r in rows]

    def remove_source(self, url) -> bool:
        """Delete the source and everything read from it, so a later source reusing the id starts empty."""
        with self._lock, self._db:
            row = self._db.execute("SELECT id FROM sources WHERE url = ?", (url,)).fetchone()
            if row is None:
                return False
            for table in ("items", "comments", "comment_reads", "fetch_state"):
                self._db.execute(f"DELETE FROM {table} WHERE source_id = ?", (row["id"],))
            self._db.execute("DELETE FROM sources WHERE id = ?", (row["id"],))
        for path in self.raw_dir.glob(f"{row['id']}_*.json.gz"):
            path.unlink(missing_ok=True)
        return True

    def upsert_item(self, source_id, item: dict) -> dict:
        """Store a post or listing; report whether it is new, edited, or flipped sold/available."""
        now = self._clock()
        digest = content_hash(item)
        sold = None if item.get("sold") is None else int(bool(item["sold"]))
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT content_hash, sold FROM items WHERE source_id = ? AND item_id = ?",
                (source_id, item["item_id"]),
            ).fetchone()
            self._db.execute(
                "INSERT INTO items (source_id, item_id, created_at, content_hash, sold, first_seen, last_seen, data) "
                "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_id, item_id) DO UPDATE SET "
                "created_at=excluded.created_at, content_hash=excluded.content_hash, sold=excluded.sold, "
                "last_seen=excluded.last_seen, data=excluded.data",
                (source_id, item["item_id"], item.get("created_at"), digest, sold, now, now,
                 json.dumps(item, default=str)),
            )
        if row is None:
            return {"new": True, "edited": False, "sold_changed": False}
        return {
            "new": False,
            "edited": row["content_hash"] != digest,
            "sold_changed": sold is not None and row["sold"] is not None and row["sold"] != sold,
        }

    def items_for(self, source_id, include_expired=False) -> list[dict]:
        sql = "SELECT data, expired FROM items WHERE source_id = ?" + ("" if include_expired else " AND expired = 0")
        rows = self._read(sql + " ORDER BY created_at DESC", (source_id,))
        return [{**json.loads(r["data"]), "expired": bool(r["expired"])} for r in rows]

    def get_item(self, source_id, item_id) -> dict | None:
        rows = self._read("SELECT data FROM items WHERE source_id = ? AND item_id = ?", (source_id, item_id))
        return json.loads(rows[0]["data"]) if rows else None

    def comments_read_count(self, source_id, item_id) -> int | None:
        """The post's comment count at its last complete comment read."""
        rows = self._read("SELECT comment_count FROM comment_reads WHERE source_id = ? AND item_id = ?",
                          (source_id, item_id))
        return rows[0]["comment_count"] if rows else None

    def set_comments_read(self, source_id, item_id, comment_count) -> None:
        self._write("INSERT INTO comment_reads (source_id, item_id, comment_count) VALUES (?,?,?) "
                    "ON CONFLICT(source_id, item_id) DO UPDATE SET comment_count=excluded.comment_count",
                    (source_id, item_id, comment_count))

    def upsert_comments(self, source_id, item_id, comments: list[dict]) -> None:
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO comments (source_id, item_id, comment_id, created_at, data) VALUES (?,?,?,?,?) "
                "ON CONFLICT(source_id, comment_id) DO UPDATE SET data=excluded.data",
                [(source_id, item_id, c["comment_id"], c.get("created_at"), json.dumps(c)) for c in comments],
            )

    def comments_of_source(self, source_id) -> list[dict]:
        rows = self._read("SELECT data FROM comments WHERE source_id = ? ORDER BY item_id, created_at", (source_id,))
        return [json.loads(r["data"]) for r in rows]

    def comments_for(self, source_id, item_id) -> list[dict]:
        rows = self._read(
            "SELECT data FROM comments WHERE source_id = ? AND item_id = ? ORDER BY created_at", (source_id, item_id)
        )
        return [json.loads(r["data"]) for r in rows]

    def expire_old(self) -> int:
        cutoff = self._clock() - HISTORY_DAYS * DAY
        return self._write(
            "UPDATE items SET expired = 1 WHERE expired = 0 AND created_at IS NOT NULL AND created_at < ?", (cutoff,)
        ).rowcount

    def fetch_state(self, source_id) -> FetchState | None:
        rows = self._read("SELECT * FROM fetch_state WHERE source_id = ?", (source_id,))
        return FetchState(**{k: rows[0][k] for k in FetchState.__annotations__}) if rows else None

    def set_fetch_state(self, source_id, outcome: str, newest_created_at=None, detail=None) -> None:
        now = self._clock()
        good = outcome in GOOD_OUTCOMES
        self._write(
            "INSERT INTO fetch_state (source_id, last_outcome, attempted_at, fetched_at, newest_created_at, detail) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET last_outcome=excluded.last_outcome, "
            "attempted_at=excluded.attempted_at, detail=excluded.detail, "
            "fetched_at=COALESCE(excluded.fetched_at, fetched_at), "
            "newest_created_at=CASE WHEN excluded.newest_created_at IS NULL THEN newest_created_at "
            "ELSE MAX(excluded.newest_created_at, COALESCE(newest_created_at, 0)) END",
            (source_id, str(outcome), now, now if good else None, newest_created_at, detail),
        )

    def save_raw(self, source_id, label: str, text: str) -> Path:
        with self._lock:
            self._raw_seq += 1
            name = f"{source_id}_{label.replace("_", "-")}_{int(self._clock() * 1000)}_{self._raw_seq}.json.gz"
        path = self.raw_dir / name
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(text)
        return path

    def _raw_files(self):
        """(saved_at, source_id, label, path) for every raw reply, oldest first."""
        files = []
        for path in self.raw_dir.glob("*.json.gz"):
            source_id, label, millis, _ = path.name.split("_", 3)
            files.append((int(millis) / 1000, int(source_id), label, path))
        return sorted(files)

    def iter_raw(self, since: float | None = None):
        """Yield (source_id, label, text) for kept raw replies, oldest first."""
        for saved_at, source_id, label, path in self._raw_files():
            if since is not None and saved_at < since:
                continue
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    text = f.read()
            except FileNotFoundError:
                continue
            yield source_id, label, text

    def prune_raw(self) -> int:
        cutoff = self._clock() - RAW_KEEP_DAYS * DAY
        old = [path for saved_at, _, _, path in self._raw_files() if saved_at < cutoff]
        for p in old:
            p.unlink(missing_ok=True)
        return len(old)
