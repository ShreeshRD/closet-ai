"""FastAPI entrypoint for closet-ai."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from closet_ai import auth
from closet_ai.config import HOST, PORT, ensure_dirs, static_dir
from closet_ai.db import init_db
from closet_ai.routes import auth_routes, capture, items


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    init_db()
    yield


app = FastAPI(title="Closet AI", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(static_dir())), name="static")
app.include_router(auth_routes.router)
app.include_router(items.router)
app.include_router(capture.router)


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
    import uvicorn

    ensure_dirs()
    init_db()
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    run()
