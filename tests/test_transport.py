from itertools import pairwise
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

from crawler.config import Config
from crawler.sessions import SessionStore
from crawler.transport import HostBlocked, Transport

FB = "https://www.facebook.com/api/graphql/"
IG = "https://www.instagram.com/api/graphql"


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeSession:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        r = self.replies.pop(0) if self.replies else SimpleNamespace(status_code=200, text="{}", url=url)
        if isinstance(r, Exception):
            raise r
        return r


def make(replies=(), rate=2.0):
    clock = FakeClock()
    session = FakeSession(replies)
    t = Transport(
        Config(places=("Sylhet",), rate_per_second=rate),
        session_factory=lambda: session,
        clock=clock.time,
        sleep=clock.sleep,
        jitter=lambda lo, hi: 1.0,
    )
    return t, session, clock


def ok(url=FB):
    return SimpleNamespace(status_code=200, text="{}", url=url)


def spacing(t, clock, url):
    """Seconds the limiter now waits between two requests to url (1 / rate)."""
    t.post(url, data={})
    start = clock.now
    t.post(url, data={})
    return clock.now - start


def test_limiter_spaces_requests():
    t, _, clock = make()
    stamps = []
    for _ in range(4):
        t.post(FB, data={})
        stamps.append(clock.now)
    gaps = [b - a for a, b in pairwise(stamps)]
    assert gaps == pytest.approx([0.5, 0.5, 0.5])


def test_jitter_stays_within_twenty_percent():
    seen = []
    t, _, _ = make()
    t._jitter = lambda lo, hi: seen.append((lo, hi)) or hi
    t.post(FB, data={})
    t.post(FB, data={})
    assert seen and all(pair == (0.8, 1.2) for pair in seen)


def test_rate_halves_on_drop():
    t, session, clock = make([CurlConnectionError("reset"), ok()])
    r = t.post(FB, data={})
    assert r.status_code == 200
    assert len(session.calls) == 2
    assert spacing(t, clock, FB) == pytest.approx(1.0)


def test_second_drop_is_raised_not_retried_again():
    t, session, _ = make([CurlConnectionError("reset"), CurlConnectionError("reset"), ok()])
    with pytest.raises(CurlConnectionError):
        t.post(FB, data={})
    assert len(session.calls) == 2


def test_hosts_have_separate_limiters():
    t, _, clock = make([CurlConnectionError("reset"), ok()])
    t.post(FB, data={})
    assert spacing(t, clock, IG) == pytest.approx(0.5)
    assert spacing(t, clock, FB) == pytest.approx(1.0)


def test_blocked_host_cools_down():
    t, _, clock = make([SimpleNamespace(status_code=429, text="", url=FB), ok(), ok(IG)])
    assert t.post(FB, data={}).status_code == 429
    with pytest.raises(HostBlocked):
        t.post(FB, data={})
    assert t.post(IG, data={}).status_code == 200
    clock.now += t.cooldown_seconds + 1
    assert t.post(FB, data={}).status_code == 200


def test_classified_block_cools_down_the_whole_host():
    t, _, _ = make()
    assert t.report_block("https://www.facebook.com/login/?next=x") == "cooldown"
    with pytest.raises(HostBlocked):
        t.get("https://www.facebook.com/marketplace/1/search?query=a")


def test_every_request_sets_timeout():
    t, session, _ = make()
    t.get(FB)
    t.post(IG, data={})
    assert all(kw.get("timeout") for _, _, kw in session.calls)


def test_429_is_not_retried():
    t, session, _ = make([SimpleNamespace(status_code=429, text="", url=FB), ok()])
    t.post(FB, data={})
    assert len(session.calls) == 1


def test_rate_recovers_slowly_after_drop():
    t, _, clock = make([CurlConnectionError("reset")])
    t.post(FB, data={})  # dropped, then the retry is success 1
    for _ in range(t.recovery_after - 4):
        t.post(FB, data={})
    assert spacing(t, clock, FB) == pytest.approx(1.0)  # successes 18 and 19
    t.post(FB, data={})  # success 20 speeds up the slots booked after it
    assert 0.5 <= spacing(t, clock, FB) < 1.0


def test_cooldown_started_while_waiting_stops_the_request():
    t, session, clock = make()
    t.post(FB, data={})
    real_sleep = clock.sleep

    def sleep_then_block(seconds):
        real_sleep(seconds)
        t.report_block(FB)

    t._sleep = sleep_then_block
    with pytest.raises(HostBlocked):
        t.post(FB, data={})
    assert len(session.calls) == 1


