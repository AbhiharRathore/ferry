import random
import socket

import pytest

from app.retry import backoff_seconds
from app.security import UnsafeURL, check_url, hash_api_key, new_api_key, sign, signature_headers, verify


def fake_resolver(*ips):
    def resolve(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (ip, port)) for ip in ips]
    return resolve


# --- signatures --------------------------------------------------------------

def test_signature_round_trip():
    body = b'{"a":1}'
    headers = signature_headers("whsec_x", "msg_1", body, now=1_700_000_000)
    assert verify("whsec_x", headers, body, now=1_700_000_010)


def test_tampered_body_fails():
    headers = signature_headers("whsec_x", "msg_1", b'{"a":1}', now=1_700_000_000)
    assert not verify("whsec_x", headers, b'{"a":2}', now=1_700_000_000)


def test_wrong_secret_fails():
    headers = signature_headers("whsec_x", "msg_1", b"{}", now=1_700_000_000)
    assert not verify("whsec_y", headers, b"{}", now=1_700_000_000)


def test_stale_timestamp_fails_even_with_valid_signature():
    headers = signature_headers("whsec_x", "msg_1", b"{}", now=1_700_000_000)
    assert not verify("whsec_x", headers, b"{}", tolerance=300, now=1_700_000_301)


def test_signature_is_bound_to_message_id():
    headers = signature_headers("whsec_x", "msg_1", b"{}", now=1_700_000_000)
    headers["Ferry-Id"] = "msg_2"
    assert not verify("whsec_x", headers, b"{}", now=1_700_000_000)


def test_missing_headers_fail_cleanly():
    assert not verify("whsec_x", {}, b"{}")


def test_signature_format():
    assert sign("s", "m", 1, b"x").startswith("v1=")


# --- API keys ----------------------------------------------------------------

def test_api_keys_are_random_and_hashed():
    a, b = new_api_key(), new_api_key()
    assert a != b and a.startswith("fry_")
    assert hash_api_key(a) != a and len(hash_api_key(a)) == 64


# --- SSRF guard --------------------------------------------------------------

@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.1", "169.254.169.254",
                                "0.0.0.0", "100.64.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1"])
def test_private_addresses_are_blocked(ip):
    with pytest.raises(UnsafeURL):
        check_url("https://hooks.example.com/x", allow_private=False, allow_http=True, resolve=fake_resolver(ip))


def test_public_address_is_allowed():
    check_url("https://hooks.example.com/x", allow_private=False, allow_http=True,
              resolve=fake_resolver("93.184.216.34"))


def test_one_private_answer_among_public_ones_is_enough_to_block():
    with pytest.raises(UnsafeURL):
        check_url("https://hooks.example.com/x", allow_private=False, allow_http=True,
                  resolve=fake_resolver("93.184.216.34", "10.0.0.1"))


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "https://user:pw@example.com/x",
                                 "https:///nohost"])
def test_bad_urls_are_rejected(url):
    with pytest.raises(UnsafeURL):
        check_url(url, allow_private=True, allow_http=True)


def test_http_can_be_disallowed():
    with pytest.raises(UnsafeURL):
        check_url("http://example.com/x", allow_private=True, allow_http=False)


# --- backoff -----------------------------------------------------------------

def test_backoff_grows_then_caps():
    rng = random.Random(1)
    delays = [backoff_seconds(n, base=5, cap=3600, rng=rng) for n in range(1, 15)]
    assert 2.5 <= delays[0] <= 5
    assert 5 <= delays[1] <= 10
    # 5 * 2^10 = 5120 > 3600, so from attempt 11 on the ceiling is the cap.
    assert all(1800 <= d <= 3600 for d in delays[10:])


def test_backoff_has_jitter():
    values = {round(backoff_seconds(4, base=5, cap=3600), 6) for _ in range(20)}
    assert len(values) > 1


def test_backoff_rejects_attempt_zero():
    with pytest.raises(ValueError):
        backoff_seconds(0, 5, 60)


def test_database_url_gets_the_psycopg_driver():
    from app.config import Settings

    for given in ("postgres://u:p@h/db", "postgresql://u:p@h/db"):
        assert Settings(database_url=given).database_url == "postgresql+psycopg://u:p@h/db"
    assert Settings(database_url="postgresql+psycopg://u:p@h/db").database_url == "postgresql+psycopg://u:p@h/db"
