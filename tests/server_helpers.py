from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from threading import Thread


@contextmanager
def running_server(handler, label=""):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        server.label = label
        server.requests = []
        server.request_headers = []
        server.response_headers = []
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=5)
