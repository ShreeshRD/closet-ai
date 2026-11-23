"""Capture flow: photo in → analysis + identity matches → confirm / same-as."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from PIL import Image

from closet_ai.auth import require_session
from closet_ai.config import images_dir, templates_dir
from closet_ai.db import ITEM_TYPES, PATTERNS, SLEEVES, get_db
from closet_ai.services import catalog, identity, vision

router = APIRouter(tags=["capture"])
templates = Jinja2Templates(directory=str(templates_dir()))

_pending: dict[str, dict[str, Any]] = {}
_PENDING_MAX = 64


def _ctx(request: Request, **extra):
    base = {"item_types": ITEM_TYPES, "patterns": PATTERNS, "sleeves": SLEEVES}
    base.update(extra)
    return base


def _remember(token: str, payload: dict[str, Any]) -> None:
    if len(_pending) >= _PENDING_MAX:
        _pending.pop(next(iter(_pending)), None)
    _pending[token] = payload


@router.get("/capture", response_class=HTMLResponse)
async def capture_page(request: Request):
    require_session(request)
    return templates.TemplateResponse(request, "capture.html", _ctx(request))


@router.post("/api/capture")
async def capture_upload(request: Request, file: UploadFile = File(...)):
    require_session(request)
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(400, "File must be an image")
    data = await file.read()
    if len(data) < 100:
        raise HTTPException(400, "Image too small")
    if len(data) > 12 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 12MB)")

    try:
        analysis = vision.analyze(data)
    except Exception as e:
        raise HTTPException(400, f"Could not read image: {e}")

    rel = identity.save_capture(analysis["image"])
    token = uuid.uuid4().hex[:16]
    _remember(
        token,
        {
            "rel": rel,
            "embedding": analysis["embedding"],
            "color": analysis["color"],
            "color_rgb": analysis["color_rgb"],
        },
    )
    matches = identity.match_existing(analysis["embedding"])
    return {
        "token": token,
        "photo_url": identity.public_image_url(rel),
        "prefill": {
            "color": analysis["color"],
            "color_rgb": list(analysis["color_rgb"]),
            "type": "tshirt",
            "pattern": "solid",
            "sleeve": "short",
            "quantity": 1,
            "name": "",
        },
        "matches": [
            {
                "score": m["score"],
                "item_id": m["item"]["id"],
                "display_name": m["item"]["display_name"],
                "type": m["item"]["type"],
                "state": m["item"]["state"],
            }
            for m in matches
        ],
    }


@router.post("/api/capture/same-as")
async def capture_same_as(
    request: Request,
    token: str = Form(...),
    item_id: int = Form(...),
):
    require_session(request)
    pending = _pending.pop(token, None)
    if not pending:
        raise HTTPException(410, "Capture expired — take a new photo")

    path = identity.absolute_image(pending["rel"])
    if not path.exists():
        raise HTTPException(410, "Capture file missing")
    img = Image.open(path).convert("RGB")

    with get_db() as conn:
        item = catalog.get_item(conn, item_id)
        if not item:
            raise HTTPException(404, "Item not found")
        set_primary = not item.get("primary_photo_path")

    result = identity.attach_photo_to_item(
        item_id, img, set_primary=set_primary, embedding=pending["embedding"]
    )
    return {"ok": True, "item": result["item"]}


@router.post("/api/capture/confirm")
async def capture_confirm(request: Request, payload: dict):
    require_session(request)
    token = str(payload.get("token") or "")
    pending = _pending.pop(token, None)
    if not pending:
        raise HTTPException(410, "Capture expired — take a new photo")

    name = str(payload.get("name") or "").strip()
    type_ = str(payload.get("type") or "")
    color = str(payload.get("color") or "").strip() or pending["color"]
    pattern = str(payload.get("pattern") or "solid")
    sleeve = str(payload.get("sleeve") or "unknown")
    try:
        quantity = max(1, int(payload.get("quantity") or 1))
    except (TypeError, ValueError):
        quantity = 1

    path = identity.absolute_image(pending["rel"])
    if not path.exists():
        raise HTTPException(410, "Capture file missing")
    img = Image.open(path).convert("RGB")

    try:
        with get_db() as conn:
            item = catalog.create_item(
                conn,
                name=name,
                type=type_,
                color=color,
                pattern=pattern,
                sleeve=sleeve,
                quantity=quantity,
            )
            item_id = item["id"]
    except ValueError as e:
        raise HTTPException(400, str(e))

    attached = identity.attach_photo_to_item(
        item_id, img, set_primary=True, embedding=pending["embedding"]
    )
    with get_db() as conn:
        item = catalog.get_item(conn, item_id)
    return {"ok": True, "item": item, "redirect": f"/items/{item['id']}"}


@router.get("/images/{file_path:path}")
async def serve_image(request: Request, file_path: str):
    require_session(request)
    base = images_dir().resolve()
    target = (base / file_path).resolve()
    if not str(target).startswith(str(base)) or not target.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(target)
