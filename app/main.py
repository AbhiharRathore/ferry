import logging
import time
import uuid

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from .db import get_session
from .logs import configure_logging
from .routes import endpoints, messages

log = logging.getLogger("ferry.api")


def create_app() -> FastAPI:
    configure_logging()
    api = FastAPI(
        title="Ferry",
        version="1.0.0",
        description="Reliable webhook delivery: signed requests, retries with backoff, "
                    "dead-lettering, and a Postgres-backed queue.",
    )
    api.include_router(endpoints.router)
    api.include_router(messages.router)

    @api.middleware("http")
    async def request_log(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        t0 = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        log.info("request", extra={"request_id": request_id, "method": request.method,
                                   "path": request.url.path, "status": response.status_code,
                                   "ms": round((time.perf_counter() - t0) * 1000, 1)})
        return response

    @api.get("/healthz", tags=["ops"])
    def healthz(session: Session = Depends(get_session)):
        try:
            session.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "degraded", "database": "unreachable"}, status_code=503)
        return {"status": "ok", "database": "ok"}

    return api


app = create_app()
