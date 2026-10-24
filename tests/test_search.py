import threading
import time

from crawler.config import Config
from crawler.outcomes import FetchResult, Outcome
from crawler.search import search
from crawler.store import Store

NOW = 1_800_000_000.0
CONFIG = Config(places=("Sylhet",), marketplace_city_ids={"Sylhet": "1"})


class FakeReader:
    strategy = "fake"

    def __init__(self, posts=None, comments=None, crash_on=None):
        self.posts_by_url = posts or {}
        self.comments_by_post = comments or {}
        self.crash_on = crash_on
        self.post_calls, self.comment_calls = [], []

    def posts(self, source, newest_seen):
        self.post_calls.append((source.url, newest_seen))
        if source.url == self.crash_on:
            raise RuntimeError("parser exploded at /Users/dhrupo/secret/path.py")
        return self.posts_by_url.get(source.url, FetchResult(Outcome.NOTHING_NEW, []))

    def owner_for(self, source, post):
        return "owner"

    def comments(self, source, post, owner_id):
        self.comment_calls.append(post["item_id"])
        return self.comments_by_post.get(post["item_id"], FetchResult(Outcome.NOTHING_NEW, []))


def post(pid, t=NOW - 3600, comments=0):
    return {"item_id": pid, "kind": "post", "created_at": t, "text": f"post {pid}", "comment_count": comments}


def comment(cid, pid):
    return {"comment_id": cid, "item_id": pid, "parent_id": None, "depth": 0, "created_at": NOW - 60,
            "text": "price?", "is_owner": False}


def setup(tmp_path, reader, sources=(("https://www.facebook.com/a", "phones"),)):
    clock = [NOW]
    store = Store(tmp_path, clock=lambda: clock[0])
    for url, category in sources:
        store.add_source(url, "fb_page", "Sylhet", category)
    return store, clock


def run(store, reader, place="Sylhet", category="phones", keyword=None, marketplace=None, clock=None):
    return list(search(CONFIG, store, place, category, keyword, readers={"fb_page": reader},
                       marketplace=marketplace or (lambda place, keyword: FetchResult(Outcome.NOTHING_NEW, [])),
                       now=(lambda: clock[0]) if clock else (lambda: NOW)))


def test_out_of_scope_place(tmp_path):
    reader = FakeReader()
    store, _ = setup(tmp_path, reader)
    events = run(store, reader, place="Chittagong")
    assert [e["outcome"] for e in events] == ["out_of_scope"]
    assert reader.post_calls == []


def test_only_matching_category_read(tmp_path):
    reader = FakeReader()
    store, _ = setup(tmp_path, reader, sources=(("https://www.facebook.com/a", "phones"),
                                                ("https://www.facebook.com/b", "clothes")))
    run(store, reader, category="phones")
    assert [url for url, _ in reader.post_calls] == ["https://www.facebook.com/a"]


def test_posts_event_comes_before_comments_event(tmp_path):
    reader = FakeReader(posts={"https://www.facebook.com/a": FetchResult(Outcome.OK, [post("p1", comments=1)])},
                        comments={"p1": FetchResult(Outcome.OK, [comment("c1", "p1")])})
    store, _ = setup(tmp_path, reader)
    events = [e for e in run(store, reader) if e["source"]["kind"] == "fb_page"]
    assert [(e["phase"], e["outcome"]) for e in events] == [("posts", "ok"), ("comments", "ok")]
    assert [i["item_id"] for i in events[0]["items"]] == ["p1"]
    assert [i["comment_id"] for i in events[1]["items"]] == ["c1"]


def test_fresh_source_is_cached(tmp_path):
    reader = FakeReader(posts={"https://www.facebook.com/a": FetchResult(Outcome.OK, [post("p1")])})
    store, clock = setup(tmp_path, reader)
    run(store, reader, clock=clock)
    clock[0] += 59 * 60
    events = [e for e in run(store, reader, clock=clock) if e["source"]["kind"] == "fb_page"]
    assert len(reader.post_calls) == 1
    assert events[0]["outcome"] == "cached" and [i["item_id"] for i in events[0]["items"]] == ["p1"]


def test_stale_source_fetches_incremental(tmp_path):
    reader = FakeReader(posts={"https://www.facebook.com/a": FetchResult(Outcome.OK, [post("p1", t=NOW - 7200)])})
    store, clock = setup(tmp_path, reader)
    run(store, reader, clock=clock)
    clock[0] += 61 * 60
    reader.posts_by_url["https://www.facebook.com/a"] = FetchResult(Outcome.OK, [post("p2", t=NOW)])
    events = [e for e in run(store, reader, clock=clock) if e["phase"] == "posts" and e["source"]["kind"] == "fb_page"]
    assert reader.post_calls[1] == ("https://www.facebook.com/a", NOW - 7200)
    items = {i["item_id"]: i for i in events[0]["items"]}
    assert set(items) == {"p1", "p2"}
    assert items["p2"]["changes"]["new"] is True and items["p1"]["changes"]["new"] is False


