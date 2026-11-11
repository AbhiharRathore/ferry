"""Tests run against a real Postgres (the docker-compose `db` service, or the CI service).

SKIP LOCKED, JSONB, arrays and the claim query are Postgres features, so SQLite
would test something else. The test database is created if it doesn't exist.
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

BASE_URL = os.environ.get("FERRY_TEST_DATABASE_URL", "postgresql+psycopg://ferry:ferry@localhost:5433/ferry_test")
os.environ["FERRY_DATABASE_URL"] = BASE_URL
os.environ["FERRY_ADMIN_TOKEN"] = "test-admin-token"
os.environ["FERRY_ALLOW_PRIVATE_TARGETS"] = "true"  # receivers in tests listen on 127.0.0.1

import httpx  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.db import reset_engine, session_factory  # noqa: E402
from app.main import create_app  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(__file__))


def _ensure_database(url: str) -> None:
    u = make_url(url)
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        if not c.scalar(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": u.database}):
            c.execute(text(f'CREATE DATABASE "{u.database}"'))
    admin.dispose()


@pytest.fixture(scope="session", autouse=True)
def database():
    _ensure_database(BASE_URL)
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "migrations"))
    cfg.set_main_option("sqlalchemy.url", BASE_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield
    reset_engine()


@pytest.fixture(autouse=True)
def clean_tables(database):
    get_settings.cache_clear()
    reset_engine()
    with session_factory()() as s:
        s.execute(text("TRUNCATE attempts, deliveries, messages, endpoints, applications CASCADE"))
        s.commit()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def factory():
    return session_factory()


def worker_settings(**overrides) -> Settings:
    base = dict(allow_private_targets=True, retry_base_seconds=0.01, retry_max_seconds=0.05,
                max_attempts=5, breaker_threshold=50, request_timeout_seconds=2.0, lease_seconds=30)
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def http():
    with httpx.Client(timeout=2.0) as c:
        yield c


class Receiver:
    """A real HTTP server on 127.0.0.1 that records requests and replays scripted responses."""

    def __init__(self):
        self.requests: list[dict] = []
        self.script: list[int] = []      # status codes to return in order; then `default`
        self.default = 200
        self.delay = 0.0
        self.lock = threading.Lock()
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with receiver.lock:
                    receiver.requests.append({"headers": dict(self.headers), "body": body, "path": self.path})
                    code = receiver.script.pop(0) if receiver.script else receiver.default
                if receiver.delay:
                    time.sleep(receiver.delay)
                self.send_response(code)
                if code in (301, 302):
                    self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"received": True}).encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver():
    r = Receiver()
    yield r
    r.close()


ADMIN = {"Authorization": "Bearer test-admin-token"}


def make_app(client, name="shop") -> dict:
    r = client.post("/v1/apps", json={"name": name}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['api_key']}"}


def make_endpoint(client, auth, url, event_types=None) -> dict:
    r = client.post("/v1/endpoints", json={"url": url, "event_types": event_types}, headers=auth)
    assert r.status_code == 201, r.text
    return r.json()


def send_message(client, auth, event_type="order.created", payload=None, key=None):
    headers = dict(auth)
    if key:
        headers["Idempotency-Key"] = key
    return client.post("/v1/messages", json={"event_type": event_type, "payload": payload or {"n": 1}},
                       headers=headers)


def drain(factory, http, settings, max_seconds=10.0) -> None:
    """Run the worker until nothing is pending or in flight."""
    from app.worker import run_once

    deadline = time.time() + max_seconds
    while time.time() < deadline:
        run_once(factory, http, settings)
        with factory() as s:
            left = s.scalar(text(
                "SELECT count(*) FROM deliveries d JOIN endpoints e ON e.id = d.endpoint_id "
                "WHERE d.status IN ('pending','in_flight') AND e.disabled_at IS NULL"))
        if not left:
            return
        time.sleep(0.02)
    raise AssertionError("worker did not drain in time")
