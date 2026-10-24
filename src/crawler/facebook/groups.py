import time

from ..config import Config
from ..outcomes import FetchResult
from .pages import fetch_feed, history_start

CONNECTION = ("node", "group_feed")
MAX_PAGES = 10


def fetch_group_posts(transport, config: Config, group_id: str, *, newest_seen=None, now=time.time,
                      on_raw=None, max_pages=MAX_PAGES) -> FetchResult:
    """Logged out, a public group shows a ranked "most relevant" sample, so old posts do not end the walk."""
    def variables_for(cursor):
        return {"count": 3, "cursor": cursor, "feedLocation": "GROUP", "feedType": "DISCUSSION", "feedbackSource": 0,
                "filterTopicId": None, "focusCommentID": None, "privacySelectorRenderLocation": "COMET_STREAM",
                "renderLocation": "group", "scale": 2, "stream_initial_count": 1, "useDefaultActor": False,
                "id": group_id}

    return fetch_feed(
        transport, doc_id=config.doc_ids["fb_group"], friendly="GroupsCometFeedRegularStoriesPaginationQuery",
        referer=f"https://www.facebook.com/groups/{group_id}/", connection=CONNECTION, label="group",
        variables_for=variables_for, stop_before=history_start(now(), newest_seen), newest_first=False,
        on_raw=on_raw, max_pages=max_pages)
