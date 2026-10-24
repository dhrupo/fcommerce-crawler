import json
from pathlib import Path
from types import SimpleNamespace

from crawler.config import Config
from crawler.facebook.marketplace import parse_listing, parse_search, search_marketplace
from crawler.outcomes import Outcome

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = Config(places=("Sylhet",), marketplace_city_ids={"Sylhet": "112294682115503"})


class FakeTransport:
    def __init__(self, text, url=None):
        self.text, self.url, self.asked = text, url, []

    def get(self, url, **kwargs):
        self.asked.append(url)
        return SimpleNamespace(status_code=200, text=self.text, url=self.url or url)


def html_with(edges):
    payload = {"require": [{"marketplace_search": {"feed_units": {"edges": edges}}}]}
    return f'<script type="application/json">{json.dumps(payload)}</script>'


def listing(**over):
    base = {"__typename": "GroupCommerceProductItem", "id": "L1", "marketplace_listing_title": "iPhone 13",
            "creation_time": 1792765407, "listing_price": {"amount": "72000.00", "formatted_amount": "BDT72,000"},
            "location": {"reverse_geocode": {"city": "সিলেট", "city_page": {"display_name": "Sylhet"}}},
            "is_sold": False, "primary_listing_photo": {"image": {"uri": "https://scontent/x.jpg"}},
            "marketplace_listing_seller": {"__typename": "User", "name": "Seller Person", "id": "pfbidSELLER"}}
    return {**base, **over}


def test_search_fixture_yields_listings():
    outcome, listings = parse_search((FIXTURES / "fb_marketplace_search.html").read_text())
    assert outcome is Outcome.OK
    assert len(listings) == 24
    first = listings[0]
    assert first["kind"] == "listing"
    assert first["item_id"] == "1110255885265960"
    assert first["price"] == {"amount": 14500.0, "formatted": "BDT14,500"}
    assert first["city"] == "Sylhet"
    assert first["sold"] is False
    assert first["images"][0].startswith("https://scontent")
    assert first["permalink"] == "https://www.facebook.com/marketplace/item/1110255885265960/"


def test_missing_price_is_unknown():
    assert parse_listing(listing(listing_price=None))["price"] == "unknown"
    assert parse_listing(listing(listing_price={"amount": "0.00", "formatted_amount": "FREE"}))["price"] == "unknown"


def test_no_seller_name():
    dumped = json.dumps(parse_listing(listing()))
    assert "Seller Person" not in dumped and "pfbidSELLER" not in dumped
    _, listings = parse_search((FIXTURES / "fb_marketplace_search.html").read_text())
    assert all("seller" not in json.dumps(item) for item in listings)


def test_search_uses_place_city_id_and_keyword():
    t = FakeTransport(html_with([{"node": {"listing": listing()}}]))
    result = search_marketplace(t, CONFIG, "Sylhet", "iphone 13")
    assert result.outcome is Outcome.OK and len(result.items) == 1
    assert t.asked == ["https://www.facebook.com/marketplace/112294682115503/search?query=iphone%2013"]


def test_empty_search_is_nothing_new():
    assert parse_search(html_with([]))[0] is Outcome.NOTHING_NEW


def test_page_without_search_results_is_schema_changed():
    assert parse_search("<html><body>new layout</body></html>")[0] is Outcome.SCHEMA_CHANGED


def test_login_wall_is_blocked():
    t = FakeTransport("<html></html>", url="https://www.facebook.com/login/?next=%2Fmarketplace")
    assert search_marketplace(t, CONFIG, "Sylhet", "iphone").outcome is Outcome.BLOCKED


def test_place_without_city_id_is_reported():
    result = search_marketplace(FakeTransport(""), Config(places=("Dhaka",)), "Dhaka", "iphone")
    assert result.outcome is Outcome.OUT_OF_SCOPE
    assert "marketplace_city_ids" in result.detail
