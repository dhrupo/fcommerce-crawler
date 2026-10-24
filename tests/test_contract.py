from crawler.config import Config
from crawler.outcomes import FetchResult, Outcome
from crawler.search import search
from crawler.store import Store

NOW = 1_800_000_000.0
FIELDS = {"schema_version", "source", "strategy", "parser_version", "fetched_at", "changes", "expired"}


class Reader:
    strategy = "fake"

    def posts(self, source, newest_seen):
        return FetchResult(Outcome.OK, [{"item_id": "p1", "kind": "post", "created_at": NOW, "comment_count": 1}])

    def owner_for(self, source, post):
        return None

    def comments(self, source, post, owner_id):
        return FetchResult(Outcome.OK, [{"comment_id": "c1", "item_id": "p1", "created_at": NOW, "text": "x",
                                         "depth": 0, "parent_id": None, "is_owner": False}])


def marketplace(place, keyword):
    return FetchResult(Outcome.OK, [{"item_id": "L1", "kind": "listing", "title": "t", "sold": False}])


def test_every_record_has_contract_fields(tmp_path):
    clock = [NOW]
    store = Store(tmp_path, clock=lambda: clock[0])
    store.add_source("https://www.facebook.com/a", "fb_page", "Sylhet", "phones")
    cfg = Config(places=("Sylhet",))
    events = []
    for _ in range(2):  # second run is served from the 1-hour store
        events += search(cfg, store, "Sylhet", "phones", "iphone", readers={"fb_page": Reader()},
                         marketplace=marketplace, now=lambda: clock[0])
    records = [r for e in events for r in e["items"]]
    assert {e["outcome"] for e in events} >= {"ok", "cached"}
    assert len(records) >= 6
    for r in records:
        assert FIELDS <= r.keys(), r
        assert set(r["changes"]) == {"new", "edited", "sold_changed"}
        assert r["schema_version"] == 1 and r["source"] and r["strategy"] and r["fetched_at"]


def test_records_never_carry_a_post_author_id(tmp_path):
    class GroupReader(Reader):
        def posts(self, source, newest_seen):
            return FetchResult(Outcome.OK, [{"item_id": "g1", "kind": "post", "created_at": NOW,
                                             "author_id": "pfbidPRIVATEPERSON", "comment_count": 0}])

    store = Store(tmp_path, clock=lambda: NOW)
    store.add_source("https://www.facebook.com/groups/1/", "fb_group", "Sylhet", "phones")
    events = list(search(Config(places=("Sylhet",)), store, "Sylhet", "phones", readers={"fb_group": GroupReader()},
                         marketplace=marketplace, now=lambda: NOW))
    assert "pfbidPRIVATEPERSON" not in str(events)
    assert store.get_item(store.list_sources()[0].id, "g1")["author_id"] == "pfbidPRIVATEPERSON"


def test_group_records_drop_owner_id(tmp_path):
    class GroupReader(Reader):
        def posts(self, source, newest_seen):
            return FetchResult(Outcome.OK, [{"item_id": "g1", "kind": "post", "created_at": NOW,
                                             "owner_id": "900000000000034", "comment_count": 0}])

    store = Store(tmp_path, clock=lambda: NOW)
    store.add_source("https://www.facebook.com/groups/1/", "fb_group", "Sylhet", "phones")
    events = list(search(Config(places=("Sylhet",)), store, "Sylhet", "phones", readers={"fb_group": GroupReader()},
                         marketplace=marketplace, now=lambda: NOW))
    assert "900000000000034" not in str(events)


def test_events_carry_schema_version(tmp_path):
    store = Store(tmp_path, clock=lambda: NOW)
    store.add_source("https://www.facebook.com/a", "fb_page", "Sylhet", "phones")
    events = list(search(Config(places=("Sylhet",)), store, "Sylhet", "phones", "iphone",
                         readers={"fb_page": Reader()}, marketplace=marketplace, now=lambda: NOW))
    events += list(search(Config(places=("Dhaka",)), store, "Sylhet", None, readers={}, marketplace=marketplace,
                          now=lambda: NOW))
    assert events and all(e["schema_version"] == 1 for e in events)


def test_parser_version_is_the_one_that_parsed(tmp_path):
    store = Store(tmp_path, clock=lambda: NOW)
    src = store.add_source("https://www.facebook.com/a", "fb_page", "Sylhet", "phones")
    store.upsert_item(src.id, {"item_id": "p0", "kind": "post", "created_at": NOW, "parser_version": "2026-01-01"})
    store.set_fetch_state(src.id, "ok")
    events = list(search(Config(places=("Sylhet",)), store, "Sylhet", "phones", readers={"fb_page": Reader()},
                         marketplace=marketplace, now=lambda: NOW))
    old = next(r for e in events for r in e["items"] if r.get("item_id") == "p0")
    assert old["parser_version"] == "2026-01-01"
