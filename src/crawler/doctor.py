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
    html = transport.get(page_url, headers={"accept-language": "en-US"}).text
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
    from .outcomes import Outcome

    checks = []
    outcome, page_id = resolve_page_id(transport, page_url)
    posts = fetch_page_posts(transport, config, page_id, max_pages=2) if page_id else None
    checks.append(("fb page posts", posts.outcome if posts else outcome, len(posts.items) if posts else 0))
    with_comments = next((p for p in (posts.items if posts else []) if (p.get("comment_count") or 0) > 0), None)
    if with_comments:
        got = fetch_comments(transport, config, with_comments, with_comments.get("owner_id"))
        checks.append(("fb comments", got.outcome, len(got.items)))
    else:
        checks.append(("fb comments", "no post with comments in the first pages", 0))
    place = next(iter(config.marketplace_city_ids), None)
    if place:
        got = search_marketplace(transport, config, place, "iphone")
        checks.append((f"marketplace {place}", got.outcome, len(got.items)))
    else:
        checks.append(("marketplace", "no marketplace_city_ids in config", 0))
    got = InstagramClient(transport, config, ig_username).fetch_posts(max_pages=1)
    checks.append((f"instagram {ig_username}", got.outcome, len(got.items)))

    passed = 0
    for name, outcome, count in checks:
        ok = outcome == Outcome.OK and count > 0
        passed += ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<22} {outcome}  items={count}")
    print(f"tested {passed} of {len(checks)} passed")
    return passed == len(checks)
