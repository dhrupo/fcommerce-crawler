from collections import deque

from ..outcomes import json_documents

SKIP_IN_POST = frozenset({"attached_story"})


def walk(obj, skip=frozenset()):
    """Every dict under obj, shallowest first, not descending into keys in `skip`."""
    queue = deque([obj])
    while queue:
        node = queue.popleft()
        if isinstance(node, dict):
            yield node
            queue.extend(v for k, v in node.items() if k not in skip)
        elif isinstance(node, list):
            queue.extend(node)


def first(obj, key, skip=SKIP_IN_POST):
    """Shallowest non-null value for `key` anywhere under obj."""
    return next((d[key] for d in walk(obj, skip) if d.get(key) is not None), None)


def first_count(obj, key, sub="count", skip=SKIP_IN_POST):
    for d in walk(obj, skip):
        value = d.get(key)
        if isinstance(value, dict) and isinstance(value.get(sub), int):
            return value[sub]
    return None


def find_stories(docs) -> list[dict]:
    """Top-level Story nodes across all streamed documents, in order, once each."""
    stories, seen = [], set()
    for doc in docs:
        queue = deque([doc.get("data")])
        while queue:
            node = queue.popleft()
            if isinstance(node, dict):
                if node.get("__typename") == "Story" and node.get("post_id"):
                    if node["post_id"] not in seen:
                        seen.add(node["post_id"])
                        stories.append(node)
                    continue
                queue.extend(node.values())
            elif isinstance(node, list):
                queue.extend(node)
    return stories


def image_uri(media: dict) -> str | None:
    for key in ("viewer_image", "photo_image", "image"):
        uri = (media.get(key) or {}).get("uri")
        if uri:
            return uri
    return None


def images(story) -> list[str]:
    urls, seen = [], set()
    for d in walk(story):
        typename = d.get("__typename")
        if typename == "Photo":
            uri = image_uri(d)
        elif typename == "Video":
            uri = ((d.get("preferred_thumbnail") or {}).get("image") or {}).get("uri")
        else:
            continue
        key = d.get("id") or uri
        if uri and key not in seen:
            seen.add(key)
            urls.append(uri)
    return urls


def message_text(story, skip=SKIP_IN_POST) -> str | None:
    message = first(story, "message", skip)
    return message.get("text") if isinstance(message, dict) else None


def parse_story(story: dict) -> dict:
    post_id = str(story["post_id"])
    feedback = story.get("feedback") if isinstance(story.get("feedback"), dict) else {}
    owner = first(feedback, "owning_profile") or first(story, "owning_profile") or {}
    actors = first(story, "actors") or [{}]
    shared = story.get("attached_story")
    return {
        "item_id": post_id,
        "kind": "post",
        "created_at": first(story, "creation_time"),
        "text": message_text(story),
        "permalink": first(story, "permalink_url") or first(story, "wwwURL") or f"https://www.facebook.com/{post_id}",
        "images": images(story),
        "is_video": any(d.get("__typename") == "Video" for d in walk(story)),
        "comment_count": first_count(story, "comments", "total_count"),
        "reaction_count": first_count(story, "reaction_count"),
        "share_count": first_count(story, "share_count"),
        "owner_id": owner.get("id") if isinstance(owner, dict) else None,
        "author_id": (actors[0] or {}).get("id") if isinstance(actors, list) and actors else None,
        "feedback_id": feedback.get("id"),
        "shared_from": {"post_id": shared.get("post_id"), "text": message_text(shared, frozenset())}
        if isinstance(shared, dict) else None,
    }


def parse_feed(text: str) -> list[dict]:
    return [parse_story(s) for s in find_stories(json_documents(text))]


def page_info(docs, connection: tuple[str, ...]) -> dict:
    """page_info for a connection, whether inline or sent as a deferred chunk."""
    for doc in docs:
        data = doc.get("data")
        if list(doc.get("path") or []) == list(connection) and isinstance(data, dict) and "page_info" in data:
            return data["page_info"] or {}
        if "path" not in doc:
            node = data
            for key in connection:
                node = node.get(key) if isinstance(node, dict) else None
            if isinstance(node, dict) and isinstance(node.get("page_info"), dict):
                return node["page_info"]
    return {}


def next_cursor(docs, connection: tuple[str, ...]) -> str | None:
    info = page_info(docs, connection)
    return info.get("end_cursor") if info.get("has_next_page") else None
