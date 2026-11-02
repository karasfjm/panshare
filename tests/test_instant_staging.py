"""秒传暂存目录的回归测试。

起因（线上真实故障）：网页上给某个账号**手动设置**过暂存目录后，
第一次播放会 500 —— ``_staging()`` 走「按路径找目录」那条分支时往
``self._staging_cache`` 里写，而缓存是函数末尾才补建的，
于是抛 AttributeError。表现为「115 秒传不生效」。

这里不联网：用假的 115 客户端把三条分支（缓存命中 / 手动 cid / 手动路径 /
默认目录名）都走一遍，任何一条再出现 AttributeError 都会立刻失败。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database  # noqa: E402
from app.instant import InstantManager  # noqa: E402


class FakeClient:
    """够用的假客户端：目录 id 用固定值。"""

    _instant_account = "acc"

    def __init__(self):
        self.made: list[tuple[int, str]] = []
        self.existing = {"p115-strm-instant": 1000}

    def get_node(self, node_id):
        return {"id": int(node_id), "is_dir": True, "name": "dir"}

    def find_child_dir(self, parent_id, name):
        return 700 if name == "最近接收" else self.existing.get(name)

    def mkdir(self, parent_id, name):
        self.made.append((int(parent_id), name))
        cid = 2000 + len(self.made)
        self.existing[name] = cid
        return cid


def _manager(staging: dict | None = None) -> tuple[InstantManager, Database]:
    tmp = tempfile.mkdtemp(prefix="p115-instant-")
    db = Database(str(Path(tmp) / "t.db"))
    cfg = SimpleNamespace(api_qps=0.0, instant_dir_name="p115-strm-instant")
    mgr = InstantManager(cfg, db, api=None)
    mgr._clients["acc"] = FakeClient()
    if staging is not None:
        db.set_meta("instant_staging", json.dumps(staging, ensure_ascii=False))
    return mgr, db


def test_default_staging_dir():
    """没设置过暂存目录：按默认目录名找，第一次调用不报错。"""
    mgr, _ = _manager()
    assert mgr._staging("acc") == 1000


def test_staging_custom_path():
    """手动设置了路径（cid=0）：按名字找到目录，不能抛 AttributeError。"""
    mgr, _ = _manager({"acc": {"path": "/最近接收", "cid": 0}})
    assert mgr._staging("acc") == 700


def test_staging_custom_cid():
    """手动设置了 cid：直接用它，不能抛 AttributeError。"""
    mgr, _ = _manager({"acc": {"path": "", "cid": 555}})
    assert mgr._staging("acc") == 555


def test_staging_cache_reuse_and_reset():
    """第二次调用走缓存；改了设置要立刻失效。"""
    mgr, _ = _manager({"acc": {"path": "/最近接收", "cid": 0}})
    assert mgr._staging("acc") == 700
    assert mgr._staging_cache["acc"] == 700
    assert mgr._staging("acc") == 700
    mgr.set_staging_dir("acc", "", 0)
    assert "acc" not in mgr._staging_cache
    # 恢复默认目录：默认目录已存在则直接用，不存在则建
    assert mgr._staging("acc") == 1000
