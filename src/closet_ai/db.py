"""SQLite schema and connection helpers."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from closet_ai.config import db_path, ensure_dirs

ITEM_TYPES = (
    "shirt",
    "tshirt",
    "polo",
    "full_sleeve_tshirt",
    "pants",
    "shorts",
    "three_fourths",
    "trackpants",
    "jacket",
    "socks",
    "underwear",
)

PATTERNS = (
    "solid",
    "striped",
    "plaid",
    "checked",
    "graphic",
    "floral",
    "other",
)

SLEEVES = (
    "sleeveless",
    "short",
    "three_fourths",
    "long",
    "unknown",
)

_TYPE_CHECK = ",\n".join(f"    '{t}'" for t in ITEM_TYPES)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL CHECK (type IN (
{_TYPE_CHECK}
  )),
  color TEXT NOT NULL DEFAULT '',
  pattern TEXT NOT NULL DEFAULT 'solid',
  sleeve TEXT NOT NULL DEFAULT 'unknown',
  quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 1),
  state TEXT NOT NULL DEFAULT 'available' CHECK (state IN ('available','due')),
  wears_since_wash INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  primary_photo_path TEXT
);

CREATE TABLE IF NOT EXISTS ratings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  dirt INTEGER NOT NULL CHECK (dirt BETWEEN 1 AND 5),
  rated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wear_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  event_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wash_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  washed_at TEXT NOT NULL,
  snapshot_json TEXT
);

CREATE TABLE IF NOT EXISTS item_photos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  embedding BLOB,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ratings_item ON ratings(item_id, rated_at);
CREATE INDEX IF NOT EXISTS idx_wear_events_item ON wear_events(item_id, event_at);
CREATE INDEX IF NOT EXISTS idx_items_state ON items(state);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _type_supported(conn: sqlite3.Connection, type_name: str) -> bool:
    """Return True if current items CHECK accepts type_name."""
    try:
        with conn:
            conn.execute(
                "INSERT INTO items (name, type, created_at) VALUES ('', ?, ?)",
                (type_name, utcnow()),
            )
            conn.execute(
                "DELETE FROM items WHERE id = last_insert_rowid() AND wears_since_wash = 0 AND state = 'available'"
            )
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False


def _rebuild_items_for_types(conn: sqlite3.Connection) -> None:
    """Recreate items so CHECK matches ITEM_TYPES.

    Uses SQLite's recommended replacement sequence (create items_new → copy →
    drop items → rename items_new → items) so child foreign keys are not left
    pointing at a renamed parent table. Fresh DBs never take this path.
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("DROP TABLE IF EXISTS items_new")
        conn.execute(
            f"""
            CREATE TABLE items_new (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL DEFAULT '',
              type TEXT NOT NULL CHECK (type IN (
{_TYPE_CHECK}
              )),
              color TEXT NOT NULL DEFAULT '',
              pattern TEXT NOT NULL DEFAULT 'solid',
              sleeve TEXT NOT NULL DEFAULT 'unknown',
              quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 1),
              state TEXT NOT NULL DEFAULT 'available' CHECK (state IN ('available','due')),
              wears_since_wash INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              primary_photo_path TEXT
            )
            """
        )
        conn.execute(
            """
            INSERT INTO items_new (id, name, type, color, pattern, sleeve, quantity,
                                   state, wears_since_wash, created_at, primary_photo_path)
            SELECT id, name, type, color, pattern, sleeve, quantity,
                   state, wears_since_wash, created_at, primary_photo_path
            FROM items
            """
        )
        conn.execute("DROP TABLE items")
        conn.execute("ALTER TABLE items_new RENAME TO items")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_state ON items(state)")
        # Verify child FKs still resolve (ratings/wear/wash/photos reference items.id)
        for child, col in (
            ("ratings", "item_id"),
            ("wear_events", "item_id"),
            ("wash_events", "item_id"),
            ("item_photos", "item_id"),
        ):
            orphans = conn.execute(
                f"SELECT COUNT(*) AS c FROM {child} "
                f"LEFT JOIN items ON items.id = {child}.{col} "
                f"WHERE {child}.{col} IS NOT NULL AND items.id IS NULL"
            ).fetchone()["c"]
            if orphans:
                raise RuntimeError(
                    f"migration would orphan {orphans} rows in {child}"
                )
        conn.execute("PRAGMA foreign_key_check")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _repair_child_fks(conn: sqlite3.Connection) -> None:
    """Repoint child FKs at `items` if they still reference a dropped `items_old`.

    Older migrations renamed `items` → `items_old` and left child tables
    pointing at the old name; inserts then fail with "no such table: items_old".
    """
    children = ("ratings", "wear_events", "wash_events", "item_photos")
    needs = False
    for name in children:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        if row and row["sql"] and "items_old" in row["sql"]:
            needs = True
            break
    if not needs:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for name in children:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
            if not row or not row["sql"] or "items_old" not in row["sql"]:
                continue
            sql = row["sql"].replace("items_old", "items")
            # CREATE TABLE IF NOT EXISTS name … → rebuild via temp rename
            conn.execute(f"DROP TABLE IF EXISTS {name}_new")
            # sqlite_master sql may lack IF NOT EXISTS; force create new
            create_sql = sql.replace(
                f"CREATE TABLE {name}", f"CREATE TABLE {name}_new", 1
            ).replace(
                f'CREATE TABLE "{name}"', f'CREATE TABLE "{name}_new"', 1
            )
            if f"CREATE TABLE {name}_new" not in create_sql and f'CREATE TABLE "{name}_new"' not in create_sql:
                # fallback: insert IF NOT EXISTS after CREATE TABLE
                create_sql = sql.replace(
                    "CREATE TABLE", "CREATE TABLE", 1
                )
                # last resort: rewrite name
                import re as _re

                create_sql = _re.sub(
                    rf'CREATE TABLE\s+"?{name}"?',
                    f"CREATE TABLE {name}_new",
                    sql,
                    count=1,
                    flags=_re.I,
                )
            conn.execute(create_sql)
            conn.execute(
                f"INSERT INTO {name}_new SELECT * FROM {name}"
            )
            conn.execute(f"DROP TABLE {name}")
            conn.execute(f"ALTER TABLE {name}_new RENAME TO {name}")
        # Rebuild indexes that belonged to children
        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS idx_ratings_item ON ratings(item_id, rated_at)",
            "CREATE INDEX IF NOT EXISTS idx_wear_events_item ON wear_events(item_id, event_at)",
        ):
            conn.execute(idx_sql)
        conn.execute("PRAGMA foreign_key_check")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def init_db() -> None:
    ensure_dirs()
    with connect() as conn:
        conn.executescript(SCHEMA)
        # If any current type is rejected, rebuild items with updated CHECK
        for t in ITEM_TYPES:
            if not _type_supported(conn, t):
                _rebuild_items_for_types(conn)
                break
        _repair_child_fks(conn)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('schema_version', '3') "
            "ON CONFLICT(key) DO UPDATE SET value = '3'"
        )


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def display_name(name: str, type_: str, color: str = "") -> str:
    """Human label when name is optional/empty."""
    name = (name or "").strip()
    if name:
        return name
    base = (type_ or "item").replace("_", " ")
    color = (color or "").strip()
    return f"{color} {base}".strip() if color else base
