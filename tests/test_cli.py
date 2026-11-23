from datetime import datetime, timedelta, timezone

import httpx

from fake_openshorts import FakeOpenShorts
from fake_world import Clock, FakeDownloader, FakeHelix, clip, stream, vod
from gta_shorts import cli
from gta_shorts.config import load
from gta_shorts.openshorts_client import OpenShorts
from gta_shorts.state import State
from gta_shorts.watch import Watcher


def test_status_reports_queue_budget_and_results(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GTA_SHORTS_API_URL", raising=False)
    conf = tmp_path / "config.toml"
    conf.write_text('[selection]\nmin_viewer_clips = 1\n[watch]\npriority = ["prio"]\n'
                    'max_stretches_per_day = 1\n[output]\ndir = "runs"\n', encoding="utf-8")
    cfg = load(conf)
    assert cli.main(["-c", str(conf), "status"]) == 1        # no state yet
    assert "No watcher state" in capsys.readouterr().out

    clock = Clock(datetime.now(timezone.utc).replace(microsecond=0))
    helix = FakeHelix()
    helix.logins["prio"] = ("7", "Prio")
    helix.top.append(stream("1", "big", 9000))
    for uid, login, vid in (("1", "big", "v1"), ("7", "prio", "v7")):
        helix.vods[uid] = [vod(vid, uid, login, ended=clock.t - timedelta(hours=1))]
        helix.clips[vid] = [clip(600, 100), clip(5000, 40)]
    state = State(cfg.state_db)
    api = OpenShorts("http://o", transport=httpx.MockTransport(FakeOpenShorts()), sleep=lambda s: None)
    Watcher(cfg, helix, api, state, log=lambda s: None, clock=clock, sleep=clock.sleep,
            downloader=FakeDownloader(), probe=lambda p: 1.0, disk_free=lambda p: 99.0).run(once=True)
    state.close()

    assert cli.main(["-c", str(conf), "status"]) == 0
    out = capsys.readouterr().out
    assert "Tracking 2 channel(s), priority: prio" in out
    assert "1/1 stretches submitted" in out and "2 clip(s) made" in out
    assert "Queue (3):" in out and "* queued" in out          # prio's 2nd stretch, starred
    assert "prio" in out.split("Finished in the last 24 h:")[1]
