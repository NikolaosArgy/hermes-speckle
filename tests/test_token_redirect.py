import importlib.util
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "speckle_api_under_test", Path(__file__).resolve().parents[1] / "speckle_api.py"
)
speckle_api = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = speckle_api
_spec.loader.exec_module(speckle_api)


def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _fetch_via_redirect(storage_host):
    seen = {}

    class Storage(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["authorization"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    storage = _serve(Storage)
    target = f"http://{storage_host}:{storage.server_port}/file"

    class Server(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Server)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/preview", headers={"Authorization": "Bearer secret"}
        )
        with speckle_api.open_url(req, timeout=5) as resp:
            assert resp.read() == b"ok"
    finally:
        server.shutdown()
        storage.shutdown()
    return seen["authorization"]


def test_token_is_dropped_when_a_redirect_leaves_the_host():
    # Same machine, different port: a different host:port, as storage on another domain would be.
    assert _fetch_via_redirect("127.0.0.1") is None


def test_plain_urllib_would_have_forwarded_it():
    seen = {}

    class Storage(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["authorization"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    storage = _serve(Storage)

    class Server(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{storage.server_port}/file")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = _serve(Server)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/preview", headers={"Authorization": "Bearer secret"}
        )
        urllib.request.urlopen(req, timeout=5).close()
    finally:
        server.shutdown()
        storage.shutdown()
    assert seen["authorization"] == "Bearer secret"
