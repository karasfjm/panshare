"""ed2k 的导入导出（换设备用）。

同播复制已经从 ed2k 上移到账号级，见 ``tests/test_copies.py``。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database
from app.ed2k import Ed2kManager
from app.strm import StrmStore

HASH_A = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
HASH_B = "90f8e7d6c5b4a39281706f5e4d3c2b1a"
CID = 4242


# --------------------------------------------------------------------- 基础件
def _db(tmp: Path) -> Database:
    return Database(tmp / "p115-strm.db")


def _store(tmp: Path) -> StrmStore:
    return StrmStore(tmp / "strm", base_url="http://127.0.0.1:9527", ed2k_dir="ed2k")


class _FakeApi:
    """带「文件系统」的假 115：支持列目录、取节点、复制、删除。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.files: dict[int, dict] = {}
        self._next_id = 9000
        self._next_pc = 0

    # -- 目录/文件 ------------------------------------------------------
    def get_node(self, node_id: int) -> dict | None:
        return self.files.get(int(node_id))

    def list_dir(self, cid: int, **kw) -> tuple[list, list]:
        return [], [n for n in self.files.values() if n["parent_id"] == cid]

    def ensure_pickcode(self, node: dict) -> str:
        return node.get("pickcode") or ""

    def copy_file(self, file_id: int, parent_id: int,
                  cooldown: float = 0.0) -> dict | None:
        src = self.files[int(file_id)]
        self._next_id += 1
        self._next_pc += 1
        stem, _, ext = src["name"].rpartition(".")
        node = {
            "id": self._next_id,
            "name": f"{stem}({self._next_pc}).{ext}",
            "pickcode": f"pc-copy{self._next_pc}",
            "size": src["size"],
            "is_dir": False,
            "parent_id": parent_id,
        }
        self.files[node["id"]] = node
        self.calls.append(("copy_file", int(file_id), parent_id))
        return {
            "file_id": node["id"], "pickcode": node["pickcode"],
            "name": node["name"], "size": node["size"],
        }

    def delete_files(self, fids) -> bool:
        ids = [int(f) for f in fids]
        self.calls.append(("delete_files", ids))
        for fid in ids:
            self.files.pop(fid, None)
        return True

    # -- 转存 ----------------------------------------------------------
    def cloud_add(self, url: str, dir_cid: int, info_hash: str = "") -> dict:
        self.calls.append(("cloud_add", dir_cid, info_hash))
        return {"ok": True, "message": "", "errcode": 0}

    def cloud_task(self, key: str) -> dict:
        return {"state": False}

    def find_child_dir(self, pid: int, name: str) -> int:
        return 0

    def mkdir(self, pid: int, name: str) -> int:
        return CID

    # -- 断言用 --------------------------------------------------------
    @property
    def copies(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "copy_file"]

    @property
    def deleted(self) -> list[list[int]]:
        return [c[1] for c in self.calls if c[0] == "delete_files"]


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        ed2k_enabled=True, ed2k_dir_cid=0, ed2k_dir_name="p115-strm-ed2k",
        ed2k_strm_dir="ed2k", ed2k_ttl_hours=24.0, ed2k_clean_interval=600.0,
        ed2k_poll_interval=20.0, ed2k_play_wait=0.2, ed2k_auto_transfer=False,
        scan_app="", scan_cooldown=0,
    )


def _mgr(tmp: Path) -> tuple[Ed2kManager, _FakeApi, Database]:
    db, store, api = _db(tmp), _store(tmp), _FakeApi()
    return Ed2kManager(_cfg(), db, api, store), api, db


def _make_ready(mgr: Ed2kManager, api: _FakeApi, key: str, name: str,
                file_id: int = 1001, pickcode: str = "pc-main") -> None:
    """把一条记录假装成「已经转存好」（file_id 指向转存目录里的真实节点）。"""
    api.files[file_id] = {
        "id": file_id, "name": name, "pickcode": pickcode, "size": 123,
        "is_dir": False, "parent_id": CID,
    }
    mgr.db.update_ed2k(key, file_id=file_id, pickcode=pickcode,
                       status="ready", dir_cid=CID, message="")


# ------------------------------------------------------------------- 导入导出
def test_export_contains_tasks_and_settings(tmp_path):
    mgr, api, _ = _mgr(tmp_path)
    mgr.add(f"ed2k://|file|甲.mkv|123|{HASH_A}|/")
    mgr.add(f"ed2k://|file|乙.mkv|456|{HASH_B}|/")
    _make_ready(mgr, api, HASH_A, "甲.mkv")

    data = mgr.export_data()

    assert data["kind"] == "ed2k" and data["version"] == 1
    assert {i["key"] for i in data["items"]} == {HASH_A, HASH_B}
    row = next(i for i in data["items"] if i["key"] == HASH_A)
    assert row["file_id"] == "1001"            # 19 位 id 走字符串，别丢精度
    assert row["pickcode"] == "pc-main"
    assert data["items"][0]["url"].startswith("ed2k://|file|")
    assert data["settings"]["strm_dir"] == "ed2k"

    # 不带文件 id 的导出（换 115 账号时用）
    lean = mgr.export_data(include_files=False)
    assert all("file_id" not in i for i in lean["items"])


