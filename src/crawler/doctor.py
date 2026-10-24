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
