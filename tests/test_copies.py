"""账号级同播复制（app/copies.py）。

要保证的几件事：

1. 配置跟账号走（存在这个账号自己的库里），不是全局、也不是 ed2k 专属；
2. 同一台设备重复播放不复制；第二台设备才开始复制，各用一条直链；
3. 副本可复用、受上限约束、过期会被清掉（连 115 上的文件一起）；
4. 副本放专用目录，扫描器会跳过它，不会把副本再索引成新片；
5. strm 播放和 ed2k 播放都走同一套判断。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.copies import COPY_DIR_NAME, CopyManager
from app.db import Database
from app.ed2k import Ed2kManager
from app.strm import StrmStore

HASH_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
COPY_DIR = 7777


class _FakeApi:
    """够用的假 115：根目录下找目录、建目录、复制文件、删文件。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.files: dict[int, dict] = {}
        self.dirs: dict[int, str] = {}
        self._next_id = 9000
        self._next_pc = 0

    def list_dir(self, cid: int, **kw) -> tuple[list, list]:
        dirs = [{"id": i, "name": n, "is_dir": True, "parent_id": 0}
                for i, n in self.dirs.items()]
        files = [n for n in self.files.values() if n["parent_id"] == cid]
        return dirs, files

    def find_child_dir(self, parent_id: int, name: str) -> int | None:
        for cid, dir_name in self.dirs.items():
            if dir_name == name:
                return cid
        return None

    def mkdir(self, parent_id: int, name: str) -> int:
        self.dirs[COPY_DIR] = name
        self.calls.append(("mkdir", name))
        return COPY_DIR

    def copy_file(self, file_id: int, parent_id: int,
                  cooldown: float = 0.0) -> dict | None:
        src = self.files[int(file_id)]
        self._next_id += 1
        self._next_pc += 1
        node = {
            "id": self._next_id,
            "name": f"{src['name']}({self._next_pc})",
            "pickcode": f"pc-copy{self._next_pc}",
            "size": src["size"],
            "is_dir": False,
            "parent_id": parent_id,
        }
        self.files[node["id"]] = node
        self.calls.append(("copy_file", int(file_id), parent_id))
        return {"file_id": node["id"], "pickcode": node["pickcode"],
                "name": node["name"], "size": node["size"]}

    def delete_files(self, fids) -> bool:
        ids = [int(f) for f in fids]
        self.calls.append(("delete_files", ids))
        for fid in ids:
            self.files.pop(fid, None)
        return True

    @property
    def copies(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "copy_file"]

    @property
    def deleted(self) -> list[list[int]]:
        return [c[1] for c in self.calls if c[0] == "delete_files"]


def _mgr(tmp: Path) -> tuple[CopyManager, _FakeApi, Database]:
    db = Database(tmp / "p115-strm.db")
    api = _FakeApi()
    return CopyManager(db, api, SimpleNamespace(scan_cooldown=0)), api, db


def _seed_file(db: Database, api: _FakeApi | None = None,
               file_id: int = 1001, pickcode: str = "pc-main",
               name: str = "片.mkv", size: int = 123) -> None:
    """把一个文件写进索引：真实场景里 pickcode → file_id 就是靠它反查的；
    同时放进假 115 的目录里，复制时才有源文件可读。"""
    db.upsert_file({
        "id": file_id, "parent_id": 1, "name": name, "path": "/" + name,
        "pickcode": pickcode, "size": size,
    })
    if api is not None:
        api.files[file_id] = {"id": file_id, "name": name, "pickcode": pickcode,
                              "size": size, "is_dir": False, "parent_id": 1}


# ------------------------------------------------------------------ 配置
def test_settings_live_in_the_account_db(tmp_path):
    mgr, _, db = _mgr(tmp_path)

    # 默认开启，参数是 30 分钟 / 3 份
    s = mgr.settings()
    assert s["enabled"] is True
    assert s["ttl"] == 1800 and s["ttl_minutes"] == 30
    assert s["max"] == 3

    saved = mgr.save_settings(enabled=False, ttl=600, max_copies=5)
    assert saved["enabled"] is False and saved["ttl"] == 600 and saved["max"] == 5
    # 存在这个账号自己的库里，不是全局
    assert db.get_meta("copy_enabled") == "0"

    # 下限保护：TTL 不得小于 5 分钟，副本数 1..10
    low = mgr.save_settings(ttl=1, max_copies=0)
    assert low["ttl"] == 300 and low["max"] == 1
    high = mgr.save_settings(max_copies=99)
    assert high["max"] == 10


def test_settings_survive_a_new_manager(tmp_path):
    mgr, api, db = _mgr(tmp_path)
    mgr.save_settings(enabled=False, ttl=600, max_copies=2)

    fresh = CopyManager(db, api, SimpleNamespace(scan_cooldown=0))
    assert fresh.settings()["enabled"] is False
    assert fresh.settings()["ttl"] == 600


