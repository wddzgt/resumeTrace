"""ResumeTrace 服务入口:uvicorn server.api.main:app --port 8000"""
import logging
import os
import time
from pathlib import Path

from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .app import app

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("resumetrace")

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_headers=["*"],
                   allow_methods=["*"])


@app.middleware("http")
async def no_cache_ui(request, call_next):
    t0 = time.monotonic()
    resp = await call_next(request)
    ms = (time.monotonic() - t0) * 1000
    if request.url.path.startswith("/ui"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    if request.url.path.startswith("/v1/"):
        logger.info("%s %s → %d (%.0fms)", request.method, request.url.path, resp.status_code, ms)
    return resp


@app.get("/health")
async def health():
    from ..db import store
    try:
        with store.conn() as c:
            c.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "error"}


app.mount("/ui", StaticFiles(directory=Path(__file__).resolve().parents[2] / "web",
                             html=True), name="ui")

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting ResumeTrace on port %s", os.environ.get("RT_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("RT_PORT", "8000")))
