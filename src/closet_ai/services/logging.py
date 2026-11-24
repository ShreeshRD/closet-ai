"""Undress wear log, dirt ratings, undo, wash, backlog, daily backup."""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from closet_ai.config import backups_dir, db_path
from closet_ai.db import display_name, get_db, utcnow
from closet_ai.services import catalog


def log_undress(
    conn: sqlite3.Connection,
    ratings: dict[int, int],
) -> list[dict[str, Any]]:
    """Record undress: +1 wear + dirt rating per item; recompute due.

    `ratings` maps item_id → dirt 1..5. Returns one result per item:
    {item_id, display_name, wears_since_wash, state, due_reason}.
    """
    results: list[dict[str, Any]] = []
    now = utcnow()
    for item_id, dirt in ratings.items():
        dirt = int(dirt)
        if dirt < 1 or dirt > 5:
            raise ValueError("dirt must be 1–5")
        item = catalog.get_item(conn, item_id)
        if not item:
            raise ValueError(f"item {item_id} not found")
        if item["state"] != "available":
            raise ValueError(f"{item['display_name']} is not available to log")

        # Qty pool: one undress = +1 wear on the pool row (not ×quantity).
        # Laundry is a pool-level state — the whole row goes due together when
        # rules trip; individual copies are not tracked separately in v1.
        conn.execute(
            "INSERT INTO wear_events (item_id, event_at) VALUES (?, ?)",
            (item_id, now),
        )
        conn.execute(
            "INSERT INTO ratings (item_id, dirt, rated_at) VALUES (?, ?, ?)",
            (item_id, dirt, now),
        )
        conn.execute(
            "UPDATE items SET wears_since_wash = wears_since_wash + 1 WHERE id = ?",
            (item_id,),
        )
        reason = catalog.refresh_item_state(conn, item_id)
        fresh = catalog.get_item(conn, item_id)
        assert fresh is not None
        qty = int(fresh.get("quantity") or 1)
        results.append(
            {
                "item_id": item_id,
                "display_name": fresh["display_name"],
                "quantity": qty,
                # Explicit: one pool wear, not qty wears
                "wears_added": 1,
                "wears_since_wash": fresh["wears_since_wash"],
                "state": fresh["state"],
                "due_reason": reason if fresh["state"] == "due" else None,
            }
        )
    return results


def undo_last_rating(conn: sqlite3.Connection, item_id: int) -> bool:
    """Remove the latest rating (and its paired wear event if same timestamp)."""
    rating = conn.execute(
        "SELECT id, dirt, rated_at FROM ratings WHERE item_id = ? "
        "ORDER BY rated_at DESC, id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    if not rating:
        return False
    conn.execute("DELETE FROM ratings WHERE id = ?", (rating["id"],))
    # Pair: undress inserts wear then rating with same timestamp string
    wear = conn.execute(
        "SELECT id FROM wear_events WHERE item_id = ? AND event_at = ? "
        "ORDER BY id DESC LIMIT 1",
        (item_id, rating["rated_at"]),
    ).fetchone()
    if wear:
        conn.execute("DELETE FROM wear_events WHERE id = ?", (wear["id"],))
        conn.execute(
            "UPDATE items SET wears_since_wash = MAX(0, wears_since_wash - 1) "
            "WHERE id = ?",
            (item_id,),
        )
    catalog.refresh_item_state(conn, item_id)
    return True


def mark_washed(conn: sqlite3.Connection, item_ids: list[int]) -> int:
    """Reset counters, archive snapshot, set available. Returns count washed."""
    now = utcnow()
    count = 0
    for item_id in item_ids:
        item = catalog.get_item(conn, item_id)
        if not item:
            continue
        wears = conn.execute(
            "SELECT COUNT(*) AS c FROM wear_events WHERE item_id = ?", (item_id,)
        ).fetchone()["c"]
        ratings = conn.execute(
            "SELECT dirt, rated_at FROM ratings WHERE item_id = ? ORDER BY rated_at",
            (item_id,),
        ).fetchall()
        snapshot = {
            "item": {
                k: item[k]
                for k in (
                    "id",
                    "name",
                    "type",
                    "color",
                    "pattern",
                    "sleeve",
                    "quantity",
                    "state",
                    "wears_since_wash",
                    "created_at",
                )
                if k in item
            },
            "wear_count_total": wears,
            "ratings": [{"dirt": r["dirt"], "rated_at": r["rated_at"]} for r in ratings],
            "washed_at": now,
        }
        conn.execute(
            "INSERT INTO wash_events (item_id, washed_at, snapshot_json) VALUES (?, ?, ?)",
            (item_id, now, json.dumps(snapshot)),
        )
        conn.execute(
            "UPDATE items SET wears_since_wash = 0, state = 'available' WHERE id = ?",
            (item_id,),
        )
        catalog.refresh_item_state(conn, item_id)
        count += 1
    return count


def pending_backlog(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Undress events with no rating at/after the event (forgot to rate)."""
    rows = conn.execute(
        """
        SELECT w.id AS wear_id, w.event_at, i.*
        FROM wear_events w
        JOIN items i ON i.id = w.item_id
        WHERE NOT EXISTS (
          SELECT 1 FROM ratings r
          WHERE r.item_id = w.item_id AND r.rated_at >= w.event_at
        )
        ORDER BY w.event_at DESC
        LIMIT 20
        """
    ).fetchall()
    out = []
    seen: set[int] = set()
    for r in rows:
        d = {k: r[k] for k in r.keys()}
        item_id = d.pop("item_id", None) or d.get("id")
        d.pop("wear_id", None)
        d.pop("event_at", None)
        if item_id in seen:
            continue
        seen.add(item_id)
        d["display_name"] = display_name(
            d.get("name", ""), d.get("type", ""), d.get("color", "")
        )
        out.append(d)
    return out


def backup_db() -> Path | None:
    """Copy wardrobe.db to data/backups/wardrobe-YYYY-MM-DD.db (idempotent/day)."""
    src = db_path()
    if not src.is_file():
        return None
    dest_dir = backups_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = dest_dir / f"wardrobe-{day}.db"
    if dest.exists():
        return dest
    # WAL sidecar may exist; checkpoint via sqlite backup API for consistency
    try:
        import sqlite3 as _sq

        src_conn = _sq.connect(str(src))
        dest_conn = _sq.connect(str(dest))
        with dest_conn:
            src_conn.backup(dest_conn)
        dest_conn.close()
        src_conn.close()
    except Exception:
        try:
            shutil.copy2(src, dest)
        except OSError:
            return None
    return dest
