"""Delivery worker: claim due deliveries, POST them, record the outcome.

Run one or more of these (`python -m app.worker`). Postgres is the queue: the
claim uses FOR UPDATE SKIP LOCKED, so concurrent workers each take different
rows instead of blocking on, or double-sending, the same ones.

Delivery is at-least-once. A worker that dies after the HTTP call but before
recording the result leaves a lease that expires; another worker then sends the
same message again. Receivers dedupe on the Ferry-Id header, which is the same
for every retry of a message.
"""

import json
import logging
import signal
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings
from .db import session_factory
from .logs import configure_logging
from .retry import backoff_seconds
from .security import UnsafeURL, check_url, pinned_request, signature_headers

log = logging.getLogger("ferry.worker")

CLAIM_SQL = text("""
WITH due AS (
    SELECT d.id
    FROM deliveries d
    JOIN endpoints e ON e.id = d.endpoint_id
    WHERE e.disabled_at IS NULL
      AND (
            (d.status = 'pending'   AND d.next_attempt_at <= now())
         OR (d.status = 'in_flight' AND d.locked_until   <  now())   -- lease expired: worker died
      )
    ORDER BY d.next_attempt_at
    LIMIT :limit
    FOR UPDATE OF d SKIP LOCKED
)
UPDATE deliveries d
SET status        = 'in_flight',
    locked_until  = now() + make_interval(secs => :lease),
    lease_token   = gen_random_uuid(),
    attempt_count = d.attempt_count + 1
FROM due, messages m, endpoints e
WHERE d.id = due.id AND m.id = d.message_id AND e.id = d.endpoint_id
RETURNING d.id, d.lease_token, d.attempt_count, d.endpoint_id,
          m.id AS message_id, m.event_type, m.payload, m.created_at, e.url, e.secret
""")


@dataclass
class Job:
    delivery_id: uuid.UUID
    lease_token: uuid.UUID
    attempt_no: int
    endpoint_id: uuid.UUID
    message_id: uuid.UUID
    event_type: str
    payload: dict
    created_at: datetime
    url: str
    secret: str


@dataclass
class Outcome:
    started_at: datetime
    duration_ms: int
    status_code: int | None = None
    error: str | None = None
    snippet: str | None = None

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 300


def claim(session: Session, settings: Settings) -> list[Job]:
    rows = session.execute(CLAIM_SQL, {"limit": settings.batch_size, "lease": settings.lease_seconds}).all()
    session.commit()  # release row locks now; the lease protects the rows from here on
    return [Job(r.id, r.lease_token, r.attempt_count, r.endpoint_id, r.message_id, r.event_type,
                r.payload, r.created_at, r.url, r.secret) for r in rows]


def body_for(job: Job) -> bytes:
    """The exact bytes that get signed. Identical on every retry of a message."""
    envelope = {
        "id": str(job.message_id),
        "type": job.event_type,
        "timestamp": job.created_at.astimezone(timezone.utc).isoformat(),
        "data": job.payload,
    }
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()


def send(client: httpx.Client, job: Job, settings: Settings) -> Outcome:
    started = datetime.now(timezone.utc)
    t0 = time.perf_counter()

    def done(**kw) -> Outcome:
        return Outcome(started_at=started, duration_ms=int((time.perf_counter() - t0) * 1000), **kw)

    try:
        # Re-check at send time: the host's DNS may have changed since registration.
        vetted = check_url(job.url, allow_private=settings.allow_private_targets, allow_http=settings.allow_http)
    except UnsafeURL as exc:
        return done(error=f"blocked: {exc}")

    # Connect to the address we just vetted rather than letting httpx resolve again.
    target, host_header, extensions = pinned_request(job.url, vetted)
    body = body_for(job)
    headers = {"Content-Type": "application/json", "User-Agent": "Ferry-Webhooks/1.0",
               **host_header, **signature_headers(job.secret, str(job.message_id), body)}
    try:
        # No redirects: a 3xx to an internal address would sidestep the SSRF check.
        resp = client.post(target, content=body, headers=headers, follow_redirects=False, extensions=extensions)
    except httpx.TimeoutException:
        return done(error=f"timeout after {settings.request_timeout_seconds:g}s")
    except httpx.HTTPError as exc:
        return done(error=f"{type(exc).__name__}: {exc}"[:500])
    snippet = resp.content[: settings.response_snippet_bytes].decode("utf-8", "replace")
    error = None if 200 <= resp.status_code < 300 else f"HTTP {resp.status_code}"
    return done(status_code=resp.status_code, error=error, snippet=snippet)


