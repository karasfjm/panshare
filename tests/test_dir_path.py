"""分享转存 / ed2k 的「接收目录」回显回归测试。

用户反馈：在网页上选完网盘目录，输入框还是空的。原因是只存了 cid 和
目录名，没存完整路径，前端拿不到可显示的东西。这里锁住：

* 保存时带 ``dir_path`` 就存路径，``settings()["dir_path"]`` 原样返回；
* 不带路径的老调用退回目录名（不能让输入框空着）；
* 清空目录（cid=0）时路径也要一起清掉，免得显示一个已经不用的路径。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database
from app.ed2k import Ed2kManager
from app.share import ShareManager


class FakeAPI:
    """只实现保存设置用到的 get_node / 建目录要用的几个方法。"""

    def __init__(self, dirs: dict[int, str]) -> None:
        self.dirs = dirs

    def get_node(self, cid: int) -> dict | None:
        name = self.dirs.get(int(cid))
        if name is None:
            return None
        return {"id": str(cid), "name": name, "is_dir": True}


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        share_enabled=True, share_dir_cid=0, share_dir_name="", share_strm_dir="share",
        ed2k_enabled=True, ed2k_dir_cid=0, ed2k_dir_name="", ed2k_strm_dir="ed2k",
        ed2k_ttl_hours=24, ed2k_clean_interval=3600,
        strm_root=tempfile.mkdtemp(prefix="panshare-dirpath-"),
    )


def _db() -> Database:
    tmp = tempfile.mkdtemp(prefix="panshare-dirpath-db-")
    return Database(str(Path(tmp) / "t.db"))


def _store(cfg) -> SimpleNamespace:
    # 只需要能被 _sync_store_dir 写一个属性
    return SimpleNamespace(ed2k_dir="ed2k")


DIRS = {101: "国漫", 202: "电影"}


def test_share_dir_path_roundtrip():
    cfg, db = _cfg(), _db()
    share = ShareManager(cfg, db, FakeAPI(DIRS), _store(cfg))
    got = share.save_settings(101, dir_path="/影音/国漫")
    assert got["dir_path"] == "/影音/国漫"
    assert got["dir_name"] == "国漫"
    assert got["dir_cid"] == "101"
    # 重新读一次（模拟刷新页面）也要还在
    assert share.settings()["dir_path"] == "/影音/国漫"


def test_share_dir_path_falls_back_to_name():
    cfg, db = _cfg(), _db()
    share = ShareManager(cfg, db, FakeAPI(DIRS), _store(cfg))
    got = share.save_settings(101)          # 老前端不传路径
    assert got["dir_path"] == "国漫"        # 至少有个能显示的东西
    assert share.settings()["dir_name"] == "国漫"


def test_share_dir_path_cleared_with_cid_zero():
    cfg, db = _cfg(), _db()
    share = ShareManager(cfg, db, FakeAPI(DIRS), _store(cfg))
    share.save_settings(101, dir_path="/影音/国漫")
    got = share.save_settings(0)
    assert got["dir_path"] == "" and got["dir_cid"] == ""


def test_ed2k_dir_path_roundtrip():
    cfg, db = _cfg(), _db()
    ed2k = Ed2kManager(cfg, db, FakeAPI(DIRS), _store(cfg))
    got = ed2k.save_settings(202, dir_path="/影音/ed2k")
    assert got["dir_path"] == "/影音/ed2k"
    assert got["dir_name"] == "电影"
    assert ed2k.settings()["dir_path"] == "/影音/ed2k"


def test_ed2k_dir_path_falls_back_to_name():
    cfg, db = _cfg(), _db()
    ed2k = Ed2kManager(cfg, db, FakeAPI(DIRS), _store(cfg))
    assert ed2k.save_settings(202)["dir_path"] == "电影"
    assert ed2k.save_settings(0)["dir_path"] == ""