# ------------------------------------------------------------------ 播放分配
def test_same_device_plays_twice_without_copy(tmp_path):
    mgr, api, db = _mgr(tmp_path)
    _seed_file(db, api)

    assert mgr.pick("pc-main", "ip1|ua1") == "pc-main"
    assert mgr.pick("pc-main", "ip1|ua1") == "pc-main"
    assert api.copies == []


def test_second_device_gets_a_copy(tmp_path):
    mgr, api, db = _mgr(tmp_path)
    _seed_file(db, api)

    first = mgr.pick("pc-main", "192.168.2.10|Infuse")
    second = mgr.pick("pc-main", "192.168.2.11|Infuse")

    assert first == "pc-main"
    assert second == "pc-copy1" and second != first
    assert len(api.copies) == 1
    assert len(db.list_play_copies("pc:pc-main")) == 1
    # 同一台设备再来一次，仍用自己那份副本，不会又复制
    assert mgr.pick("pc-main", "192.168.2.11|Infuse") == "pc-copy1"
    assert len(api.copies) == 1


def test_copy_is_reused_by_a_third_device(tmp_path):
    mgr, api, db = _mgr(tmp_path)
    _seed_file(db, api)
    mgr.pick("pc-main", "ip1|ua")
    mgr.pick("pc-main", "ip2|ua")

    # 第二台设备走了（会话过期）、第一台还在放：第三台复用那份副本
    with mgr._guard:
        mgr._sessions["pc:pc-main"]["ip2|ua"]["at"] -= 10_000
    third = mgr.pick("pc-main", "ip3|ua")

    assert third == "pc-copy1"
    assert len(api.copies) == 1


def test_copy_max_falls_back_to_main_file(tmp_path):
    mgr, api, db = _mgr(tmp_path)
    _seed_file(db, api)
    mgr.save_settings(max_copies=1)

    mgr.pick("pc-main", "ip1|ua")
    mgr.pick("pc-main", "ip2|ua")
    third = mgr.pick("pc-main", "ip3|ua")

    assert third == "pc-main"          # 到上限了，退回主文件
    assert len(api.copies) == 1


def test_disabled_never_copies(tmp_path):
    mgr, api, db = _mgr(tmp_path)
    _seed_file(db, api)
    mgr.save_settings(enabled=False)

    mgr.pick("pc-main", "ip1|ua")
    assert mgr.pick("pc-main", "ip2|ua") == "pc-main"
    assert api.copies == []


def test_unknown_pickcode_is_left_alone(tmp_path):
    mgr, api, _ = _mgr(tmp_path)

    # 索引里查不到 file_id：没法复制，原样返回，不能报错
    assert mgr.pick("pc-missing", "ip1|ua") == "pc-missing"
    assert mgr.pick("pc-missing", "ip2|ua") == "pc-missing"
    assert api.copies == []


def test_copy_failure_falls_back(tmp_path):
    mgr, api, db = _mgr(tmp_path)
    _seed_file(db, api)

    def boom(*a, **kw):
        raise RuntimeError("115 抽风")

    api.copy_file = boom
    mgr.pick("pc-main", "ip1|ua")
    assert mgr.pick("pc-main", "ip2|ua") == "pc-main"


# ------------------------------------------------------------------ 副本目录
def test_copies_go_to_a_dedicated_dir(tmp_path):
    mgr, api, db = _mgr(tmp_path)
    _seed_file(db, api)

    mgr.pick("pc-main", "ip1|ua")
    mgr.pick("pc-main", "ip2|ua")

    # 第一次复制时建目录，之后就复用同一个 cid
    assert api.dirs[COPY_DIR] == COPY_DIR_NAME
    assert len([c for c in api.calls if c[0] == "mkdir"]) == 1
    assert all(c[2] == COPY_DIR for c in api.copies)
    assert db.get_meta_int("copy_dir_cid") == COPY_DIR


def test_scanner_skips_the_copy_dir(tmp_path):
    from app.scanner import Scanner

    _, _, db = _mgr(tmp_path)
    db.set_meta("copy_dir_cid", "7777")
    cfg = SimpleNamespace(ed2k_dir_cid=0)
    scanner = Scanner(cfg, db, None, None)
    assert 7777 in scanner.ed2k_dirs()


# ------------------------------------------------------------------ 清理
def test_expired_copies_are_deleted(tmp_path):
    mgr, api, db = _mgr(tmp_path)
    _seed_file(db, api)
    mgr.pick("pc-main", "ip1|ua")
    mgr.pick("pc-main", "ip2|ua")
    copy_id = db.list_play_copies("pc:pc-main")[0]["id"]

    # 没过期：不删
    assert mgr.clean()["removed"] == 0

    # 会话过期 + 上次使用时间推远：连 115 文件一起删
    with mgr._guard:
        for state in mgr._sessions["pc:pc-main"].values():
            state["at"] -= 10_000
    db.update_play_copy(copy_id, last_used_at=int(time.time()) - 4000)
    result = mgr.clean()

    assert result["removed"] == 1
    assert db.list_play_copies("pc:pc-main") == []
    assert sum(len(ids) for ids in api.deleted) == 1


