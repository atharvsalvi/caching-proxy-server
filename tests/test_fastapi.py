import gzip
from http.client import HTTPConnection

import pytest

from benchmarks.run import serve_proxy
from server_helpers import running_server
from test_http_handling import Origin


def request(port, path, *, method="GET", body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.headers, response.read()
    finally:
        conn.close()


@pytest.mark.redis
def test_compressed_language_variants_on_reserved_path(live_store):
    def reply(handler):
        data = gzip.compress(handler.headers["Accept-Language"].encode(), mtime=0)
        handler.send_response(200)
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Vary", "Accept-Language")
        handler.end_headers()
        handler.wfile.write(data)

    with running_server(Origin) as origin:
        origin.reply = reply
        with serve_proxy("fastapi", f"http://127.0.0.1:{origin.server_port}") as (port, _):
            path = "/docs?x=a%2Fb&x=c"
            for language, expected in [("en", "MISS"), ("fr", "MISS"), ("fr", "HIT"), ("en", "HIT")]:
                status, fields, data = request(port, path, headers={"Accept-Language": language,
                                                                    "Accept-Encoding": "gzip"})
                assert (status, fields["X-Cache"], fields["Content-Encoding"]) == (200, expected, "gzip")
                assert gzip.decompress(data) == language.encode()
            assert origin.requests == [("GET", path)] * 2


def test_upload_and_redirect_with_duplicate_cookies():
    def reply(handler):
        handler.send_response(302)
        handler.send_header("Location", "/next")
        handler.send_header("Set-Cookie", "a=1")
        handler.send_header("Set-Cookie", "b=2")
        handler.end_headers()
        handler.wfile.write(b"redirect")

    with running_server(Origin) as origin:
        origin.reply = reply
        with serve_proxy("fastapi", f"http://127.0.0.1:{origin.server_port}/base") as (port, _):
            status, fields, data = request(port, "/send", method="POST", body=b"upload")
            assert origin.requests == [("POST", "/base/send")]
            assert origin.body == b"upload"
            assert (status, fields["Location"], data) == (302, "/next", b"redirect")
            assert fields.get_all("Set-Cookie") == ["a=1", "b=2"]
