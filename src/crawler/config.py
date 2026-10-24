import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MAX_PLACES = 2
MAX_RATE = 4.0

DEFAULT_DOC_IDS = {
    "fb_timeline": "28591463417151325",
    "fb_comments": "27806180149070312",
    "fb_replies": "26570577339199586",
    "fb_group": "25716860671307636",
    "ig_profile_posts": "28570182382647478",
    "ig_post": "27130156389949648",
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    places: tuple[str, ...]
    categories: tuple[str, ...] = ()
    rate_per_second: float = 2.0
    rate_ceiling: float = MAX_RATE
    doc_ids: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DOC_IDS))
    marketplace_city_ids: dict[str, str] = field(default_factory=dict)
    data_dir: Path = Path("data")

    def has_place(self, place: str) -> bool:
        return place.casefold() in {p.casefold() for p in self.places}


def load_config(path: Path) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e

    places = tuple(raw.get("places") or ())
    if not places:
        raise ConfigError("config needs at least 1 place")
    if len(places) > MAX_PLACES:
        raise ConfigError(f"config allows at most {MAX_PLACES} places, got {len(places)}")

    rates = raw.get("rates") or {}
    ceiling = float(rates.get("ceiling", MAX_RATE))
    if ceiling > MAX_RATE:
        raise ConfigError(f"rate ceiling {ceiling}/s is above the {MAX_RATE}/s maximum")
    per_second = min(float(rates.get("per_second", 2.0)), ceiling)

    return Config(
        places=places,
        categories=tuple(raw.get("categories") or ()),
        rate_per_second=per_second,
        rate_ceiling=ceiling,
        doc_ids={**DEFAULT_DOC_IDS, **(raw.get("doc_ids") or {})},
        marketplace_city_ids=dict(raw.get("marketplace_city_ids") or {}),
        data_dir=Path(raw.get("data_dir", "data")),
    )
