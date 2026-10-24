import pytest

from crawler.config import ConfigError, load_config


def write(tmp_path, body):
    p = tmp_path / "crawler.toml"
    p.write_text(body)
    return p


def test_rejects_more_than_two_places(tmp_path):
    with pytest.raises(ConfigError, match="at most 2 places"):
        load_config(write(tmp_path, 'places = ["Sylhet", "Dhaka", "Chittagong"]\n'))


def test_rejects_rate_ceiling_above_four(tmp_path):
    with pytest.raises(ConfigError, match="ceiling"):
        load_config(write(tmp_path, 'places = ["Sylhet"]\n[rates]\nceiling = 6\n'))


def test_defaults_have_known_doc_ids(tmp_path):
    cfg = load_config(write(tmp_path, 'places = ["Sylhet"]\n'))
    assert cfg.doc_ids["fb_timeline"] == "28591463417151325"
    assert cfg.doc_ids["fb_comments"] == "27806180149070312"
    assert cfg.doc_ids["fb_replies"] == "26570577339199586"
    assert cfg.doc_ids["fb_group"] == "25716860671307636"
    assert cfg.doc_ids["ig_profile_posts"] == "28570182382647478"
    assert cfg.doc_ids["ig_post"] == "27130156389949648"
    assert cfg.places == ("Sylhet",)
    assert cfg.rate_per_second == 2.0
    assert cfg.rate_ceiling == 4.0


def test_config_overrides_one_doc_id_and_keeps_the_rest(tmp_path):
    cfg = load_config(write(tmp_path, 'places = ["Sylhet"]\n[doc_ids]\nfb_comments = "1"\n'))
    assert cfg.doc_ids["fb_comments"] == "1"
    assert cfg.doc_ids["fb_replies"] == "26570577339199586"


def test_rejects_non_positive_rate(tmp_path):
    with pytest.raises(ConfigError, match="per_second"):
        load_config(write(tmp_path, 'places = ["Sylhet"]\n[rates]\nper_second = 0\n'))


def test_wrong_value_types_are_config_errors(tmp_path):
    for body, needle in (('places = ["Sylhet"]\n[rates]\nper_second = "fast"\n', "per_second"),
                         ('places = "Sylhet"\n', "places"),
                         ('places = ["Sylhet"]\ndoc_ids = "x"\n', "doc_ids")):
        with pytest.raises(ConfigError, match=needle):
            load_config(write(tmp_path, body))
