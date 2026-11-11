"""A minimal webhook receiver that verifies Ferry signatures.

    FERRY_ENDPOINT_SECRET=whsec_... python examples/receiver.py 9000

Copy `verify` into your own service (or import it from app.security): it needs
only the standard library.
"""

import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

SECRET = os.environ["FERRY_ENDPOINT_SECRET"]
seen_ids: set[str] = set()  # use a database table with a unique index in real code


def verify(secret: str, headers, body: bytes, tolerance: int = 300) -> bool:
    try:
        msg_id, ts, given = headers["Ferry-Id"], int(headers["Ferry-Timestamp"]), headers["Ferry-Signature"]
    except (KeyError, ValueError, TypeError):
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    expected = "v1=" + hmac.new(secret.encode(), f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(given, expected)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if not verify(SECRET, self.headers, body):
            self.send_response(401)
            self.end_headers()
            return
        event = json.loads(body)
        if event["id"] in seen_ids:          # at-least-once delivery: retries reuse the id
            print("duplicate, ignored:", event["id"])
        else:
            seen_ids.add(event["id"])
            print("received", event["type"], event["data"])
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
