import json
import re
import time

from ..config import Config
from ..facebook.pages import crossed_boundary, history_start
from ..outcomes import FetchResult, Outcome, classify, json_documents

GRAPHQL_URL = "https://www.instagram.com/api/graphql"
APP_ID = "936619743392459"
POSTS = ("xdt_api__v1__feed__user_timeline_graphql_connection",)
MEDIA = ("xig_polaris_media",)
MAX_PAGES = 30
NAV_HEADERS = {"Accept": "text/html,application/xhtml+xml", "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
               "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1", "Upgrade-Insecure-Requests": "1"}


def image_urls(node: dict) -> list[str]:
    media = node.get("carousel_media") or [node]
    urls = [((m.get("image_versions2") or {}).get("candidates") or [{}])[0].get("url") for m in media]
    return [u for u in urls if u]


def parse_post(node: dict) -> dict:
    return {
        "item_id": str(node["pk"]),
        "kind": "post",
        "created_at": node.get("taken_at"),
        "text": (node.get("caption") or {}).get("text"),
        "permalink": f"https://www.instagram.com/p/{node.get('code')}/",
        "images": image_urls(node),
        "is_video": node.get("media_type") == 2,
        "comment_count": node.get("comment_count"),
        "reaction_count": node.get("like_count"),
        "owner_id": str((node.get("user") or {}).get("pk") or "") or None,
    }


def parse_comment(node: dict, item_id: str, owner_id: str | None) -> dict:
    user = node.get("user") or {}
    return {
        "comment_id": str(node.get("pk") or node.get("id")),
        "item_id": item_id,
        "parent_id": node.get("parent_comment_id"),
        "depth": 1 if node.get("parent_comment_id") else 0,
        "created_at": node.get("created_at"),
        "text": node.get("text") or "",
        "is_owner": bool(owner_id) and str(user.get("pk") or user.get("id")) == owner_id,
    }


class InstagramClient:
    """Logged-out profile reader: profile HTML gives the lsd token, then Polaris GraphQL for posts and comments."""

    def __init__(self, transport, config: Config, username: str, now=time.time, on_raw=None):
        self.transport, self.config, self.username = transport, config, username
        self.now, self.on_raw = now, on_raw
        self.lsd: str | None = None
        self.owner_id: str | None = None

    def _bootstrap(self) -> Outcome:
        if self.lsd:
            return Outcome.OK
        response = self.transport.get(f"https://www.instagram.com/{self.username}/", headers=NAV_HEADERS)
        outcome = classify(response)
        if outcome is not Outcome.OK:
            return outcome
        lsd = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', response.text)
        owner = re.search(r'"profile_id":"(\d+)"', response.text)
        if not lsd:
            return Outcome.SCHEMA_CHANGED
        self.lsd, self.owner_id = lsd.group(1), owner.group(1) if owner else None
        return Outcome.OK

    def _ask(self, doc_key: str, friendly: str, variables: dict, referer: str, connection, label: str):
        headers = {"x-ig-app-id": APP_ID, "x-csrftoken": self.transport.session().cookies.get("csrftoken") or "",
                   "x-requested-with": "XMLHttpRequest", "x-fb-lsd": self.lsd, "x-fb-friendly-name": friendly,
                   "Accept": "*/*", "Origin": "https://www.instagram.com", "Referer": referer,
                   "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
        data = {"lsd": self.lsd, "fb_api_caller_class": "RelayModern", "fb_api_req_friendly_name": friendly,
                "variables": json.dumps(variables, separators=(",", ":")), "doc_id": self.config.doc_ids[doc_key],
                "server_timestamps": "true"}
        response = self.transport.post(GRAPHQL_URL, data=data, headers=headers)
        if self.on_raw:
            self.on_raw(label, response.text)
        outcome = classify(response, connection)
        return outcome, (json_documents(response.text) if outcome is Outcome.OK else [])

    def fetch_posts(self, newest_seen=None, max_pages=MAX_PAGES) -> FetchResult:
        if (outcome := self._bootstrap()) is not Outcome.OK:
            return FetchResult(outcome, [], f"instagram {self.username}: {outcome}")
        stop_before = history_start(self.now(), newest_seen)
        items, cursor = [], None
        for page_number in range(1, max_pages + 1):
            variables = {"data": {"count": 12, "include_reel_media_seen_timestamp": True,
                                  "include_relationship_info": True, "latest_besties_reel_media": True,
                                  "latest_reel_media": True},
                         "username": self.username,
                         "__relay_internal__pv__PolarisMultiCaptionCarouselEnabledrelayprovider": False,
                         "__relay_internal__pv__PolarisReelsRecoDebugOverlayEnabledrelayprovider": False,
                         "__relay_internal__pv__PolarisShortDramaEnabledrelayprovider": False}
            if cursor:
                variables.update(after=cursor, first=12)
            outcome, docs = self._ask("ig_profile_posts", "PolarisProfilePostsQuery", variables,
                                      f"https://www.instagram.com/{self.username}/", POSTS, "ig-posts")
            if outcome is Outcome.NOTHING_NEW:
                break
            if outcome is not Outcome.OK:
                return FetchResult(outcome, items, f"instagram {self.username} page {page_number}: {outcome}")
            conn = docs[0]["data"][POSTS[0]]
            posts = [parse_post(e["node"]) for e in conn.get("edges") or [] if (e.get("node") or {}).get("pk")]
            is_fresh = [p["created_at"] is None or p["created_at"] > stop_before for p in posts]
            items += [p for p, fresh in zip(posts, is_fresh, strict=True) if fresh]
            info = conn.get("page_info") or {}
            cursor = info.get("end_cursor") if info.get("has_next_page") else None
            if not cursor or crossed_boundary(is_fresh):
                break
        return FetchResult(Outcome.OK if items else Outcome.NOTHING_NEW, items)

    def fetch_comments(self, post: dict) -> FetchResult:
        """The first comments Instagram shows logged out (about 14); there is no logged-out paging."""
        if (outcome := self._bootstrap()) is not Outcome.OK:
            return FetchResult(outcome, [], f"instagram {self.username}: {outcome}")
        outcome, docs = self._ask("ig_post", "PolarisLoggedOutDesktopWWWPostRootContentQuery",
                                  {"media_id": post["item_id"]}, post["permalink"], MEDIA, "ig-post")
        if outcome is not Outcome.OK:
            return FetchResult(outcome, [], f"instagram comments {post['item_id']}: {outcome}")
        media = docs[0]["data"][MEDIA[0]]
        edges = (media.get("comments_connection") or {}).get("edges") or []
        comments = [parse_comment(e["node"], post["item_id"], self.owner_id) for e in edges if e.get("node")]
        return FetchResult(Outcome.OK if comments else Outcome.NOTHING_NEW, comments)
