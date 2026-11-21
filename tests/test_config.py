from pathlib import Path

import pytest

from gta_shorts import config


def test_load_reads_sections_and_resolves_output_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "sec")
    (tmp_path / "config.toml").write_text(
        '[twitch]\nlanguage = ""\n[selection]\nstretch_minutes = 8\n'
        '[openshorts]\nlayouts = ["auto", "punch_in"]\ntarget_clips = 3\n'
        '[output]\ndir = "out"\n', encoding="utf-8")
    cfg = config.load(tmp_path / "config.toml")
    assert cfg.language is None and cfg.stretch_minutes == 8
    assert cfg.layouts == ["auto", "punch_in"] and cfg.target_clips == 3
    assert cfg.output_dir == tmp_path / "out"
    assert (cfg.twitch_client_id, cfg.twitch_client_secret) == ("cid", "sec")


def test_unknown_section_is_an_error(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[posting]\nx = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.load(p)


def test_example_config_loads(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    cfg = config.load(root / "config.example.toml")
    assert cfg.game_name == "Grand Theft Auto VI" and cfg.api_url.startswith("http")
