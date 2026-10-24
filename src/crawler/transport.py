import random
import threading
import time
from urllib.parse import urlparse

from curl_cffi import requests as curl
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

from .config import Config

TIMEOUT_SECONDS = 30
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
        self.lock = threading.Lock()


class Transport:
    """Chrome-impersonating HTTP with one polite limiter per host (facebook.com, instagram.com)."""

    cooldown_seconds = 15 * 60
    recovery_after = 20

    def __init__(self, config: Config, session_factory=None, clock=time.monotonic, sleep=time.sleep,
                 jitter=random.uniform):
        self._config = config
        self._session_factory = session_factory or (lambda: curl.Session(impersonate="chrome"))
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._limiters: dict[str, HostLimiter] = {}
        self._limiters_lock = threading.Lock()
        self._local = threading.local()

    def session(self):
        """This thread's session; cookies set by one request carry to the next on the same thread."""
        if not hasattr(self._local, "session"):
            self._local.session = self._session_factory()
        return self._local.session

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def rate_for(self, url: str) -> float:
        return self._limiter(url).rate

    def mark_blocked(self, url: str) -> None:
        limiter = self._limiter(url)
        with limiter.lock:
            self._back_off(limiter)
            limiter.blocked_until = self._clock() + self.cooldown_seconds

    def request(self, method: str, url: str, **kwargs):
        limiter = self._limiter(url)
        kwargs.setdefault("timeout", TIMEOUT_SECONDS)
        for attempt in (1, 2):
            self._wait_turn(url, limiter)
            try:
                response = self.session().request(method, url, **kwargs)
            except DROPS:
                with limiter.lock:
                    self._back_off(limiter)
                if attempt == 2:
                    raise
                continue
            if response.status_code == 429:
                self.mark_blocked(url)
            else:
                self._note_success(limiter)
            return response

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

    def _back_off(self, limiter: HostLimiter) -> None:
        limiter.rate = max(MIN_RATE, limiter.rate / 2)
        limiter.successes = 0

    def _note_success(self, limiter: HostLimiter) -> None:
        with limiter.lock:
            limiter.successes += 1
            if limiter.successes >= self.recovery_after and limiter.rate < limiter.base:
                limiter.rate = min(limiter.base, limiter.rate * 1.25)
                limiter.successes = 0
