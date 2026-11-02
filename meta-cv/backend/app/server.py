"""Production entry point: the API under /api and the built frontend (frontend/dist) at /.

    uvicorn app.server:app --host 0.0.0.0 --port 8000

The API paths match the Vite dev proxy (/api/* -> API with the prefix stripped), so dev and
production behave the same. Unknown non-file paths (/results, ...) get index.html so the
single-page app can route them.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import config
from .main import app as api, startup


@asynccontextmanager
async def lifespan(_: FastAPI):
    startup()  # a mounted sub-app's own lifespan does not run
    yield


app = FastAPI(title="META", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request, call_next):
    """No framing by other sites (clickjacking), no content-type guessing, no full URLs sent to other sites."""
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


app.mount("/api", api)


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str):
    dist = Path(config.FRONTEND_DIST).resolve()
    index = dist / "index.html"
    if not index.exists():
        raise HTTPException(503, "frontend not built - run npm run build in frontend/")
    target = (dist / path).resolve()
    if path and target.is_file() and dist in target.parents:
        return FileResponse(target)
    return FileResponse(index)
