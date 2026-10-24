# fcommerce-crawler

On-demand reader for public Facebook Pages, groups, Marketplace and Instagram seller profiles.
No login, cookies, proxies or paid services. Nothing runs unless you ask.

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

## Test

```sh
uv run pytest            # offline, recorded fixtures
uv run pytest --live     # also hits real Facebook/Instagram
uv run ruff check
```
