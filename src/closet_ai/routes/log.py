"""Undress log + due/wash routes (plan Phase 2)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from closet_ai.auth import require_session
from closet_ai.config import templates_dir
from closet_ai.db import get_db
from closet_ai.services import candidates, logging as wearlog

router = APIRouter(tags=["log"])
templates = Jinja2Templates(directory=str(templates_dir()))


def _ctx(request: Request, **extra):
    base: dict[str, Any] = {}
    base.update(extra)
    return base


@router.get("/log", response_class=HTMLResponse)
async def log_page(request: Request, q: str | None = None):
    """Undress picker: all available tiles (recent dimmed) + backlog."""
    require_session(request)
    with get_db() as conn:
        items = candidates.smart_candidates(conn, q=q)
        backlog = wearlog.pending_backlog(conn)
        due = candidates.due_items(conn)
    return templates.TemplateResponse(
        request,
        "log.html",
        _ctx(
            request,
            items=items,
            backlog=backlog,
            due=due,
            q=q or "",
            flash=None,
        ),
    )


@router.get("/due", response_class=HTMLResponse)
async def due_page(request: Request):
    """Wash queue: due items + mark washed (single / batch)."""
    require_session(request)
    with get_db() as conn:
        due = candidates.due_items(conn)
    return templates.TemplateResponse(
        request, "due.html", _ctx(request, due=due, flash=None)
    )


class LogPayload(BaseModel):
    ratings: dict[str, int] = Field(default_factory=dict)


@router.post("/api/log/undress")
async def api_log_undress(body: LogPayload, request: Request):
    """POST {ratings: {itemId: dirt}} → wear + rating + due refresh."""
    require_session(request)
    try:
        parsed = {int(k): int(v) for k, v in body.ratings.items()}
    except (TypeError, ValueError) as e:
        raise HTTPException(400, "ratings must map item id → 1..5") from e
    if not parsed:
        raise HTTPException(400, "select at least one item")
    for dirt in parsed.values():
        if dirt < 1 or dirt > 5:
            raise HTTPException(400, "dirt must be 1–5")
    try:
        with get_db() as conn:
            results = wearlog.log_undress(conn, parsed)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "results": results}


@router.post("/api/log/undo")
async def api_undo_rating(request: Request, item_id: int = Form(...)):
    require_session(request)
    with get_db() as conn:
        ok = wearlog.undo_last_rating(conn, item_id)
    if not ok:
        raise HTTPException(404, "no rating to undo")
    return {"ok": True}


@router.post("/api/wash")
async def api_mark_washed(request: Request, item_ids: list[int] = Form(...)):
    """Mark one or more items washed (batch from due list)."""
    require_session(request)
    if not item_ids:
        raise HTTPException(400, "no items")
    with get_db() as conn:
        n = wearlog.mark_washed(conn, item_ids)
    return {"ok": True, "washed": n}


@router.post("/api/wash/{item_id}")
async def api_mark_washed_one(request: Request, item_id: int):
    require_session(request)
    with get_db() as conn:
        n = wearlog.mark_washed(conn, [item_id])
    if not n:
        raise HTTPException(404, "Item not found")
    return {"ok": True, "washed": n}


# HTML form posts -----------------------------------------------------------

@router.post("/due/wash")
async def due_wash_form(request: Request, item_ids: list[str] = Form(...)):
    require_session(request)
    ids: list[int] = []
    for v in item_ids:
        try:
            ids.append(int(str(v)))
        except (TypeError, ValueError):
            continue
    if not ids:
        raise HTTPException(400, "no items")
    with get_db() as conn:
        wearlog.mark_washed(conn, ids)
    return RedirectResponse("/due?washed=1", status_code=303)


@router.post("/due/wash/{item_id}")
async def due_wash_one_form(request: Request, item_id: int):
    require_session(request)
    with get_db() as conn:
        wearlog.mark_washed(conn, [item_id])
    referer = request.headers.get("referer") or "/due"
    return RedirectResponse(referer, status_code=303)
