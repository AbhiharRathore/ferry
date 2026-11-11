"""API keys, webhook signatures and the SSRF guard."""

import hashlib
import hmac
import ipaddress
import secrets
import socket
import time
from urllib.parse import urlsplit

API_KEY_PREFIX = "fry_"
SECRET_PREFIX = "whsec_"


# --- API keys ----------------------------------------------------------------

def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_endpoint_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(24)


# --- Signatures --------------------------------------------------------------
# Signed content is "<message id>.<unix timestamp>.<raw body>". Including the
# timestamp lets receivers reject replays; including the id binds the signature
# to one message so a body can't be re-sent under another id.

def sign(secret: str, message_id: str, timestamp: int, body: bytes) -> str:
    signed = f"{message_id}.{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def signature_headers(secret: str, message_id: str, body: bytes, now: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if now is None else now
    return {
        "Ferry-Id": message_id,
        "Ferry-Timestamp": str(ts),
        "Ferry-Signature": sign(secret, message_id, ts, body),
    }


def verify(secret: str, headers: dict[str, str], body: bytes, tolerance: int = 300, now: int | None = None) -> bool:
    """What a receiver runs. Constant-time compare; rejects stale timestamps."""
    try:
        message_id = headers["Ferry-Id"]
        ts = int(headers["Ferry-Timestamp"])
        given = headers["Ferry-Signature"]
    except (KeyError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - ts) > tolerance:
        return False
    return hmac.compare_digest(given, sign(secret, message_id, ts, body))


# --- SSRF guard --------------------------------------------------------------

class UnsafeURL(ValueError):
    pass


def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_url(url: str, *, allow_private: bool, allow_http: bool, resolve=socket.getaddrinfo) -> None:
    """Reject URLs that would make the server call itself or an internal network.

    Every address the host resolves to must be public; one private answer is
    enough to refuse (an attacker controls their own DNS). The worker re-checks
    before each delivery, because DNS can change after registration.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or (parts.scheme == "http" and not allow_http):
        raise UnsafeURL("URL must use https" if not allow_http else "URL must use http or https")
    if not parts.hostname:
        raise UnsafeURL("URL has no host")
    if parts.username or parts.password:
        raise UnsafeURL("URL must not contain credentials")
    if allow_private:
        return
    try:
        infos = resolve(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURL(f"host does not resolve: {parts.hostname}") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise UnsafeURL(f"host does not resolve: {parts.hostname}")
    for addr in addresses:
        if not _ip_is_public(ipaddress.ip_address(addr.split("%")[0])):
            raise UnsafeURL(f"{parts.hostname} resolves to a non-public address ({addr})")
