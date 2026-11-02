"""Emby 播放改写里的「秒传预解析」回归测试。

线上现象（日志）::

    预解析 115 直链失败（回退本服务地址）: http://172.17.0.1:9527/p115/instant/...
        : 文件还没有秒传到账号「不吃香菜」

原因：预解析发生在播放器拿到地址**之前**，这时文件往往只在 ``instant`` 表里
登记过（有 sha1/size/source_pickcode），还没有目标账号的 pickcode。
``direct_url`` 对这种情况只会抛异常，于是回退成「本服务地址」——
而那个地址是容器内网/宿主机 IP，播放器（电视/手机）根本连不上，
表现就是秒传不生效、一直转圈。

修法：预解析先用 ``ensure_direct`` —— 没秒传就先秒传一次，再取直链。
这里不联网，全是假对象。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app.db import Database  # noqa: E402
from app.emby import EmbyProxy  # noqa: E402
from app.instant import InstantError, InstantManager  # noqa: E402

SHA1 = "05CF2AC84EA74EC3C9C5152F3C6247F9772AD4FF"
OUR_URL = (f"http://172.17.0.1:9527/p115/instant/{SHA1}.mkv"
           "?account=%E4%B8%8D%E5%90%83%E9%A6%99%E8%8F%9C&size=1091514942")


def _db() -> Database:
    tmp = tempfile.mkdtemp(prefix="p115-preresolve-")
    return Database(str(Path(tmp) / "t.db"))


def _proxy(db: Database, instant: object, preresolve: bool = True) -> EmbyProxy:
    cfg = SimpleNamespace(
        emby_enabled=True, emby_url="http://127.0.0.1:8096", emby_strm_prefix="",
        emby_path_rules=(), play_cache_seconds=600, emby_proxy_port=0,
        public_base_url="http://192.168.2.124:9527",
        instant_preresolve=preresolve, instant_dir_name="p115-strm-instant",
    )
    store = SimpleNamespace(base_url="http://192.168.2.124:9527", route_prefix="")
    return EmbyProxy(cfg, db, api=None, store=store, instant=instant)


def test_ensure_direct_resolves_when_not_yet_transferred():
    """只有登记、没有 pickcode：ensure_direct 要先秒传再取链。"""
    db = _db()
    cfg = SimpleNamespace(api_qps=0.0, instant_dir_name="p115-strm-instant",
                          play_user_agent="UA", instant_cache_seconds=600)
    mgr = InstantManager(cfg, db, api=None)
    db.upsert_instant({
        "sha1": SHA1, "account": "不吃香菜", "name": "The.Boys.S01E04.mkv",
        "size": 1091514942, "source_pickcode": "dh3vqi5swxlf6xxtf",
        "source_account": "33t",
    })

    calls: list[tuple] = []

    def fake_resolve(sha1, size, name, account, source_pickcode="", source_account=""):
        calls.append((sha1, size, name, account, source_pickcode, source_account))
        db.upsert_instant({
            "sha1": sha1, "account": account, "name": name, "size": size,
            "source_pickcode": source_pickcode, "source_account": source_account,
            "pickcode": "cs0l7fdfv0k8idblv", "file_id": 1,
        })
        return db.get_instant(sha1, account)

    mgr.resolve = fake_resolve  # type: ignore[assignment]
    mgr.direct_url = lambda sha1, account, user_agent="": (  # type: ignore[assignment]
        f"https://cdn.example/{sha1}.mkv", {}
    )

    url, _ = mgr.ensure_direct(SHA1, "不吃香菜", user_agent="Infuse")
    assert url.endswith(f"{SHA1}.mkv")
    assert calls and calls[0][3] == "不吃香菜"
    assert calls[0][4] == "dh3vqi5swxlf6xxtf"   # 二次校验要用源 pickcode
    assert calls[0][5] == "33t"                  # 源账号也要带上


def test_ensure_direct_skips_resolve_when_already_transferred():
    """已经有 pickcode：不要再秒传第二次。"""
    db = _db()
    cfg = SimpleNamespace(api_qps=0.0, instant_dir_name="p115-strm-instant",
                          play_user_agent="UA", instant_cache_seconds=600)
    mgr = InstantManager(cfg, db, api=None)
    db.upsert_instant({
        "sha1": SHA1, "account": "不吃香菜", "name": "x.mkv", "size": 10,
        "source_pickcode": "pc", "pickcode": "done", "file_id": 1,
    })
    mgr.resolve = lambda *a, **k: pytest.fail("不该再秒传")  # type: ignore[assignment]
    mgr.direct_url = lambda sha1, account, user_agent="": ("https://cdn/x", {})  # type: ignore[assignment]
    assert mgr.ensure_direct(SHA1, "不吃香菜")[0] == "https://cdn/x"


def test_ensure_direct_without_record_raises():
    mgr = InstantManager(
        SimpleNamespace(api_qps=0.0, instant_dir_name="d", play_user_agent="UA",
                        instant_cache_seconds=600),
        _db(), api=None,
    )
    with pytest.raises(InstantError):
        mgr.ensure_direct(SHA1, "不吃香菜")


def test_pre_resolve_returns_cdn_link_instead_of_local_address():
    """预解析要拿 ensure_direct 的结果，绝不能把本服务地址发给播放器。"""
    db = _db()
    used: list[str] = []

    class FakeInstant:
        def ensure_direct(self, sha1, account, user_agent=""):
            used.append(f"{sha1}|{account}|{user_agent}")
            return "https://cdnfhnfile.115cdn.net/x/The.Boys.S01E04.mkv?f=1", {}

        def direct_url(self, sha1, account, user_agent=""):
            raise InstantError(f"文件还没有秒传到账号「{account}」")

    proxy = _proxy(db, FakeInstant())
    url = proxy._direct_uncached(OUR_URL, "Infuse/7.0")
    assert url.startswith("https://cdnfhnfile.115cdn.net/")
    assert used == [f"{SHA1}|不吃香菜|Infuse/7.0"]


def test_pre_resolve_falls_back_when_transfer_fails():
    """秒传本身失败（文件不在 115 资源库等）：回退，不要抛给 Emby。"""

    class FailingInstant:
        def ensure_direct(self, sha1, account, user_agent=""):
            raise InstantError("秒传失败（statuscode=1）")

        def direct_url(self, sha1, account, user_agent=""):
            raise InstantError("还没有秒传")

    proxy = _proxy(_db(), FailingInstant())
    assert proxy._direct_uncached(OUR_URL, "Infuse/7.0") is None


def test_pre_resolve_can_be_switched_off():
    """INSTANT_PRERESOLVE=0：仍然只用 direct_url（旧行为，给不想阻塞的场景留口）。"""
    seen: list[str] = []

    class FakeInstant:
        def ensure_direct(self, sha1, account, user_agent=""):
            pytest.fail("关掉预解析后不该调用 ensure_direct")

        def direct_url(self, sha1, account, user_agent=""):
            seen.append(account)
            return "https://cdn/old", {}

    proxy = _proxy(_db(), FakeInstant(), preresolve=False)
    assert proxy._direct_uncached(OUR_URL, "Infuse/7.0") == "https://cdn/old"
    assert seen == ["不吃香菜"]


def test_pre_resolve_times_out_without_blocking_playback():
    """秒传卡住时：按超时回退，别把 Emby 的 PlaybackInfo 一起拖死。"""
    import time as _time

    class HangingInstant:
        def ensure_direct(self, sha1, account, user_agent=""):
            _time.sleep(5)
            return "https://cdn/late", {}

    proxy = _proxy(_db(), HangingInstant())
    proxy.cfg.instant_preresolve_timeout = 0.3
    started = _time.time()
    assert proxy._direct_uncached(OUR_URL, "Infuse/7.0") is None
    assert _time.time() - started < 3