def record(session: Session, job: Job, out: Outcome, settings: Settings) -> str:
    """Store the attempt and move the delivery to its next state. Returns that state."""
    session.execute(text("""
        INSERT INTO attempts (delivery_id, attempt_no, started_at, duration_ms, status_code, error, response_snippet)
        VALUES (:d, :n, :s, :ms, :code, :err, :snip)
    """), {"d": job.delivery_id, "n": job.attempt_no, "s": out.started_at, "ms": out.duration_ms,
           "code": out.status_code, "err": out.error, "snip": out.snippet})

    owned = "WHERE id = :d AND lease_token = :t AND status = 'in_flight'"
    params = {"d": job.delivery_id, "t": job.lease_token}

    if out.ok:
        new_state = "succeeded"
        res = session.execute(text(f"""
            UPDATE deliveries SET status = 'succeeded', succeeded_at = now(), locked_until = NULL,
                                  lease_token = NULL, last_error = NULL {owned}"""), params)
        if res.rowcount:
            session.execute(text("UPDATE endpoints SET failure_streak = 0 WHERE id = :e"), {"e": job.endpoint_id})
    elif out.status_code == 410:
        # 410 Gone is the receiver saying "stop": dead-letter and disable instead of retrying.
        new_state = "failed"
        res = session.execute(text(f"""
            UPDATE deliveries SET status = 'failed', locked_until = NULL, lease_token = NULL,
                                  last_error = :err {owned}"""), {**params, "err": "HTTP 410 Gone"})
        if res.rowcount:
            session.execute(text("""UPDATE endpoints SET disabled_at = now(),
                                    disabled_reason = 'endpoint returned 410 Gone' WHERE id = :e"""),
                            {"e": job.endpoint_id})
    else:
        streak = session.execute(text(
            "UPDATE endpoints SET failure_streak = failure_streak + 1 WHERE id = :e RETURNING failure_streak"
        ), {"e": job.endpoint_id}).scalar_one()
        if streak >= settings.breaker_threshold:
            session.execute(text("""UPDATE endpoints SET disabled_at = now(), disabled_reason = :r
                                    WHERE id = :e AND disabled_at IS NULL"""),
                            {"e": job.endpoint_id, "r": f"circuit breaker: {streak} consecutive failures"})
        if job.attempt_no >= settings.max_attempts:
            new_state = "failed"
            res = session.execute(text(f"""
                UPDATE deliveries SET status = 'failed', locked_until = NULL, lease_token = NULL,
                                      last_error = :err {owned}"""), {**params, "err": out.error})
        else:
            new_state = "pending"
            delay = backoff_seconds(job.attempt_no, settings.retry_base_seconds, settings.retry_max_seconds)
            res = session.execute(text(f"""
                UPDATE deliveries SET status = 'pending', locked_until = NULL, lease_token = NULL,
                                      last_error = :err, next_attempt_at = now() + make_interval(secs => :delay)
                {owned}"""), {**params, "err": out.error, "delay": delay})
    if not res.rowcount:
        # Our lease expired and another worker re-claimed the row. Keep the attempt
        # log, leave the delivery to the current lease holder.
        new_state = "lease_lost"
    session.commit()
    return new_state


def run_once(factory: sessionmaker, client: httpx.Client, settings: Settings) -> int:
    with factory() as session:
        jobs = claim(session, settings)
    for job in jobs:
        out = send(client, job, settings)
        with factory() as session:
            state = record(session, job, out, settings)
        log.info("delivery", extra={"delivery_id": str(job.delivery_id), "attempt": job.attempt_no,
                                    "status_code": out.status_code, "ms": out.duration_ms, "state": state,
                                    "error": out.error})
    return len(jobs)


def main() -> None:
    configure_logging()
    settings = get_settings()
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        log.info("shutdown requested; finishing current batch")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    run_loop(lambda: stopping, settings)


def run_loop(should_stop, settings: Settings) -> None:
    """Poll for work until should_stop() is true. Shared by the CLI worker and the embedded one."""
    factory = session_factory()
    with httpx.Client(timeout=settings.request_timeout_seconds) as client:
        log.info("worker started")
        while not should_stop():
            try:
                if run_once(factory, client, settings) == 0:
                    time.sleep(settings.poll_interval_seconds)
            except Exception:
                # A database blip must not kill the loop; log it and try again shortly.
                log.exception("worker iteration failed")
                time.sleep(settings.poll_interval_seconds)
        log.info("worker stopped")


if __name__ == "__main__":
    main()
