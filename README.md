# Ferry

Reliable webhook delivery. Your service hands Ferry an event; Ferry signs it, POSTs it to every
subscribed endpoint, retries with exponential backoff when receivers fail, dead-letters what
can't be delivered, and refuses to call internal networks.

Built with FastAPI, PostgreSQL (also used as the job queue), SQLAlchemy 2, Alembic and httpx.
60 tests run against a real Postgres.

```
client ──POST /v1/messages──▶ API ──one transaction──▶ Postgres: messages + deliveries
                                                              ▲
worker × N ──claim due rows (FOR UPDATE SKIP LOCKED)──────────┘
   ├─ sign (HMAC-SHA256) + POST ──▶ customer endpoint
   └─ record attempt → succeeded | retry later | dead-letter | trip breaker
```

## Why it's built this way

| Problem | What Ferry does | Where |
|---|---|---|
| Several workers must not send the same webhook | The claim query locks due rows with `FOR UPDATE SKIP LOCKED`; each worker takes different rows and never waits on another | `app/worker.py` `CLAIM_SQL` |
| A worker can crash mid-delivery | Each claim sets a lease (`locked_until`) and a fresh `lease_token`. Expired leases are reclaimed; a stale worker's late result is rejected because its token no longer matches | `record()` |
| "Exactly once" isn't possible over HTTP | Delivery is at-least-once. Every retry carries the same `Ferry-Id` and byte-identical body so receivers can dedupe | `body_for()`, `examples/receiver.py` |
| Client retries its own request | `Idempotency-Key` header, unique per app. A repeat returns the original (`200`, `Idempotent-Replayed: true`); the same key with a different body is `409` | `app/routes/messages.py` |
| Retry storms after an outage | Exponential backoff with jitter: about base·2ⁿ⁻¹ seconds, capped, half of it random | `app/retry.py` |
| A receiver is down for hours | Circuit breaker: after N consecutive failures the endpoint is disabled and its deliveries wait; re-enabling resumes them. `410 Gone` disables at once | `record()`, `PATCH /v1/endpoints/{id}` |
| Receivers must know a request is genuine | `Ferry-Signature: v1=HMAC(secret, id.timestamp.body)` plus `Ferry-Timestamp` for a replay window. Verification uses a constant-time compare | `app/security.py` |
| SSRF: a customer registers `http://169.254.169.254/` or `localhost` | URLs must resolve only to public addresses, checked at registration **and** before every send (DNS can change). Redirects aren't followed | `check_url()` |
| Leaked database | API keys are stored as SHA-256 hashes (they are 32 random bytes, so a slow hash adds nothing) and shown once | `app/security.py` |

## Run it

Requires Docker.

```bash
docker compose up -d --build        # Postgres, API on :8000 (runs migrations), one worker
```

Create an app and an endpoint (the admin token is set in `docker-compose.yml` for local use):

```bash
curl -s -X POST localhost:8000/v1/apps -H "Authorization: Bearer local-dev-admin-token-change-me" \
  -H "Content-Type: application/json" -d '{"name":"shop"}'
# → {"id": "...", "api_key": "fry_..."}   (shown once)

curl -s -X POST localhost:8000/v1/endpoints -H "Authorization: Bearer fry_..." \
  -H "Content-Type: application/json" -d '{"url":"https://example.com/hooks","event_types":["order.paid"]}'
# → {..., "secret": "whsec_..."}

curl -s -X POST localhost:8000/v1/messages -H "Authorization: Bearer fry_..." \
  -H "Idempotency-Key: order-1001" -H "Content-Type: application/json" \
  -d '{"event_type":"order.paid","payload":{"order_id":1001}}'
```

Interactive API docs: `http://localhost:8000/docs`. To try it against a receiver on your own
machine, start `examples/receiver.py` and run the stack with `FERRY_ALLOW_PRIVATE_TARGETS=true`
(the SSRF guard otherwise blocks local addresses, as it should).

## API

| Method | Path | |
|---|---|---|
| POST | `/v1/apps` | Admin token. Creates an app and returns its API key once |
| POST, GET | `/v1/endpoints` | Register (returns the signing secret once) / list |
| GET, PATCH | `/v1/endpoints/{id}` | Read / change URL, event types, `enabled` |
| GET | `/v1/endpoints/{id}/stats` | Deliveries by status; 24h attempts, success rate, p50 latency |
| POST | `/v1/messages` | Send an event; optional `Idempotency-Key` |
| GET | `/v1/messages/{id}` | Message with its deliveries |
| GET | `/v1/deliveries?status=&endpoint_id=` | List (e.g. `status=failed` is the dead-letter queue) |
| GET | `/v1/deliveries/{id}` | Delivery with every attempt (status code, latency, error, response snippet) |
| POST | `/v1/deliveries/{id}/retry` | Re-queue a dead-lettered delivery with a fresh attempt budget |
| GET | `/healthz` | Database check |

Every response carries `X-Request-ID`; API and worker logs are JSON lines.

## Verifying webhooks (receiver side)

```python
expected = "v1=" + hmac.new(secret.encode(), f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).hexdigest()
ok = abs(time.time() - ts) <= 300 and hmac.compare_digest(headers["Ferry-Signature"], expected)
```

Full example with dedupe: [`examples/receiver.py`](examples/receiver.py).

## Tests

```bash
docker compose up -d db
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # bin/ on macOS/Linux
.venv/Scripts/python -m pytest
```

The suite creates a `ferry_test` database and runs migrations down and up. Highlights:

- **Four workers, 120 messages:** every message arrives exactly once and no delivery needs a
  second attempt (`test_two_workers_never_send_the_same_delivery_twice`).
- **Crashed worker:** its lease expires, another worker takes over, and the stale worker's late
  result is refused (`test_expired_lease_is_reclaimed_and_stale_worker_cannot_overwrite`).
- **Idempotency race:** 8 concurrent requests with one key create exactly one message. This test
  caught a real bug: an ORM autoflush inserted the row before the duplicate-key handler, so
  the race surfaced as a 500.
- Retries with identical id and body, dead-letter and manual replay, `410 Gone`, circuit
  breaker, timeouts, redirects not followed, SSRF at registration and at send time, signatures
  (tampering, wrong secret, stale timestamp, id swap), tenant isolation.

CI (`.github/workflows/ci.yml`) runs the migrations up, down and up, then the suite, on a
Postgres service, and builds the Docker image.

## Configuration

All settings are `FERRY_*` environment variables; see `.env.example` and `app/config.py`.
Defaults: 8 attempts, 5 s base delay, 1 h cap, breaker after 20 consecutive failures,
10 s request timeout, 256 KB payload limit.

## Deploying (free tier)

`render.yaml` is a Render Blueprint: one free web service and Render's free Postgres. Render's
free plan has no background workers, so the demo sets `FERRY_EMBEDDED_WORKER=true` and the
worker runs as a thread inside the API. In a real deployment, run `python -m app.worker`
separately so the API and workers scale independently. `FERRY_ADMIN_TOKEN` is generated by Render
and visible only in its dashboard. Free-tier caveats: the service sleeps after 15 minutes idle
(the worker sleeps with it), and the free database expires after 30 days.

## Known limits

- The SSRF check resolves DNS, then httpx resolves again when connecting. A hostile DNS server
  could answer differently in between (DNS rebinding). Closing that fully means connecting to
  the checked IP directly; not done yet.
- Endpoint signing secrets are stored in plain text because the worker needs them to sign.
  Encrypting them at rest with a key from the environment is the next step.
- One worker sends its batch sequentially; scale by running more workers.
- No per-endpoint rate limiting or message retention policy yet.
