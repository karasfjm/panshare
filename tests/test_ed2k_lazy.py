"""ed2k「直接生成 strm、播放时才转存」+ 115 STRM 定时巡检 + 登录页背景配置。

三块都是线上提的需求，各自锁住关键行为：

1. ed2k 添加时**只写 strm**（不提交离线任务、不占网盘），播放时才转存；
   开关打开后恢复成「添加即转存」；
2. 115 STRM 每条配置可以单独开定时巡检并设间隔，到点只跑增量，
   有别的配置在跑 / 手动全量在跑时让路；
3. 登录页背景（TMDB）可以在网页上配 key / 代理 / 自定义图片，保存即生效。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.config import Config
from app.db import Database
from app.ed2k import Ed2kManager, Ed2kNotReady
from app.scheduler import StrmConfigScheduler
from app.server import create_app
from app.strm import StrmStore
from app.tmdb import BackdropSource

HASH_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
HASH_B = "90f8e7d6c5b4a39281706f5e4d3c2b1a"


# --------------------------------------------------------------------- 基础件
def _db(tmp: Path) -> Database:
    return Database(tmp / "p115-strm.db")


def _store(tmp: Path) -> StrmStore:
    return StrmStore(tmp / "strm", base_url="http://127.0.0.1:9527", ed2k_dir="ed2k")


class _FakeApi:
    """只实现 ed2k 用到的 115 接口，调用都记下来。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def cloud_add(self, url: str, dir_cid: int, info_hash: str = "") -> dict:
        self.calls.append(("cloud_add", dir_cid, info_hash))
        return {"ok": True, "message": "", "errcode": 0}

    def cloud_task(self, key: str) -> dict:
        return {"state": False}

    def get_node(self, cid: int) -> dict | None:
        return {"id": cid, "is_dir": True, "name": "ed2k", "parent_id": 0}

    def find_child_dir(self, pid: int, name: str) -> int:
        return 0

    def mkdir(self, pid: int, name: str) -> int:
        return 4242

    def list_dir(self, cid: int, **kw) -> tuple[list, list]:
        return [], []

    def ensure_pickcode(self, node: dict) -> str:
        return "pc"

    @property
    def cloud_adds(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "cloud_add"]


def _ed2k_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        ed2k_enabled=True, ed2k_dir_cid=0, ed2k_dir_name="p115-strm-ed2k",
        ed2k_strm_dir="ed2k", ed2k_ttl_hours=24.0, ed2k_clean_interval=600.0,
        ed2k_poll_interval=20.0, ed2k_play_wait=0.2, ed2k_auto_transfer=False,
        scan_app="", scan_cooldown=0,
    )


def _mgr(tmp: Path) -> tuple[Ed2kManager, _FakeApi, Database, StrmStore]:
    db, store, api = _db(tmp), _store(tmp), _FakeApi()
    return Ed2kManager(_ed2k_cfg(), db, api, store), api, db, store


# --------------------------------------------------------------------- 1. ed2k
def test_add_only_writes_strm_when_lazy(tmp_path):
    """懒转存：添加时一次都不碰 115，strm 立刻能看见。"""
    mgr, api, db, _ = _mgr(tmp_path)
    rec = mgr.add(f"ed2k://|file|测试影片.mkv|123|{HASH_A}|/")

    assert api.cloud_adds == []                 # 没提交离线任务
    assert rec["status"] == "idle"              # 状态是「待播放转存」
    strm = Path(rec["strm_path"])
    assert strm.exists() and strm.suffix == ".strm"
    assert f"/p115/ed2k/{HASH_A}" in strm.read_text(encoding="utf-8")
    assert int(db.get_ed2k(HASH_A)["dir_cid"]) == 0   # 连目录都还没建


def test_play_submits_transfer_and_creates_dir(tmp_path):
    """播放时才提交转存：目录到这一步才创建，等不到就报「正在转存」。"""
    mgr, api, db, _ = _mgr(tmp_path)
    mgr.add(f"ed2k://|file|测试影片.mkv|123|{HASH_A}|/")

    try:
        mgr.playable(HASH_A, wait=0.2)
    except Ed2kNotReady as exc:
        assert "转存" in str(exc) or "排队" in str(exc)
    else:  # pragma: no cover - 走到这里说明假 API 被当成真的转存成功
        raise AssertionError("文件没到位时不该返回可播放")

    assert len(api.cloud_adds) == 1
    assert int(db.get_ed2k(HASH_A)["dir_cid"]) == 4242


def test_switch_on_transfers_immediately(tmp_path):
    """打开「添加后立即转存」后恢复老行为：添加时就提交。"""
    mgr, api, _, _ = _mgr(tmp_path)
    mgr.save_settings(auto_transfer=True)
    assert mgr.settings()["auto_transfer"] is True

    rec = mgr.add(f"ed2k://|file|第二部.mp4|456|{HASH_B}|/")
    assert len(api.cloud_adds) == 1
    assert rec["status"] in ("pending", "ready")