def test_export_then_import_rebuilds_strm(tmp_path):
    src_dir = tmp_path / "old"
    src_dir.mkdir()
    old, api, _ = _mgr(src_dir)
    old.add(f"ed2k://|file|甲.mkv|123|{HASH_A}|/")
    old.add(f"ed2k://|file|乙.mkv|456|{HASH_B}|/")
    _make_ready(old, api, HASH_A, "甲.mkv")
    backup = old.export_data()

    new_dir = tmp_path / "new"
    new_dir.mkdir()
    mgr, _, db = _mgr(new_dir)          # 全新设备：库和 strm 目录都是空的
    result = mgr.import_data(backup, mode="merge")

    assert result["added"] == 2 and result["failed"] == 0
    assert result["strm"] == 2
    for key in (HASH_A, HASH_B):
        rec = db.get_ed2k(key)
        assert rec is not None
        assert rec["status"] == "idle"          # 导入后等播放时自校验
        assert Path(rec["strm_path"]).exists()
    # 文件 id 跟着导入（同账号换机器时第一次播放就能直接命中）
    assert int(db.get_ed2k(HASH_A)["file_id"]) == 1001


def test_import_merge_update_replace(tmp_path):
    mgr, _, db = _mgr(tmp_path)
    mgr.add(f"ed2k://|file|旧名字.mkv|123|{HASH_A}|/")
    payload = {
        "kind": "ed2k", "version": 1,
        "items": [
            {"key": HASH_A, "url": f"ed2k://|file|新名字.mkv|999|{HASH_A}|/",
             "name": "新名字.mkv", "size": 999},
            {"key": HASH_B, "url": f"ed2k://|file|乙.mkv|456|{HASH_B}|/",
             "name": "乙.mkv", "size": 456},
        ],
    }

    merge = mgr.import_data(payload, mode="merge")
    assert (merge["added"], merge["skipped"]) == (1, 1)
    assert db.get_ed2k(HASH_A)["name"] == "旧名字.mkv"      # merge 不覆盖

    update = mgr.import_data(payload, mode="update")
    assert update["updated"] == 2
    assert db.get_ed2k(HASH_A)["name"] == "新名字.mkv"

    replace = mgr.import_data(payload, mode="replace")
    assert replace["removed"] == 2 and replace["added"] == 2
    assert len(db.list_ed2k()) == 2


def test_import_skips_bad_items(tmp_path):
    mgr, _, db = _mgr(tmp_path)
    result = mgr.import_data({
        "items": [
            {"url": "https://example.com/not-ed2k"},
            {"url": f"ed2k://|file|好的.mkv|1|{HASH_A}|/"},
            "not-a-dict",
        ]
    })
    assert (result["added"], result["failed"], result["total"]) == (1, 2, 3)
    assert result["errors"] and "not-ed2k" not in result["errors"][0]
    assert len(db.list_ed2k()) == 1


def test_import_rejects_non_ed2k_file(tmp_path):
    import pytest

    from app.ed2k import Ed2kError

    mgr, _, _ = _mgr(tmp_path)
    with pytest.raises(Ed2kError):
        mgr.import_data({"hello": "world"})
    with pytest.raises(Ed2kError):
        mgr.import_data({"items": []}, mode="unknown")


# ----------------------------------------------------------------- 接口层
def test_export_import_endpoints(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Config
    from app.server import create_app

    cfg = Config()
    cfg.strm_root = tmp_path / "strm"
    cfg.data_dir = tmp_path / "config"
    cfg.web_username, cfg.web_password = "", ""      # 关掉登录，测试里直接调
    cfg.ed2k_enabled = True
    db, store, api = _db(tmp_path), _store(tmp_path), _FakeApi()
    mgr = Ed2kManager(cfg, db, api, store)
    app = create_app(cfg, db, SimpleNamespace(user_id=lambda: "1"), store,
                     SimpleNamespace(invalidate_roots=lambda: None),
                     SimpleNamespace(), {}, mgr)
    client = TestClient(app)
    mgr.add(f"ed2k://|file|甲.mkv|123|{HASH_A}|/")

    resp = client.get("/api/ed2k/export")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    backup = resp.json()
    assert backup["kind"] == "ed2k" and len(backup["items"]) == 1

    # 同一条再导一遍：merge 跳过，replace 先清后导
    merge = client.post("/api/ed2k/import?mode=merge", json=backup)
    assert merge.status_code == 200 and merge.json()["skipped"] == 1

    replace = client.post("/api/ed2k/import?mode=replace", json=backup)
    body = replace.json()
    assert body["removed"] == 1 and body["added"] == 1

    # 坏请求体 / 坏结构都要给 400，别 500
    bad = client.post("/api/ed2k/import", content=b"not-json",
                      headers={"Content-Type": "application/json"})
    assert bad.status_code == 400
    wrong = client.post("/api/ed2k/import", json={"hello": 1})
    assert wrong.status_code == 400 and "items" in wrong.json()["detail"]

    # ed2k 自己的设置（同播复制已挪到账号级，不在这条接口上）
    saved = client.post("/api/ed2k/settings?auto_transfer=1&ttl_hours=12")
    assert saved.status_code == 200
    settings = saved.json()["settings"]
    assert settings["auto_transfer"] is True and settings["ttl_hours"] == 12
