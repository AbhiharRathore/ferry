import json
import threading
import time
from collections import Counter

from sqlalchemy import text

from app.security import verify
from app.worker import claim, record, run_once, send
from tests.conftest import drain, make_app, make_endpoint, send_message, worker_settings


def delivery(client, auth, delivery_id):
    return client.get(f"/v1/deliveries/{delivery_id}", headers=auth).json()


def test_successful_delivery_is_signed_and_verifiable(client, receiver, factory, http):
    auth = make_app(client)
    ep = make_endpoint(client, auth, receiver.url)
    msg = send_message(client, auth, "order.created", {"order_id": 42}).json()
    drain(factory, http, worker_settings())

    assert len(receiver.requests) == 1
    req = receiver.requests[0]
    headers = {k: v for k, v in req["headers"].items() if k.startswith("Ferry-")}
    assert verify(ep["secret"], headers, req["body"])
    body = json.loads(req["body"])
    assert body["id"] == msg["id"] and body["type"] == "order.created" and body["data"] == {"order_id": 42}

    d = delivery(client, auth, msg["deliveries"][0]["id"])
    assert d["status"] == "succeeded" and d["attempt_count"] == 1
    assert d["attempts"][0]["status_code"] == 200


def test_failures_are_retried_with_the_same_id_and_body(client, receiver, factory, http):
    auth = make_app(client)
    make_endpoint(client, auth, receiver.url)
    receiver.script = [500, 503]
    msg = send_message(client, auth).json()
    drain(factory, http, worker_settings())

    assert len(receiver.requests) == 3
    assert len({r["headers"]["Ferry-Id"] for r in receiver.requests}) == 1
    assert len({r["body"] for r in receiver.requests}) == 1
    d = delivery(client, auth, msg["deliveries"][0]["id"])
    assert d["status"] == "succeeded"
    assert [a["status_code"] for a in d["attempts"]] == [500, 503, 200]


def test_out_of_attempts_goes_to_dead_letter_then_manual_retry(client, receiver, factory, http):
    auth = make_app(client)
    make_endpoint(client, auth, receiver.url)
    receiver.default = 500
    msg = send_message(client, auth).json()
    drain(factory, http, worker_settings(max_attempts=3))
    did = msg["deliveries"][0]["id"]
    d = delivery(client, auth, did)
    assert d["status"] == "failed" and d["attempt_count"] == 3 and d["last_error"] == "HTTP 500"
    assert client.get("/v1/deliveries?status=failed", headers=auth).json()[0]["id"] == did

    receiver.default = 200
    assert client.post(f"/v1/deliveries/{did}/retry", headers=auth).json()["status"] == "pending"
    drain(factory, http, worker_settings(max_attempts=3))
    d = delivery(client, auth, did)
    assert d["status"] == "succeeded"
    assert len(d["attempts"]) == 4  # history kept across the manual retry


def test_410_gone_dead_letters_and_disables_the_endpoint(client, receiver, factory, http):
    auth = make_app(client)
    ep = make_endpoint(client, auth, receiver.url)
    receiver.default = 410
    msg = send_message(client, auth).json()
    drain(factory, http, worker_settings())
    assert len(receiver.requests) == 1
    assert delivery(client, auth, msg["deliveries"][0]["id"])["status"] == "failed"
    e = client.get(f"/v1/endpoints/{ep['id']}", headers=auth).json()
    assert e["enabled"] is False and "410" in e["disabled_reason"]


def test_circuit_breaker_holds_deliveries_until_reenabled(client, receiver, factory, http):
    auth = make_app(client)
    ep = make_endpoint(client, auth, receiver.url)
    receiver.default = 500
    msgs = [send_message(client, auth, payload={"i": i}).json() for i in range(3)]
    settings = worker_settings(breaker_threshold=3, max_attempts=100)
    drain(factory, http, settings)  # stops once the only endpoint is disabled

    e = client.get(f"/v1/endpoints/{ep['id']}", headers=auth).json()
    assert e["enabled"] is False and e["disabled_reason"].startswith("circuit breaker")
    sent_while_open = len(receiver.requests)
    time.sleep(0.1)
    run_once(factory, http, settings)
    assert len(receiver.requests) == sent_while_open  # nothing sent to a disabled endpoint

    receiver.default = 200
    client.patch(f"/v1/endpoints/{ep['id']}", json={"enabled": True}, headers=auth)
    drain(factory, http, settings)
    statuses = {delivery(client, auth, m["deliveries"][0]["id"])["status"] for m in msgs}
    assert statuses == {"succeeded"}


