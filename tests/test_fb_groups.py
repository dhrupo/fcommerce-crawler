import json
from pathlib import Path
from types import SimpleNamespace

from crawler.config import Config
from crawler.facebook.groups import fetch_group_posts
from crawler.outcomes import Outcome

FIXTURES = Path(__file__).parent / "fixtures"
DAY = 86400


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def post(self, url, data=None, **kwargs):
        self.sent.append(json.loads(data["variables"]))
        return SimpleNamespace(status_code=200, text=self.replies.pop(0), url=url)


def group_page(posts, cursor="next"):
    edges = [{"node": {"__typename": "Story", "post_id": pid, "creation_time": t}} for pid, t in posts]
    conn = {"edges": edges, "page_info": {"has_next_page": cursor is not None, "end_cursor": cursor}}
    return json.dumps({"data": {"node": {"__typename": "Group", "id": "g", "group_feed": conn}}})


def test_group_fixture_yields_posts_with_times():
    text = (FIXTURES / "fb_group_feed_p1.json").read_text()
    t = FakeTransport([text])
    now = 1_792_900_000
    result = fetch_group_posts(t, Config(places=("Sylhet",)), "392585550772135", now=lambda: now, max_pages=1)
    assert result.outcome is Outcome.OK
    assert len(result.items) == 3
    assert all(isinstance(p["created_at"], int) and p["author_id"] for p in result.items)
    assert t.sent[0]["id"] == "392585550772135"


def test_ranked_group_feed_keeps_paging_past_an_old_post():
    now = 1_800_000_000
    t = FakeTransport([
        group_page([("a", now - DAY), ("old", now - 90 * DAY), ("b", now - 2 * DAY)]),
        group_page([("c", now - 3 * DAY)], cursor=None),
    ])
    result = fetch_group_posts(t, Config(places=("Sylhet",)), "g", now=lambda: now)
    assert [p["item_id"] for p in result.items] == ["a", "b", "c"]


def test_ranked_group_feed_ignores_last_seen():
    now = 1_800_000_000
    t = FakeTransport([group_page([("surfaced_late", now - 20 * DAY)], cursor=None)])
    result = fetch_group_posts(t, Config(places=("Sylhet",)), "g", now=lambda: now, newest_seen=now - DAY)
    assert [p["item_id"] for p in result.items] == ["surfaced_late"]
