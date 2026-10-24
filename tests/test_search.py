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
    reader = FakeReader(posts={"https://www.facebook.com/a": first},
                        comments={"p1": FetchResult(Outcome.OK, [comment("c1", "p1"), comment("c2", "p1")]),
                                  "p2": FetchResult(Outcome.OK, [comment("c3", "p2")])})
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


def test_fetch_ignores_one_hour_memory(tmp_path):
    from crawler.search import fetch_source

    reader = FakeReader(posts={"https://www.facebook.com/a": FetchResult(Outcome.OK, [post("p1")])})
    store, clock = setup(tmp_path, reader)
    run(store, reader, clock=clock)
    events = fetch_source(CONFIG, store, {"fb_page": reader}, store.get_source("https://www.facebook.com/a"),
                          now=lambda: clock[0])
    assert len(reader.post_calls) == 2
    assert events[0]["outcome"] == "ok"


def test_partial_read_does_not_advance_last_seen_and_still_reads_comments(tmp_path):
    partial = FetchResult(Outcome.SCHEMA_CHANGED, [post("p1", t=NOW - 60, comments=1)], "page 11: schema_changed")
    reader = FakeReader(posts={"https://www.facebook.com/a": partial},
                        comments={"p1": FetchResult(Outcome.OK, [comment("c1", "p1")])})
    store, clock = setup(tmp_path, reader)
    events = [e for e in run(store, reader, clock=clock) if e["source"]["kind"] == "fb_page"]
    assert [(e["phase"], e["outcome"]) for e in events] == [("posts", "schema_changed"), ("comments", "ok")]
    assert reader.comment_calls == ["p1"]
    src = store.get_source("https://www.facebook.com/a")
    assert store.fetch_state(src.id).newest_created_at is None
    run(store, reader, clock=clock)
    assert reader.post_calls[1] == ("https://www.facebook.com/a", None)


def test_blocked_read_skips_comments(tmp_path):
    blocked = FetchResult(Outcome.BLOCKED, [post("p1", comments=1)], "page 2: blocked")
    reader = FakeReader(posts={"https://www.facebook.com/a": blocked})
    store, _ = setup(tmp_path, reader)
    events = [e for e in run(store, reader) if e["source"]["kind"] == "fb_page"]
    assert events[1]["outcome"] == "blocked" and reader.comment_calls == []


def test_comments_read_when_count_is_unknown(tmp_path):
    unknown = {**post("p1"), "comment_count": None}
    reader = FakeReader(posts={"https://www.facebook.com/a": FetchResult(Outcome.OK, [unknown])})
    store, _ = setup(tmp_path, reader)
    run(store, reader)
    assert reader.comment_calls == ["p1"]


def test_failed_comment_read_is_retried_next_time(tmp_path):
    reader = FakeReader(posts={"https://www.facebook.com/a": FetchResult(Outcome.OK, [post("p1", comments=5)])},
                        comments={"p1": FetchResult(Outcome.SCHEMA_CHANGED, [], "comments: schema_changed")})
    store, clock = setup(tmp_path, reader)
    run(store, reader, clock=clock)
    clock[0] += 2 * 3600
    reader.comments_by_post["p1"] = FetchResult(Outcome.OK, [comment("c1", "p1")])
    run(store, reader, clock=clock)
    assert reader.comment_calls == ["p1", "p1"]
    clock[0] += 2 * 3600
    run(store, reader, clock=clock)
    assert reader.comment_calls == ["p1", "p1"]


def test_instagram_client_bootstraps_again_for_each_read(tmp_path):
    from crawler.search import InstagramReader

    store = Store(tmp_path, clock=lambda: NOW)
    src = store.add_source("https://www.instagram.com/shop/", "ig_profile", "Sylhet", "phones")
    reader = InstagramReader(CONFIG, transport=None, store=store, now=lambda: NOW)
    first = reader.client(src, fresh=True)
    assert reader.client(src) is first
    assert reader.client(src, fresh=True) is not first


