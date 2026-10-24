import json
from pathlib import Path
from types import SimpleNamespace

from crawler.config import Config
from crawler.instagram.client import InstagramClient
from crawler.outcomes import Outcome

FIXTURES = Path(__file__).parent / "fixtures"
DAY = 86400
NEWEST = 1775837895


class FakeTransport:
    def __init__(self, posts):
        self.posts = list(posts)
        self.sent = []
        self._session = SimpleNamespace(cookies={"csrftoken": "anon-csrf"})

    def session(self):
        return self._session

    def get(self, url, **kwargs):
        return SimpleNamespace(status_code=200, text=(FIXTURES / "ig_profile.html").read_text(), url=url)

    def post(self, url, data=None, headers=None, **kwargs):
        self.sent.append((data, headers))
        status, text = self.posts.pop(0)
        return SimpleNamespace(status_code=status, text=text, url=url)


def client(posts, now=NEWEST + DAY):
    t = FakeTransport(posts)
    return InstagramClient(t, Config(places=("Sylhet",)), "nasa", now=lambda: now), t


def test_profile_posts_from_fixture():
    c, t = client([(200, (FIXTURES / "ig_profile_posts.json").read_text())])
    result = c.fetch_posts(max_pages=1)
    assert result.outcome is Outcome.OK
    assert len(result.items) == 10
    post = next(p for p in result.items if p["item_id"] == "3872331137620292903")
    assert post["permalink"] == "https://www.instagram.com/p/DW9SKc2irUn/"
    assert post["created_at"] == NEWEST
    assert post["comment_count"] == 3490
    assert post["is_video"] is True
    assert post["images"] and post["text"]
    assert any(len(p["images"]) > 1 for p in result.items)
    data, headers = t.sent[0]
    assert data["lsd"] and headers["x-fb-lsd"] == data["lsd"] and headers["x-csrftoken"] == "anon-csrf"
    assert c.owner_id == "528817151"


def test_profile_posts_stop_at_60_days():
    c, _ = client([(200, (FIXTURES / "ig_profile_posts.json").read_text())], now=1775780000 + 60 * DAY)
    result = c.fetch_posts()
    assert result.items and all(p["created_at"] > 1775780000 for p in result.items)


def test_post_comments_from_fixture():
    c, t = client([(200, (FIXTURES / "ig_post.json").read_text())])
    result = c.fetch_comments({"item_id": "4001971394119634476", "permalink": "https://www.instagram.com/p/DeJ28eTxo4s/"})
    assert result.outcome is Outcome.OK
    assert len(result.items) == 14
    assert all(isinstance(x["created_at"], int) and x["is_owner"] is False for x in result.items)
    assert json.loads(t.sent[0][0]["variables"]) == {"media_id": "4001971394119634476"}
    dumped = json.dumps(result.items)
    assert "username" not in dumped and "profile_pic" not in dumped


def test_401_require_login_is_blocked():
    c, _ = client([(401, (FIXTURES / "ig_require_login.json").read_text())])
    assert c.fetch_posts().outcome is Outcome.BLOCKED


def test_profile_without_lsd_token_is_schema_changed():
    c, t = client([])
    t.get = lambda url, **kw: SimpleNamespace(status_code=200, text="<html>new layout</html>", url=url)
    assert c.fetch_posts().outcome is Outcome.SCHEMA_CHANGED