FB_COOKIES = ".facebook.com\tTRUE\t/\tTRUE\t1893456000\tc_user\t1000001\n" \
             ".facebook.com\tTRUE\t/\tTRUE\t1893456000\txs\tPLANTED-XS\n"
HOME = '<script>["DTSGInitialData",[],{"token":"DTSG1"},258]</script>'


class FakeCookies(dict):
    def set(self, name, value, domain=None):
        self[name] = value


class RecordingSession:
    def __init__(self, handler):
        self.cookies = FakeCookies()
        self.calls = []
        self.handler = handler

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.handler(self, method, url, kwargs)


def login_transport(tmp_path, handler=None, with_session=True):
    clock = FakeClock()
    sessions = SessionStore(tmp_path, clock=clock.time)
    if with_session:
        sessions.import_export("fb", FB_COOKIES)
    made = []

    def default(session, method, url, kwargs):
        text = HOME if method == "GET" and url == "https://www.facebook.com/" else "{}"
        return SimpleNamespace(status_code=200, text=text, url=url)

    def factory():
        made.append(RecordingSession(handler or default))
        return made[-1]

    t = Transport(Config(places=("Sylhet",)), session_factory=factory, clock=clock.time, sleep=clock.sleep,
                  jitter=lambda lo, hi: 1.0, sessions=sessions)
    return t, made, clock, sessions


def form():
    return {"av": "0", "__user": "0", "fb_dtsg": "", "doc_id": "1", "variables": "{}"}


def test_no_session_never_sends_cookies(tmp_path):
    t, made, _, _ = login_transport(tmp_path, with_session=False)
    t.post(FB, data=form())
    assert t.report_block(FB) == "cooldown"
    assert len(made) == 1 and made[0].cookies == {}
    assert made[0].calls[0][2]["data"]["av"] == "0" and made[0].calls[0][2]["data"]["fb_dtsg"] == ""


def test_block_without_session_cools_down(tmp_path):
    t, _, _, _ = login_transport(tmp_path, with_session=False)
    assert t.report_block(FB) == "cooldown"
    with pytest.raises(HostBlocked):
        t.post(FB, data=form())


def test_block_switches_to_login_when_session_exists(tmp_path):
    t, made, clock, sessions = login_transport(tmp_path)
    t.post(FB, data=form())
    assert t.report_block(FB) == "login"
    t.post(FB, data=form())
    anonymous, logged_in = made
    assert anonymous.cookies == {}
    assert logged_in.cookies["xs"] == "PLANTED-XS"
    assert sessions.state("fb") == "in use"
    assert spacing(t, clock, FB) == pytest.approx(0.5)


def test_login_mode_fills_fb_form_identity(tmp_path):
    t, made, _, _ = login_transport(tmp_path)
    t.report_block(FB)
    sent_form = form()
    t.post(FB, data=sent_form)
    calls = made[0].calls
    assert calls[0][:2] == ("GET", "https://www.facebook.com/")
    data = calls[1][2]["data"]
    assert (data["av"], data["__user"], data["fb_dtsg"]) == ("1000001", "1000001", "DTSG1")
    assert sent_form["av"] == "0"
    t.post(FB, data=form())
    assert [c[0] for c in calls] == ["GET", "POST", "POST"]


def test_stale_fb_dtsg_is_refetched_once(tmp_path):
    tokens = iter(["OLD", "NEW", "NEWER"])

    def handler(session, method, url, kwargs):
        if method == "GET":
            return SimpleNamespace(status_code=200, url=url,
                                   text=f'["DTSGInitialData",[],{{"token":"{next(tokens)}"}},1]')
        stale = kwargs["data"]["fb_dtsg"] != "NEW"
        return SimpleNamespace(status_code=200, url=url, text='for (;;);{"error":1357004}' if stale else "{}")

    t, made, _, _ = login_transport(tmp_path, handler)
    t.report_block(FB)
    assert t.post(FB, data=form()).text == "{}"
    assert [c[0] for c in made[0].calls] == ["GET", "POST", "GET", "POST"]


def test_stale_fb_dtsg_twice_is_not_looped(tmp_path):
    def handler(session, method, url, kwargs):
        text = '["DTSGInitialData",[],{"token":"T"},1]' if method == "GET" else 'for (;;);{"error":1357004}'
        return SimpleNamespace(status_code=200, url=url, text=text)

    t, made, _, _ = login_transport(tmp_path, handler)
    t.report_block(FB)
    t.post(FB, data=form())
    assert [c[0] for c in made[0].calls] == ["GET", "POST", "GET", "POST"]


