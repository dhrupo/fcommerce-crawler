import pytest

from crawler.config import Config
from crawler.doctor import live_smoke
from crawler.store import Store
from crawler.transport import Transport


@pytest.mark.live
def test_live_smoke(tmp_path):
    config = Config(places=("Sylhet",), marketplace_city_ids={"Sylhet": "112294682115503"})
    assert live_smoke(Transport(config), config, Store(tmp_path))
