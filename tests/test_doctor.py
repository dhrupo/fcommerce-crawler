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
