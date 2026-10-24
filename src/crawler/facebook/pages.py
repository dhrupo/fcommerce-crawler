import json
import time

from ..config import Config
from ..outcomes import FetchResult, Outcome, classify, json_documents
from .parse import find_stories, next_cursor, parse_story

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
CONNECTION = ("node", "timeline_list_feed_units")
HISTORY_DAYS = 60
MAX_PAGES = 200
RELAY_FLAGS = {
    "__relay_internal__pv__GHLShouldChangeAdIdFieldNamerelayprovider": True,
    "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": True,
    "__relay_internal__pv__CometFeedStory_enable_reactor_facepilerelayprovider": False,
    "__relay_internal__pv__CometFeedStory_enable_social_bubblesrelayprovider": False,
    "__relay_internal__pv__CometFeedStory_enable_post_permalink_white_space_clickrelayprovider": False,
    "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": True,
    "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
    "__relay_internal__pv__IsWorkUserrelayprovider": False,
    "__relay_internal__pv__TestPilotShouldIncludeDemoAdUseCaserelayprovider": False,
    "__relay_internal__pv__FBReels_deprecate_short_form_video_context_gkrelayprovider": True,
    "__relay_internal__pv__FBReels_enable_view_dubbed_audio_type_gkrelayprovider": True,
    "__relay_internal__pv__CometFeedShareMedia_shouldPrefetchShareImagerelayprovider": False,
    "__relay_internal__pv__CometImmersivePhotoCanUserDisable3DMotionrelayprovider": False,
    "__relay_internal__pv__WorkCometIsEmployeeGKProviderrelayprovider": False,
    "__relay_internal__pv__IsMergQAPollsrelayprovider": False,
    "__relay_internal__pv__FBReelsMediaFooter_comet_enable_reels_ads_gkrelayprovider": True,
    "__relay_internal__pv__CometUFIReactionsEnableShortNamerelayprovider": False,
    "__relay_internal__pv__CometUFICommentAutoTranslationTyperelayprovider": "AUTO_TRANSLATE",
    "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
    "__relay_internal__pv__CometUFISingleLineUFIrelayprovider": True,
    "__relay_internal__pv__relay_provider_comet_ufi_ssr_seo_deferrelayprovider": True,
    "__relay_internal__pv__CometUFI_dedicated_comment_routable_dialog_gkrelayprovider": True,
    "__relay_internal__pv__ReelsIFUCard_reelsIFULikeCountrelayprovider": False,
    "__relay_internal__pv__FBReelsIFUTileContent_reelsIFUPlayOnHoverrelayprovider": True,
    "__relay_internal__pv__StoriesShouldEnablePhotosensitiveContentWarningrelayprovider": False,
    "__relay_internal__pv__ShouldEnableBakedInTextStoriesrelayprovider": False,
    "__relay_internal__pv__StoriesShouldIncludeFbNotesrelayprovider": True,
}


def graphql_form(doc_id: str, friendly_name: str, variables: dict) -> dict:
    """Logged-out GraphQL form: no user, no fb_dtsg."""
    return {
        "av": "0", "__user": "0", "__a": "1", "__comet_req": "15", "fb_dtsg": "",
        "fb_api_caller_class": "RelayModern", "fb_api_req_friendly_name": friendly_name,
        "server_timestamps": "true", "doc_id": doc_id, "variables": json.dumps(variables),
    }


def headers(referer: str, friendly_name: str) -> dict:
    return {
        "content-type": "application/x-www-form-urlencoded", "origin": "https://www.facebook.com",
        "referer": referer, "x-fb-friendly-name": friendly_name, "accept-language": "en-US,en;q=0.9",
        "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
    }


def crossed_boundary(is_fresh: list[bool]) -> bool:
    """True once an old post follows a fresh one; a pinned old post sits before them and does not count."""
    if True not in is_fresh:
        return True
    return False in is_fresh[is_fresh.index(True):]


def fetch_feed(transport, *, doc_id: str, friendly: str, referer: str, connection: tuple[str, ...], label: str,
               variables_for, stop_before: float, newest_first: bool, on_raw=None, max_pages=MAX_PAGES) -> FetchResult:
    """Page through a story feed, keeping posts newer than stop_before."""
    head = headers(referer, friendly)
    items, cursor = [], None
    for page_number in range(1, max_pages + 1):
        response = transport.post(GRAPHQL_URL, data=graphql_form(doc_id, friendly, variables_for(cursor)), headers=head)
        if on_raw:
            on_raw(label, response.text)
        outcome = classify(response, connection)
        if outcome is Outcome.NOTHING_NEW:
            break
        if outcome is not Outcome.OK:
            return FetchResult(outcome, items, f"page {page_number}: {outcome}")

        docs = json_documents(response.text)
        posts = [parse_story(s) for s in find_stories(docs)]
        is_fresh = [p["created_at"] is None or p["created_at"] > stop_before for p in posts]
        items += [p for p, fresh in zip(posts, is_fresh, strict=True) if fresh]
        cursor = next_cursor(docs, connection)
        if not cursor or (newest_first and crossed_boundary(is_fresh)):
            break
    return FetchResult(Outcome.OK if items else Outcome.NOTHING_NEW, items)


def history_start(now: float, newest_seen: float | None) -> float:
    return max(now - HISTORY_DAYS * 86400, newest_seen or 0)


def fetch_page_posts(transport, config: Config, page_id: str, *, newest_seen=None, now=time.time,
                     on_raw=None, max_pages=MAX_PAGES) -> FetchResult:
    """Newest-first timeline, back to 60 days ago or to the newest post already stored."""
    def variables_for(cursor):
        return {"afterTime": None, "beforeTime": None, "count": 3, "cursor": cursor, "feedLocation": "TIMELINE",
                "renderLocation": "timeline", "scale": 2, "id": page_id, **RELAY_FLAGS}

    return fetch_feed(
        transport, doc_id=config.doc_ids["fb_timeline"], friendly="ProfileCometTimelineFeedRefetchQuery",
        referer=f"https://www.facebook.com/profile.php?id={page_id}", connection=CONNECTION, label="timeline",
        variables_for=variables_for, stop_before=history_start(now(), newest_seen), newest_first=True,
        on_raw=on_raw, max_pages=max_pages)
