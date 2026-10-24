import re
from urllib.parse import quote

from ..outcomes import Outcome, classify

ID_IN_URL = (r"profile\.php\?id=(\d+)", r"/profile/(\d+)", r"[?&]id=(\d+)")
ID_IN_HTML = (r'"userID":"(\d{5,})"', r'"pageID":"(\d{5,})"', r"fb://profile/(\d{5,})", r'"profile_owner":"(\d{5,})"')
HEADERS = {"accept-language": "en-US,en;q=0.9"}


def page_id_from_html(html: str) -> str | None:
    for pattern in ID_IN_HTML:
        if m := re.search(pattern, html):
            return m.group(1)
    return None


def resolve_page_id(transport, url: str) -> tuple[Outcome, str | None]:
    """Vanity Page URL -> numeric id: the id in the URL, else the Page HTML, else the page plugin."""
    for pattern in ID_IN_URL:
        if m := re.search(pattern, url):
            return Outcome.OK, m.group(1)
    for candidate in (url, f"https://www.facebook.com/plugins/page.php?href={quote(url, safe='')}"):
        response = transport.get(candidate, headers=HEADERS)
        outcome = classify(response)
        if outcome is not Outcome.OK:
            return outcome, None
        if page_id := page_id_from_html(response.text):
            return Outcome.OK, page_id
    return Outcome.GONE, None


def group_id_from_url(url: str) -> str | None:
    """Only numeric group URLs: logged out, vanity group names do not resolve to an id."""
    m = re.search(r"/groups/(\d+)", url)
    return m.group(1) if m else None
