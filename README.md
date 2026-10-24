# fcommerce-crawler

On-demand reader for public Facebook Pages, groups, Marketplace and Instagram seller profiles.
Logged-out by default: no proxies or paid services, and nothing runs unless you ask.
An account is used only if you import its cookies, and only after a site blocks logged-out reading.

## Run

```sh
uv sync
cp crawler.example.toml crawler.toml   # edit places, sources, rates
uv run crawler sources add https://www.facebook.com/iStoreSyl --place Sylhet --category phones
uv run crawler search --place Sylhet --category phones "iphone 13"
uv run crawler serve                    # POST /search on 127.0.0.1:8765, NDJSON stream
uv run crawler status
uv run crawler doctor                   # find current doc_ids; add --live for a smoke check
```

## Optional login

When Facebook or Instagram blocks logged-out reading, the crawler can switch to an account for that site for
15 minutes, then go back to logged-out. Use a spare account: automated reading can get an account restricted.

1. Log in to facebook.com / instagram.com in Chrome.
2. Export the cookies for that site as `cookies.txt` (Netscape) or JSON, with a cookie-export extension.
3. Import them; the file can be deleted afterwards:

```sh
uv run crawler login fb --cookies ~/Downloads/facebook.com_cookies.txt
uv run crawler login ig --cookies ~/Downloads/instagram.com_cookies.json
uv run crawler status            # login fb: none | ready | in use | expired
uv run crawler doctor --live --login
uv run crawler logout fb
```

Cookies are saved in `<data_dir>/sessions/` with owner-only permissions and never printed. If a site refuses
the account, the session is marked `expired`, results say `login_expired`, and reading continues logged-out after
the cooldown. Import fresh cookies to use it again.

## Test

```sh
uv run pytest            # offline, recorded fixtures
uv run pytest --live     # also hits real Facebook/Instagram
uv run ruff check
```
