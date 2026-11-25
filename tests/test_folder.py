from datetime import timedelta

import pytest

from gta_shorts import cli
from gta_shorts.post import Outbox, slug
from test_post import PostWorld


@pytest.fixture()
def world(tmp_path):
    w = PostWorld(tmp_path, post_mode="folder", post_folder_size=2)
    w.box = Outbox(w.cfg, w.state, log=w.lines.append, clock=w.clock)
    w.dir = w.cfg.output_dir / "to_post"
    yield w
    w.state.close()


def files(w):
    return sorted(p.name for p in w.dir.iterdir())


def test_folder_holds_the_best_clips_with_their_captions(world):
    world.clip("v1", "big", 95, k=1, caption="Cops gave up 😭 #GTA6")
    world.clip("v1", "big", 90, k=2)
    world.clip("v2", "other", 75)
    world.clip("v3", "meh", 40)                               # below min_score
    assert world.box.sync() == ["0001_big_hook-1.mp4", "0002_other_hook-1.mp4"]   # not big twice
    assert files(world) == ["0001_big_hook-1.mp4", "0001_big_hook-1.txt",
                            "0002_other_hook-1.mp4", "0002_other_hook-1.txt"]
    assert (world.dir / "0001_big_hook-1.txt").read_text("utf-8") == \
        "Cops gave up 😭 #GTA6\n\n🎥 twitch.tv/big\n"
    assert (world.dir / "0001_big_hook-1.mp4").read_bytes() == b"MP4" * 100
    assert world.box.sync() == []                             # full: nothing more
    assert world.up.uploads == []                             # nothing went to Upload-Post


def test_taking_a_clip_out_counts_as_posted_and_makes_room(world):
    first = world.clip("v1", "big", 95, k=1)
    world.clip("v2", "other", 90)
    world.clip("v1", "big", 80, k=2)
    world.box.sync()
    (world.dir / "0001_big_hook-1.mp4").unlink()              # uploaded, then deleted
    world.clock.advance(minutes=5)
    assert world.box.sync() == ["0003_big_hook-2.mp4"]
    row = world.post(first)
    assert row["status"] == "manual" and row["posted_at"] == "2026-11-21T12:05:00Z"
    assert world.state.clip_for_post(first)["posted_at"]
    assert "0001_big_hook-1.txt" not in files(world)          # its caption went too
    assert any("counted as posted" in l for l in world.lines)


def test_clip_left_too_long_is_swapped_for_a_newer_one(world):
    old = world.clip("v1", "big", 95, ago=timedelta(hours=70))
    world.clip("v2", "other", 90)
    world.box.sync()
    world.clock.advance(hours=3)                              # old is now 73 h old
    fresh = world.clip("v3", "new", 80)
    assert world.box.sync() == ["0003_new_hook-1.mp4"]
    assert world.post(old)["status"] == "expired" and world.post(fresh)["status"] == "exported"
    assert not any(f.startswith("0001") for f in files(world))


def test_failed_copy_puts_the_clip_back_in_line(world, monkeypatch):
    path = world.clip("v1", "big", 95)

    def disk_full(src, dst):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr("gta_shorts.post.shutil.copyfile", disk_full)
    assert world.box.sync() == []
    assert world.post(path)["status"] == "retry" and files(world) == []
    monkeypatch.undo()
    assert world.box.sync() == ["0002_big_hook-1.mp4"]


def test_leftover_partial_copy_is_cleaned_up(world):
    world.dir.mkdir(parents=True)
    (world.dir / ".0001_big_x.mp4.part").write_bytes(b"half")
    world.box.sync()
    assert files(world) == []


def test_the_poster_loop_fills_the_folder(world):
    world.clip("v1", "big", 95)
    world.run(minutes=3)
    assert files(world) == ["0001_big_hook-1.mp4", "0001_big_hook-1.txt"]
    assert any("nothing to post" in l for l in world.lines)   # only 1 of 2 slots filled
    assert world.up.uploads == []


def test_slug():
    assert slug("He stole a cop car mid-chase 😭") == "he-stole-a-cop-car-mid-chase"
    assert slug("💀💀") == "" and slug(None) == ""
    assert len(slug("word " * 30)) <= 40


def test_cli_folder_round_posts_and_skip_by_file_name(tmp_path, monkeypatch, capsys):
    conf = tmp_path / "config.toml"
    conf.write_text('[output]\ndir = "runs"\n[post]\nfolder_size = 1\n', encoding="utf-8")
    w = PostWorld(tmp_path)
    from datetime import datetime, timezone
    w.clock.t = datetime.now(timezone.utc).replace(microsecond=0)   # the CLI uses the real clock
    w.clip("v1", "big", 95)
    other = w.clip("v2", "other", 90)
    assert cli.main(["-c", str(conf), "post", "--once"]) == 0
    assert "0001_big_hook-1.mp4" in capsys.readouterr().out
    assert cli.main(["-c", str(conf), "posts"]) == 0
    out = capsys.readouterr().out
    assert "Posting: folder mode, 1/1 clip(s) in to_post/" in out and "0001_big_hook-1.mp4" in out

    assert cli.main(["-c", str(conf), "skip", "0001_big_hook-1.mp4"]) == 0
    assert "taken out of to_post/" in capsys.readouterr().out
    assert not (tmp_path / "runs" / "to_post" / "0001_big_hook-1.mp4").exists()
    assert cli.main(["-c", str(conf), "post", "--once"]) == 0
    assert w.state.post(other)["file"] == "0002_other_hook-1.mp4"
    with pytest.raises(SystemExit):                           # --private is for upload-post
        cli.main(["-c", str(conf), "post", "--once", "--private"])
    w.state.close()