def test_force_clean_ignores_ttl_but_spares_live_copies(tmp_path):
    mgr, api, db = _mgr(tmp_path)
    _seed_file(db, api)
    mgr.pick("pc-main", "ip1|ua")
    mgr.pick("pc-main", "ip2|ua")

    # 还在播：既没过期也不该被 force 抢走（删了那台设备下次 seek 会 404）
    assert mgr.clean()["removed"] == 0
    assert mgr.clean(force=True)["removed"] == 0

    # 设备走了之后，force 可以跳过 TTL 直接清
    with mgr._guard:
        for state in mgr._sessions["pc:pc-main"].values():
            state["at"] -= 10_000
    assert mgr.clean()["removed"] == 0          # 没到 TTL
    assert mgr.clean(force=True)["removed"] == 1
    assert db.list_play_copies() == []


# ------------------------------------------------------------------ ed2k 链路
class _Ed2kApi(_FakeApi):
    def cloud_add(self, url: str, dir_cid: int, info_hash: str = "") -> dict:
        return {"ok": True, "message": "", "errcode": 0}

    def cloud_task(self, key: str) -> dict:
        return {"state": False}

    def get_node(self, node_id: int) -> dict | None:
        return self.files.get(int(node_id))


def test_ed2k_playback_uses_the_same_manager(tmp_path):
    db = Database(tmp_path / "p115-strm.db")
    api = _Ed2kApi()
    store = StrmStore(tmp_path / "strm", base_url="http://127.0.0.1:9527",
                      ed2k_dir="ed2k")
    cfg = SimpleNamespace(
        ed2k_enabled=True, ed2k_dir_cid=0, ed2k_dir_name="p115-strm-ed2k",
        ed2k_strm_dir="ed2k", ed2k_ttl_hours=24.0, ed2k_clean_interval=600.0,
        ed2k_poll_interval=20.0, ed2k_play_wait=0.2, ed2k_auto_transfer=False,
        scan_app="", scan_cooldown=0,
    )
    mgr = Ed2kManager(cfg, db, api, store)
    copies = CopyManager(db, api, cfg)
    mgr.copies = copies

    mgr.add(f"ed2k://|file|片.mkv|123|{HASH_A}|/")
    api.files[1001] = {"id": 1001, "name": "片.mkv", "pickcode": "pc-main",
                       "size": 123, "is_dir": False, "parent_id": 4242}
    db.update_ed2k(HASH_A, file_id=1001, pickcode="pc-main", status="ready",
                   dir_cid=4242, message="")

    first = copies.pick(mgr.playable(HASH_A, wait=0), "ip1|ua",
                        file_id=1001, name="片.mkv")
    second = copies.pick(mgr.playable(HASH_A, wait=0), "ip2|ua",
                         file_id=1001, name="片.mkv")

    assert first == "pc-main" and second == "pc-copy1"

    # 删除任务时连带删掉副本记录
    mgr.delete(HASH_A)
    assert db.list_play_copies("pc:pc-main") == []


# ------------------------------------------------------------------ 接口层
def test_account_copies_endpoints(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Config
    from app.server import create_app

    cfg = Config()
    cfg.strm_root = tmp_path / "strm"
    cfg.data_dir = tmp_path / "config"
    cfg.web_username, cfg.web_password = "", ""
    db, api = Database(tmp_path / "p115-strm.db"), _FakeApi()
    store = StrmStore(cfg.strm_root, base_url="http://127.0.0.1:9527")
    cm = CopyManager(db, api, SimpleNamespace(scan_cooldown=0))
    app = create_app(cfg, db, SimpleNamespace(user_id=lambda: "1"), store,
                     SimpleNamespace(invalidate_roots=lambda: None),
                     SimpleNamespace(), {}, None, copies=cm)
    client = TestClient(app)

    got = client.get("/api/accounts/main/copies")       # main = 主账号（内部名空串）
    assert got.status_code == 200
    assert got.json()["settings"]["enabled"] is True

    saved = client.post("/api/accounts/main/copies"
                        "?enabled=0&ttl_minutes=20&max_copies=2")
    assert saved.status_code == 200
    settings = saved.json()["settings"]
    assert settings["enabled"] is False
    assert settings["ttl"] == 1200 and settings["max"] == 2

    cleaned = client.post("/api/accounts/main/copies/clean")
    assert cleaned.status_code == 200 and cleaned.json()["removed"] == 0
