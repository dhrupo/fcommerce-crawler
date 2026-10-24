import json
from pathlib import Path

import pytest

from crawler.cli import main
from crawler.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "crawler.toml"
    p.write_text('places = ["Sylhet"]\ncategories = ["phones"]\ndata_dir = "data"\n')
    return p


def store_for(cfg):
    return Store(cfg.parent / "data")


def test_sources_add_requires_place_and_category(cfg, capsys):
    with pytest.raises(SystemExit) as e:
        main(["--config", str(cfg), "sources", "add", "https://www.facebook.com/iStoreSyl"])
    assert e.value.code == 2
    with pytest.raises(SystemExit):
        main(["--config", str(cfg), "sources", "add", "https://www.facebook.com/iStoreSyl", "--place", "Sylhet"])
    assert store_for(cfg).list_sources() == []


def test_sources_add_rejects_unconfigured_place(cfg, capsys):
    code = main(["--config", str(cfg), "sources", "add", "https://www.facebook.com/x", "--place", "Dhaka",
                 "--category", "phones"])
    assert code == 1 and "Dhaka" in capsys.readouterr().err


def test_sources_add_detects_kind_and_rejects_vanity_group(cfg, capsys):
    base = ["--config", str(cfg), "sources", "add"]
    tail = ["--place", "Sylhet", "--category", "phones"]
    assert main([*base, "https://www.facebook.com/iStoreSyl", *tail]) == 0
    assert main([*base, "https://www.instagram.com/some.shop/", *tail]) == 0
    assert main([*base, "https://www.facebook.com/groups/392585550772135/", *tail]) == 0
    assert main([*base, "https://www.facebook.com/groups/sylhet.mobile", *tail]) == 1
    kinds = {s.url: s.kind for s in store_for(cfg).list_sources()}
    assert sorted(kinds.values()) == ["fb_group", "fb_page", "ig_profile"]
    capsys.readouterr()
    assert main(["--config", str(cfg), "sources", "list"]) == 0
    assert "iStoreSyl" in capsys.readouterr().out
    assert main(["--config", str(cfg), "sources", "remove", "https://www.instagram.com/some.shop/"]) == 0
    assert len(store_for(cfg).list_sources()) == 2


def test_status_shows_last_outcome_per_source(cfg, capsys):
    s = store_for(cfg)
    a = s.add_source("https://www.facebook.com/a", "fb_page", "Sylhet", "phones")
    s.add_source("https://www.facebook.com/b", "fb_page", "Sylhet", "phones")
    s.set_fetch_state(a.id, "blocked", detail="page 1: blocked")
    capsys.readouterr()
    assert main(["--config", str(cfg), "status"]) == 0
    lines = capsys.readouterr().out.splitlines()
    row_a = next(line for line in lines if "/a" in line)
    row_b = next(line for line in lines if "/b" in line)
    assert "blocked" in row_a and "page 1: blocked" in row_a
    assert "never fetched" in row_b


def test_reparse_rebuilds_from_raw(cfg, capsys):
    s = store_for(cfg)
    src = s.add_source("https://www.facebook.com/iStoreSyl", "fb_page", "Sylhet", "phones")
    s.save_raw(src.id, "timeline", (FIXTURES / "fb_timeline_p1.json").read_text())
    s.save_raw(src.id, "comments", (FIXTURES / "fb_comments_owner.json").read_text())
    s.save_raw(src.id, "replies", (FIXTURES / "fb_replies.json").read_text())
    assert main(["--config", str(cfg), "reparse", "--since", "2000-01-01"]) == 0
    items = {i["item_id"] for i in s.items_for(src.id, include_expired=True)}
    assert {"1659016182682042", "1645604120689915", "1632044745379186"} <= items
    s.upsert_item(src.id, {"item_id": "1496609455589383", "kind": "post", "owner_id": "100057210291191"})
    assert main(["--config", str(cfg), "reparse", "--since", "2000-01-01"]) == 0
    comments = s.comments_for(src.id, "1496609455589383")
    assert len(comments) == 8
    reply = next(c for c in comments if c["depth"] == 1)
    assert reply["parent_id"] == "26066920936340147" and reply["is_owner"] is True
    assert "rebuilt" in capsys.readouterr().out


def test_search_prints_one_json_line_per_event(cfg, capsys):
    assert main(["--config", str(cfg), "search", "--place", "Chittagong", "iphone"]) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [e["outcome"] for e in events] == ["out_of_scope"]


