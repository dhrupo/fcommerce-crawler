import json
import re
from urllib.parse import quote

from ..config import Config
from ..outcomes import FetchResult, Outcome, classify
from .parse import walk

SCRIPT_JSON = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.DOTALL)


def parse_price(price) -> dict | str:
    """Zero or missing means the seller did not give a price ("inbox"): keep it as unknown."""
    try:
        amount = float((price or {}).get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    return {"amount": amount, "formatted": price.get("formatted_amount")} if amount > 0 else "unknown"


def parse_listing(node: dict) -> dict:
    geo = (node.get("location") or {}).get("reverse_geocode") or {}
    photo = ((node.get("primary_listing_photo") or {}).get("image") or {}).get("uri")
    return {
        "item_id": str(node["id"]),
        "kind": "listing",
        "created_at": node.get("creation_time"),
        "title": node.get("marketplace_listing_title"),
        "price": parse_price(node.get("listing_price")),
        "city": (geo.get("city_page") or {}).get("display_name") or geo.get("city"),
        "images": [photo] if photo else [],
        "sold": bool(node.get("is_sold")),
        "permalink": f"https://www.facebook.com/marketplace/item/{node['id']}/",
    }


def parse_search(html: str) -> tuple[Outcome, list[dict]]:
    feeds = []
    for block in SCRIPT_JSON.findall(html):
        if "marketplace_search" not in block:
            continue
        try:
            data = json.loads(block)
        except ValueError:
            continue
        feeds += [d["marketplace_search"] for d in walk(data) if isinstance(d.get("marketplace_search"), dict)]
    if not feeds:
        return Outcome.SCHEMA_CHANGED, []
    listings, seen = [], set()
    for feed in feeds:
        for d in walk(feed):
            is_listing = d.get("__typename") == "GroupCommerceProductItem" and "marketplace_listing_title" in d
            if is_listing and d.get("id") and d["id"] not in seen:
                seen.add(d["id"])
                listings.append(parse_listing(d))
    return (Outcome.OK if listings else Outcome.NOTHING_NEW), listings


def search_marketplace(transport, config: Config, place: str, keyword: str, on_raw=None) -> FetchResult:
    city_id = next((v for k, v in config.marketplace_city_ids.items() if k.casefold() == place.casefold()), None)
    if not city_id:
        return FetchResult(Outcome.OUT_OF_SCOPE, [], f"no marketplace_city_ids entry for {place}")
    url = f"https://www.facebook.com/marketplace/{city_id}/search?query={quote(keyword)}"
    response = transport.get(url, headers={"accept-language": "en-US,en;q=0.9"})
    if on_raw:
        on_raw("marketplace", response.text)
    outcome = classify(response)
    if outcome is not Outcome.OK:
        return FetchResult(outcome, [], f"marketplace {place}: {outcome}")
    outcome, listings = parse_search(response.text)
    return FetchResult(outcome, listings, None if outcome is Outcome.OK else f"marketplace {place}: {outcome}")
