"""插件框架的回归测试。

要证明的几件事：
* 插件清单能读出来，「没装就不加载」是真的（不会 import 依赖重的模块）；
* 升级迁移只认「数据库里有它的配置」，新用户不会平白多出三个插件；
* 装卸能动态挂/摘路由 —— 卸载后接口真的 404，不用重启进程；
* 依赖状态能算对（企微插件没装 playwright 时必须报"缺依赖"）。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config
from app.db import Database
from app.plugins import PluginError, PluginManager

CATALOG_IDS = {"cloud189", "notify", "wxip"}


def _mgr() -> PluginManager:
    tmp = tempfile.mkdtemp(prefix="panshare-plugin-")
    cfg = Config()
    cfg.data_dir = Path(tmp)
    db = Database(str(Path(tmp) / "t.db"))
    return PluginManager(cfg, db)


def _install(mgr: PluginManager, pid: str) -> None:
    """把插件标成已安装已启用（不真的下载依赖）。"""
    mgr._state["installed"][pid] = {"version": "1.0.0", "at": 0}
    mgr._state["enabled"][pid] = True


class _Fake189:
    """天翼云盘管理器的替身：只实现被测试碰到的那几个方法。"""

    def status(self) -> dict:
        return {"logged_in": False, "accounts": [], "configs": [],
                "has_token": False, "renew_ahead_hours": 24}

    def default_id(self) -> str:
        return ""

    def is_account(self, aid: str) -> bool:
        return False


# ------------------------------------------------------------------ 清单
def test_catalog_has_three_plugins():
    mgr = _mgr()
    ids = {m["id"] for m in mgr.manifests()}
    assert ids == CATALOG_IDS, ids
    for manifest in mgr.manifests():
        assert manifest.get("nav") and manifest.get("hash"), manifest
        assert str(manifest.get("hash")).startswith("#/"), manifest
        assert (Path(manifest["_dir"]) / manifest["backend"]).exists(), manifest
        assert (Path(manifest["_dir"]) / manifest["web"]).exists(), manifest


def test_status_reports_uninstalled_by_default():
    rows = {p["id"]: p for p in _mgr().status()["plugins"]}
    assert set(rows) == CATALOG_IDS
    assert all(not p["installed"] for p in rows.values())
    # 没装之前不应该有后台线程在跑
    assert all(not p["loaded"] for p in rows.values())


def test_wxip_reports_missing_browser_deps():
    rows = {p["id"]: p for p in _mgr().status()["plugins"]}
    deps = rows["wxip"]["deps"]
    assert deps["ready"] is False
    assert "playwright" in deps["missing"]
    assert deps["browser"] == ["chromium"]
    # 纯 Python 的插件不该被判定成"缺依赖"
    assert rows["cloud189"]["deps"]["ready"] is True


# ------------------------------------------------------------------ 迁移
def test_fresh_install_seeds_nothing():
    """新部署的实例：三个插件都是未安装，不该被默认塞进来。"""
    mgr = _mgr()
    mgr.seed()
    assert mgr._state["installed"] == {}
    assert mgr._state["removed"] == {}


def test_upgrade_only_seeds_plugins_that_were_in_use():
    mgr = _mgr()
    mgr.db.set_meta("notify_settings", '{"enabled": true}')
    mgr.db.upsert_cloud189_account({
        "id": "c189a", "name": "账号A", "login_name": "a@189.cn",
        "access_token": "t", "session_key": "sk", "expires_at": 0,
        "is_default": 1, "source": "cookie",
    })
    mgr.seed()
    installed = set(mgr._state["installed"])
    assert installed == {"notify", "cloud189"}, installed
    assert mgr.is_enabled("notify") and mgr.is_enabled("cloud189")
    # 没用过企微的机器不该被塞一个要下 700MB 的插件
    assert "wxip" not in installed


def test_link_settings_alone_is_enough_to_seed_notify():
    """只配过「双向联动」没动过通知渠道的机器，也该把「通知与联动」补上。"""
    mgr = _mgr()
    mgr.db.set_meta("link_settings", '{"remote_to_local": true}')
    mgr.seed()
    assert "notify" in mgr._state["installed"]


def test_user_uninstall_is_not_undone_by_seed():
    """用户主动卸载过：数据库里配置还在，也不能每次重启又冒出来。"""
    mgr = _mgr()
    mgr.db.set_meta("wxip_settings", "{}")
    mgr.seed()
    assert "wxip" in mgr._state["installed"]
    mgr.uninstall("wxip")
    mgr.seed()
    assert "wxip" not in mgr._state["installed"]
    # 重新装一次要能清掉"用户卸过"的记号
    mgr._state["installed"]["wxip"] = {"version": "1", "at": 0}
    mgr._state["enabled"]["wxip"] = True
    mgr._state["removed"].pop("wxip")
    mgr.seed()
    assert "wxip" in mgr._state["installed"]


# ------------------------------------------------------------------ 安装
def test_install_worker_marks_installed_and_loads(monkeypatch):
    mgr = _mgr()
    # 依赖安装真的会去跑 pip/apt，这里换掉；只验状态机
    mgr._pip_install = lambda *a, **k: None
    mgr._apt_install = lambda *a, **k: None
    mgr._browser_install = lambda *a, **k: None
    loaded: list[str] = []
    mgr.load = lambda pid: loaded.append(pid) or SimpleNamespace()

    mgr.tasks["cloud189"] = {"status": "running", "log": []}
    mgr._install_worker("cloud189", mgr.manifest("cloud189"))

    assert mgr.is_installed("cloud189") and mgr.is_enabled("cloud189")
    assert loaded == ["cloud189"]
    assert mgr.tasks["cloud189"]["status"] == "done"
    assert any("安装完成" in line for line in mgr.tasks["cloud189"]["log"])


def test_install_failure_keeps_plugin_uninstalled():
    mgr = _mgr()
    mgr._pip_install = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("没网"))
    mgr.tasks["wxip"] = {"status": "running", "log": []}
    mgr._install_worker("wxip", mgr.manifest("wxip"))
    assert not mgr.is_installed("wxip")
    assert mgr.tasks["wxip"]["status"] == "error"
    assert "没网" in mgr.tasks["wxip"]["error"]


def test_install_twice_is_rejected_while_running():
    mgr = _mgr()
    mgr.tasks["wxip"] = {"status": "running", "log": []}
    with pytest.raises(PluginError, match="正在安装"):
        mgr.install("wxip")


# ------------------------------------------------------------------ 路由装卸
def test_load_mounts_routes_and_uninstall_removes_them():
    """插件路由是动态挂上去的：卸载后同一进程里就该 404，不重启。"""
    mgr = _mgr()
    app = FastAPI()
    mgr.attach(app, lambda request, token="": None)
    _install(mgr, "cloud189")
    mgr.inject("cloud189", _Fake189())

    paths = len(app.router.routes)
    assert TestClient(app).get("/api/cloud189").status_code == 200

    mgr.uninstall("cloud189")
    client = TestClient(app)
    assert client.get("/api/cloud189").status_code == 404
    assert len(app.router.routes) < paths        # 路由真的摘掉了，没重启进程
    assert not mgr.is_installed("cloud189")


def test_disable_stops_routes_without_uninstalling():
    mgr = _mgr()
    app = FastAPI()
    mgr.attach(app, lambda request, token="": None)
    _install(mgr, "cloud189")
    mgr.inject("cloud189", _Fake189())
    mgr.set_enabled("cloud189", False)
    assert mgr.is_installed("cloud189")          # 还装着，只是停用
    assert TestClient(app).get("/api/cloud189").status_code == 404
    mgr.set_enabled("cloud189", True)
    assert TestClient(app).get("/api/cloud189").status_code == 200


def test_notify_plugin_exposes_both_notify_and_link():
    mgr = _mgr()
    app = FastAPI()
    mgr.attach(app, lambda request, token="": None)
    mgr.bind(cfg=mgr.cfg, db=mgr.db, hub=None, engines=list)
    _install(mgr, "notify")
    notifier = mgr.load("notify").obj
    assert notifier.linker is not None
    client = TestClient(app)
    assert client.get("/api/notify").status_code == 200
    body = client.get("/api/link").json()
    assert "settings" in body and "pending" in body


def test_web_asset_only_served_for_installed_plugin():
    mgr = _mgr()
    assert mgr.web_asset("wxip") is None
    _install(mgr, "wxip")
    path = mgr.web_asset("wxip")
    assert path is not None and path.name == "web.js"
    assert "registerPlugin" in path.read_text(encoding="utf-8")


def test_plugin_js_registers_itself_into_shell():
    """插件前端自己往侧栏注册：这样卸载后入口才会跟着消失。"""
    mgr = _mgr()
    for pid in CATALOG_IDS:
        text = (Path(mgr.manifest(pid)["_dir"]) / "web.js").read_text(encoding="utf-8")
        assert "Shell.registerPlugin" in text, pid


def test_unknown_plugin_is_rejected():
    mgr = _mgr()
    with pytest.raises(PluginError, match="没有这个插件"):
        mgr.install("nope")


def test_task_log_is_trimmed():
    mgr = _mgr()
    mgr.tasks["x"] = {"status": "running", "log": []}
    for i in range(400):
        mgr._say("x", f"第 {i} 行")
    assert len(mgr.tasks["x"]["log"]) <= 300