def test_bad_since_date_fails_plainly(cfg, capsys):
    assert main(["--config", str(cfg), "reparse", "--since", "24-10-2026"]) == 1
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_serve_on_busy_port_fails_plainly(cfg, capsys):
    import socket

    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    try:
        assert main(["--config", str(cfg), "serve", "--port", str(busy.getsockname()[1])]) == 1
    finally:
        busy.close()
    assert "cannot listen" in capsys.readouterr().err


def test_fetch_marketplace_source_is_refused(cfg, capsys):
    store_for(cfg).add_source("marketplace:Sylhet", "fb_marketplace", "Sylhet", None)
    assert main(["--config", str(cfg), "fetch", "marketplace:Sylhet"]) == 1
    assert "search" in capsys.readouterr().err


def test_sources_add_rejects_unconfigured_category(cfg, capsys):
    code = main(["--config", str(cfg), "sources", "add", "https://www.facebook.com/x", "--place", "Sylhet",
                 "--category", "phnoes"])
    assert code == 1 and "phnoes" in capsys.readouterr().err


def test_reparse_reads_streamed_comment_edges(cfg):
    s = store_for(cfg)
    src = s.add_source("https://www.facebook.com/iStoreSyl", "fb_page", "Sylhet", "phones")
    s.upsert_item(src.id, {"item_id": "1496609455589383", "kind": "post", "owner_id": "1"})

    def comment(cid):
        return {"legacy_fbid": cid, "created_time": 1, "depth": 0, "body": {"text": "price?"},
                "author": {"id": "u"}, "feedback": {}}

    conn = ("node", "comment_rendering_instance_for_feed_location", "comments")
    first = {"data": {"node": {"__typename": "Feedback", "id": "ZmVlZGJhY2s6MTQ5NjYwOTQ1NTU4OTM4Mw==",
                               conn[1]: {"comments": {"edges": [{"node": comment("c1")}], "page_info": {}}}}}}
    streamed = {"path": [*conn, "edges", 1], "data": {"node": comment("c2")}}
    s.save_raw(src.id, "comments", json.dumps(first) + "\n" + json.dumps(streamed))
    assert main(["--config", str(cfg), "reparse", "--since", "2000-01-01"]) == 0
    assert [c["comment_id"] for c in s.comments_for(src.id, "1496609455589383")] == ["c1", "c2"]


FB_EXPORT = (".facebook.com\tTRUE\t/\tTRUE\t1893456000\tc_user\t1000001\n"
             ".facebook.com\tTRUE\t/\tTRUE\t1893456000\txs\tPLANTED-XS-VALUE\n")


def test_login_saves_session_and_status_hides_values(cfg, tmp_path, capsys):
    export = tmp_path / "fb-cookies.txt"
    export.write_text(FB_EXPORT)
    assert main(["--config", str(cfg), "login", "fb", "--cookies", str(export)]) == 0
    out = capsys.readouterr().out
    assert "fb" in out and "PLANTED-XS-VALUE" not in out and "1000001" not in out
    assert main(["--config", str(cfg), "status"]) == 0
    status = capsys.readouterr().out
    assert "login fb: ready" in status and "login ig: none" in status
    assert "PLANTED-XS-VALUE" not in status


def test_login_with_bad_export_fails_plainly(cfg, tmp_path, capsys):
    export = tmp_path / "ig.txt"
    export.write_text(FB_EXPORT)
    assert main(["--config", str(cfg), "login", "ig", "--cookies", str(export)]) == 1
    assert "sessionid" in capsys.readouterr().err
    assert main(["--config", str(cfg), "login", "ig", "--cookies", str(tmp_path / "missing.txt")]) == 1


def test_logout_removes_session(cfg, tmp_path, capsys):
    export = tmp_path / "fb-cookies.txt"
    export.write_text(FB_EXPORT)
    main(["--config", str(cfg), "login", "fb", "--cookies", str(export)])
    assert main(["--config", str(cfg), "logout", "fb"]) == 0
    assert not (cfg.parent / "data" / "sessions" / "fb.json").exists()
    assert main(["--config", str(cfg), "logout", "fb"]) == 1


def test_cli_transport_gets_the_session_store(cfg, monkeypatch):
    seen = {}

    def factory(config, sessions=None):
        seen["sessions"] = sessions
        raise SystemExit(0)

    with pytest.raises(SystemExit):
        main(["--config", str(cfg), "search", "--place", "Sylhet", "x"], transport_factory=factory)
    assert seen["sessions"] is not None
