from pathlib import Path
from types import SimpleNamespace

from crawler.config import Config
from crawler.doctor import doc_ids_in_bundle, scan_doc_ids

FIXTURES = Path(__file__).parent / "fixtures"


def test_finds_doc_id_in_bundle_fixture():
    found = doc_ids_in_bundle((FIXTURES / "fb_bundle.js").read_text())
    assert found["ProfileCometTimelineFeedRefetchQuery"] == "28869336609396559"
    assert "CometNotRelay" not in found


def test_scan_reports_config_drift():
    page = '<script src="https://static.xx.fbcdn.net/rsrc.php/v4/yA/r/abc.js" async></script>'
    bundle = (FIXTURES / "fb_bundle.js").read_text()

    class T:
        def get(self, url, **kw):
            return SimpleNamespace(status_code=200, url=url, text=bundle if url.endswith(".js") else page)

    rows = scan_doc_ids(T(), Config(places=("Sylhet",)), "https://www.facebook.com/iStoreSyl")
    timeline = next(r for r in rows if r["key"] == "fb_timeline")
    assert timeline == {"key": "fb_timeline", "query": "ProfileCometTimelineFeedRefetchQuery",
                        "config": "28591463417151325", "found": "28869336609396559"}
    assert next(r for r in rows if r["key"] == "fb_comments")["found"] is None


def test_scan_reads_inline_base64_bundles():
    import base64

    js = base64.b64encode((FIXTURES / "fb_bundle.js").read_bytes()).decode()
    page = f'<script src="data:application/x-javascript; charset=utf-8;base64,{js}"></script>'

    class T:
        def get(self, url, **kw):
            return SimpleNamespace(status_code=200, url=url, text=page)

    rows = scan_doc_ids(T(), Config(places=("Sylhet",)), "https://www.facebook.com/iStoreSyl")
    assert next(r for r in rows if r["key"] == "fb_timeline")["found"] == "28869336609396559"


def test_scan_reports_blocked_page():
    class T:
        def get(self, url, **kw):
            return SimpleNamespace(status_code=429, url=url, text="")

    rows = scan_doc_ids(T(), Config(places=("Sylhet",)), "https://www.facebook.com/iStoreSyl")
    assert rows and all(r["found"] == "blocked" for r in rows)


def test_live_smoke_survives_a_host_block(tmp_path, capsys):
    from crawler.doctor import live_smoke
    from crawler.store import Store
    from crawler.transport import HostBlocked

    class T:
        def get(self, url, **kw):
            raise HostBlocked("facebook.com", 0)

        post = get

        def cookie(self, url, name):
            return None

        def mode_token(self, url):
            return (0, False)

        def report_block(self, url, token=None):
            return "cooldown"

    config = Config(places=("Sylhet",), marketplace_city_ids={"Sylhet": "1"})
    assert live_smoke(T(), config, Store(tmp_path)) is False
    assert "tested 0 of 4 passed" in capsys.readouterr().out


def test_live_smoke_expires_a_refused_login(tmp_path, capsys):
    from crawler.doctor import live_smoke
    from crawler.store import Store

    reports = []

    class T:
        def get(self, url, **kw):
            return SimpleNamespace(status_code=200, url="https://www.facebook.com/checkpoint/?next=x", text="")

        post = get

        def cookie(self, url, name):
            return None

        def mode_token(self, url):
            return (0, True)

        def report_block(self, url, token=None):
            reports.append(url)
            return "expired"

    config = Config(places=("Sylhet",), marketplace_city_ids={"Sylhet": "1"})
    live_smoke(T(), config, Store(tmp_path))
    out = capsys.readouterr().out
    assert reports and "login_expired" in out
