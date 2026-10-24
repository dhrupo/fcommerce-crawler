import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from .config import ConfigError, load_config
from .facebook.bootstrap import group_id_from_url
from .store import Store

KINDS = ("fb_page", "fb_group", "ig_profile")


def source_kind(url: str) -> str:
    if "instagram.com/" in url:
        return "ig_profile"
    return "fb_group" if "/groups/" in url else "fb_page"


def when(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="crawler", description="On-demand, login-free seller post reader")
    p.add_argument("--config", default="crawler.toml", help="path to crawler.toml")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="read the sources for a place (and Marketplace) and print NDJSON events")
    s.add_argument("--place", required=True)
    s.add_argument("--category")
    s.add_argument("keyword", nargs="?")

    f = sub.add_parser("fetch", help="read one stored source now, ignoring the 1-hour memory")
    f.add_argument("url")

    src = sub.add_parser("sources", help="manage the hand-made source list")
    ssub = src.add_subparsers(dest="action", required=True)
    add = ssub.add_parser("add")
    add.add_argument("url")
    add.add_argument("--place", required=True)
    add.add_argument("--category", required=True)
    add.add_argument("--kind", choices=KINDS)
    lst = ssub.add_parser("list")
    lst.add_argument("--place")
    rm = ssub.add_parser("remove")
    rm.add_argument("url")

    sub.add_parser("status", help="last outcome per source")
    r = sub.add_parser("reparse", help="rebuild items from kept raw replies")
    r.add_argument("--since", required=True, help="YYYY-MM-DD")
    d = sub.add_parser("doctor", help="find current doc_ids in Facebook's JS bundles")
    d.add_argument("--page", default="https://www.facebook.com/iStoreSyl")
    d.add_argument("--live", action="store_true", help="also run the live smoke check")
    sv = sub.add_parser("serve", help="local HTTP API: POST /search streams NDJSON")
    sv.add_argument("--port", type=int, default=8765)
    return p


def fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv=None, transport_factory=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        config = load_config(Path(args.config))
    except ConfigError as e:
        return fail(str(e))
    data_dir = config.data_dir if config.data_dir.is_absolute() else Path(args.config).parent / config.data_dir
    store = Store(data_dir)

    def transport():
        from .transport import Transport
        return (transport_factory or Transport)(config)

    if args.command == "sources":
        return sources(args, config, store)
    if args.command == "status":
        for s in store.list_sources():
            st = store.fetch_state(s.id)
            state = (f"{st.last_outcome:<14} tried {when(st.attempted_at)}  good {when(st.fetched_at)}  "
                     f"{st.detail or ''}") if st else "never fetched"
            print(f"{s.place:<10} {s.category or '-':<10} {s.kind:<14} {s.url}  {state}")
        return 0
    if args.command == "reparse":
        from .search import reparse_raw
        since = datetime.strptime(args.since, "%Y-%m-%d").timestamp()
        counts = reparse_raw(store, since)
        print(f"rebuilt {counts['posts']} items and {counts['comments']} comments ({counts['skipped']} skipped)")
        return 0
    if args.command == "search":
        from .search import default_marketplace, default_readers, search
        t = transport()
        events = search(config, store, args.place, args.category, args.keyword,
                        readers=default_readers(config, t, store), marketplace=default_marketplace(config, t, store))
        for event in events:
            print(json.dumps(event, ensure_ascii=False, default=str), flush=True)
        return 0
    if args.command == "fetch":
        from .search import default_readers, fetch_source
        source = store.get_source(args.url)
        if source is None:
            return fail(f"{args.url} is not a source; add it with `crawler sources add` first")
        t = transport()
        for event in fetch_source(store, default_readers(config, t, store), source):
            print(json.dumps(event, ensure_ascii=False, default=str), flush=True)
        return 0
    if args.command == "doctor":
        from .doctor import live_smoke, scan_doc_ids
        t = transport()
        for row in scan_doc_ids(t, config, args.page):
            mark = "same" if row["found"] == row["config"] else ("not found" if not row["found"] else "DIFFERENT")
            print(f"{row['key']:<12} {row['query']:<45} config={row['config']} found={row['found']}  {mark}")
        if args.live:
            return 0 if live_smoke(t, config, store) else 1
        return 0
    if args.command == "serve":
        from .api import serve
        serve(config, store, transport(), port=args.port)
        return 0
    return 1


def sources(args, config, store) -> int:
    if args.action == "add":
        if not config.has_place(args.place):
            return fail(f"{args.place} is not one of the configured places: {', '.join(config.places)}")
        kind = args.kind or source_kind(args.url)
        if kind == "fb_group" and not group_id_from_url(args.url):
            return fail("logged out, only numeric group URLs work: https://www.facebook.com/groups/<id>/")
        s = store.add_source(args.url, kind, args.place, args.category)
        print(f"added {s.kind} {s.url} ({s.place}, {s.category})")
        return 0
    if args.action == "list":
        for s in store.list_sources(place=args.place):
            print(f"{s.place:<10} {s.category or '-':<10} {s.kind:<14} {s.url}")
        return 0
    if args.action == "remove":
        return 0 if store.remove_source(args.url) else fail(f"{args.url} is not a source")
    return 1


if __name__ == "__main__":
    sys.exit(main())
