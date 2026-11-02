"""联动删除（本地 strm ⇄ 网盘）与刮削文件清理的回归测试。

这几条是新增功能，锁住几个「删错了就回不来」的关键点：

* 刮削清理只删同名的 nfo / 海报 / 字幕，目录级 poster.jpg、别人的文件、
  ``影片.2023.nfo`` 这类都不许碰；
* 本地 strm 消失后要等宽限期才删网盘，且一轮缺失过多（挂载掉线）时熔断；
* ``dry_run`` 只记录不动手；``STRM_ROOT`` 不可用直接跳过。
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database
from app.linker import DEFAULTS, LinkManager, link_settings
from app.strm import StrmStore


class FakeAPI:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    def delete_files(self, fids) -> bool:
        self.deleted.extend(int(f) for f in fids)
        return True


def _db() -> Database:
    tmp = tempfile.mkdtemp(prefix="panshare-link-db-")
    return Database(str(Path(tmp) / "t.db"))


def _store(root: str | None = None) -> StrmStore:
    root = root or tempfile.mkdtemp(prefix="panshare-link-strm-")
    return StrmStore(Path(root), base_url="http://127.0.0.1:9527")


def _engine(db: Database, api: FakeAPI, store: StrmStore, name: str = ""):
    return SimpleNamespace(name=name, display=name or "主账号", db=db, api=api,
                           store=store)


def _touch(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ 刮削清理
def test_remove_scraped_deletes_same_name_only():
    store = _store()
    base = Path(store.root)
    strm = _touch(base / "影片" / "影片.strm", "http://x")
    keep_self = _touch(base / "影片" / "影片.nfo")
    keep_poster = _touch(base / "影片" / "影片-poster.jpg")
    keep_sub = _touch(base / "影片" / "影片.zh.srt")
    # 这些都不该删
    dir_poster = _touch(base / "影片" / "poster.jpg")
    other = _touch(base / "影片" / "另一部.nfo")
    year = _touch(base / "影片" / "影片.2023.nfo")

    removed = store.remove_scraped(str(strm))
    assert set(removed) == {str(keep_self), str(keep_poster), str(keep_sub)}
    assert not keep_self.exists() and not keep_poster.exists() and not keep_sub.exists()
    assert dir_poster.exists() and other.exists() and year.exists()
    # strm 本身不归它管
    assert strm.exists()


def test_remove_scraped_handles_keep_ext_naming():
    """`a.mkv.strm` 这种保后缀命名，刮削文件叫 `a.nfo`。"""
    store = _store()
    base = Path(store.root)
    strm = _touch(base / "a.mkv.strm", "http://x")
    nfo = _touch(base / "a.nfo")
    poster = _touch(base / "a-fanart.jpg")
    assert set(store.remove_scraped(str(strm))) == {str(nfo), str(poster)}
    assert not nfo.exists() and not poster.exists()


def test_remove_scraped_ignores_outside_root():
    store = _store()
    outside = Path(tempfile.mkdtemp(prefix="panshare-outside-"))
    strm = _touch(outside / "x.strm", "http://x")
    nfo = _touch(outside / "x.nfo")
    assert store.remove_scraped(str(strm)) == []
    assert nfo.exists()


# ------------------------------------------------------------------ 联动设置
def test_link_settings_defaults_and_roundtrip():
    db = _db()
    assert link_settings(db) == DEFAULTS
    mgr = LinkManager(SimpleNamespace(strm_root=tempfile.mkdtemp()), db,
                      engines=list)
    saved = mgr.save_settings({"local_to_remote": True, "grace_seconds": 10,
                               "max_ratio": 0.5})
    assert saved["local_to_remote"] is True
    assert saved["grace_seconds"] == 10
    assert link_settings(db)["max_ratio"] == 0.5
    # 非法值要兜底，别让线程拿到负数间隔空转
    fixed = mgr.save_settings({"interval": 1, "grace_seconds": -5, "min_missing": 0})
    assert fixed["interval"] >= 30 and fixed["grace_seconds"] == 0
    assert fixed["min_missing"] >= 1


# ------------------------------------------------------------------ 删除判定
def _seed(db: Database, store: StrmStore, count: int = 1,
          missing: int = 0) -> tuple[list[int], list[str]]:
    ids: list[int] = []
    paths: list[str] = []
    base = Path(store.root)
    for i in range(count):
        strm = base / "d" / f"f{i}.strm"
        # 前 count - missing 个存在，最后 missing 个「被用户删掉」
        if i < count - missing:
            _touch(strm, "http://x")
        ids.append(1000 + i)
        paths.append(str(strm))
        db.upsert_file({
            "id": 1000 + i, "parent_id": 1, "name": f"f{i}.mkv",
            "path": f"d/f{i}.mkv", "pickcode": f"pc{i}", "size": 1,
            "strm_path": str(strm),
        })
    return ids, paths


def _manager(db, store, api, engines):
    cfg = SimpleNamespace(strm_root=str(store.root))
    mgr = LinkManager(cfg, db, engines=lambda: engines)
    mgr.save_settings({"local_to_remote": True, "grace_seconds": 0,
                       "min_missing": 5})
    return mgr


def test_scan_deletes_remote_after_grace():
    store, db, api = _store(), _db(), FakeAPI()
    ids, _ = _seed(db, store, count=3, missing=1)
    mgr = _manager(db, store, api, [_engine(db, api, store)])
    result = mgr.scan_once()
    assert result["deleted"] == 1
    assert api.deleted == [ids[2]]
    assert db.get_file(ids[2]) is None
    assert db.get_file(ids[0]) is not None


def test_scan_waits_for_grace_period():
    store, db, api = _store(), _db(), FakeAPI()
    _seed(db, store, count=2, missing=1)
    mgr = _manager(db, store, api, [_engine(db, api, store)])
    mgr.save_settings({"grace_seconds": 600})
    assert mgr.scan_once()["deleted"] == 0
    assert api.deleted == []
    # 宽限期内算「待删除」，能看到但不会动
    assert len(mgr.pending()) == 1


def test_scan_circuit_breaker_on_missing_root():
    store, db, api = _store(), _db(), FakeAPI()
    _seed(db, store, count=3, missing=3)
    mgr = _manager(db, store, api, [_engine(db, api, store)])
    mgr.save_settings({"min_missing": 1, "max_ratio": 0.2})
    # 整个目录被挪走：缺失 3/3 = 100%，超过 20% 阈值 → 熔断
    result = mgr.scan_once()
    assert result["deleted"] == 0
    assert "阈值" in result["aborted"]
    assert api.deleted == []


def test_scan_skips_when_strm_root_missing():
    store, db, api = _store(), _db(), FakeAPI()
    _seed(db, store, count=2, missing=2)
    gone = SimpleNamespace(strm_root=str(Path(store.root) / "not-mounted"))
    mgr = LinkManager(gone, db, engines=lambda: [_engine(db, api, store)])
    mgr.save_settings({"local_to_remote": True, "grace_seconds": 0})
    result = mgr.scan_once()
    assert result["deleted"] == 0
    assert "STRM_ROOT" in result["aborted"]
    assert api.deleted == []


def test_scan_dry_run_only_lists():
    store, db, api = _store(), _db(), FakeAPI()
    _seed(db, store, count=2, missing=1)
    mgr = _manager(db, store, api, [_engine(db, api, store)])
    mgr.save_settings({"dry_run": True})
    result = mgr.scan_once()
    assert result["deleted"] == 0 and result["dry_run"] is True
    assert api.deleted == []
    assert db.get_file(1001) is not None


def test_scan_recovers_when_strm_comes_back():
    store, db, api = _store(), _db(), FakeAPI()
    _ids, paths = _seed(db, store, count=2, missing=1)
    mgr = _manager(db, store, api, [_engine(db, api, store)])
    mgr.save_settings({"grace_seconds": 600})
    mgr.scan_once()
    assert len(mgr.pending()) == 1
    _touch(Path(paths[1]), "http://x")      # 用户又生成回来了
    mgr.scan_once()
    assert mgr.pending() == []
    assert api.deleted == []


def test_commit_deletes_without_waiting():
    store, db, api = _store(), _db(), FakeAPI()
    ids, _ = _seed(db, store, count=2, missing=1)
    mgr = _manager(db, store, api, [_engine(db, api, store)])
    mgr.save_settings({"grace_seconds": 600})
    mgr.scan_once()
    out = mgr.commit()
    assert out["deleted"] == 1
    assert api.deleted == [ids[1]]
    assert db.get_file(ids[1]) is None


def test_scan_ignores_files_without_strm():
    """索引里有、但从没写过 strm 的行（比如 pickcode 缺失）不该被当成「用户删了」。"""
    store, db, api = _store(), _db(), FakeAPI()
    db.upsert_file({"id": 1, "parent_id": 0, "name": "a.mkv", "path": "a.mkv",
                    "pickcode": "", "strm_path": ""})
    mgr = _manager(db, store, api, [_engine(db, api, store)])
    assert mgr.scan_once()["deleted"] == 0
    assert db.get_file(1) is not None


def test_last_result_cached():
    store, db, api = _store(), _db(), FakeAPI()
    _seed(db, store, count=1)
    mgr = _manager(db, store, api, [_engine(db, api, store)])
    mgr.scan_once()
    st = mgr.status()
    assert st["last"]["checked"] == 1
    assert st["settings"]["local_to_remote"] is True
    assert st["strm_root_ok"] is True
    assert time.time() - st["last"]["at"] < 5


def test_status_indexed_counts_strm_rows():
    """网页「索引内 strm」用的是库里的总数，跟本轮有没有跑过无关。"""
    store, db, api = _store(), _db(), FakeAPI()
    _seed(db, store, count=3)
    mgr = _manager(db, store, api, [_engine(db, api, store)])
    assert mgr.status()["indexed"] == 3      # 还没扫过就能显示
    mgr.scan_once()
    assert mgr.status()["indexed"] == 3
    db.upsert_file({"id": 2001, "parent_id": 1, "name": "no-strm.mkv",
                    "path": "d/no-strm.mkv", "pickcode": "x", "size": 1,
                    "strm_path": ""})
    assert mgr.status()["indexed"] == 3      # 没有 strm 的行不算
