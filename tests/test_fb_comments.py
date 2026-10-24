import json
from pathlib import Path
from types import SimpleNamespace

from crawler.config import Config
from crawler.facebook.comments import fetch_comments, parse_comment
from crawler.outcomes import Outcome

FIXTURES = Path(__file__).parent / "fixtures"
OWNER = "100057210291191"
POST = {"item_id": "1496609455589383", "feedback_id": "ZmVlZGJhY2s6MTQ5NjYwOTQ1NTU4OTM4Mw==", "owner_id": OWNER}


def comments_page(nodes, cursor=None):
    conn = {"edges": [{"node": n} for n in nodes], "page_info": {"has_next_page": bool(cursor), "end_cursor": cursor}}
    return json.dumps({"data": {"node": {"__typename": "Feedback", "id": "F",
                                         "comment_rendering_instance_for_feed_location": {"comments": conn}}}})


def replies_page(nodes, cursor=None):
    conn = {"edges": [{"node": n} for n in nodes], "page_info": {"has_next_page": bool(cursor), "end_cursor": cursor}}
    return json.dumps({"data": {"node": {"__typename": "Feedback", "id": "R", "replies_connection": conn}}})


def node(cid, author="pfbidBUYER", text="price?", depth=0, replies=0, t=100, name="Real Buyer Name", ranges=()):
    return {"legacy_fbid": cid, "created_time": t, "depth": depth, "body": {"text": text, "ranges": list(ranges)},
            "author": {"__typename": "User", "id": author, "name": name, "url": "https://www.facebook.com/real.buyer"},
            "feedback": {"id": f"fb-{cid}", "replies_fields": {"total_count": replies},
                         "expansion_info": {"expansion_token": f"tok-{cid}" if replies else None}}}


class FakeTransport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def post(self, url, data=None, **kwargs):
        self.sent.append((data["fb_api_req_friendly_name"], json.loads(data["variables"])))
        return SimpleNamespace(status_code=200, text=self.replies.pop(0), url=url)


def run(replies, post=POST, owner=OWNER):
    t = FakeTransport(replies)
    return fetch_comments(t, Config(places=("Sylhet",)), post, owner_id=owner), t


def test_all_comments_and_replies_from_fixture():
    owner_page = (FIXTURES / "fb_comments_owner.json").read_text()
    reply = (FIXTURES / "fb_replies.json").read_text()
    empty = replies_page([])
    # 4 of the 7 comments have replies; serve the recorded reply page for the first, empty for the rest
    result, t = run([owner_page, reply, empty, empty, empty])
    assert result.outcome is Outcome.OK
    assert len([c for c in result.items if c["depth"] == 0]) == 7
    replies = [c for c in result.items if c["depth"] == 1]
    assert len(replies) == 1 and replies[0]["parent_id"] == "26066920936340147"
    assert all(isinstance(c["created_at"], int) for c in result.items)
    assert t.sent[0][1]["commentsIntentToken"] == "CHRONOLOGICAL_UNFILTERED_INTENT_V1"


def test_owner_reply_flagged():
    reply = json.loads((FIXTURES / "fb_replies.json").read_text().split("\n")[0])
    owner_node = reply["data"]["node"]["replies_connection"]["edges"][0]["node"]
    assert parse_comment(owner_node, POST["item_id"], OWNER, parent_id="c1")["is_owner"] is True
    assert parse_comment(node("c2"), POST["item_id"], OWNER)["is_owner"] is False


def test_group_owner_is_post_author():
    post = {**POST, "owner_id": None, "author_id": "pfbidSELLER"}
    result, _ = run([comments_page([node("c1", author="pfbidSELLER"), node("c2")])], post=post, owner="pfbidSELLER")
    assert [c["is_owner"] for c in result.items] == [True, False]


def test_leading_mention_stripped():
    text = "Real Buyer Name 72k, inbox"
    c = parse_comment(node("c1", text=text, ranges=[{"offset": 0, "length": 15, "entity": {"__typename": "User"}}]),
                      "p", OWNER)
    assert c["text"] == "72k, inbox"


