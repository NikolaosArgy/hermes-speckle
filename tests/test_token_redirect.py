import importlib.util
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "speckle_api_under_test", Path(__file__).resolve().parents[1] / "speckle_api.py"
)
speckle_api = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = speckle_api
_spec.loader.exec_module(speckle_api)


class _Recorder:
    """A local HTTP server that records what reached it."""

    def __init__(self, respond):
        self.hits = []
        recorder = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                recorder.hits.append({"path": self.path, "authorization": self.headers.get("Authorization")})
                respond(self)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()


def _ok(handler):
    handler.send_response(200)
    handler.end_headers()
    handler.wfile.write(b"ok")


def _redirect_to(location):
    def respond(handler):
        if handler.path == "/start":
            handler.send_response(302)
            handler.send_header("Location", location)
            handler.end_headers()
        else:
            _ok(handler)

    return respond


def _get(url):
    return urllib.request.Request(url, headers={"Authorization": "Bearer secret"})


@pytest.fixture
def servers():
    made = []
    yield lambda respond: made.append(_Recorder(respond)) or made[-1]
    for s in made:
        s.close()


def test_request_off_the_configured_server_is_refused(servers):
    elsewhere = servers(_ok)
    speckle_api.configure(server_url="http://127.0.0.1:1")
    with pytest.raises(speckle_api.OffServerRequest):
        speckle_api.open_url(_get(f"{elsewhere.url}/latest/meta-data"), timeout=5)
    assert elsewhere.hits == []


def test_redirect_off_the_configured_server_is_refused(servers):
    # A different port is a different host: storage, a LAN address or cloud metadata alike.
    elsewhere = servers(_ok)
    speckle = servers(_redirect_to(f"{elsewhere.url}/file"))
    speckle_api.configure(server_url=speckle.url)
    with pytest.raises(speckle_api.OffServerRequest):
        speckle_api.open_url(_get(f"{speckle.url}/start"), timeout=5)
    assert elsewhere.hits == []


def test_redirect_on_the_configured_server_is_followed_with_the_token(servers):
    speckle = servers(None)
    speckle.server.RequestHandlerClass.do_GET = _same_host_handler(speckle)
    speckle_api.configure(server_url=speckle.url)
    with speckle_api.open_url(_get(f"{speckle.url}/start"), timeout=5) as resp:
        assert resp.read() == b"ok"
    assert [h["path"] for h in speckle.hits] == ["/start", "/preview.png"]
    assert all(h["authorization"] == "Bearer secret" for h in speckle.hits)


def _same_host_handler(recorder):
    def do_get(self):
        recorder.hits.append({"path": self.path, "authorization": self.headers.get("Authorization")})
        _redirect_to(f"{recorder.url}/preview.png")(self)

    return do_get


def test_plain_urllib_would_follow_a_redirect_anywhere_with_the_token(servers):
    elsewhere = servers(_ok)
    speckle = servers(_redirect_to(f"{elsewhere.url}/file"))
    urllib.request.urlopen(_get(f"{speckle.url}/start"), timeout=5).close()
    assert elsewhere.hits == [{"path": "/file", "authorization": "Bearer secret"}]
