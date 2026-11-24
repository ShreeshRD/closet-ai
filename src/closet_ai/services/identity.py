"""Identity: attach photos/embeddings; match new sightings to catalog items."""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from typing import Any

from closet_ai.config import images_dir
from closet_ai.db import get_db, utcnow
from closet_ai.services import catalog, vision


def capture_dir() -> Path:
    return images_dir() / "captures"


def item_dir(item_id: int) -> Path:
    return images_dir() / "items" / str(item_id)


def save_capture(img, *, prefix: str = "cap") -> str:
    """Persist a working capture image; return path relative to images_dir."""
    name = f"{prefix}_{uuid.uuid4().hex[:12]}.jpg"
    dest = capture_dir() / name
    vision.save_jpeg(img, dest)
    return str(Path("captures") / name)


def absolute_image(rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    return images_dir() / p


def load_item_embeddings(conn: sqlite3.Connection) -> list[tuple[int, bytes]]:
    """Latest embedding per item; prefer the primary photo when present."""
    items = conn.execute(
        "SELECT id, primary_photo_path FROM items"
    ).fetchall()
    out: list[tuple[int, bytes]] = []
    for it in items:
        iid = it["id"]
        emb: bytes | None = None
        if it["primary_photo_path"]:
            row = conn.execute(
                "SELECT embedding FROM item_photos WHERE item_id = ? AND path = ? "
                "AND embedding IS NOT NULL ORDER BY id DESC LIMIT 1",
                (iid, it["primary_photo_path"]),
            ).fetchone()
            if not row:
                # basename match (path may have been rewritten)
                name = Path(it["primary_photo_path"]).name
                row = conn.execute(
                    "SELECT embedding FROM item_photos WHERE item_id = ? "
                    "AND embedding IS NOT NULL AND path LIKE ? ORDER BY id DESC LIMIT 1",
                    (iid, f"%{name}"),
                ).fetchone()
            if row:
                emb = row["embedding"]
        if emb is None:
            row = conn.execute(
                "SELECT embedding FROM item_photos WHERE item_id = ? "
                "AND embedding IS NOT NULL ORDER BY id DESC LIMIT 1",
                (iid,),
            ).fetchone()
            if row:
                emb = row["embedding"]
        if emb:
            out.append((iid, emb))
    return out


def match_existing(emb: bytes) -> list[dict[str, Any]]:
    with get_db() as conn:
        candidates = load_item_embeddings(conn)
        hits = vision.find_matches(emb, candidates, threshold=0.90, limit=3)
        out = []
        for iid, score in hits:
            item = catalog.get_item(conn, iid)
            if item:
                out.append({"score": round(score, 4), "item": item})
        return out


def attach_photo_to_item(
    item_id: int,
    img,
    *,
    set_primary: bool = False,
    embedding: bytes | None = None,
) -> dict[str, Any]:
    """Store photo + embedding on an item; optionally set as primary."""
    emb = embedding if embedding is not None else vision.embedding(img)
    # Save under item folder
    name = f"{uuid.uuid4().hex[:12]}.jpg"
    dest = item_dir(item_id) / name
    vision.save_jpeg(img, dest)
    rel = str(Path("items") / str(item_id) / name)

    with get_db() as conn:
        conn.execute(
            "INSERT INTO item_photos (item_id, path, embedding, created_at) VALUES (?, ?, ?, ?)",
            (item_id, rel, emb, utcnow()),
        )
        if set_primary:
            conn.execute(
                "UPDATE items SET primary_photo_path = ? WHERE id = ?",
                (rel, item_id),
            )
        item = catalog.get_item(conn, item_id)
    return {"path": rel, "item": item}


def public_image_url(rel: str) -> str:
    return f"/images/{Path(rel).as_posix()}"
