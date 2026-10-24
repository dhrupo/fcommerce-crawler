import json
import threading
import urllib.error
import urllib.request

import pytest

from crawler.api import make_server
from crawler.config import Config
from crawler.outcomes import FetchResult, Outcome
from crawler.store import Store


class Reader:
    strategy = "fake"

    def __init__(self, crash=False):
        self.crash = crash

    def posts(self, source, newest_seen):
        if self.crash:
            raise RuntimeError("boom in /Users/dhrupo/private/file.py line 3")
        return FetchResult(Outcome.OK, [{"item_id": "p1", "kind": "post", "created_at": 1.0e10}])

    def owner_for(self, source, post):
        return None

    def comments(self, source, post, owner_id):
        return FetchResult(Outcome.NOTHING_NEW, [])


@pytest.fixture
def server(tmp_path):
    def start(reader):
        store = Store(tmp_path)
        store.add_source("https://www.facebook.com/a", "fb_page", "Sylhet", "phones")
        srv = make_server(Config(places=("Sylhet",)), store, {"fb_page": reader},
                          lambda place, keyword: FetchResult(Outcome.NOTHING_NEW, []), port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    started = []
    yield start
    for srv in started:
        srv.shutdown()


def post(url, body):
    req = urllib.request.Request(url + "/search", data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.headers["content-type"], [json.loads(line) for line in r.read().decode().splitlines()]


def test_search_streams_ndjson_lines(server):
    url = server(Reader())
    content_type, events = post(url, {"place": "Sylhet", "category": "phones"})
    assert content_type.startswith("application/x-ndjson")
    assert [(e["phase"], e["outcome"]) for e in events] == [("posts", "ok"), ("comments", "nothing_new")]
    assert events[0]["items"][0]["item_id"] == "p1"


def test_crash_record_has_no_traceback(server):
    url = server(Reader(crash=True))
    _, events = post(url, {"place": "Sylhet", "category": "phones"})
    crash = events[0]
    assert crash["outcome"] == "crash"
    text = json.dumps(events)
    assert "Traceback" not in text and "/Users/" not in text and "boom" not in text


def test_bad_request_is_400_with_a_plain_message(server):
    url = server(Reader())
    with pytest.raises(urllib.error.HTTPError) as e:
        post(url, {"category": "phones"})
    assert e.value.code == 400
    assert json.loads(e.value.read())["error"] == "place is required"


def test_server_binds_to_localhost_only(server):
    url = server(Reader())
    assert url.startswith("http://127.0.0.1:")
