"""Settings UI: wash thresholds + climate_hint (Phase 3)."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from closet_ai.auth import require_session
from closet_ai.config import templates_dir
from closet_ai.db import ITEM_TYPES, get_db
from closet_ai.services import app_settings, catalog
from closet_ai.services.wash_rules import (
    AVG_DIRT_THRESHOLD,
    DEFAULT_THRESHOLDS,
    LATEST_DIRT_THRESHOLD,
    MIN_RATINGS_FOR_AVG,
)

router = APIRouter(tags=["settings"])
templates = Jinja2Templates(directory=str(templates_dir()))


def _ctx(request: Request, **extra):
    base = {
        "item_types": ITEM_TYPES,
        "avg_threshold": AVG_DIRT_THRESHOLD,
        "latest_threshold": LATEST_DIRT_THRESHOLD,
        "min_ratings": MIN_RATINGS_FOR_AVG,
    }
    base.update(extra)
    return base


def _recompute_all(conn) -> None:
    for row in conn.execute("SELECT id FROM items").fetchall():
        catalog.refresh_item_state(conn, row["id"])


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str | None = None, error: str | None = None):
    require_session(request)
    with get_db() as conn:
        rows = app_settings.rows_for_form(conn)
        climate = app_settings.climate_hint_enabled(conn)
    return templates.TemplateResponse(
        request,
        "settings.html",
        _ctx(request, rows=rows, climate=climate, saved=saved, error=error),
    )


@router.post("/settings/thresholds")
async def save_thresholds(request: Request):
    """Persist per-type overrides only when they differ from DEFAULT_THRESHOLDS."""
    require_session(request)
    form = await request.form()
    clean: dict[str, dict[str, int]] = {}
    try:
        for t in ITEM_TYPES:
            d = DEFAULT_THRESHOLDS.get(t, (10, "medium", 60))
            entry: dict[str, int] = {}
            mw_raw = str(form.get(f"max_wears_{t}", "") or "").strip()
            dc_raw = str(form.get(f"days_cap_{t}", "") or "").strip()
            if mw_raw:
                mw = int(mw_raw)
                if mw != d[0]:
                    entry["max_wears"] = mw
            if dc_raw:
                dc = int(dc_raw)
                if dc != d[2]:
                    entry["days_cap"] = dc
            if entry:
                clean[t] = entry
        with get_db() as conn:
            app_settings.save_threshold_overrides(conn, clean)
            _recompute_all(conn)
    except ValueError as e:
        return RedirectResponse(f"/settings?error={str(e)[:80]}", status_code=303)
    return RedirectResponse("/settings?saved=thresholds", status_code=303)


@router.post("/settings/thresholds/reset")
async def reset_thresholds(request: Request):
    require_session(request)
    with get_db() as conn:
        app_settings.clear_threshold_overrides(conn)
        _recompute_all(conn)
    return RedirectResponse("/settings?saved=reset", status_code=303)


@router.post("/settings/climate")
async def save_climate(request: Request):
    require_session(request)
    form = await request.form()
    enabled = str(form.get("climate", "0")) == "1"
    with get_db() as conn:
        app_settings.set_climate_hint(conn, enabled)
    return RedirectResponse("/settings?saved=climate", status_code=303)
