import json
from dataclasses import dataclass, field
from enum import StrEnum


class Outcome(StrEnum):
    OK = "ok"
    CACHED = "cached"
    NOTHING_NEW = "nothing_new"
    BLOCKED = "blocked"
    SCHEMA_CHANGED = "schema_changed"
    GONE = "gone"
    OUT_OF_SCOPE = "out_of_scope"
    LOGIN_EXPIRED = "login_expired"
    CRASH = "crash"


BLOCK_CODES = {1675004}
LOGIN_REQUIRED_CODES = {1357001}
LOGIN_URL_PARTS = ("/login", "/accounts/login", "/checkpoint")
HTML_BLOCK_MARKERS = ("CometErrorRoot", "blocked by our security", "temporarily blocked")


def json_documents(text: str) -> list[dict]:
    """Every top-level JSON object in a reply; FB streams one per line after `for (;;);`."""
    text = text.replace("for (;;);", "")
    decoder = json.JSONDecoder(strict=False)
    docs, i = [], 0
    while (start := text.find("{", i)) >= 0:
        try:
            doc, i = decoder.raw_decode(text, start)
        except ValueError:
            break
        if isinstance(doc, dict):
            docs.append(doc)
    return docs


def dig(obj, path):
    for key in path:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def classify(response, connection: tuple[str, ...] | None = None) -> Outcome:
    """Label a raw reply. `connection` is the key path under `data` that must hold the result."""
    status = response.status_code
    if status in (401, 403, 429):
        return Outcome.BLOCKED
    if status in (404, 410):
        return Outcome.GONE
    if status != 200:
        return Outcome.CRASH
    if any(part in str(response.url) for part in LOGIN_URL_PARTS):
        return Outcome.BLOCKED

    text = response.text or ""
    if connection is None:
        head = text[:20000]
        return Outcome.BLOCKED if any(m in head for m in HTML_BLOCK_MARKERS) else Outcome.OK

    docs = json_documents(text)
    errors = [e for d in docs for e in (d.get("errors") or []) if isinstance(e, dict)]
    if (any(e.get("code") in BLOCK_CODES for e in errors) or any(d.get("require_login") for d in docs)
            or any(d.get("error") in LOGIN_REQUIRED_CODES for d in docs)):
        return Outcome.BLOCKED

    found = next((dig(d.get("data"), connection) for d in docs if "path" not in d), None)
    if not isinstance(found, dict):
        return Outcome.SCHEMA_CHANGED

    streamed = [d for d in docs if d.get("path", [])[: len(connection) + 1] == [*connection, "edges"]]
    has_page_info = "page_info" in found or any(
        list(d.get("path", [])) == list(connection) and "page_info" in (d.get("data") or {}) for d in docs
    )
    if found.get("edges") or streamed or "edges" not in found:
        return Outcome.OK
    return Outcome.NOTHING_NEW if has_page_info else Outcome.SCHEMA_CHANGED


@dataclass
class FetchResult:
    outcome: Outcome
    items: list = field(default_factory=list)
    detail: str | None = None