def test_block_while_logged_in_expires_session_and_cools_down(tmp_path):
    t, _, _, sessions = login_transport(tmp_path)
    assert t.report_block(FB) == "login"
    assert t.report_block(FB) == "expired"
    assert sessions.state("fb") == "expired"
    with pytest.raises(HostBlocked):
        t.post(FB, data=form())


def test_login_mode_ends_after_window(tmp_path):
    t, made, clock, sessions = login_transport(tmp_path)
    t.report_block(FB)
    clock.now += t.login_seconds + 1
    t.post(FB, data=form())
    assert made[-1].cookies == {} and made[-1].calls[-1][2]["data"]["av"] == "0"
    assert sessions.state("fb") == "ready"


def test_429_with_session_switches_once_not_twice(tmp_path):
    replies = iter([SimpleNamespace(status_code=429, text="", url=FB)])

    def handler(session, method, url, kwargs):
        if method == "GET":
            return SimpleNamespace(status_code=200, url=url, text=HOME)
        return next(replies, SimpleNamespace(status_code=200, text="{}", url=url))

    t, _, _, sessions = login_transport(tmp_path, handler)
    token = t.mode_token(FB)
    assert t.post(FB, data=form()).status_code == 429
    assert t.report_block(FB, token) == "login"
    assert sessions.state("fb") == "in use"
    assert t.post(FB, data=form()).status_code == 200


def test_instagram_cookie_comes_from_the_session_in_use(tmp_path):
    t, made, _, sessions = login_transport(tmp_path, with_session=False)
    sessions.import_export("ig", '[{"domain":".instagram.com","name":"sessionid","value":"S"},'
                                 '{"domain":".instagram.com","name":"csrftoken","value":"LOGGED-CSRF"}]')
    t.get(IG)
    made[0].cookies["csrftoken"] = "ANON-CSRF"
    assert t.cookie(IG, "csrftoken") == "ANON-CSRF"
    t.report_block(IG)
    assert t.cookie(IG, "csrftoken") == "LOGGED-CSRF"


def test_parallel_block_reports_switch_once(tmp_path):
    t, _, _, sessions = login_transport(tmp_path)
    token = t.mode_token(FB)
    assert t.report_block(FB, token) == "login"
    assert t.report_block(FB, token) == "login"
    assert sessions.state("fb") == "in use"


def test_in_use_shows_with_real_clocks(tmp_path):
    sessions = SessionStore(tmp_path)
    sessions.import_export("fb", FB_COOKIES)
    t = Transport(Config(places=("Sylhet",)), session_factory=lambda: RecordingSession(lambda *a: None),
                  sessions=sessions)
    t.report_block(FB)
    assert sessions.state("fb") == "in use"


def test_rate_limit_while_logged_in_cools_down_without_expiring(tmp_path):
    replies = iter([SimpleNamespace(status_code=429, text="", url=FB),
                    SimpleNamespace(status_code=200, url=FB,
                                    text='{"errors":[{"code":1675004,"summary":"Rate limit exceeded"}]}')])

    def handler(session, method, url, kwargs):
        if method == "GET":
            return SimpleNamespace(status_code=200, url=url, text=HOME)
        return next(replies)

    t, _, clock, sessions = login_transport(tmp_path, handler)
    t.report_block(FB)
    token = t.mode_token(FB)
    t.post(FB, data=form())
    assert t.report_block(FB, token) == "cooldown" and sessions.state("fb") != "expired"
    clock.now += t.cooldown_seconds + 1
    t.report_block(FB)
    token = t.mode_token(FB)
    t.post(FB, data=form())
    assert t.report_block(FB, token) == "cooldown" and sessions.state("fb") != "expired"


def test_missing_fb_dtsg_is_fetched_once_per_login(tmp_path):
    pages = iter(["<html>checkpoint</html>", HOME])

    def handler(session, method, url, kwargs):
        if method == "GET":
            return SimpleNamespace(status_code=200, url=url, text=next(pages))
        return SimpleNamespace(status_code=200, url=url, text="{}")

    t, made, _, _ = login_transport(tmp_path, handler)
    t.report_block(FB)
    t.post(FB, data=form())
    t.post(FB, data=form())
    posts = [c for c in made[0].calls if c[0] == "POST"]
    assert [p[2]["data"]["fb_dtsg"] for p in posts] == ["", ""]
    assert [c[0] for c in made[0].calls].count("GET") == 1


