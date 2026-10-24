from pathlib import Path
from types import SimpleNamespace

from crawler.facebook.bootstrap import group_id_from_url, page_id_from_html, resolve_page_id
from crawler.outcomes import Outcome

FIXTURES = Path(__file__).parent / "fixtures"


class FakeTransport:
    def __init__(self, pages):
        self.pages = pages
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        status, text = self.pages.get(url.split("?")[0], (404, ""))
        return SimpleNamespace(status_code=status, text=text, url=url)


def test_page_id_from_fixture_html():
    assert page_id_from_html((FIXTURES / "fb_page.html").read_text()) == "100057210291191"


def test_viewer_id_zero_is_not_a_page_id():
    assert page_id_from_html('{"userID":"0"}') is None


def test_numeric_url_needs_no_request():
    t = FakeTransport({})
    assert resolve_page_id(t, "https://www.facebook.com/profile.php?id=100057210291191") == (Outcome.OK, "100057210291191")
    assert t.urls == []


def test_falls_back_to_page_plugin():
    t = FakeTransport({
        "https://www.facebook.com/iStoreSyl": (200, "<html>no ids here</html>"),
        "https://www.facebook.com/plugins/page.php": (200, '<a href="https://www.facebook.com/x" data-pageid="1"></a>'
                                                            '{"pageID":"100057210291191"}'),
    })
    assert resolve_page_id(t, "https://www.facebook.com/iStoreSyl") == (Outcome.OK, "100057210291191")
    assert len(t.urls) == 2


def test_page_not_found_is_gone():
    t = FakeTransport({"https://www.facebook.com/iStoreSyl": (200, "<html></html>"),
                       "https://www.facebook.com/plugins/page.php": (200, "<html></html>")})
    assert resolve_page_id(t, "https://www.facebook.com/iStoreSyl") == (Outcome.GONE, None)


def test_login_wall_is_blocked():
    t = FakeTransport({})
    t.get = lambda url, **kw: SimpleNamespace(status_code=200, text="", url="https://www.facebook.com/login/?next=x")
    assert resolve_page_id(t, "https://www.facebook.com/iStoreSyl") == (Outcome.BLOCKED, None)


def test_vanity_group_url_returns_gone_or_needs_id():
    assert group_id_from_url("https://www.facebook.com/groups/392585550772135/") == "392585550772135"
    assert group_id_from_url("https://www.facebook.com/groups/sylhet.mobile.market") is None
