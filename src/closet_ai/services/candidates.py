"""Smart undress candidates (locked design §3.6)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from closet_ai.db import display_name
from closet_ai.services import catalog

# Items worn within this window stay visible but dimmed in the log grid
RECENT_WEAR_HOURS = 12


def _recent_cutoff() -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=RECENT_WEAR_HOURS)
    ).isoformat()


def _enrich(row: sqlite3.Row) -> dict[str, Any]:
    d = {k: row[k] for k in row.keys()}
    d["display_name"] = display_name(
        d.get("name", ""), d.get("type", ""), d.get("color", "")
    )
    return d


def smart_candidates(
    conn: sqlite3.Connection, *, q: str | None = None
) -> list[dict[str, Any]]:
    """All available items for the log grid.

    Worn within RECENT_WEAR_HOURS → `is_recent=True` (UI dims the tile).
    Fresh items sort first, then least-worn.
    """
    for item in catalog.list_items(conn):
        catalog.refresh_item_state(conn, item["id"])

    cutoff = _recent_cutoff()
    query = (q or "").strip()
    like = f"%{query}%"
    rows = conn.execute(
        """
        SELECT i.*,
               (SELECT MAX(w.event_at) FROM wear_events w
                 WHERE w.item_id = i.id AND w.event_at >= ?) AS recent_at
        FROM items i
        WHERE i.state = 'available'
          AND (? = '' OR i.name LIKE ? OR i.type LIKE ? OR i.color LIKE ?)
        ORDER BY
          CASE WHEN recent_at IS NULL THEN 0 ELSE 1 END,
          i.wears_since_wash ASC,
          i.created_at DESC,
          i.id DESC
        """,
        [cutoff, query, like, like, like],
    ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        d = _enrich(r)
        recent_at = d.pop("recent_at", None)
        d["is_recent"] = bool(recent_at)
        d["last_worn_at"] = recent_at
        out.append(d)
    return out


def due_items(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    for item in catalog.list_items(conn):
        catalog.refresh_item_state(conn, item["id"])
    out: list[dict[str, Any]] = []
    for r in conn.execute(
        "SELECT * FROM items WHERE state = 'due' ORDER BY created_at DESC"
    ).fetchall():
        d = _enrich(r)
        wash = conn.execute(
            "SELECT washed_at FROM wash_events WHERE item_id = ? "
            "ORDER BY washed_at DESC, id DESC LIMIT 1",
            (d["id"],),
        ).fetchone()
        d["last_washed_at"] = wash["washed_at"] if wash else None
        out.append(d)
    return out
