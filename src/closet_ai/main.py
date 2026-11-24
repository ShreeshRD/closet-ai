"""FastAPI entrypoint for closet-ai."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from closet_ai import auth
from closet_ai.config import (
    HOST,
    PORT,
    SSL_CERT_FILE,
    SSL_ENABLED,
    SSL_KEY_FILE,
    ensure_dirs,
    static_dir,
)
from closet_ai.db import init_db
from closet_ai.routes import auth_routes, capture, items, log, settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    init_db()
    # Phase 2: daily SQLite backup on startup (idempotent per UTC day)
    try:
        from closet_ai.services.logging import backup_db

        backup_db()
    except Exception:
        pass
    yield
    from closet_ai.services import voice

    try:
        voice.stop_listening()
    except Exception:
        pass


app = FastAPI(title="Closet AI", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(static_dir())), name="static")
app.include_router(auth_routes.router)
app.include_router(items.router)
app.include_router(capture.router)
app.include_router(log.router)
app.include_router(settings.router)


@app.middleware("http")
async def require_auth(request: Request, call_next):
    path = request.url.path
    public = path.startswith("/static") or path in ("/login", "/logout", "/healthz")
    if not public and not auth.session_valid(request):
        # JSON API: 401 (not a redirect) so clients can distinguish auth failure
        if path.startswith("/api/"):
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "PIN required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


def run() -> None:
    import logging
    import uvicorn

    # Surface voice ASR transcripts in /tmp/closet-ai.log for debugging
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    ensure_dirs()
    init_db()
    kwargs: dict = {"host": HOST, "port": PORT, "log_level": "info"}
    if SSL_ENABLED:
        # Secure context → getUserMedia works from phone/laptop on LAN
        kwargs["ssl_certfile"] = str(SSL_CERT_FILE)
        kwargs["ssl_keyfile"] = str(SSL_KEY_FILE)
        print(f"HTTPS on https://{HOST}:{PORT} (self-signed — accept browser warning once)")
    else:
        print(
            f"HTTP on http://{HOST}:{PORT} — camera blocked on LAN; "
            "generate certs in data/certs/ for HTTPS"
        )
    uvicorn.run(app, **kwargs)


if __name__ == "__main__":
    run()
