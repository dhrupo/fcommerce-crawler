import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import Config
from .search import default_marketplace, default_readers, make_event, search, source_info
from .store import Store

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
MAX_BODY = 10_000
MAX_TEXT = 200


def parse_request(raw: bytes) -> tuple[dict | None, str | None]:
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return None, "body must be JSON"
    if not isinstance(body, dict):
        return None, "body must be a JSON object"
    fields = {}
    for key in ("place", "category", "keyword"):
        value = body.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > MAX_TEXT):
            return None, f"{key} must be text up to {MAX_TEXT} characters"
        fields[key] = value.strip() if value else None
    if not fields["place"]:
        return None, "place is required"
    return fields, None


def make_server(config: Config, store: Store, readers: dict, marketplace, port: int = 8765) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        timeout = 30

        def send_json(self, status, payload):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def refusal(self) -> tuple[int, str] | None:
            """Only same-machine JSON callers: a web page cannot send this without a preflight we never answer."""
            port = self.server.server_address[1]
            local = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("host") not in local:
                return 403, "host not allowed"
            origin = self.headers.get("origin")
            if origin is not None and origin.removeprefix("http://") not in local:
                return 403, "origin not allowed"
            if (self.headers.get("content-type") or "").split(";")[0].strip().lower() != "application/json":
                return 415, "content-type must be application/json"
            return None

        def do_POST(self):
            if self.path != "/search":
                return self.send_json(404, {"error": "not found"})
            if refused := self.refusal():
                return self.send_json(refused[0], {"error": refused[1]})
            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                return self.send_json(400, {"error": "bad content-length"})
            if length > MAX_BODY:
                return self.send_json(413, {"error": "body too large"})
            fields, error = parse_request(self.rfile.read(length))
            if error:
                return self.send_json(400, {"error": error})
            self.send_response(200)
            self.send_header("content-type", "application/x-ndjson; charset=utf-8")
            self.end_headers()
            try:
                for event in search(config, store, fields["place"], fields["category"], fields["keyword"],
                                    readers=readers, marketplace=marketplace):
                    self.wfile.write((json.dumps(event, ensure_ascii=False, default=str) + "\n").encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                log.exception("search request failed")
                crash = make_event(source_info(None, fields["place"], fields["category"]), "posts", "crash", [],
                                   "search failed; see the crawler log", False, time.time())
                self.wfile.write((json.dumps(crash) + "\n").encode())

        def log_message(self, fmt, *args):
            log.info("%s " + fmt, self.address_string(), *args)

    return ThreadingHTTPServer((HOST, port), Handler)


def serve(config: Config, store: Store, transport, port: int = 8765) -> None:
    server = make_server(config, store, default_readers(config, transport, store),
                         default_marketplace(config, transport, store), port)
    print(f"listening on http://{HOST}:{server.server_address[1]}  (POST /search)", flush=True)
    server.serve_forever()
