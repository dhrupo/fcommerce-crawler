import json
from pathlib import Path

from crawler.facebook.parse import parse_feed, parse_story

FIXTURES = Path(__file__).parent / "fixtures"


def test_timeline_fixture_yields_posts():
    posts = parse_feed((FIXTURES / "fb_timeline_p1.json").read_text())
    assert [p["item_id"] for p in posts] == ["1659016182682042", "1645604120689915", "1632044745379186"]
    first = posts[0]
    assert first["kind"] == "post"
    assert first["created_at"] == 1791713085
    assert first["text"] == "iPhone 18 Pro Max Blue"
    assert first["permalink"].startswith("https://www.facebook.com/iStoreSyl/posts/")
    assert first["owner_id"] == "100057210291191"
    assert first["comment_count"] == 2
    assert first["reaction_count"] == 6
    assert len(first["images"]) == 2 and all(u.startswith("https://scontent") for u in first["images"])
    assert first["feedback_id"] == "ZmVlZGJhY2s6MTY1OTAxNjE4MjY4MjA0Mg=="


def test_post_without_text_does_not_crash():
    post = parse_story({"__typename": "Story", "post_id": "1", "creation_time": 5})
    assert post["item_id"] == "1"
    assert post["text"] is None
    assert post["images"] == []
    assert post["comment_count"] is None
    assert post["permalink"] == "https://www.facebook.com/1"


def test_null_sections_do_not_crash():
    story = {"__typename": "Story", "post_id": "1", "comet_sections": None, "attachments": [None],
             "feedback": None, "actors": None}
    assert parse_story(story)["item_id"] == "1"


def test_image_falls_back_from_viewer_to_photo_to_image():
    story = {"__typename": "Story", "post_id": "1", "attachments": [
        {"media": {"__typename": "Photo", "id": "a", "viewer_image": {"uri": "https://v"}, "image": {"uri": "https://i"}}},
        {"media": {"__typename": "Photo", "id": "b", "photo_image": {"uri": "https://p"}, "image": {"uri": "https://i2"}}},
        {"media": {"__typename": "Photo", "id": "c", "image": {"uri": "https://i3"}}},
        {"media": {"__typename": "Photo", "id": "a", "image": {"uri": "https://dupe"}}},
    ]}
    assert parse_story(story)["images"] == ["https://v", "https://p", "https://i3"]


def test_reel_kept_with_caption():
    posts = parse_feed((FIXTURES / "fb_timeline_p1.json").read_text())
    reel = next(p for p in posts if p["item_id"] == "1632044745379186")
    assert reel["is_video"] is True
    assert reel["text"].startswith("Apple Macbook M5 Max")


def test_shared_post_kept_with_original():
    original = {"__typename": "Story", "post_id": "9", "message": {"text": "original caption"}}
    story = {"__typename": "Story", "post_id": "1", "creation_time": 5, "message": {"text": "look"},
             "attached_story": original}
    post = parse_story(story)
    assert post["text"] == "look"
    assert post["shared_from"] == {"post_id": "9", "text": "original caption"}


def test_group_post_time_found_deeper():
    posts = parse_feed((FIXTURES / "fb_group_feed_p1.json").read_text())
    assert posts and all(isinstance(p["created_at"], int) for p in posts)


def test_parse_feed_ignores_non_story_nodes():
    text = json.dumps({"data": {"node": {"__typename": "User", "id": "1"}}})
    assert parse_feed(text) == []