def test_restore_strm_covers_lazy_records(tmp_path):
    """strm 卷被清空后，从没播放过的懒转存记录也要补回来。"""
    mgr = _mgr(tmp_path)[0]
    rec = mgr.add(f"ed2k://|file|测试影片.mkv|123|{HASH_A}|/")
    Path(rec["strm_path"]).unlink()

    assert mgr.restore_strm() >= 1
    assert Path(rec["strm_path"]).exists()


def test_concurrent_play_submits_once(tmp_path):
    """播放器同时开几个连接：只提交一次转存（重复提交会被 115 删-建，打断正在跑的转存）。"""
    import threading

    mgr, api, _, _ = _mgr(tmp_path)
    mgr.add(f"ed2k://|file|测试影片.mkv|123|{HASH_A}|/")

    def worker():
        try:
            mgr.playable(HASH_A, wait=0.2)
        except Ed2kNotReady:
            pass

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(api.cloud_adds) == 1


# --------------------------------------------------------------------- 2. 定时巡检
class _FakeConfigDb:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def list_strm_configs(self):
        return [dict(r) for r in self.rows]


def test_strm_config_keeps_schedule_columns(tmp_path):
    """新列与 path_dirs 都要真的落库（path_dirs 以前被 upsert 静默丢了）。"""
    db = _db(tmp_path)
    db.upsert_strm_config({
        "id": "cfg_1", "name": "电影", "remote_cid": "1",
        "remote_cids": "1,2", "remote_paths": "/a,/b",
        "local_dir": "/strm/a",
        "path_dirs": json.dumps([{"cid": "1", "path": "/a", "dir": "/strm/a"}]),
        "auto_scan": 1, "scan_interval": 900,
    })
    row = db.get_strm_config("cfg_1")
    assert int(row["auto_scan"]) == 1
    assert int(row["scan_interval"]) == 900
    assert "/strm/a" in row["path_dirs"]


def test_scheduler_fires_incremental_on_interval():
    fired: list[tuple[str, str]] = []
    rows = [{"id": "cfg_1", "name": "电影", "auto_scan": 1,
             "scan_interval": 900, "last_run": 0, "status": "idle"}]
    db = _FakeConfigDb(rows)
    sch = StrmConfigScheduler(db, lambda cid, mode: fired.append((cid, mode)))

    now = time.time()
    assert sch.run_pending(now=now) == 1
    assert fired == [("cfg_1", "inc")]          # 只跑增量，不碰全量
    assert sch.run_pending(now=now + 60) == 0   # 间隔没到
    assert sch.run_pending(now=now + 901) == 1

    rows[0]["status"] = "running"
    assert sch.run_pending(now=now + 4000) == 0  # 同一配置在跑：让路
    rows[0]["status"] = "idle"
    rows[0]["auto_scan"] = 0
    assert sch.run_pending(now=now + 9000) == 0  # 开关关掉：不再跑


def test_scheduler_yields_to_manual_scan():
    db = _FakeConfigDb([{"id": "cfg_1", "auto_scan": 1, "scan_interval": 60,
                         "last_run": 0, "status": "idle"}])
    busy = {"on": True}
    sch = StrmConfigScheduler(db, lambda cid, mode: None, busy=lambda: busy["on"])
    assert sch.run_pending() == 0
    busy["on"] = False
    assert sch.run_pending() == 1


# --------------------------------------------------------------------- 3. 登录页背景
def test_backdrop_configure_takes_effect(monkeypatch):
    for name in ("TMDB_API_KEY", "TMDB_READ_TOKEN", "TMDB_PROXY", "LOGIN_BACKDROPS"):
        monkeypatch.delenv(name, raising=False)
    src = BackdropSource()
    assert src.available is False

    src.configure(api_key="k", proxy="http://127.0.0.1:7890",
                  custom=["https://image.tmdb.org/t/p/original/a.jpg"])
    assert src.available is True
    assert src.status()["tmdb"] is True and src.status()["proxy"] is True

    src.configure(api_key="", proxy="", custom=[])
    assert src.available is False               # 空值 = 清掉，不是保持原样


def test_backdrop_settings_endpoint(tmp_path):
    cfg = Config()
    cfg.strm_root = tmp_path / "strm"
    cfg.data_dir = tmp_path / "config"
    cfg.web_username, cfg.web_password = "", ""   # 关掉登录，测试里方便直接调
    db = _db(tmp_path)
    app = create_app(cfg, db, SimpleNamespace(user_id=lambda: "1"), _store(tmp_path),
                     SimpleNamespace(invalidate_roots=lambda: None),
                     SimpleNamespace(), {}, None)
    client = TestClient(app)

    assert client.get("/api/login/backdrop").json()["reason"] == "no_tmdb_key"

    resp = client.put("/api/login/backdrop/settings", json={
        "api_key": "abc", "proxy": "http://127.0.0.1:1080",
        "custom": "https://image.tmdb.org/t/p/original/x.jpg",
    })
    assert resp.status_code == 200
    assert resp.json()["status"]["available"] is True
    assert json.loads(db.get_meta("login_backdrop"))["api_key"] == "abc"

    got = client.get("/api/login/backdrop/settings").json()
    assert got["settings"]["api_key"] == "abc"
    assert got["settings"]["custom"] == ["https://image.tmdb.org/t/p/original/x.jpg"]
