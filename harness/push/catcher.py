#!/usr/bin/env python3
"""The push catcher: what the server under test POSTs to a push subscription URL.

Stands in for the push relay. Listens with TLS on :443 under a certificate for
push.harness.test, issued by a CA generated per run that only the server under test
trusts (SSL_CERT_FILE), and records every POST body it receives. A plain-HTTP :8081,
published to 127.0.0.1 only, hands the record to push_probe.py: GET /posts returns them
all, oldest first.

Standard library only.
"""

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

POSTS = []
LOCK = threading.Lock()


class Push(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"unparsed": body.decode(errors="replace")}
        with LOCK:
            POSTS.append({"path": self.path, "body": parsed})
        # The relay answers 201; anything else would make the server retry.
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):
        pass


class Record(BaseHTTPRequestHandler):
    def do_GET(self):
        with LOCK:
            out = json.dumps(POSTS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


def main():
    tls = ThreadingHTTPServer(("0.0.0.0", 443), Push)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("/certs/push.pem", "/certs/push.key")
    tls.socket = ctx.wrap_socket(tls.socket, server_side=True)
    threading.Thread(target=tls.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8081), Record).serve_forever()


if __name__ == "__main__":
    main()