def test_mention_offsets_count_utf16_units():
    text = "😀 Rahim ভাই 72k"
    c = parse_comment(node("c1", text=text, ranges=[{"offset": 3, "length": 5, "entity": {"__typename": "User"}}]),
                      "p", OWNER)
    assert c["text"] == "😀 ভাই 72k"


def test_buyer_name_not_in_output():
    text = "Real Buyer Name price?"
    result, _ = run([comments_page([node("c1", text=text, ranges=[
        {"offset": 0, "length": 15, "entity": {"__typename": "User"}}])])])
    dumped = json.dumps(result.items)
    assert "Real Buyer Name" not in dumped
    assert "real.buyer" not in dumped
    assert "pfbidBUYER" not in dumped


def test_comments_page_through_cursor():
    result, t = run([comments_page([node("c1")], cursor="K1"), comments_page([node("c2")])])
    assert [c["comment_id"] for c in result.items] == ["c1", "c2"]
    assert t.sent[1][1]["commentsAfterCursor"] == "K1"


def test_replies_page_and_nest():
    result, t = run([
        comments_page([node("c1", replies=3)]),
        replies_page([node("r1", depth=1, replies=1)], cursor="RK"),
        replies_page([node("rr1", depth=2)]),
        replies_page([node("r2", depth=1)]),
    ])
    by_id = {c["comment_id"]: c for c in result.items}
    assert by_id["r1"]["parent_id"] == "c1" and by_id["rr1"]["parent_id"] == "r1" and by_id["r2"]["parent_id"] == "c1"
    assert [v.get("repliesAfterCursor") for name, v in t.sent if name.startswith("Depth1")] == [None, None, "RK"]


def test_broken_comment_page_is_reported():
    broken = json.dumps({"data": {"node": {"__typename": "Feedback", "id": "F"}}})
    result, _ = run([broken])
    assert result.outcome is Outcome.SCHEMA_CHANGED


def test_missing_feedback_id_is_built_from_post_id():
    _, t = run([comments_page([])], post={"item_id": "1496609455589383", "feedback_id": None, "owner_id": OWNER})
    assert t.sent[0][1]["id"] == "ZmVlZGJhY2s6MTQ5NjYwOTQ1NTU4OTM4Mw=="


def test_transient_error_on_comments_is_retried():
    transient = json.dumps({"errors": [{"code": 1675012, "severity": "CRITICAL",
                                        "message": "missing_required_variable_value"}]})
    result, _ = run([transient, comments_page([node("c1")])])
    assert result.outcome is Outcome.OK and [c["comment_id"] for c in result.items] == ["c1"]


def test_reply_link_without_a_count_is_still_followed():
    parent = node("c1", replies=1)
    del parent["feedback"]["replies_fields"]
    result, _ = run([comments_page([parent]), replies_page([node("r1", depth=1)])])
    assert [c["comment_id"] for c in result.items] == ["c1", "r1"]


def test_mention_without_offset_is_left_alone():
    c = parse_comment(node("c1", text="72k inbox", ranges=[{"length": 3, "entity": {"__typename": "User"}}]), "p", OWNER)
    assert c["text"] == "72k inbox"


def test_streamed_comment_edges_are_read():
    first = json.loads(comments_page([node("c1")]))
    streamed = {"label": "stream", "path": ["node", "comment_rendering_instance_for_feed_location", "comments",
                                            "edges", 1], "data": {"node": node("c2")}}
    result, _ = run([json.dumps(first) + "\n" + json.dumps(streamed)])
    assert [c["comment_id"] for c in result.items] == ["c1", "c2"]


def test_block_mid_comments_keeps_what_was_read():
    from crawler.transport import HostBlocked

    class Blocking(FakeTransport):
        def post(self, url, data=None, **kwargs):
            if self.sent:
                raise HostBlocked("facebook.com", 0)
            return super().post(url, data=data, **kwargs)

    t = Blocking([comments_page([node("c1")], cursor="K")])
    result = fetch_comments(t, Config(places=("Sylhet",)), POST, owner_id=OWNER)
    assert result.outcome is Outcome.BLOCKED and [c["comment_id"] for c in result.items] == ["c1"]
