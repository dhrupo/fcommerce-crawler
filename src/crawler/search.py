import base64
import binascii
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from .config import Config
from .facebook.bootstrap import group_id_from_url, resolve_page_id
from .facebook.comments import COMMENTS, REPLIES, fetch_comments
from .facebook.comments import parse_comment as parse_fb_comment
from .facebook.groups import fetch_group_posts
from .facebook.marketplace import parse_search, search_marketplace
from .facebook.pages import fetch_page_posts
from .facebook.parse import parse_feed
from .instagram import client as ig
from .instagram.client import InstagramClient
from .outcomes import FetchResult, Outcome, dig, json_documents
from .store import Source, Store
from .transport import HostBlocked, Transport

log = logging.getLogger(__name__)

FRESH_SECONDS = 3600
SCHEMA_VERSION = 1
PARSER_VERSION = "2026-10-24"
NO_CHANGES = {"new": False, "edited": False, "sold_changed": False}
FETCHED = (Outcome.OK, Outcome.NOTHING_NEW)


def source_info(source: Source | None, place=None, category=None) -> dict:
    if source is None:
        return {"url": None, "kind": None, "name": None, "place": place, "category": category}
    return {"url": source.url, "kind": source.kind, "name": source.name, "place": source.place,
            "category": source.category}


def record(item: dict, source: Source, strategy: str, fetched_at, changes=None) -> dict:
    """One output row: the parsed item plus the fields every consumer can rely on.

    author_id stays in the store for owner detection; in a group it is a private person, so it is never output.
    """
    item = {k: v for k, v in item.items() if k != "author_id"}
    return {**item, "schema_version": SCHEMA_VERSION, "source": source.url, "strategy": strategy,
            "parser_version": PARSER_VERSION, "fetched_at": fetched_at, "changes": changes or NO_CHANGES,
            "expired": bool(item.get("expired"))}


class _SourceJob:
    def __init__(self, store: Store, reader, source: Source, now, emit):
        self.store, self.reader, self.source, self.now, self.emit = store, reader, source, now, emit

    def event(self, phase, outcome, items, detail=None, served_from_store=False):
        self.emit({"source": source_info(self.source), "phase": phase, "outcome": str(outcome), "items": items,
                   "detail": detail, "served_from_store": served_from_store, "fetched_at": self.now()})

    def stored_posts(self, strategy, changes=None, fetched_at=None):
        changes = changes or {}
        return [record(i, self.source, strategy, fetched_at, changes.get(i["item_id"]))
                for i in self.store.items_for(self.source.id)]

    def stored_comments(self, strategy, fetched_at=None):
        return [record(c, self.source, strategy, fetched_at)
                for i in self.store.items_for(self.source.id) for c in self.store.comments_for(self.source.id, i["item_id"])]

    def run(self, force=False):
        state = self.store.fetch_state(self.source.id)
        if not force and state and state.fetched_at and self.now() - state.fetched_at < FRESH_SECONDS:
            strategy = "store"
            self.event("posts", Outcome.CACHED, self.stored_posts(strategy, fetched_at=state.fetched_at),
                       served_from_store=True)
            self.event("comments", Outcome.CACHED, self.stored_comments(strategy, state.fetched_at),
                       served_from_store=True)
            return
        if self.reader is None:
            self.fail("posts", Outcome.CRASH, f"no reader for {self.source.kind}")
            return
        try:
            result = self.reader.posts(self.source, state.newest_created_at if state else None)
        except HostBlocked:
            result = FetchResult(Outcome.BLOCKED, [], "host is cooling down after a block")
        except Exception:
            log.exception("posts of %s crashed", self.source.url)
            self.fail("posts", Outcome.CRASH, "reading posts failed; see the crawler log")
            return
        self.save_posts(result)

    def fail(self, phase, outcome, detail):
        self.store.set_fetch_state(self.source.id, outcome, detail=detail)
        strategy = getattr(self.reader, "strategy", "store")
        self.event(phase, outcome, self.stored_posts(strategy), detail, served_from_store=True)

    def save_posts(self, result: FetchResult):
        strategy, now = self.reader.strategy, self.now()
        before = {p["item_id"]: self.store.stored_comment_count(self.source.id, p["item_id"]) for p in result.items}
        changes = {p["item_id"]: self.store.upsert_item(self.source.id, p) for p in result.items}
        complete = result.outcome in FETCHED
        # Only a complete read may move "last seen"; after a partial one the older posts are still missing.
        newest = max((p["created_at"] for p in result.items if p.get("created_at")), default=None) if complete else None
        self.store.set_fetch_state(self.source.id, result.outcome, newest_created_at=newest, detail=result.detail)
        self.event("posts", result.outcome, self.stored_posts(strategy, changes, now), result.detail, not complete)
        if result.outcome is Outcome.BLOCKED or not result.items:
            self.event("comments", result.outcome, self.stored_comments(strategy, now), result.detail, True)
            return

        grew = [p for p in result.items
                if p.get("comment_count") is None or p["comment_count"] > (before.get(p["item_id"]) or 0)]
        outcome, detail = Outcome.NOTHING_NEW, None
        for post in grew:
            try:
                got = self.reader.comments(self.source, post, self.reader.owner_for(self.source, post))
            except HostBlocked:
                outcome, detail = Outcome.BLOCKED, "host is cooling down after a block"
                break
            except Exception:
                log.exception("comments of %s on %s crashed", post["item_id"], self.source.url)
                outcome, detail = Outcome.CRASH, "reading comments failed; see the crawler log"
                continue
            self.store.upsert_comments(self.source.id, post["item_id"], got.items)
            if got.outcome not in FETCHED:
                outcome, detail = got.outcome, got.detail
                if got.outcome is Outcome.BLOCKED:
                    break
            elif got.outcome is Outcome.OK and outcome is Outcome.NOTHING_NEW:
                outcome = Outcome.OK
        self.event("comments", outcome, self.stored_comments(strategy, now), detail, outcome not in FETCHED)


