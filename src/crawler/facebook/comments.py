import base64

from ..config import Config
from ..outcomes import FetchResult, Outcome, json_documents
from .pages import ask_graphql, graphql_form, headers
from .parse import next_cursor

COMMENTS = ("node", "comment_rendering_instance_for_feed_location", "comments")
REPLIES = ("node", "replies_connection")
MAX_PAGES = 100
UFI_FLAGS = {
    "__relay_internal__pv__CometUFICommentAutoTranslationTyperelayprovider": "AUTO_TRANSLATE",
    "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
    "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": True,
    "__relay_internal__pv__IsWorkUserrelayprovider": False,
}


def strip_person_mentions(body: dict) -> str:
    """Remove every tagged person from the text; FB range offsets count UTF-16 units."""
    text = body.get("text") or ""
    spans = sorted(
        ((r.get("offset") or 0, r.get("length") or 0) for r in body.get("ranges") or []
         if isinstance(r, dict) and (r.get("entity") or {}).get("__typename") == "User"),
        reverse=True,
    )
    if not spans:
        return text
    units = text.encode("utf-16-le")
    for offset, length in spans:
        units = units[: offset * 2] + units[(offset + length) * 2:]
    return " ".join(units.decode("utf-16-le", errors="ignore").split())


def parse_comment(node: dict, item_id: str, owner_id: str | None, parent_id: str | None = None) -> dict:
    author = node.get("author") or {}
    return {
        "comment_id": str(node.get("legacy_fbid") or node.get("id")),
        "item_id": item_id,
        "parent_id": parent_id,
        "depth": node.get("depth") or 0,
        "created_at": node.get("created_time"),
        "text": strip_person_mentions(node.get("body") or {}),
        "is_owner": bool(owner_id) and author.get("id") == owner_id,
    }


def _reply_handle(node: dict) -> tuple[str, str] | None:
    feedback = node.get("feedback") or {}
    token = (feedback.get("expansion_info") or {}).get("expansion_token")
    count = (feedback.get("replies_fields") or {}).get("total_count") or 0
    return (feedback["id"], token) if token and count and feedback.get("id") else None


class _Walker:
    def __init__(self, transport, config, item_id, owner_id, on_raw):
        self.transport, self.config = transport, config
        self.item_id, self.owner_id, self.on_raw = item_id, owner_id, on_raw
        self.items: list[dict] = []

    def _ask(self, doc_key, friendly, variables, connection, label):
        outcome, response = ask_graphql(
            self.transport, graphql_form(self.config.doc_ids[doc_key], friendly, variables),
            headers(f"https://www.facebook.com/{self.item_id}", friendly), connection, label, self.on_raw)
        docs = json_documents(response.text) if outcome is Outcome.OK else []
        return outcome, docs

    def _take(self, docs, connection, parent_id) -> Outcome:
        conn = docs[0].get("data")
        for key in connection:
            conn = (conn or {}).get(key)
        for edge in conn.get("edges") or []:
            node = edge.get("node") or {}
            comment = parse_comment(node, self.item_id, self.owner_id, parent_id)
            self.items.append(comment)
            if handle := _reply_handle(node):
                outcome = self.replies(*handle, comment["comment_id"])
                if outcome is not Outcome.OK:
                    return outcome
        return Outcome.OK

    def comments(self, feedback_id) -> Outcome:
        cursor = None
        for _ in range(MAX_PAGES):
            variables = {"commentsAfterCount": -1, "commentsAfterCursor": cursor, "commentsBeforeCount": None,
                         "commentsBeforeCursor": None, "commentsIntentToken": "CHRONOLOGICAL_UNFILTERED_INTENT_V1",
                         "feedLocation": "POST_PERMALINK_DIALOG", "focusCommentID": None, "scale": 2,
                         "useDefaultActor": False, "id": feedback_id, **UFI_FLAGS}
            outcome, docs = self._ask("fb_comments", "CommentsListComponentsPaginationQuery", variables,
                                      COMMENTS, "comments")
            if outcome is Outcome.NOTHING_NEW:
                return Outcome.OK
            if outcome is not Outcome.OK or (outcome := self._take(docs, COMMENTS, None)) is not Outcome.OK:
                return outcome
            if not (cursor := next_cursor(docs, COMMENTS)):
                return Outcome.OK
        return Outcome.OK

    def replies(self, feedback_id, token, parent_id) -> Outcome:
        cursor = None
        for _ in range(MAX_PAGES):
            variables = {"clientKey": None, "expansionToken": token, "feedLocation": "POST_PERMALINK_DIALOG",
                         "focusCommentID": None, "scale": 2, "useDefaultActor": False, "id": feedback_id,
                         "repliesAfterCursor": cursor, **UFI_FLAGS}
            outcome, docs = self._ask("fb_replies", "Depth1CommentsListPaginationQuery", variables, REPLIES,
                                      "replies")
            if outcome is Outcome.NOTHING_NEW:
                return Outcome.OK
            if outcome is not Outcome.OK or (outcome := self._take(docs, REPLIES, parent_id)) is not Outcome.OK:
                return outcome
            if not (cursor := next_cursor(docs, REPLIES)):
                return Outcome.OK
        return Outcome.OK


def fetch_comments(transport, config: Config, post: dict, owner_id: str | None, on_raw=None) -> FetchResult:
    """All comments of a post, oldest first, with every reply level. owner_id marks the seller's own replies."""
    walker = _Walker(transport, config, post["item_id"], owner_id, on_raw)
    feedback_id = post.get("feedback_id") or base64.b64encode(f"feedback:{post['item_id']}".encode()).decode()
    outcome = walker.comments(feedback_id)
    detail = None if outcome is Outcome.OK else f"comments of {post['item_id']}: {outcome}"
    return FetchResult(outcome, walker.items, detail)
