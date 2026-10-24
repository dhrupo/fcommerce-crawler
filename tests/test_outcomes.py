import json
from pathlib import Path
from types import SimpleNamespace

from crawler.outcomes import Outcome, classify

FIXTURES = Path(__file__).parent / "fixtures"
TIMELINE = ("node", "timeline_list_feed_units")
WARNING = {"message": "A server error missing_required_variable_value occured.", "severity": "WARNING"}


def reply(text="", status=200, url="https://www.facebook.com/api/graphql/"):
    return SimpleNamespace(status_code=status, url=url, text=text)


def body(data, errors=None):
    doc = {"data": data}
    if errors:
        doc["errors"] = errors
    return json.dumps(doc)


def test_429_is_blocked():
    assert classify(reply("", status=429), TIMELINE) is Outcome.BLOCKED


def test_login_redirect_is_blocked():
    r = reply("<html>Log in</html>", url="https://www.facebook.com/login/?next=%2Fmarketplace")
    assert classify(r) is Outcome.BLOCKED
    r = reply("<html></html>", url="https://www.instagram.com/accounts/login/?next=%2Fapi%2Fv1%2Fmedia")
    assert classify(r) is Outcome.BLOCKED


def test_fb_error_1675004_is_blocked():
    errors = [{"code": 1675004, "summary": "Rate limit exceeded", "severity": "CRITICAL"}]
    assert classify(reply(body(None, errors)), TIMELINE) is Outcome.BLOCKED


def test_post_text_mentioning_1675004_is_not_blocked():
    data = {"node": {"timeline_list_feed_units": {"edges": [{"node": {"message": "call 01675004"}}]}}}
    assert classify(reply(body(data)), TIMELINE) is Outcome.OK


def test_200_missing_keys_is_schema_changed():
    assert classify(reply(body({"node": {"__typename": "User", "id": "1"}})), TIMELINE) is Outcome.SCHEMA_CHANGED
    assert classify(reply("not json"), TIMELINE) is Outcome.SCHEMA_CHANGED


def test_missing_required_variable_is_schema_changed():
    data = {"node": {"__typename": "User", "id": "1", "timeline_list_feed_units": None}}
    assert classify(reply(body(data, [WARNING])), TIMELINE) is Outcome.SCHEMA_CHANGED


def test_missing_required_variable_warning_with_data_is_ok():
    text = (FIXTURES / "fb_timeline_p1.json").read_text()
    assert "missing_required_variable_value" in text
    assert classify(reply(text), TIMELINE) is Outcome.OK


def test_invalid_cursor_fixture_is_schema_changed():
    text = (FIXTURES / "fb_timeline_invalid_cursor.json").read_text()
    assert classify(reply(text), TIMELINE) is Outcome.SCHEMA_CHANGED


def test_empty_with_page_info_is_nothing_new():
    data = {"node": {"timeline_list_feed_units": {"edges": [], "page_info": {"has_next_page": False}}}}
    assert classify(reply(body(data)), TIMELINE) is Outcome.NOTHING_NEW


def test_empty_200_without_page_info_is_schema_changed_not_nothing_new():
    data = {"node": {"timeline_list_feed_units": {"edges": []}}}
    assert classify(reply(body(data)), TIMELINE) is Outcome.SCHEMA_CHANGED


def test_ig_401_require_login_is_blocked():
    text = (FIXTURES / "ig_require_login.json").read_text()
    path = ("xdt_api__v1__feed__user_timeline_graphql_connection",)
    assert classify(reply(text, status=401, url="https://www.instagram.com/api/graphql"), path) is Outcome.BLOCKED


def test_404_is_gone():
    assert classify(reply("", status=404)) is Outcome.GONE


def test_fb_login_required_reply_is_blocked():
    assert classify(reply('for (;;);{"__ar":1,"error":1357001,"errorSummary":"Please log in"}'), TIMELINE) \
        is Outcome.BLOCKED


def test_login_expired_is_a_label():
    assert Outcome.LOGIN_EXPIRED == "login_expired"
