import logging
import random
import re
import threading
import time
from urllib.parse import urlparse

from curl_cffi import requests as curl
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

from .config import Config
from .outcomes import rate_limited
from .sessions import SITES, SessionStore, site_of

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 30
FB_HOME = "https://www.facebook.com/"
DTSG = re.compile(r'"DTSGInitialData",\[\],\{"token":"([^"]+)"')
STALE_DTSG = '"error":1357004'
DROPS = (CurlConnectionError, CurlTimeout)
MIN_RATE = 0.25


class HostBlocked(Exception):
    def __init__(self, host: str, until: float):
        super().__init__(f"{host} is cooling down after a block")
        self.host = host
        self.until = until


def host_key(url: str) -> str:
    return ".".join((urlparse(url).hostname or "").split(".")[-2:])


class HostLimiter:
    def __init__(self, rate: float, ceiling: float):
        self.base = min(rate, ceiling)
        self.rate = self.base
        self.next_at = 0.0
        self.blocked_until = 0.0
        self.successes = 0
        self.login_until = 0.0
        self.generation = 0
        self.last_report = "cooldown"
        self.lock = threading.Lock()


class Transport:
    """Chrome-impersonating HTTP with one polite limiter per host (facebook.com, instagram.com)."""

    cooldown_seconds = 15 * 60
    login_seconds = 15 * 60
    recovery_after = 20

    def __init__(self, config: Config, session_factory=None, clock=time.monotonic, sleep=time.sleep,
                 jitter=random.uniform, sessions: SessionStore | None = None):
        self._config = config
        self._session_factory = session_factory or (lambda: curl.Session(impersonate="chrome"))
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._limiters: dict[str, HostLimiter] = {}
        self._limiters_lock = threading.Lock()
        self._local = threading.local()
        self._sessions = sessions
        self._dtsg: tuple[int, str | None] | None = None
        self._dtsg_lock = threading.Lock()

    def session(self):
        """This thread's session; cookies set by one request carry to the next on the same thread."""
        if not hasattr(self._local, "session"):
            self._local.session = self._session_factory()
        return self._local.session

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def cookie(self, url: str, name: str) -> str | None:
        return self._session_for(url, self._limiter(url)).cookies.get(name)

    def mode_token(self, url: str) -> tuple[int, bool]:
        """Take before a fetch: (mode generation, logged in now). A block reported with an old generation is
        ignored because another thread already acted on one."""
        limiter = self._limiter(url)
        return limiter.generation, self._logged_in_now(url, limiter)

    def force_login(self, url: str) -> bool:
        limiter, site = self._limiter(url), site_of(url)
        with limiter.lock:
            if not (self._sessions and site and self._sessions.load(site)):
                return False
            self._start_login(limiter, site)
            return True

    def report_block(self, url: str, token: tuple[int, bool] | None = None) -> str:
        """A site said "blocked": switch to the imported login ("login"), expire it ("expired"), or cool down."""
        limiter, site = self._limiter(url), site_of(url)
        # How this thread's own last request to the host went out decides; the fetch that got blocked ran here.
        if token is None:
            sent_logged_in, slowed_down = self._login_active(limiter), False
        else:
            sent_logged_in, slowed_down = self._last_sent().get(host_key(url), (token[1], False))
        with limiter.lock:
            now = self._clock()
            if (token is not None and token[0] != limiter.generation) or now < limiter.blocked_until:
                report = limiter.last_report
                return "cooldown" if report == "expired" and not sent_logged_in else report
            if sent_logged_in and slowed_down:
                self._cool_down(limiter, "cooldown")
            elif sent_logged_in:
                self._sessions.mark_expired(site)
                limiter.login_until = 0.0
                self._cool_down(limiter, "expired")
            elif self._sessions and site and self._sessions.load(site):
                self._start_login(limiter, site)
            else:
                self._cool_down(limiter, "cooldown")
            log.info("%s blocked: %s", host_key(url), limiter.last_report)
            return limiter.last_report

    def request(self, method: str, url: str, **kwargs):
        limiter = self._limiter(url)
        kwargs.setdefault("timeout", TIMEOUT_SECONDS)
        token = self.mode_token(url)
        logged_in = token[1]
        fills_identity = logged_in and method == "POST" and site_of(url) == "fb" and isinstance(kwargs.get("data"), dict)
        if fills_identity:
            kwargs["data"] = self._with_fb_identity(kwargs["data"], limiter)
        response = self._send(method, url, limiter, kwargs, logged_in)
        if fills_identity and STALE_DTSG in response.text[:500]:
            kwargs["data"] = self._with_fb_identity(kwargs["data"], limiter, stale=kwargs["data"]["fb_dtsg"])
            response = self._send(method, url, limiter, kwargs, logged_in)
        slowed_down = rate_limited(response)
        self._last_sent()[host_key(url)] = (logged_in, slowed_down)
        if slowed_down:
            self.report_block(url, token)
        else:
            self._note_success(limiter)
        return response

    def _last_sent(self) -> dict:
        return self._local.__dict__.setdefault("last_sent", {})

    def _logged_in_now(self, url: str, limiter: HostLimiter) -> bool:
        site = site_of(url)
        return bool(site and self._login_active(limiter) and self._sessions and self._sessions.load(site))

    def _send(self, method: str, url: str, limiter: HostLimiter, kwargs: dict, logged_in: bool):
        for attempt in (1, 2):
            self._wait_turn(url, limiter)
            session = self._logged_in_session(url, limiter) if logged_in else self.session()
            try:
                return session.request(method, url, **kwargs)
            except DROPS:
                with limiter.lock:
                    self._back_off(limiter)
                if attempt == 2:
                    raise

    def _login_active(self, limiter: HostLimiter) -> bool:
        return limiter.login_until > self._clock()

    def _start_login(self, limiter: HostLimiter, site: str) -> None:
        limiter.login_until = self._clock() + self.login_seconds
        self._sessions.mark_in_use(site, self.login_seconds)
        limiter.generation += 1
        limiter.last_report = "login"

    def _cool_down(self, limiter: HostLimiter, report: str) -> None:
        self._back_off(limiter)
        limiter.blocked_until = self._clock() + self.cooldown_seconds
        limiter.generation += 1
        limiter.last_report = report

    def _session_for(self, url: str, limiter: HostLimiter):
        return self._logged_in_session(url, limiter) if self._logged_in_now(url, limiter) else self.session()

    def _logged_in_session(self, url: str, limiter: HostLimiter):
        site = site_of(url)
        cache = self._local.__dict__.setdefault("logged_in", {})
        key = (site, limiter.generation)
        if key not in cache:
            for old in [k for k in cache if k[0] == site]:
                stale = cache.pop(old)
                if hasattr(stale, "close"):
                    stale.close()
            session = self._session_factory()
            for name, value in (self._sessions.load(site) or {}).items():
                session.cookies.set(name, value, domain="." + SITES[site][0])
            cache[key] = session
        return cache[key]

    def _with_fb_identity(self, data: dict, limiter: HostLimiter, stale: str | None = None) -> dict:
        """One home-page read per login window; a stale token is refetched once, by the first thread to see it."""
        user = (self._sessions.load("fb") or {}).get("c_user")
        with self._dtsg_lock:
            cached = self._dtsg if self._dtsg and self._dtsg[0] == limiter.generation else None
            if cached is None or (stale is not None and (cached[1] or "") == stale):
                home = self._send("GET", FB_HOME, limiter, {"timeout": TIMEOUT_SECONDS}, True)
                found = DTSG.search(home.text)
                cached = (limiter.generation, found.group(1) if found else None)
                self._dtsg = cached
        return {**data, "av": user, "__user": user, "fb_dtsg": cached[1] or ""}

    def _limiter(self, url: str) -> HostLimiter:
        key = host_key(url)
        with self._limiters_lock:
            if key not in self._limiters:
                self._limiters[key] = HostLimiter(self._config.rate_per_second, self._config.rate_ceiling)
            return self._limiters[key]

    def _wait_turn(self, url: str, limiter: HostLimiter) -> None:
        with limiter.lock:
            now = self._clock()
            if now < limiter.blocked_until:
                raise HostBlocked(host_key(url), limiter.blocked_until)
            slot = max(now, limiter.next_at)
            limiter.next_at = slot + (1 / limiter.rate) * self._jitter(0.8, 1.2)
        if slot > now:
            self._sleep(slot - now)
            with limiter.lock:
                if self._clock() < limiter.blocked_until:
                    raise HostBlocked(host_key(url), limiter.blocked_until)

    def _back_off(self, limiter: HostLimiter) -> None:
        limiter.rate = max(MIN_RATE, limiter.rate / 2)
        limiter.successes = 0

    def _note_success(self, limiter: HostLimiter) -> None:
        with limiter.lock:
            limiter.successes += 1
            if limiter.successes >= self.recovery_after and limiter.rate < limiter.base:
                limiter.rate = min(limiter.base, limiter.rate * 1.25)
                limiter.successes = 0
