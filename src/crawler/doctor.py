import base64
import binascii
import re

from .config import Config

RELAY_ID = re.compile(r'__d\("(\w+)_facebookRelayOperation",\[\],\(function\([\w,]*\)\{\w+\.exports="(\d+)"\}')
INLINE_JS = re.compile(r'<script[^>]+src="data:application/x-javascript[^,]*;base64,([A-Za-z0-9+/=]+)"')
SCRIPT_SRC = re.compile(r'<script[^>]+src="(https://static\.xx\.fbcdn\.net/rsrc\.php/[^"]+\.js[^"]*)"')
MAX_BUNDLES = 40
QUERIES = {
    "fb_timeline": "ProfileCometTimelineFeedRefetchQuery",
    "fb_comments": "CommentsListComponentsPaginationQuery",
    "fb_replies": "Depth1CommentsListPaginationQuery",
    "fb_group": "GroupsCometFeedRegularStoriesPaginationQuery",
}


def doc_ids_in_bundle(js: str) -> dict[str, str]:
    return dict(RELAY_ID.findall(js))


def scan_doc_ids(transport, config: Config, page_url: str) -> list[dict]:
    """Read a public Page's JS bundles and compare the doc_ids found with the config. Run by hand only."""
    from .outcomes import Outcome, classify

    page = transport.get(page_url, headers={"accept-language": "en-US"})
    if (outcome := classify(page)) is not Outcome.OK:
        return [{"key": key, "query": query, "config": config.doc_ids.get(key), "found": str(outcome)}
                for key, query in QUERIES.items()]
    html = page.text
    found: dict[str, str] = {}
    for encoded in INLINE_JS.findall(html):
        try:
            found.update(doc_ids_in_bundle(base64.b64decode(encoded).decode("utf-8", "ignore")))
        except binascii.Error:
            continue
    for src in list(dict.fromkeys(SCRIPT_SRC.findall(html)))[:MAX_BUNDLES]:
        found.update(doc_ids_in_bundle(transport.get(src).text))
        if all(q in found for q in QUERIES.values()):
            break
    return [{"key": key, "query": query, "config": config.doc_ids.get(key), "found": found.get(query)}
            for key, query in QUERIES.items()]


def live_smoke(transport, config: Config, store, page_url: str = "https://www.facebook.com/iStoreSyl",
               ig_username: str = "nasa") -> bool:
    """One real Page (posts + comments), one Marketplace search and one IG profile. Prints tested X of Y."""
    from .facebook.bootstrap import resolve_page_id
    from .facebook.comments import fetch_comments
    from .facebook.marketplace import search_marketplace
    from .facebook.pages import fetch_page_posts
    from .instagram.client import InstagramClient
    from .outcomes import FetchResult, Outcome
    from .search import fetch_with_login
    from .transport import DROPS, HostBlocked

    def attempt(url, step):
        """Through the same login-aware path as a search, so a refused login is expired here too."""
        try:
            result = fetch_with_login(transport, url, step)
            return result.outcome, len(result.items)
        except HostBlocked:
            return Outcome.BLOCKED, 0
        except DROPS:
            return "network error", 0

    found = {}

    def page_posts():
        outcome, page_id = resolve_page_id(transport, page_url)
        if not page_id:
            return FetchResult(outcome, [])
        found["posts"] = fetch_page_posts(transport, config, page_id, max_pages=2)
        return found["posts"]

    def comments():
        posts = found.get("posts")
        post = next((p for p in (posts.items if posts else []) if (p.get("comment_count") or 0) > 0), None)
        if not post:
            return FetchResult("no post with comments in the first pages", [])
        return fetch_comments(transport, config, post, post.get("owner_id"))

    place = next(iter(config.marketplace_city_ids), None)
    checks = [("fb page posts", *attempt(page_url, page_posts)),
              ("fb comments", *attempt(page_url, comments)),
              (f"marketplace {place}", *attempt("https://www.facebook.com/marketplace",
                                                lambda: search_marketplace(transport, config, place, "iphone")))
              if place else ("marketplace", "no marketplace_city_ids in config", 0),
              (f"instagram {ig_username}", *attempt(
                  "https://www.instagram.com/", lambda: InstagramClient(transport, config, ig_username)
                  .fetch_posts(max_pages=1)))]

    passed = 0
    for name, outcome, count in checks:
        ok = outcome == Outcome.OK and count > 0
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<22} {outcome}  items={count}")
    print(f"tested {passed} of {len(checks)} passed")
    return passed == len(checks)
