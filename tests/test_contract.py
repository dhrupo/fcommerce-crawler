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
