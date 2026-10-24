import json
import logging
from types import SimpleNamespace

from crawler.cli import main
from crawler.config import Config
from crawler.search import default_readers, search
from crawler.sessions import SessionStore
from crawler.store import Store
from crawler.transport import Transport

PLANTED = ("PLANTEDXS0000", "PLANTEDCUSER0", "PLANTEDIGSESS", "PLANTEDCSRF00")
FB_EXPORT = (f".facebook.com\tTRUE\t/\tTRUE\t1893456000\tc_user\t{PLANTED[1]}\n"
             f".facebook.com\tTRUE\t/\tTRUE\t1893456000\txs\t{PLANTED[0]}\n")
IG_EXPORT = json.dumps([{"domain": ".instagram.com", "name": "sessionid", "value": PLANTED[2]},
                        {"domain": ".instagram.com", "name": "csrftoken", "value": PLANTED[3]}])
BLOCKED = '{"errors":[{"code":1675004,"summary":"Rate limit exceeded","severity":"CRITICAL"}]}'
TIMELINE = json.dumps({"data": {"node": {"__typename": "User", "id": "42", "timeline_list_feed_units": {
    "edges": [{"node": {"__typename": "Story", "post_id": "p1", "creation_time": 1.0e10, "message": {"text": "x"}}}],
    "page_info": {"has_next_page": False}}}}})


class Cookies(dict):
    def set(self, name, value, domain=None):
        self[name] = value


class Session:
    def __init__(self, log):
        self.cookies, self.log = Cookies(), log

    def request(self, method, url, **kwargs):
        logged_in = bool(self.cookies)
        self.log.append(logged_in)
        if method == "GET":
            return SimpleNamespace(status_code=200, url=url, text='["DTSGInitialData",[],{"token":"T"},1]')
        return SimpleNamespace(status_code=200, url=url, text=TIMELINE if logged_in else BLOCKED)


def test_cookie_values_never_in_events_status_or_logs(tmp_path, capsys, caplog):
    cfg = tmp_path / "crawler.toml"
    cfg.write_text('places = ["Sylhet"]\ndata_dir = "data"\n')
    (tmp_path / "fb.txt").write_text(FB_EXPORT)
    (tmp_path / "ig.json").write_text(IG_EXPORT)
    caplog.set_level(logging.DEBUG)
    assert main(["--config", str(cfg), "login", "fb", "--cookies", str(tmp_path / "fb.txt")]) == 0
    assert main(["--config", str(cfg), "login", "ig", "--cookies", str(tmp_path / "ig.json")]) == 0

    data = tmp_path / "data"
    store, sessions, used = Store(data), SessionStore(data), []
    store.add_source("https://www.facebook.com/a", "fb_page", "Sylhet", "phones", native_id="42")
    config = Config(places=("Sylhet",))
    t = Transport(config, session_factory=lambda: Session(used), sleep=lambda s: None, sessions=sessions)
    events = list(search(config, store, "Sylhet", "phones", readers=default_readers(config, t, store),
                         marketplace=None))
    assert True in used and False in used, "the run must go through the logged-in path"
    assert main(["--config", str(cfg), "status"]) == 0

    cli = capsys.readouterr()
    checked = {
        "events": json.dumps(events),
        "cli output": cli.out + cli.err,
        "logs": caplog.text + " ".join(r.getMessage() for r in caplog.records),
        "raw file names": " ".join(p.name for p in (data / "raw").iterdir()),
    }
    assert '"outcome": "ok"' in checked["events"]
    for name, text in checked.items():
        assert text.strip(), f"{name} is empty, so the check would pass vacuously"
        assert not [v for v in PLANTED if v in text], name