def test_refusal_of_a_logged_in_request_after_the_window_expires_the_login(tmp_path):
    t, _, clock, sessions = login_transport(tmp_path)
    t.report_block(FB)
    token = t.mode_token(FB)
    clock.now += t.login_seconds + 1
    assert t.report_block(FB, token) == "expired" and sessions.state("fb") == "expired"


def test_logged_out_request_after_the_window_does_not_expire(tmp_path):
    t, _, clock, sessions = login_transport(tmp_path)
    t.report_block(FB)
    clock.now += t.login_seconds + 1
    t.post(FB, data=form())
    token = t.mode_token(FB)
    assert t.report_block(FB, token) == "login" and sessions.state("fb") == "in use"


def test_block_report_during_a_cooldown_does_not_extend_it(tmp_path):
    t, _, clock, _ = login_transport(tmp_path, with_session=False)
    t.report_block(FB)
    first_until = t._limiter(FB).blocked_until
    clock.now += 10
    t.report_block(FB, t.mode_token(FB))
    assert t._limiter(FB).blocked_until == first_until
    assert spacing_after_cooldown(t, clock) == pytest.approx(1.0)


def spacing_after_cooldown(t, clock):
    clock.now = t._limiter(FB).blocked_until + 1
    return spacing(t, clock, FB)


def test_old_logged_in_sessions_are_dropped(tmp_path):
    t, _, clock, _ = login_transport(tmp_path)
    for _ in range(3):
        t.report_block(FB)
        t.post(FB, data=form())
        clock.now += t.login_seconds + 1
    assert len(t._local.logged_in) == 1


def test_late_refusal_of_a_logged_in_request_still_expires_the_login(tmp_path):
    import threading

    t, _, clock, sessions = login_transport(tmp_path)
    t.report_block(FB)
    token = t.mode_token(FB)
    t.post(FB, data=form())  # this thread's request went out logged in and was refused
    clock.now += t.login_seconds + 1
    other = threading.Thread(target=lambda: t.post(FB, data=form()))  # another thread, logged out
    other.start()
    other.join()
    assert t.report_block(FB, token) == "expired" and sessions.state("fb") == "expired"


def test_block_on_a_request_sent_after_the_window_does_not_expire(tmp_path):
    t, _, clock, sessions = login_transport(tmp_path)
    t.force_login(FB)
    token = t.mode_token(FB)
    clock.now += t.login_seconds + 1
    t.post(FB, data=form())  # the window ran out: this one went out logged out
    assert t.report_block(FB, token) != "expired" and sessions.state("fb") != "expired"


def test_expired_label_only_for_the_thread_that_sent_the_login(tmp_path):
    import threading

    t, _, _, _ = login_transport(tmp_path)
    t.report_block(FB)
    t.post(FB, data=form())
    assert t.report_block(FB, t.mode_token(FB)) == "expired"
    seen = []
    other = threading.Thread(target=lambda: seen.append(t.report_block(FB)))
    other.start()
    other.join()
    assert seen == ["cooldown"]


def test_rate_limit_json_with_spaces_keeps_the_login(tmp_path):
    def handler(session, method, url, kwargs):
        if method == "GET":
            return SimpleNamespace(status_code=200, url=url, text=HOME)
        return SimpleNamespace(status_code=200, url=url,
                               text='{"errors": [{"code": 1675004, "summary": "Rate limit exceeded"}]}')

    t, _, _, sessions = login_transport(tmp_path, handler)
    t.report_block(FB)
    token = t.mode_token(FB)
    t.post(FB, data=form())
    assert t.report_block(FB, token) == "cooldown" and sessions.state("fb") != "expired"


def test_logout_stops_a_running_login(tmp_path):
    t, made, _, sessions = login_transport(tmp_path)
    t.report_block(FB)
    t.post(FB, data=form())
    sessions.remove("fb")
    t.post(FB, data=form())
    last = made[-1].calls[-1] if made[-1].calls else made[0].calls[-1]
    assert last[2]["data"]["av"] == "0" and not made[-1].cookies


def test_a_leftover_send_from_an_older_mode_is_ignored(tmp_path):
    t, _, clock, sessions = login_transport(tmp_path)
    t.report_block(FB)
    t.post(FB, data=form())  # this thread's last send: logged in, in the old login window
    clock.now += t.login_seconds + 1
    t.report_block(FB)  # a later logged-out block starts a new login window (new generation)
    token = t.mode_token(FB)
    clock.now += t.login_seconds + 1  # ...which also ends without this thread sending anything
    assert t.report_block(FB, (token[0], False)) != "expired" and sessions.state("fb") != "expired"
