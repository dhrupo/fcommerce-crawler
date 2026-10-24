import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

SITES = {
    "fb": ("facebook.com", ("c_user", "xs")),
    "ig": ("instagram.com", ("sessionid", "csrftoken")),
}


class SessionError(ValueError):
    pass


def site_of(url: str) -> str | None:
    hostname = urlparse(url).hostname or ""
    return next((site for site, (host, _) in SITES.items() if hostname == host or hostname.endswith("." + host)),
                None)


def read_cookie_export(text: str) -> list[dict]:
    """Cookies from a Netscape cookies.txt or a browser-extension JSON export."""
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict):
        data = data.get("cookies")
    if isinstance(data, list):
        return [{"name": c["name"], "value": c["value"], "domain": c.get("domain") or ""}
                for c in data if isinstance(c, dict) and c.get("name") and "value" in c]
    cookies = []
    for line in text.splitlines():
        line = line.removeprefix("#HttpOnly_")
        parts = line.split("\t")
        if line.startswith("#") or len(parts) != 7:
            continue
        cookies.append({"name": parts[5], "value": parts[6], "domain": parts[0]})
    return cookies


class SessionStore:
    """Imported login cookies, one private JSON file per site under <data_dir>/sessions."""

    def __init__(self, data_dir: Path, clock=time.time):
        self.dir = Path(data_dir) / "sessions"
        self._clock = clock

    def _path(self, site: str) -> Path:
        if site not in SITES:
            raise SessionError(f"unknown site {site!r}; use one of {', '.join(SITES)}")
        return self.dir / f"{site}.json"

    def _read(self, site: str) -> dict | None:
        path = self._path(site)
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _write(self, site: str, record: dict) -> Path:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        path = self._path(site)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(record, f)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return path

    def import_export(self, site: str, text: str) -> Path:
        self._path(site)
        host, required = SITES[site]
        cookies = read_cookie_export(text)
        if not cookies:
            raise SessionError("no cookies found; export them as cookies.txt or JSON")
        mine = {c["name"]: c["value"] for c in cookies if c["domain"].lstrip(".").endswith(host)}
        missing = [name for name in required if not mine.get(name)]
        if missing:
            raise SessionError(f"the export has no {', '.join(missing)} for {host}; log in there first")
        return self._write(site, {"cookies": mine, "imported_at": self._clock(), "expired_at": None,
                                  "in_use_until": None})

    def load(self, site: str) -> dict | None:
        record = self._read(site)
        return record["cookies"] if record and not record.get("expired_at") else None

    def state(self, site: str) -> str:
        record = self._read(site)
        if not record:
            return "none"
        if record.get("expired_at"):
            return "expired"
        return "in use" if (record.get("in_use_until") or 0) > self._clock() else "ready"

    def mark_in_use(self, site: str, seconds: float) -> None:
        """Shown by `status`; kept on this store's own wall clock, not the caller's."""
        if record := self._read(site):
            self._write(site, {**record, "in_use_until": self._clock() + seconds})

    def mark_expired(self, site: str) -> None:
        if record := self._read(site):
            self._write(site, {**record, "expired_at": self._clock(), "in_use_until": None})

    def remove(self, site: str) -> bool:
        try:
            self._path(site).unlink()
            return True
        except FileNotFoundError:
            return False
