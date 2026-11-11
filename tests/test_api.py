import threading

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from tests.conftest import ADMIN, make_app, make_endpoint, send_message


# --- auth --------------------------------------------------------------------

def test_creating_an_app_needs_the_admin_token(client):
    assert client.post("/v1/apps", json={"name": "x"}).status_code == 401
    assert client.post("/v1/apps", json={"name": "x"}, headers={"Authorization": "Bearer nope"}).status_code == 403
    r = client.post("/v1/apps", json={"name": "x"}, headers=ADMIN)
    assert r.status_code == 201 and r.json()["api_key"].startswith("fry_")


def test_empty_admin_token_setting_disables_app_creation(client, monkeypatch):
    monkeypatch.setenv("FERRY_ADMIN_TOKEN", "")
    get_settings.cache_clear()
    assert client.post("/v1/apps", json={"name": "x"}, headers={"Authorization": "Bearer "}).status_code in (401, 403)


def test_api_routes_need_a_valid_key(client):
    assert client.get("/v1/endpoints").status_code == 401
    assert client.get("/v1/endpoints", headers={"Authorization": "Bearer fry_wrong"}).status_code == 401


def test_apps_cannot_see_each_others_data(client, receiver):
    a, b = make_app(client, "a"), make_app(client, "b")
    ep = make_endpoint(client, a, receiver.url)
    msg = send_message(client, a).json()
    assert client.get(f"/v1/endpoints/{ep['id']}", headers=b).status_code == 404
    assert client.get(f"/v1/messages/{msg['id']}", headers=b).status_code == 404
    assert client.get(f"/v1/deliveries/{msg['deliveries'][0]['id']}", headers=b).status_code == 404
    assert client.get("/v1/deliveries", headers=b).json() == []


# --- endpoints ---------------------------------------------------------------

def test_endpoint_secret_is_shown_once(client, receiver):
    auth = make_app(client)
    created = make_endpoint(client, auth, receiver.url)
    assert created["secret"].startswith("whsec_")
    listed = client.get("/v1/endpoints", headers=auth).json()
    assert "secret" not in listed[0]


def test_private_endpoint_urls_are_refused_when_guard_is_on(client, monkeypatch):
    auth = make_app(client)
    monkeypatch.setenv("FERRY_ALLOW_PRIVATE_TARGETS", "false")
    get_settings.cache_clear()
    for url in ("http://127.0.0.1:9000/hook", "http://169.254.169.254/latest/meta-data", "http://localhost/x"):
        r = client.post("/v1/endpoints", json={"url": url}, headers=auth)
        assert r.status_code == 422, url
        assert "unsafe endpoint URL" in r.json()["detail"]


def test_event_type_validation(client, receiver):
    auth = make_app(client)
    assert client.post("/v1/endpoints", json={"url": receiver.url, "event_types": []}, headers=auth).status_code == 422
    assert client.post("/v1/endpoints", json={"url": receiver.url, "event_types": ["bad type!"]},
                       headers=auth).status_code == 422


def test_disable_and_enable_endpoint(client, receiver):
    auth = make_app(client)
    ep = make_endpoint(client, auth, receiver.url)
    r = client.patch(f"/v1/endpoints/{ep['id']}", json={"enabled": False}, headers=auth)
    assert r.json()["enabled"] is False and r.json()["disabled_reason"] == "disabled by user"
    r = client.patch(f"/v1/endpoints/{ep['id']}", json={"enabled": True}, headers=auth)
    assert r.json()["enabled"] is True and r.json()["failure_streak"] == 0


# --- messages ----------------------------------------------------------------

def test_message_fans_out_by_event_type(client, receiver):
    auth = make_app(client)
    orders = make_endpoint(client, auth, receiver.url, ["order.created", "order.paid"])
    everything = make_endpoint(client, auth, receiver.url)
    r = send_message(client, auth, "user.created")
    assert r.status_code == 202
    assert {d["endpoint_id"] for d in r.json()["deliveries"]} == {everything["id"]}
    r = send_message(client, auth, "order.paid")
    assert {d["endpoint_id"] for d in r.json()["deliveries"]} == {orders["id"], everything["id"]}


def test_idempotency_key_replays_the_original(client, receiver):
    auth = make_app(client)
    make_endpoint(client, auth, receiver.url)
    first = send_message(client, auth, payload={"order": 7}, key="order-7")
    again = send_message(client, auth, payload={"order": 7}, key="order-7")
    assert first.status_code == 202 and again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["id"] == first.json()["id"]
    assert len(client.get("/v1/deliveries", headers=auth).json()) == 1


def test_idempotency_key_with_different_body_is_a_conflict(client, receiver):
    auth = make_app(client)
    send_message(client, auth, payload={"order": 7}, key="k1")
    r = send_message(client, auth, payload={"order": 8}, key="k1")
    assert r.status_code == 409


def test_idempotency_keys_are_scoped_per_app(client):
    a, b = make_app(client, "a"), make_app(client, "b")
    assert send_message(client, a, key="same").status_code == 202
    assert send_message(client, b, key="same").status_code == 202


def test_concurrent_requests_with_the_same_key_create_one_message(receiver):
    with TestClient(create_app()) as c:
        auth = make_app(c)
        make_endpoint(c, auth, receiver.url)
    results = []

    def post():
        with TestClient(create_app()) as c:
            results.append(send_message(c, auth, payload={"x": 1}, key="race"))

    threads = [threading.Thread(target=post) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {r.status_code for r in results} <= {200, 202}
    assert len({r.json()["id"] for r in results}) == 1
    assert sum(r.status_code == 202 for r in results) == 1


def test_payload_size_limit(client, monkeypatch):
    auth = make_app(client)
    monkeypatch.setenv("FERRY_MAX_PAYLOAD_BYTES", "100")
    get_settings.cache_clear()
    r = send_message(client, auth, payload={"blob": "x" * 200})
    assert r.status_code == 413


def test_message_validation(client):
    auth = make_app(client)
    r = client.post("/v1/messages", json={"event_type": "has space", "payload": {}}, headers=auth)
    assert r.status_code == 422
    r = client.post("/v1/messages", json={"event_type": "ok", "payload": [1, 2]}, headers=auth)
    assert r.status_code == 422


def test_only_failed_deliveries_can_be_retried(client, receiver):
    auth = make_app(client)
    make_endpoint(client, auth, receiver.url)
    d = send_message(client, auth).json()["deliveries"][0]
    r = client.post(f"/v1/deliveries/{d['id']}/retry", headers=auth)
    assert r.status_code == 409


def test_healthz_and_request_id(client):
    r = client.get("/healthz", headers={"X-Request-ID": "abc"})
    assert r.json() == {"status": "ok", "database": "ok"}
    assert r.headers["X-Request-ID"] == "abc"


def test_openapi_lists_the_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/v1/apps", "/v1/endpoints", "/v1/messages", "/v1/deliveries/{delivery_id}/retry"):
        assert p in paths
