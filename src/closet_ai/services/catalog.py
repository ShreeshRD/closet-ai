"""Catalog item CRUD and listing."""
from __future__ import annotations

import sqlite3
from typing import Any

from closet_ai.db import ITEM_TYPES, PATTERNS, SLEEVES, display_name, get_db, utcnow
from closet_ai.services.wash_rules import DEFAULT_THRESHOLDS, evaluate_due


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = {k: row[k] for k in row.keys()}
    d["display_name"] = display_name(d.get("name", ""), d.get("type", ""), d.get("color", ""))
    return d


def list_items(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM items WHERE 1=1"
    params: list[Any] = []
    if state:
        sql += " AND state = ?"
        params.append(state)
    if q:
        sql += " AND (name LIKE ? OR type LIKE ? OR color LIKE ?)"
        like = f"%{q}%"
        params.extend([like, like, like])
    sql += " ORDER BY created_at DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return _row_to_dict(row) if row else None


def create_item(
    conn: sqlite3.Connection,
    *,
    name: str,
    type: str,
    color: str = "",
    pattern: str = "solid",
    sleeve: str = "unknown",
    quantity: int = 1,
    primary_photo_path: str | None = None,
) -> dict[str, Any]:
    if type not in ITEM_TYPES:
        raise ValueError(f"invalid type: {type}")
    if pattern not in PATTERNS:
        raise ValueError(f"invalid pattern: {pattern}")
    if sleeve not in SLEEVES:
        raise ValueError(f"invalid sleeve: {sleeve}")
    quantity = max(1, int(quantity))
    name = (name or "").strip()  # optional — empty falls back to display_name()

    cur = conn.execute(
        """
        INSERT INTO items (name, type, color, pattern, sleeve, quantity,
                           state, wears_since_wash, created_at, primary_photo_path)
        VALUES (?, ?, ?, ?, ?, ?, 'available', 0, ?, ?)
        """,
        (name, type, color.strip(), pattern, sleeve, quantity, utcnow(), primary_photo_path),
    )
    item = get_item(conn, int(cur.lastrowid))
    assert item is not None
    return item


def update_item(conn: sqlite3.Connection, item_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
    item = get_item(conn, item_id)
    if not item:
        return None

    allowed = {"name", "type", "color", "pattern", "sleeve", "quantity", "primary_photo_path"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return item

    if "type" in updates and updates["type"] not in ITEM_TYPES:
        raise ValueError("invalid type")
    if "pattern" in updates and updates["pattern"] not in PATTERNS:
        raise ValueError("invalid pattern")
    if "sleeve" in updates and updates["sleeve"] not in SLEEVES:
        raise ValueError("invalid sleeve")
    if "quantity" in updates:
        updates["quantity"] = max(1, int(updates["quantity"]))
    if "name" in updates:
        updates["name"] = (str(updates["name"]) or "").strip()

    cols = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE items SET {cols} WHERE id = ?",
        (*updates.values(), item_id),
    )
    return get_item(conn, item_id)


def delete_item(conn: sqlite3.Connection, item_id: int) -> bool:
    cur = conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
    return cur.rowcount > 0


def refresh_item_state(conn: sqlite3.Connection, item_id: int) -> str | None:
    """Recompute due status for one item; return reason if newly due.

    Latest/avg dirt consider only ratings at or after the most recent wash so a
    washed item stays available until new post-wash dirt is recorded.
    """
    item = get_item(conn, item_id)
    if not item:
        return None

    last_wash = conn.execute(
        "SELECT washed_at FROM wash_events WHERE item_id = ? ORDER BY washed_at DESC, id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    last_washed_at = last_wash["washed_at"] if last_wash else None

    if last_washed_at:
        last_rating = conn.execute(
            "SELECT dirt, rated_at FROM ratings WHERE item_id = ? AND rated_at >= ? "
            "ORDER BY rated_at DESC, id DESC LIMIT 1",
            (item_id, last_washed_at),
        ).fetchone()
        rows = conn.execute(
            "SELECT dirt FROM ratings WHERE item_id = ? AND rated_at >= ?",
            (item_id, last_washed_at),
        ).fetchall()
    else:
        last_rating = conn.execute(
            "SELECT dirt, rated_at FROM ratings WHERE item_id = ? "
            "ORDER BY rated_at DESC, id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        rows = conn.execute(
            "SELECT dirt FROM ratings WHERE item_id = ?", (item_id,)
        ).fetchall()
    avg_dirt = (sum(r["dirt"] for r in rows) / len(rows)) if rows else None

    # Per-type threshold overrides from settings (Phase 3); fall back to defaults
    from closet_ai.services import app_settings

    thr = app_settings.effective_thresholds(conn).get(
        item["type"], DEFAULT_THRESHOLDS.get(item["type"], (10, "medium", 60))
    )
    due, reason = evaluate_due(
        item_type=item["type"],
        wears_since_wash=item["wears_since_wash"],
        latest_dirt=last_rating["dirt"] if last_rating else None,
        avg_dirt=avg_dirt,
        rating_count=len(rows),
        last_washed_at=last_washed_at,
        created_at=item["created_at"],
        thresholds=thr,
    )
    new_state = "due" if due else "available"
    if new_state != item["state"]:
        conn.execute(
            "UPDATE items SET state = ? WHERE id = ?", (new_state, item_id)
        )
    return reason if due else None


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    total = conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"]
    due = conn.execute(
        "SELECT COUNT(*) AS c FROM items WHERE state = 'due'"
    ).fetchone()["c"]
    return {"total": total, "due": due, "available": total - due}