def _marketplace_job(store: Store, marketplace, place: str, keyword: str, now, emit):
    url = f"marketplace:{place}"
    source = store.get_source(url) or store.add_source(url, "fb_marketplace", place, None)
    try:
        result = marketplace(place, keyword)
    except HostBlocked:
        result = FetchResult(Outcome.BLOCKED, [], "host is cooling down after a block")
    except Exception:
        log.exception("marketplace search in %s crashed", place)
        result = FetchResult(Outcome.CRASH, [], "marketplace search failed; see the crawler log")
    fetched_at = now()
    items = [record(i, source, "fb_marketplace_html", fetched_at, store.upsert_item(source.id, i))
             for i in result.items]
    store.set_fetch_state(source.id, result.outcome, detail=result.detail)
    emit({"source": source_info(source), "phase": "posts", "outcome": str(result.outcome), "items": items,
          "detail": result.detail, "served_from_store": False, "fetched_at": fetched_at})


def search(config: Config, store: Store, place: str, category: str | None, keyword: str | None = None, *,
           readers: dict, marketplace, now=time.time, workers: int = 4):
    """Stream events ({source, phase, outcome, items, ...}) as each source finishes: posts first, then comments."""
    if not config.has_place(place):
        yield {"source": source_info(None, place, category), "phase": "posts", "outcome": str(Outcome.OUT_OF_SCOPE),
               "items": [], "detail": f"{place} is not one of the configured places: {', '.join(config.places)}",
               "served_from_store": False, "fetched_at": now()}
        return

    store.expire_old()
    store.prune_raw()
    events: queue.Queue = queue.Queue()
    done = object()
    jobs = [_SourceJob(store, readers.get(s.kind), s, now, events.put).run
            for s in store.list_sources(place=place, category=category) if s.kind != "fb_marketplace"]
    if keyword:
        jobs.append(lambda: _marketplace_job(store, marketplace, place, keyword, now, events.put))

    def guarded(job):
        try:
            job()
        except Exception:
            log.exception("search job crashed")
        finally:
            events.put(done)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for job in jobs:
            pool.submit(guarded, job)
        pending = len(jobs)
        while pending:
            event = events.get()
            if event is done:
                pending -= 1
            else:
                yield event


def fetch_source(store: Store, readers: dict, source: Source, now=time.time) -> list[dict]:
    """Read one source now, ignoring the 1-hour memory."""
    events: list[dict] = []
    _SourceJob(store, readers.get(source.kind), source, now, events.append).run(force=True)
    return events


class _Reader:
    def __init__(self, config: Config, transport: Transport, store: Store, now=time.time):
        self.config, self.transport, self.store, self.now = config, transport, store, now

    def raw(self, source):
        return lambda label, text: self.store.save_raw(source.id, label, text)

    def checked(self, source, result: FetchResult) -> FetchResult:
        if result.outcome is Outcome.BLOCKED:
            self.transport.mark_blocked(source.url)
        return result


class FacebookPageReader(_Reader):
    strategy = "fb_timeline_graphql"

    def posts(self, source, newest_seen):
        page_id = source.native_id
        if not page_id:
            outcome, page_id = resolve_page_id(self.transport, source.url)
            if outcome is not Outcome.OK:
                return self.checked(source, FetchResult(outcome, [], f"page id of {source.url}: {outcome}"))
            self.store.update_source(source.id, native_id=page_id)
        return self.checked(source, fetch_page_posts(self.transport, self.config, page_id, newest_seen=newest_seen,
                                                     now=self.now, on_raw=self.raw(source)))

    def owner_for(self, source, post):
        return post.get("owner_id") or source.native_id

    def comments(self, source, post, owner_id):
        return self.checked(source, fetch_comments(self.transport, self.config, post, owner_id, self.raw(source)))


