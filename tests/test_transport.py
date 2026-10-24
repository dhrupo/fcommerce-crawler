from itertools import pairwise
from types import SimpleNamespace

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError

from crawler.config import Config
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


def test_mark_blocked_after_classified_block():
    t, _, _ = make()
    t.mark_blocked("https://www.facebook.com/login/?next=x")
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