def test_timeouts_are_recorded_and_retried(client, receiver, factory, http):
    auth = make_app(client)
    make_endpoint(client, auth, receiver.url)
    receiver.delay = 0.5
    msg = send_message(client, auth).json()
    settings = worker_settings(request_timeout_seconds=0.2, max_attempts=2)
    import httpx

    with httpx.Client(timeout=settings.request_timeout_seconds) as short:
        drain(factory, short, settings)
    d = delivery(client, auth, msg["deliveries"][0]["id"])
    assert d["status"] == "failed"
    assert all(a["error"].startswith("timeout") for a in d["attempts"])


def test_redirects_are_not_followed(client, receiver, factory, http):
    auth = make_app(client)
    make_endpoint(client, auth, receiver.url)
    receiver.default = 302
    msg = send_message(client, auth).json()
    drain(factory, http, worker_settings(max_attempts=1))
    d = delivery(client, auth, msg["deliveries"][0]["id"])
    assert d["status"] == "failed" and d["attempts"][0]["status_code"] == 302


def test_ssrf_guard_also_runs_at_send_time(client, receiver, factory, http):
    # Registered while private targets were allowed; the worker runs with the guard on.
    auth = make_app(client)
    make_endpoint(client, auth, receiver.url)
    msg = send_message(client, auth).json()
    drain(factory, http, worker_settings(allow_private_targets=False, max_attempts=1))
    assert receiver.requests == []
    d = delivery(client, auth, msg["deliveries"][0]["id"])
    assert d["status"] == "failed" and d["attempts"][0]["error"].startswith("blocked:")


def test_two_workers_never_send_the_same_delivery_twice(client, receiver, factory):
    auth = make_app(client)
    make_endpoint(client, auth, receiver.url)
    n = 120
    for i in range(n):
        assert send_message(client, auth, payload={"i": i}).status_code == 202

    settings = worker_settings(batch_size=5)
    import httpx

    def worker():
        with httpx.Client(timeout=2.0) as h:
            idle = 0
            while idle < 3:
                idle = idle + 1 if run_once(factory, h, settings) == 0 else 0

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    ids = Counter(r["headers"]["Ferry-Id"] for r in receiver.requests)
    assert len(ids) == n
    assert max(ids.values()) == 1
    with factory() as s:
        assert s.scalar(text("SELECT count(*) FROM deliveries WHERE status = 'succeeded'")) == n
        assert s.scalar(text("SELECT max(attempt_count) FROM deliveries")) == 1


def test_expired_lease_is_reclaimed_and_stale_worker_cannot_overwrite(client, receiver, factory, http):
    auth = make_app(client)
    make_endpoint(client, auth, receiver.url)
    msg = send_message(client, auth).json()

    short = worker_settings(lease_seconds=0.2)
    with factory() as s:
        [stale_job] = claim(s, short)          # worker A claims, then "hangs"
    with factory() as s:
        assert claim(s, short) == []           # lease still held: nobody else gets it
    time.sleep(0.3)
    with factory() as s:
        [fresh_job] = claim(s, short)          # lease expired: worker B takes over
    assert fresh_job.delivery_id == stale_job.delivery_id
    assert fresh_job.attempt_no == 2 and fresh_job.lease_token != stale_job.lease_token

    with factory() as s:
        assert record(s, fresh_job, send(http, fresh_job, short), short) == "succeeded"
    with factory() as s:                       # A wakes up and tries to report
        assert record(s, stale_job, send(http, stale_job, short), short) == "lease_lost"
    d = delivery(client, auth, msg["deliveries"][0]["id"])
    assert d["status"] == "succeeded"
    assert len(d["attempts"]) == 2             # both HTTP calls are in the audit log


def test_endpoint_stats(client, receiver, factory, http):
    auth = make_app(client)
    ep = make_endpoint(client, auth, receiver.url)
    receiver.script = [500]
    send_message(client, auth, payload={"a": 1})
    send_message(client, auth, payload={"a": 2})
    drain(factory, http, worker_settings())
    stats = client.get(f"/v1/endpoints/{ep['id']}/stats", headers=auth).json()
    assert stats["deliveries_by_status"] == {"succeeded": 2}
    assert stats["attempts_last_24h"] == 3
    assert stats["success_rate_last_24h"] == round(2 / 3, 4)
    assert stats["p50_latency_ms_last_24h"] is not None
