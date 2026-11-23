"""Item catalog routes (Phase 0 CRUD)."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from closet_ai.auth import require_session
from closet_ai.config import templates_dir
from closet_ai.db import ITEM_TYPES, PATTERNS, SLEEVES, get_db
from closet_ai.services import catalog

router = APIRouter(tags=["items"])
templates = Jinja2Templates(directory=str(templates_dir()))


def _ctx(request: Request, **extra):
    base = {"item_types": ITEM_TYPES, "patterns": PATTERNS, "sleeves": SLEEVES}
    base.update(extra)
    return base


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    require_session(request)
    with get_db() as conn:
        for item in catalog.list_items(conn):
            catalog.refresh_item_state(conn, item["id"])
        items = catalog.list_items(conn)
        st = catalog.stats(conn)
        due = catalog.list_items(conn, state="due")
    return templates.TemplateResponse(
        request,
        "home.html",
        _ctx(request, items=items, due=due, stats=st),
    )


@router.get("/items/new", response_class=HTMLResponse)
async def new_item_page(request: Request):
    require_session(request)
    return templates.TemplateResponse(
        request,
        "item_form.html",
        _ctx(request, item=None, error=None),
    )


@router.post("/items/new")
async def create_item(
    request: Request,
    name: str = Form(""),
    type: str = Form(...),
    color: str = Form(""),
    pattern: str = Form("solid"),
    sleeve: str = Form("unknown"),
    quantity: int = Form(1),
):
    require_session(request)
    try:
        with get_db() as conn:
            item = catalog.create_item(
                conn,
                name=name,
                type=type,
                color=color,
                pattern=pattern,
                sleeve=sleeve,
                quantity=quantity,
            )
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "item_form.html",
            _ctx(request, item=None, error=str(e)),
            status_code=400,
        )
    return RedirectResponse(f"/items/{item['id']}", status_code=303)


@router.get("/items/{item_id}", response_class=HTMLResponse)
async def item_detail(request: Request, item_id: int):
    require_session(request)
    with get_db() as conn:
        item = catalog.get_item(conn, item_id)
        if not item:
            raise HTTPException(404, "Item not found")
        catalog.refresh_item_state(conn, item_id)
        item = catalog.get_item(conn, item_id)
        ratings = conn.execute(
            "SELECT * FROM ratings WHERE item_id = ? ORDER BY rated_at DESC, id DESC LIMIT 20",
            (item_id,),
        ).fetchall()
        wears = conn.execute(
            "SELECT * FROM wear_events WHERE item_id = ? ORDER BY event_at DESC, id DESC LIMIT 20",
            (item_id,),
        ).fetchall()
        washes = conn.execute(
            "SELECT * FROM wash_events WHERE item_id = ? ORDER BY washed_at DESC, id DESC LIMIT 10",
            (item_id,),
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "item_detail.html",
        _ctx(
            request,
            item=item,
            ratings=ratings,
            wears=wears,
            washes=washes,
            error=None,
        ),
    )


@router.get("/items/{item_id}/edit", response_class=HTMLResponse)
async def edit_item_page(request: Request, item_id: int):
    require_session(request)
    with get_db() as conn:
        item = catalog.get_item(conn, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    return templates.TemplateResponse(
        request,
        "item_form.html",
        _ctx(request, item=item, error=None),
    )


@router.post("/items/{item_id}/edit")
async def update_item(
    request: Request,
    item_id: int,
    name: str = Form(""),
    type: str = Form(...),
    color: str = Form(""),
    pattern: str = Form("solid"),
    sleeve: str = Form("unknown"),
    quantity: int = Form(1),
):
    require_session(request)
    try:
        with get_db() as conn:
            item = catalog.update_item(
                conn,
                item_id,
                {
                    "name": name,
                    "type": type,
                    "color": color,
                    "pattern": pattern,
                    "sleeve": sleeve,
                    "quantity": quantity,
                },
            )
            if not item:
                raise HTTPException(404, "Item not found")
            catalog.refresh_item_state(conn, item_id)
    except ValueError as e:
        with get_db() as conn:
            item = catalog.get_item(conn, item_id)
        return templates.TemplateResponse(
            request,
            "item_form.html",
            _ctx(request, item=item, error=str(e)),
            status_code=400,
        )
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@router.post("/items/{item_id}/delete")
async def delete_item(request: Request, item_id: int):
    require_session(request)
    with get_db() as conn:
        catalog.delete_item(conn, item_id)
    return RedirectResponse("/", status_code=303)


class ItemCreateJSON(BaseModel):
    name: str = Field(default="", max_length=80)
    type: str
    color: str = ""
    pattern: str = "solid"
    sleeve: str = "unknown"
    quantity: int = Field(default=1, ge=1)


@router.post("/api/items")
async def create_item_api(body: ItemCreateJSON):
    """JSON API for future capture flow / tests."""
    try:
        with get_db() as conn:
            item = catalog.create_item(
                conn,
                name=body.name,
                type=body.type,
                color=body.color,
                pattern=body.pattern,
                sleeve=body.sleeve,
                quantity=body.quantity,
            )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return item


@router.get("/api/items")
async def list_items_api(state: str | None = None, q: str | None = None):
    with get_db() as conn:
        return catalog.list_items(conn, state=state, q=q)


@router.get("/api/items/{item_id}")
async def get_item_api(item_id: int):
    with get_db() as conn:
        item = catalog.get_item(conn, item_id)
    if not item:
        raise HTTPException(404, "Item not found")
    return item
