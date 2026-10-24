import time

from crawler.store import Store

DAY = 86400


def post(item_id="p1", text="iPhone 13 128GB 72k", created_at=None, **extra):
    return {"item_id": item_id, "kind": "post", "created_at": created_at or time.time(), "text": text, **extra}


def test_sources_are_tagged_with_place_and_category(tmp_path):
    s = Store(tmp_path)
    s.add_source("https://www.facebook.com/iStoreSyl", "fb_page", "Sylhet", "phones")
    s.add_source("https://www.facebook.com/Zoomwildbd", "fb_page", "Dhaka", "outdoor")
    assert [x.url for x in s.list_sources(place="sylhet", category="phones")] == ["https://www.facebook.com/iStoreSyl"]
    assert s.remove_source("https://www.facebook.com/Zoomwildbd")
    assert len(s.list_sources()) == 1


def test_upsert_item_detects_edit_by_hash(tmp_path):
    s = Store(tmp_path)
    src = s.add_source("https://www.facebook.com/iStoreSyl", "fb_page", "Sylhet", "phones")
    assert s.upsert_item(src.id, post()) == {"new": True, "edited": False, "sold_changed": False}
    assert s.upsert_item(src.id, post()) == {"new": False, "edited": False, "sold_changed": False}
    assert s.upsert_item(src.id, post(text="iPhone 13 128GB 70k"))["edited"] is True
    assert s.items_for(src.id)[0]["text"] == "iPhone 13 128GB 70k"


def test_reaction_count_change_is_not_an_edit(tmp_path):
    s = Store(tmp_path)
    src = s.add_source("u", "fb_page", "Sylhet", "phones")
    s.upsert_item(src.id, post(reaction_count=1))
    assert s.upsert_item(src.id, post(reaction_count=9))["edited"] is False


def test_listing_sold_flip_is_flagged(tmp_path):
    s = Store(tmp_path)
    src = s.add_source("marketplace:Sylhet", "fb_marketplace", "Sylhet", None)
    s.upsert_item(src.id, {"item_id": "L1", "kind": "listing", "title": "iPhone", "sold": False})
    assert s.upsert_item(src.id, {"item_id": "L1", "kind": "listing", "title": "iPhone", "sold": True})["sold_changed"]


def test_raw_pruned_after_30_days(tmp_path):
    now = [1_800_000_000.0 - 31 * DAY]
    s = Store(tmp_path, clock=lambda: now[0])
    old = s.save_raw(1, "timeline", "old reply")
    now[0] += 31 * DAY
    fresh = s.save_raw(1, "timeline", "fresh reply")
    assert s.prune_raw() == 1
    assert not old.exists() and fresh.exists()
    assert [text for _, _, text in s.iter_raw()] == ["fresh reply"]


def test_items_older_than_60_days_marked_expired(tmp_path):
    now = 1_800_000_000.0
    s = Store(tmp_path, clock=lambda: now)
    src = s.add_source("u", "fb_page", "Sylhet", "phones")
    s.upsert_item(src.id, post("old", created_at=now - 61 * DAY))
    s.upsert_item(src.id, post("new", created_at=now - 59 * DAY))
    assert s.expire_old() == 1
    by_id = {i["item_id"]: i for i in s.items_for(src.id, include_expired=True)}
    assert by_id["old"]["expired"] is True and by_id["new"]["expired"] is False
    assert [i["item_id"] for i in s.items_for(src.id)] == ["new"]


def test_failed_fetch_keeps_last_good_time(tmp_path):
    now = [100.0]
    s = Store(tmp_path, clock=lambda: now[0])
    src = s.add_source("u", "fb_page", "Sylhet", "phones")
    assert s.fetch_state(src.id) is None
    s.set_fetch_state(src.id, "ok", newest_created_at=90)
    st = s.fetch_state(src.id)
    assert (st.last_outcome, st.fetched_at, st.newest_created_at) == ("ok", 100.0, 90)
    now[0] = 200.0
    s.set_fetch_state(src.id, "blocked")
    st = s.fetch_state(src.id)
    assert (st.last_outcome, st.attempted_at, st.fetched_at, st.newest_created_at) == ("blocked", 200.0, 100.0, 90)
