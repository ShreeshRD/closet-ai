"""Auth routes: first-run PIN setup and unlock."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from closet_ai import auth
from closet_ai.config import templates_dir

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory=str(templates_dir()))


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if auth.session_valid(request):
        return RedirectResponse("/", status_code=303)
    remaining = auth.lockout_remaining()
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "pin_set": auth.pin_is_set(),
            "error": None,
            "lockout": int(remaining) if remaining > 0 else 0,
        },
    )


@router.post("/login")
async def login_submit(request: Request, pin: str = Form(...)):
    remaining = auth.lockout_remaining()
    if remaining > 0:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "pin_set": auth.pin_is_set(),
                "error": f"Too many attempts. Try again in {int(remaining)}s.",
                "lockout": int(remaining),
            },
            status_code=429,
        )

    if not auth.pin_is_set():
        try:
            auth.set_pin(pin)
        except ValueError as e:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"pin_set": False, "error": str(e), "lockout": 0},
                status_code=400,
            )
        resp = RedirectResponse("/", status_code=303)
        auth.issue_session(resp)
        return resp

    if auth.try_unlock(pin):
        resp = RedirectResponse("/", status_code=303)
        auth.issue_session(resp)
        return resp

    fails, _ = auth._read_fails()
    fails_left = max(0, auth.MAX_FAILS - fails)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "pin_set": True,
            "error": (
                f"Wrong PIN ({fails_left} tries left before lockout)."
                if fails_left < auth.MAX_FAILS
                else "Wrong PIN."
            ),
            "lockout": 0,
        },
        status_code=401,
    )


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    auth.clear_session(resp)
    return resp
