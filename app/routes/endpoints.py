import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_session
from ..deps import current_app, require_admin
from ..models import Application, Attempt, Delivery, Endpoint
from ..schemas import (
    AppCreate,
    AppCreated,
    EndpointCreate,
    EndpointCreated,
    EndpointOut,
    EndpointStats,
    EndpointUpdate,
)
from ..security import UnsafeURL, check_url, hash_api_key, new_api_key, new_endpoint_secret

router = APIRouter(prefix="/v1")


def endpoint_out(ep: Endpoint) -> EndpointOut:
    return EndpointOut(
        id=ep.id, url=ep.url, description=ep.description, event_types=ep.event_types,
        enabled=ep.disabled_at is None, disabled_reason=ep.disabled_reason,
        failure_streak=ep.failure_streak, created_at=ep.created_at,
    )


def _validate_url(url: str) -> None:
    s = get_settings()
    try:
        check_url(url, allow_private=s.allow_private_targets, allow_http=s.allow_http)
    except UnsafeURL as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unsafe endpoint URL: {exc}") from exc


def _own_endpoint(session: Session, app: Application, endpoint_id: uuid.UUID) -> Endpoint:
    ep = session.get(Endpoint, endpoint_id)
    # Same 404 for "doesn't exist" and "belongs to another app": don't leak ids.
    if ep is None or ep.app_id != app.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "endpoint not found")
    return ep


@router.post("/apps", status_code=status.HTTP_201_CREATED, response_model=AppCreated,
             dependencies=[Depends(require_admin)])
def create_app(body: AppCreate, session: Session = Depends(get_session)):
    key = new_api_key()
    app = Application(name=body.name, api_key_hash=hash_api_key(key))
    session.add(app)
    session.commit()
    return AppCreated(id=app.id, name=app.name, api_key=key)


@router.post("/endpoints", status_code=status.HTTP_201_CREATED, response_model=EndpointCreated)
def create_endpoint(body: EndpointCreate, app: Application = Depends(current_app),
                    session: Session = Depends(get_session)):
    _validate_url(body.url)
    ep = Endpoint(app_id=app.id, url=body.url, description=body.description,
                  event_types=body.event_types, secret=new_endpoint_secret())
    session.add(ep)
    session.commit()
    return EndpointCreated(**endpoint_out(ep).model_dump(), secret=ep.secret)


@router.get("/endpoints", response_model=list[EndpointOut])
def list_endpoints(app: Application = Depends(current_app), session: Session = Depends(get_session)):
    rows = session.scalars(select(Endpoint).where(Endpoint.app_id == app.id).order_by(Endpoint.created_at))
    return [endpoint_out(ep) for ep in rows]


@router.get("/endpoints/{endpoint_id}", response_model=EndpointOut)
def get_endpoint(endpoint_id: uuid.UUID, app: Application = Depends(current_app),
                 session: Session = Depends(get_session)):
    return endpoint_out(_own_endpoint(session, app, endpoint_id))


@router.patch("/endpoints/{endpoint_id}", response_model=EndpointOut)
def update_endpoint(endpoint_id: uuid.UUID, body: EndpointUpdate, app: Application = Depends(current_app),
                    session: Session = Depends(get_session)):
    ep = _own_endpoint(session, app, endpoint_id)
    fields = body.model_fields_set
    if "url" in fields and body.url is not None:
        _validate_url(body.url)
        ep.url = body.url
    if "description" in fields:
        ep.description = body.description
    if "event_types" in fields:
        ep.event_types = body.event_types
    if "enabled" in fields and body.enabled is not None:
        if body.enabled:
            # Re-enabling closes the breaker; held deliveries become due again.
            ep.disabled_at, ep.disabled_reason, ep.failure_streak = None, None, 0
        elif ep.disabled_at is None:
            ep.disabled_at, ep.disabled_reason = datetime.now(timezone.utc), "disabled by user"
    session.commit()
    return endpoint_out(ep)


@router.get("/endpoints/{endpoint_id}/stats", response_model=EndpointStats)
def endpoint_stats(endpoint_id: uuid.UUID, app: Application = Depends(current_app),
                   session: Session = Depends(get_session)):
    ep = _own_endpoint(session, app, endpoint_id)
    by_status = dict(session.execute(
        select(Delivery.status, func.count()).where(Delivery.endpoint_id == ep.id).group_by(Delivery.status)
    ).all())
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = (
        select(Attempt.status_code, Attempt.duration_ms)
        .join(Delivery, Delivery.id == Attempt.delivery_id)
        .where(Delivery.endpoint_id == ep.id, Attempt.started_at >= since)
    ).subquery()
    total, ok, p50 = session.execute(
        select(
            func.count(),
            func.count().filter(recent.c.status_code.between(200, 299)),
            func.percentile_cont(0.5).within_group(recent.c.duration_ms),
        ).select_from(recent)
    ).one()
    return EndpointStats(
        endpoint_id=ep.id,
        deliveries_by_status={s.value: n for s, n in by_status.items()},
        attempts_last_24h=total,
        success_rate_last_24h=round(ok / total, 4) if total else None,
        p50_latency_ms_last_24h=int(p50) if p50 is not None else None,
    )
