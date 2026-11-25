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


def test_watch_section_and_container_api_url(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GTA_SHORTS_API_URL", "http://backend:8000")
    (tmp_path / "config.toml").write_text(
        '[watch]\npriority = ["SomeStreamer", " "]\nmax_stretches_per_day = 4\n'
        'max_usd_per_day = 0\n[output]\ndir = "out"\n', encoding="utf-8")
    cfg = config.load(tmp_path / "config.toml")
    assert cfg.priority == ["somestreamer"] and cfg.max_stretches_per_day == 4
    assert cfg.max_usd_per_day is None                     # 0 = no dollar cap
    assert cfg.state_db == tmp_path / "out" / "state.db"
    assert cfg.api_url == "http://backend:8000"


def test_post_section(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UPLOAD_POST_API_KEY", " key ")
    (tmp_path / "config.toml").write_text(
        '[watch]\nmax_attempts = 5\n[post]\nmode = "Upload-Post"\nuser = " me "\nper_day = 4\n'
        'hours = [18, 2]\nprivacy = "self_only"\nmax_attempts = 2\n', encoding="utf-8")
    cfg = config.load(tmp_path / "config.toml")
    assert cfg.post_mode == "upload-post" and cfg.post_user == "me" and cfg.post_per_day == 4
    assert cfg.post_hours == [18, 2] and cfg.post_privacy == "SELF_ONLY"
    assert (cfg.max_attempts, cfg.post_max_attempts) == (5, 2)     # no clash with [watch]
    assert cfg.upload_post_api_key == "key"


@pytest.mark.parametrize("line", ['mode = "auto"', 'privacy = "everyone"', 'timezone = "New York"',
                                  "hours = [9]", "hours = [9, 25]"])
def test_bad_post_settings_are_errors(tmp_path, line):
    p = tmp_path / "config.toml"
    p.write_text(f"[post]\n{line}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[post\]"):
        config.load(p)