def test_concurrent_searches_read_a_source_once(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Slow(FakeReader):
        def posts(self, source, newest_seen):
            self.post_calls.append(source.url)
            entered.set()
            release.wait(2)
            return FetchResult(Outcome.OK, [post("p1")])

    reader = Slow()
    store, _ = setup(tmp_path, reader)
    results = []
    first = threading.Thread(target=lambda: results.append(run(store, reader)))
    first.start()
    entered.wait(2)
    second = threading.Thread(target=lambda: results.append(run(store, reader)))
    second.start()
    time.sleep(0.3)  # the second search reaches the source while the first is still reading it
    release.set()
    first.join(3)
    second.join(3)
    assert len(reader.post_calls) == 1
    assert all(any(e["items"] for e in events if e["phase"] == "posts") for events in results)


def test_closing_the_stream_stops_queued_sources(tmp_path):
    class Slow(FakeReader):
        def posts(self, source, newest_seen):
            time.sleep(0.2)
            return super().posts(source, newest_seen)

    reader = Slow()
    store, _ = setup(tmp_path, reader, sources=[(f"https://www.facebook.com/s{i}", "phones") for i in range(6)])
    stream = search(CONFIG, store, "Sylhet", "phones", readers={"fb_page": reader},
                    marketplace=lambda place, keyword: FetchResult(Outcome.NOTHING_NEW, []), now=lambda: NOW,
                    workers=1)
    next(stream)
    stream.close()
    assert len(reader.post_calls) < 6


def test_network_error_is_labelled_without_traceback(tmp_path, caplog):
    from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

    class Offline(FakeReader):
        def posts(self, source, newest_seen):
            raise CurlConnectionError("Could not resolve host: www.facebook.com")

    reader = Offline()
    store, _ = setup(tmp_path, reader)
    event = next(e for e in run(store, reader) if e["phase"] == "posts")
    assert event["outcome"] == "crash" and event["detail"].startswith("network error")
    assert not [r for r in caplog.records if r.exc_info]


def test_fetch_source_refuses_out_of_scope_and_expires_old(tmp_path):
    from crawler.search import fetch_source

    reader = FakeReader()
    store, _ = setup(tmp_path, reader)
    dhaka = store.add_source("https://www.facebook.com/d", "fb_page", "Dhaka", "phones")
    events = fetch_source(CONFIG, store, {"fb_page": reader}, dhaka, now=lambda: NOW)
    assert events[0]["outcome"] == "out_of_scope" and reader.post_calls == []
    src = store.get_source("https://www.facebook.com/a")
    store.upsert_item(src.id, post("old", t=NOW - 61 * 86400))
    events = fetch_source(CONFIG, store, {"fb_page": reader}, src, now=lambda: NOW)
    assert [i["item_id"] for i in events[0]["items"]] == []


def test_zero_comments_for_a_counted_post_is_read_again(tmp_path):
    reader = FakeReader(posts={"https://www.facebook.com/a": FetchResult(Outcome.OK, [post("p1", comments=5)])},
                        comments={"p1": FetchResult(Outcome.NOTHING_NEW, [], "no comments visible")})
    store, clock = setup(tmp_path, reader)
    run(store, reader, clock=clock)
    clock[0] += 2 * 3600
    run(store, reader, clock=clock)
    assert reader.comment_calls == ["p1", "p1"]


def test_marketplace_drops_listings_older_than_60_days(tmp_path):
    listings = [{"item_id": "new", "kind": "listing", "created_at": NOW - 86400},
                {"item_id": "old", "kind": "listing", "created_at": NOW - 200 * 86400}]
    reader = FakeReader()
    store, _ = setup(tmp_path, reader)
    events = run(store, reader, keyword="iphone", marketplace=lambda place, keyword: FetchResult(Outcome.OK, listings))
    mkt = next(e for e in events if e["source"]["kind"] == "fb_marketplace")
    assert [i["item_id"] for i in mkt["items"]] == ["new"]


class FallbackTransport:
    def __init__(self, reports):
        self.reports = list(reports)
        self.tokens = []
        self.generation = 0

    def mode_token(self, url):
        return self.generation

    def report_block(self, url, token=None):
        self.tokens.append(token)
        self.generation += 1
        return self.reports.pop(0)


def page_reader(monkeypatch, tmp_path, results, reports):
    from crawler import search as search_module
    from crawler.search import FacebookPageReader

    calls = []
    queue_ = list(results)

    def fake_fetch(transport, config, page_id, **kwargs):
        calls.append(page_id)
        return queue_.pop(0)

    monkeypatch.setattr(search_module, "fetch_page_posts", fake_fetch)
    store = Store(tmp_path, clock=lambda: NOW)
    src = store.add_source("https://www.facebook.com/a", "fb_page", "Sylhet", "phones", native_id="42")
    t = FallbackTransport(reports)
    return FacebookPageReader(CONFIG, t, store, now=lambda: NOW), src, calls, t, store


def test_blocked_source_retried_once_logged_in(monkeypatch, tmp_path):
    reader, src, calls, t, _ = page_reader(monkeypatch, tmp_path, [
        FetchResult(Outcome.BLOCKED, [], "page 1: blocked"), FetchResult(Outcome.OK, [post("p1")])], ["login"])
    result = reader.posts(src, None)
    assert result.outcome is Outcome.OK and len(calls) == 2
    assert t.tokens == [0]


def test_blocked_without_login_is_not_retried(monkeypatch, tmp_path):
    reader, src, calls, _, _ = page_reader(monkeypatch, tmp_path, [
        FetchResult(Outcome.BLOCKED, [], "page 1: blocked"), FetchResult(Outcome.OK, [post("p1")])], ["cooldown"])
    assert reader.posts(src, None).outcome is Outcome.BLOCKED and len(calls) == 1


def test_login_expired_is_labelled(monkeypatch, tmp_path):
    blocked = FetchResult(Outcome.BLOCKED, [], "page 1: blocked")
    reader, src, calls, _, store = page_reader(monkeypatch, tmp_path, [blocked, blocked], ["login", "expired"])
    events = list(search(CONFIG, store, "Sylhet", "phones", readers={"fb_page": reader},
                         marketplace=lambda place, keyword: FetchResult(Outcome.NOTHING_NEW, []), now=lambda: NOW))
    page_events = [e for e in events if e["source"]["kind"] == "fb_page"]
    assert [(e["phase"], e["outcome"]) for e in page_events] == [("posts", "login_expired"),
                                                                 ("comments", "login_expired")]
    assert "login" in page_events[0]["detail"]
    assert len(calls) == 2
    assert store.fetch_state(src.id).last_outcome == "login_expired"


def test_page_id_lookup_block_uses_login_fallback(monkeypatch, tmp_path):
    from crawler import search as search_module
    from crawler.search import FacebookPageReader

    answers = [(Outcome.BLOCKED, None), (Outcome.OK, "42")]
    monkeypatch.setattr(search_module, "resolve_page_id", lambda transport, url: answers.pop(0))
    monkeypatch.setattr(search_module, "fetch_page_posts",
                        lambda transport, config, page_id, **kw: FetchResult(Outcome.OK, [post(page_id)]))
    store = Store(tmp_path, clock=lambda: NOW)
    src = store.add_source("https://www.facebook.com/a", "fb_page", "Sylhet", "phones")
    reader = FacebookPageReader(CONFIG, FallbackTransport(["login"]), store, now=lambda: NOW)
    assert [p["item_id"] for p in reader.posts(src, None).items] == ["42"]
    assert store.get_source(src.url).native_id == "42"


def test_comments_stop_after_login_expired(tmp_path):
    reader = FakeReader(posts={"https://www.facebook.com/a": FetchResult(
        Outcome.OK, [post("p1", comments=1), post("p2", comments=1)])},
        comments={"p1": FetchResult(Outcome.LOGIN_EXPIRED, [], "refused")})
    store, _ = setup(tmp_path, reader)
    events = [e for e in run(store, reader) if e["phase"] == "comments"]
    assert reader.comment_calls == ["p1"] and events[0]["outcome"] == "login_expired"
