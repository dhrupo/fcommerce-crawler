import json
import stat

import pytest

from crawler.sessions import SessionError, SessionStore, read_cookie_export

NETSCAPE = """# Netscape HTTP Cookie File
.facebook.com\tTRUE\t/\tTRUE\t1893456000\tc_user\t100000000000001
#HttpOnly_.facebook.com\tTRUE\t/\tTRUE\t1893456000\txs\tPLANTED-XS-VALUE
.facebook.com\tTRUE\t/\tTRUE\t1893456000\tdatr\tdatrvalue
.instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tIG-SESSION
.google.com\tTRUE\t/\tTRUE\t1893456000\tSID\tgoogle-secret
"""

JSON_EXPORT = json.dumps([
    {"domain": ".instagram.com", "name": "sessionid", "value": "PLANTED-IG-SESSION"},
    {"domain": "www.instagram.com", "name": "csrftoken", "value": "csrf123"},
    {"domain": ".facebook.com", "name": "xs", "value": "fb-xs"},
])


def test_imports_netscape_cookies_txt():
    cookies = read_cookie_export(NETSCAPE)
    assert {"name": "xs", "value": "PLANTED-XS-VALUE", "domain": ".facebook.com"} in cookies
    assert len(cookies) == 5


def test_imports_json_cookie_export():
    assert {"name": "csrftoken", "value": "csrf123", "domain": "www.instagram.com"} in read_cookie_export(JSON_EXPORT)
    wrapped = json.dumps({"cookies": json.loads(JSON_EXPORT)})
    assert len(read_cookie_export(wrapped)) == 3


def test_rejects_file_without_required_cookies(tmp_path):
    store = SessionStore(tmp_path)
    with pytest.raises(SessionError, match="csrftoken"):
        store.import_export("ig", NETSCAPE)
    with pytest.raises(SessionError, match="no cookies"):
        store.import_export("fb", "not a cookie file")
    assert store.state("ig") == "none"


def test_keeps_only_that_sites_cookies(tmp_path):
    store = SessionStore(tmp_path)
    store.import_export("fb", NETSCAPE)
    assert store.load("fb") == {"c_user": "100000000000001", "xs": "PLANTED-XS-VALUE", "datr": "datrvalue"}
    store.import_export("ig", JSON_EXPORT)
    assert store.load("ig") == {"sessionid": "PLANTED-IG-SESSION", "csrftoken": "csrf123"}


def test_session_file_is_private_0600(tmp_path):
    store = SessionStore(tmp_path)
    path = store.import_export("fb", NETSCAPE)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_expired_session_is_not_used(tmp_path):
    store = SessionStore(tmp_path)
    store.import_export("fb", NETSCAPE)
    assert store.state("fb") == "ready"
    store.mark_expired("fb")
    assert store.load("fb") is None and store.state("fb") == "expired"


def test_new_login_clears_expired_flag(tmp_path):
    store = SessionStore(tmp_path)
    store.import_export("fb", NETSCAPE)
    store.mark_expired("fb")
    store.import_export("fb", NETSCAPE)
    assert store.state("fb") == "ready" and store.load("fb")


def test_in_use_until_shown_then_back_to_ready(tmp_path):
    now = [1000.0]
    store = SessionStore(tmp_path, clock=lambda: now[0])
    store.import_export("ig", JSON_EXPORT)
    store.mark_in_use("ig", until=1900.0)
    assert store.state("ig") == "in use"
    now[0] = 2000.0
    assert store.state("ig") == "ready"


def test_remove(tmp_path):
    store = SessionStore(tmp_path)
    store.import_export("fb", NETSCAPE)
    assert store.remove("fb") is True and store.state("fb") == "none"
    assert store.remove("fb") is False
