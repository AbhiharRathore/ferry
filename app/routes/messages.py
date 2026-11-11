import hashlib
import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import any_, func, literal, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..db import get_session
from ..deps import current_app
from ..models import Application, Delivery, DeliveryStatus, Endpoint, Message
from ..schemas import DeliveryDetail, DeliveryOut, MessageCreate, MessageOut

router = APIRouter(prefix="/v1")


def _request_hash(body: MessageCreate) -> str:
    canonical = json.dumps({"event_type": body.event_type, "payload": body.payload},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _load_message(session: Session, app: Application, message_id: uuid.UUID) -> Message:
    msg = session.scalar(
        select(Message).options(selectinload(Message.deliveries)).where(Message.id == message_id)
    )
    if msg is None or msg.app_id != app.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    return msg


def _replay(session: Session, app: Application, key: str, request_hash: str, response: Response) -> Message:
    existing = session.scalar(select(Message).where(Message.app_id == app.id, Message.idempotency_key == key))
    if existing.request_hash != request_hash:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Idempotency-Key was already used with a different request body")
    response.status_code = status.HTTP_200_OK
    response.headers["Idempotent-Replayed"] = "true"
    return _load_message(session, app, existing.id)


@router.post("/messages", status_code=status.HTTP_202_ACCEPTED, response_model=MessageOut)
def create_message(
    body: MessageCreate,
    response: Response,
    idempotency_key: str | None = Header(default=None, max_length=200),
    app: Application = Depends(current_app),
    session: Session = Depends(get_session),
):
    size = len(json.dumps(body.payload, separators=(",", ":")).encode())
    if size > get_settings().max_payload_bytes:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "payload too large")
    request_hash = _request_hash(body)

    if idempotency_key:
        found = session.scalar(select(Message.id).where(
            Message.app_id == app.id, Message.idempotency_key == idempotency_key))
        if found:
            return _replay(session, app, idempotency_key, request_hash, response)

    # Look up targets before adding the message: a query after session.add() would
    # autoflush the INSERT early, outside the IntegrityError handling below.
    targets = session.scalars(select(Endpoint).where(
        Endpoint.app_id == app.id,
        or_(Endpoint.event_types.is_(None), literal(body.event_type) == any_(Endpoint.event_types)),
    )).all()
    msg = Message(app_id=app.id, event_type=body.event_type, payload=body.payload,
                  idempotency_key=idempotency_key, request_hash=request_hash)
    session.add(msg)
    # Fan out in the same transaction: a message is never stored without its
    # deliveries, and deliveries never exist for a message that rolled back.
    for ep in targets:
        msg.deliveries.append(Delivery(endpoint_id=ep.id, status=DeliveryStatus.pending))
    try:
        session.commit()
    except IntegrityError:
        # Two requests with the same key raced past the lookup above; the unique
        # constraint let exactly one insert win. Answer as a replay of that one.
        session.rollback()
        if not idempotency_key:
            raise
        return _replay(session, app, idempotency_key, request_hash, response)
    return _load_message(session, app, msg.id)


@router.get("/messages/{message_id}", response_model=MessageOut)
def get_message(message_id: uuid.UUID, app: Application = Depends(current_app),
                session: Session = Depends(get_session)):
    return _load_message(session, app, message_id)


def _own_delivery(session: Session, app: Application, delivery_id: uuid.UUID) -> Delivery:
    d = session.scalar(
        select(Delivery).options(selectinload(Delivery.attempts), selectinload(Delivery.message))
        .where(Delivery.id == delivery_id)
    )
    if d is None or d.message.app_id != app.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "delivery not found")
    return d


@router.get("/deliveries", response_model=list[DeliveryOut])
def list_deliveries(
    status_filter: DeliveryStatus | None = Query(default=None, alias="status"),
    endpoint_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    app: Application = Depends(current_app),
    session: Session = Depends(get_session),
):
    q = (select(Delivery).join(Message, Message.id == Delivery.message_id)
         .where(Message.app_id == app.id).order_by(Delivery.created_at.desc()).limit(limit))
    if status_filter:
        q = q.where(Delivery.status == status_filter)
    if endpoint_id:
        q = q.where(Delivery.endpoint_id == endpoint_id)
    return list(session.scalars(q))


@router.get("/deliveries/{delivery_id}", response_model=DeliveryDetail)
def get_delivery(delivery_id: uuid.UUID, app: Application = Depends(current_app),
                 session: Session = Depends(get_session)):
    return _own_delivery(session, app, delivery_id)


@router.post("/deliveries/{delivery_id}/retry", response_model=DeliveryOut)
def retry_delivery(delivery_id: uuid.UUID, app: Application = Depends(current_app),
                   session: Session = Depends(get_session)):
    d = _own_delivery(session, app, delivery_id)
    if d.status != DeliveryStatus.failed:
        raise HTTPException(status.HTTP_409_CONFLICT, f"only failed deliveries can be retried (status is {d.status.value})")
    # A fresh budget of attempts; the attempts table keeps the full history.
    d.status, d.attempt_count, d.last_error = DeliveryStatus.pending, 0, None
    d.next_attempt_at = session.scalar(select(func.now()))
    session.commit()
    return d