def test_one_source_crash_others_stream(tmp_path):
    reader = FakeReader(posts={"https://www.facebook.com/b": FetchResult(Outcome.OK, [post("p1")])},
                        crash_on="https://www.facebook.com/a")
    store, _ = setup(tmp_path, reader, sources=(("https://www.facebook.com/a", "phones"),
                                                ("https://www.facebook.com/b", "phones")))
    events = run(store, reader)
    by_url = {e["source"]["url"]: e for e in events if e["phase"] == "posts"}
    assert by_url["https://www.facebook.com/a"]["outcome"] == "crash"
    assert by_url["https://www.facebook.com/b"]["outcome"] == "ok"
    assert store.fetch_state(store.get_source("https://www.facebook.com/a").id).last_outcome == "crash"


def test_crash_detail_has_no_internals(tmp_path):
    reader = FakeReader(crash_on="https://www.facebook.com/a")
    store, _ = setup(tmp_path, reader)
    crash = next(e for e in run(store, reader) if e["outcome"] == "crash")
    assert "secret" not in str(crash) and "Traceback" not in str(crash)


def test_comments_refetched_only_when_count_grew(tmp_path):
    first = FetchResult(Outcome.OK, [post("p1", comments=2), post("p2", comments=1), post("p3", comments=0)])
    reader = FakeReader(posts={"https://www.facebook.com/a": first})
    store, clock = setup(tmp_path, reader)
    run(store, reader, clock=clock)
    assert sorted(reader.comment_calls) == ["p1", "p2"]
    clock[0] += 2 * 3600
    reader.comment_calls.clear()
    reader.posts_by_url["https://www.facebook.com/a"] = FetchResult(
        Outcome.OK, [post("p1", comments=2), post("p2", comments=5), post("p3", comments=0)])
    run(store, reader, clock=clock)
    assert reader.comment_calls == ["p2"]


def test_zero_results_carry_a_reason(tmp_path):
    reader = FakeReader()
    store, _ = setup(tmp_path, reader)
    events = [e for e in run(store, reader) if e["source"]["kind"] == "fb_page"]
    assert events[0]["outcome"] == "nothing_new" and events[0]["items"] == []


def test_blocked_source_serves_stored_items_and_says_so(tmp_path):
    reader = FakeReader(posts={"https://www.facebook.com/a": FetchResult(Outcome.OK, [post("p1")])})
    store, clock = setup(tmp_path, reader)
    run(store, reader, clock=clock)
    clock[0] += 2 * 3600
    reader.posts_by_url["https://www.facebook.com/a"] = FetchResult(Outcome.BLOCKED, [], "page 1: blocked")
    event = next(e for e in run(store, reader, clock=clock) if e["phase"] == "posts" and e["source"]["kind"] == "fb_page")
    assert event["outcome"] == "blocked"
    assert [i["item_id"] for i in event["items"]] == ["p1"]
    assert event["served_from_store"] is True


def test_marketplace_searched_live_with_keyword(tmp_path):
    asked = []

    def marketplace(place, keyword):
        asked.append((place, keyword))
        return FetchResult(Outcome.OK, [{"item_id": "L1", "kind": "listing", "title": "iPhone 13", "sold": False}])

    reader = FakeReader()
    store, _ = setup(tmp_path, reader)
    events = run(store, reader, keyword="iphone 13", marketplace=marketplace)
    mkt = [e for e in events if e["source"]["kind"] == "fb_marketplace"]
    assert asked == [("Sylhet", "iphone 13")]
    assert mkt[0]["outcome"] == "ok" and mkt[0]["items"][0]["item_id"] == "L1"


def test_sources_are_read_in_parallel(tmp_path):
    started, release = [], threading.Event()

    class SlowReader(FakeReader):
        def posts(self, source, newest_seen):
            started.append(source.url)
            if len(started) < 2:
                release.wait(2)
            else:
                release.set()
            return FetchResult(Outcome.NOTHING_NEW, [])

    reader = SlowReader()
    store, _ = setup(tmp_path, reader, sources=(("https://www.facebook.com/a", "phones"),
                                                ("https://www.facebook.com/b", "phones")))
    t0 = time.monotonic()
    run(store, reader)
    assert len(started) == 2 and time.monotonic() - t0 < 1.5
