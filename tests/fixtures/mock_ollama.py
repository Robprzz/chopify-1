"""In-process HTTP double for the Ollama loopback contract."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

from editorial_content import Rules
from editorial_providers import Cancellation, Ollama


MODEL = "qwen3:4b"


class OllamaDouble:
    def __init__(self):
        self.models = [{"name": MODEL, "digest": "a" * 64},
                       {"name": "qwen3:cloud", "digest": "b" * 64},
                       {"name": "alias:latest", "digest": "c" * 64, "remote_host": "remote.invalid"}]
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.mode = "valid"
        self.show = {"capabilities": ["completion"]}


@contextmanager
def mock_ollama():
    fake = OllamaDouble()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, data, status=200):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == "/api/tags":
                return self.reply({"models": fake.models})
            self.reply({}, 404)

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                return self.reply(fake.show)
            if self.path != "/api/chat":
                return self.reply({}, 404)
            fake.calls.append(data)
            fake.entered.set()
            if fake.mode == "stall":
                fake.release.wait(8)
            if fake.mode == "missing":
                return self.reply({}, 404)
            if fake.mode == "invalid":
                return self.reply({"message": {"content": "{broken"}, "done": True})
            if fake.mode == "incomplete":
                return self.reply({"message": {"content": "{}"}, "done": False})
            plan = Rules().generate(None, data["messages"], data["format"], 1, Cancellation())
            if fake.mode == "ungrounded":
                plan["variants"]["curiosity"]["title"] = 9999
            self.reply({"message": {"content": json.dumps(plan)}, "done": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    class TestOllama(Ollama):
        def _connection(self, timeout):
            return http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)

    try:
        yield fake, TestOllama()
    finally:
        fake.release.set()
        server.shutdown()
        server.server_close()
        worker.join()
