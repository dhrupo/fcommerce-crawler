import json
from pathlib import Path
from types import SimpleNamespace

from crawler.config import Config
from crawler.facebook.pages import fetch_page_posts
from crawler.outcomes import Outcome

FIXTURES = Path(__file__).parent / "fixtures"
DAY = 86400
NOW = 1_800_000_000


def page(posts, cursor="next"):
    edges = [{"node": {"__typename": "Story", "post_id": pid, "creation_time": t, "message": {"text": f"post {pid}"}}}
             for pid, t in posts]
    conn = {"edges": edges, "page_info": {"has_next_page": cursor is not None, "end_cursor": cursor}}
    return json.dumps({"data": {"node": {"__typename": "User", "id": "1", "timeline_list_feed_units": conn}}})


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.cursors = []

    def post(self, url, data=None, **kwargs):
        self.cursors.append(json.loads(data["variables"])["cursor"])
        text = self.replies.pop(0)
        return SimpleNamespace(status_code=200, text=text, url=url)


def fetch(replies, now=NOW, **kwargs):
    t = FakeTransport(replies)
    result = fetch_page_posts(t, Config(places=("Sylhet",)), "1", now=lambda: now, **kwargs)
    return result, t


def test_first_page_from_fixture():
    text = (FIXTURES / "fb_timeline_p1.json").read_text()
    result, _ = fetch([text], now=1791713085 + DAY, max_pages=1)
    assert result.outcome is Outcome.OK
    assert len(result.items) == 3


def test_stops_at_60_days():
    result, t = fetch([
        page([("a", NOW - 1 * DAY), ("b", NOW - 30 * DAY)]),
        page([("c", NOW - 59 * DAY), ("d", NOW - 61 * DAY)]),
        page([("never", NOW - 80 * DAY)]),
    ])
    assert [p["item_id"] for p in result.items] == ["a", "b", "c"]
    assert len(t.cursors) == 2


def test_page_with_only_old_posts_ends_the_walk():
    result, t = fetch([page([("a", NOW - 1 * DAY)]), page([("old", NOW - 70 * DAY)]), page([("never", NOW)])])
    assert [p["item_id"] for p in result.items] == ["a"]
    assert len(t.cursors) == 2


def test_pinned_old_post_does_not_stop_paging():
    result, _ = fetch([
        page([("pinned", NOW - 400 * DAY), ("a", NOW - 1 * DAY)]),
        page([("b", NOW - 2 * DAY)], cursor=None),
    ])
    assert [p["item_id"] for p in result.items] == ["a", "b"]


def test_incremental_stops_at_last_seen():
    result, t = fetch([
        page([("new", NOW - 1 * DAY), ("seen", NOW - 3 * DAY)]),
        page([("older", NOW - 5 * DAY)]),
    ], newest_seen=NOW - 3 * DAY)
    assert [p["item_id"] for p in result.items] == ["new"]
    assert len(t.cursors) == 1


def test_nothing_newer_than_last_seen_is_nothing_new():
    result, _ = fetch([page([("seen", NOW - 3 * DAY)])], newest_seen=NOW - 3 * DAY)
    assert result.outcome is Outcome.NOTHING_NEW
    assert result.items == []


def test_cursor_passed_to_next_page():
    _, t = fetch([page([("a", NOW)], cursor="C1"), page([("b", NOW)], cursor=None)])
    assert t.cursors == [None, "C1"]


def test_failure_after_first_page_keeps_items_and_reports_it():
    broken = json.dumps({"data": {"node": {"__typename": "User", "id": "1", "timeline_list_feed_units": None}}})
    result, _ = fetch([page([("a", NOW)]), broken])
    assert result.outcome is Outcome.SCHEMA_CHANGED
    assert [p["item_id"] for p in result.items] == ["a"]
    assert "page 2" in result.detail


def test_raw_replies_handed_to_store():
    saved = []
    fetch([page([("a", NOW)], cursor=None)], on_raw=lambda label, text: saved.append(label))
    assert saved == ["timeline"]


TRANSIENT = json.dumps({"errors": [{"message": "A server error missing_required_variable_value occured.",
                                    "severity": "CRITICAL", "code": 1675012,
                                    "summary": "Your request couldn't be processed"}]})


def test_transient_fb_error_is_retried():
    result, t = fetch([TRANSIENT, page([("a", NOW)], cursor=None)])
    assert result.outcome is Outcome.OK and [p["item_id"] for p in result.items] == ["a"]
    assert t.cursors == [None, None]


def test_transient_fb_error_three_times_is_schema_changed():
    result, t = fetch([TRANSIENT, TRANSIENT, TRANSIENT, page([("never", NOW)])])
    assert result.outcome is Outcome.SCHEMA_CHANGED
    assert len(t.cursors) == 3


def test_rate_limit_error_is_not_retried():
    limited = json.dumps({"errors": [{"code": 1675004, "summary": "Rate limit exceeded", "severity": "CRITICAL"}]})
    result, t = fetch([limited, page([("a", NOW)])])
    assert result.outcome is Outcome.BLOCKED
    assert len(t.cursors) == 1


def test_first_read_with_no_recent_posts_says_why():
    result, _ = fetch([page([("old", NOW - 400 * DAY)])])
    assert result.outcome is Outcome.NOTHING_NEW
    assert "no posts" in result.detail