class FacebookGroupReader(FacebookPageReader):
    strategy = "fb_group_graphql"

    def posts(self, source, newest_seen):
        group_id = group_id_from_url(source.url)
        if not group_id:
            return FetchResult(Outcome.GONE, [], "group sources need the numeric /groups/<id> URL")
        return self.checked(source, fetch_group_posts(self.transport, self.config, group_id,
                                                      newest_seen=newest_seen, now=self.now,
                                                      on_raw=self.raw(source)))

    def owner_for(self, source, post):
        return post.get("author_id")


class InstagramReader(_Reader):
    strategy = "ig_polaris_graphql"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._clients: dict[int, InstagramClient] = {}
        self._lock = threading.Lock()

    def client(self, source) -> InstagramClient:
        with self._lock:
            if source.id not in self._clients:
                username = urlparse(source.url).path.strip("/").split("/")[0]
                self._clients[source.id] = InstagramClient(self.transport, self.config, username, self.now,
                                                           self.raw(source))
            return self._clients[source.id]

    def posts(self, source, newest_seen):
        return self.checked(source, self.client(source).fetch_posts(newest_seen))

    def owner_for(self, source, post):
        return self.client(source).owner_id

    def comments(self, source, post, owner_id):
        return self.checked(source, self.client(source).fetch_comments(post))


def default_readers(config: Config, transport: Transport, store: Store) -> dict:
    return {"fb_page": FacebookPageReader(config, transport, store),
            "fb_group": FacebookGroupReader(config, transport, store),
            "ig_profile": InstagramReader(config, transport, store)}


def default_marketplace(config: Config, transport: Transport, store: Store):
    def run(place, keyword):
        source = store.get_source(f"marketplace:{place}")
        on_raw = (lambda label, text: store.save_raw(source.id, label, text)) if source else None
        result = search_marketplace(transport, config, place, keyword, on_raw)
        if result.outcome is Outcome.BLOCKED:
            transport.mark_blocked("https://www.facebook.com/marketplace")
        return result
    return run


def _post_and_parent(feedback_id: str) -> tuple[str | None, str | None]:
    """'feedback:<post>' or 'feedback:<post>_<comment>' -> (post id, parent comment id)."""
    try:
        decoded = base64.b64decode(feedback_id).decode()
    except (binascii.Error, UnicodeDecodeError, TypeError):
        return None, None
    post, _, parent = decoded.removeprefix("feedback:").partition("_")
    return post or None, parent or None


def _owner(store: Store, source: Source, item_id) -> str | None:
    item = store.get_item(source.id, item_id) or {}
    return item.get("author_id") if source.kind == "fb_group" else item.get("owner_id") or source.native_id


def _reparse_comments(store: Store, source: Source, label: str, text: str) -> int:
    data = (json_documents(text) or [{}])[0].get("data")
    if label == "ig-post":
        media = dig(data, ig.MEDIA) or {}
        item_id = str(media.get("pk") or "")
        edges = (media.get("comments_connection") or {}).get("edges") or []
        comments = [ig.parse_comment(e["node"], item_id, _owner(store, source, item_id)) for e in edges if e.get("node")]
    else:
        connection = COMMENTS if label == "comments" else REPLIES
        item_id, parent = _post_and_parent((dig(data, ("node",)) or {}).get("id") or "")
        conn = dig(data, connection) or {}
        owner = _owner(store, source, item_id)
        comments = [parse_fb_comment(e["node"], item_id, owner, parent) for e in conn.get("edges") or [] if e.get("node")]
    if not item_id or not store.get_item(source.id, item_id):
        return 0
    store.upsert_comments(source.id, item_id, comments)
    return len(comments)


def reparse_raw(store: Store, since: float | None = None) -> dict:
    """Rebuild items and comments from the kept raw replies, e.g. after a parser fix."""
    sources = {s.id: s for s in store.list_sources()}
    counts = {"posts": 0, "comments": 0, "skipped": 0}
    for source_id, label, text in store.iter_raw(since):
        source = sources.get(source_id)
        if source is None:
            counts["skipped"] += 1
            continue
        if label in ("timeline", "group"):
            items = parse_feed(text)
        elif label == "marketplace":
            items = parse_search(text)[1]
        elif label == "ig-posts":
            conn = dig((json_documents(text) or [{}])[0].get("data"), ig.POSTS) or {}
            items = [ig.parse_post(e["node"]) for e in conn.get("edges") or [] if (e.get("node") or {}).get("pk")]
        elif label in ("comments", "replies", "ig-post"):
            counts["comments"] += _reparse_comments(store, source, label, text)
            continue
        else:
            counts["skipped"] += 1
            continue
        for item in items:
            store.upsert_item(source.id, item)
        counts["posts"] += len(items)
    store.expire_old()
    return counts
