"""首次运行没有 115 cookies 也要能起来。

以前 ``build()`` 在构造 115 客户端时直接 ``SystemExit``：没 cookies 连网页
都打不开，用户只能改 compose 再重启。现在的要求是：

1. 没有 cookies 时服务照常启动，网页能打开，状态里带 ``login.ok = false``；
2. 网页上粘贴 cookies 添加账号后立即生效（不用重启容器）；
3. 未登录期间扫描器跳过本轮，不要刷屏、也不要把索引清空（共用 STRM_ROOT
   时那会连带删掉别的账号的 strm）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config
from app.hub import AccountHub
from app.main import build
from app.p115api import P115API, P115Error


def _cfg(tmp_path: Path) -> Config:
    return Config(
        cookies="",
        cookie_file=tmp_path / "115-cookies.txt",
        data_dir=tmp_path,
        strm_root=tmp_path / "strm",
        mode="serve",
        emby_enabled=False,
        emby_url="",
    )


# ---------------------------------------------------------------- 客户端本身
def test_api_without_cookies_is_unauthenticated(tmp_path):
    api = P115API(cookie_file=tmp_path / "115-cookies.txt")
    assert api.authenticated is False
    # 没有 cookies 时要给出「去哪儿填」的提示，而不是一个 401 的原始报错
    with pytest.raises(P115Error) as err:
        api.check_login()
    assert "115 助手" in str(err.value)
    assert api.user_id() == ""


def test_api_reads_empty_cookie_file(tmp_path):
    path = tmp_path / "115-cookies.txt"
    path.write_text("\n", encoding="utf-8")
    api = P115API(cookie_file=path)
    assert api.authenticated is False


def test_replace_cookies_switches_state(tmp_path):
    api = P115API(cookie_file=tmp_path / "115-cookies.txt")
    api.replace_cookies("UID=x; CID=y; SEID=z")
    assert api.authenticated is True
    # 换回空串 = 退出登录，但不该抛错（网页上「删除账号」会走到这里）
    api.replace_cookies("")
    assert api.authenticated is False


# ---------------------------------------------------------------- 启动
def test_build_starts_without_cookies(tmp_path):
    (db, api, _store, _scanner, _watcher, _ed2k, _share, _instant,
     _emby, state, hub, _plugins) = build(_cfg(tmp_path))
    try:
        assert api.authenticated is False
        assert state["login"]["ok"] is False
        assert "cookies" in state["login"]["detail"]
        assert hub.count() == 0
    finally:
        db.close()


def test_scanner_skips_when_not_logged_in(tmp_path):
    (db, _api, _store, scanner, _watcher, _ed2k, _share, _instant,
     _emby, _state, _hub, _plugins) = build(_cfg(tmp_path))
    try:
        stats = scanner.run(full=True)
        # 没有请求、没有写文件，也没把索引清空
        assert stats.files == 0
        assert stats.written == 0
        assert stats.aborted is False
    finally:
        db.close()


def test_auto_name_for_nameless_account():
    """网页上只粘贴 cookies、不填账号名时自动取一个不冲突的名字。"""
    hub = AccountHub(Config(), SimpleNamespace())
    assert hub._auto_name() == "115"
    hub._registry = [{"name": "115"}]
    assert hub._auto_name() == "115-2"
