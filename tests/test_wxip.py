"""企微「企业可信 IP」更新的回归测试（不碰真浏览器）。

锁住的点：
* 配置默认值 / 兜底（间隔不能小于 60 秒，agent_ids 能解析成列表）；
* 没装 playwright 时 status 要给「怎么装」的提示，而不是抛异常；
* 没登录就点「立即更新」要明确告诉用户去扫码；
* IP 变了但「自动更新」关掉时只发通知、不动企微配置。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import Database
from app.wxip import WxIpManager, _agent_list, _merged, wxip_settings


def _db() -> Database:
    tmp = tempfile.mkdtemp(prefix="panshare-wxip-db-")
    return Database(str(Path(tmp) / "t.db"))


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, text: str, event: str = "", level: str = "info",
             force: bool = False) -> dict:
        self.sent.append((text, level))
        return {"ok": True}


def _mgr(db: Database | None = None, notifier=None) -> WxIpManager:
    cfg = SimpleNamespace(data_dir=Path(tempfile.mkdtemp(prefix="panshare-wxip-")))
    return WxIpManager(cfg, db or _db(), notifier=notifier)


# ------------------------------------------------------------------ 配置
def test_settings_defaults_and_clamp():
    db = _db()
    assert wxip_settings(db)["enabled"] is False
    assert wxip_settings(db)["interval"] == 300
    assert _merged({"interval": 5})["interval"] == 60      # 太短会被抬到 60
    assert _merged({"interval": "abc"})["interval"] == 300
    assert _merged({"enabled": "yes"})["enabled"] is True


def test_agent_list_parsing():
    assert _agent_list("1000002, 1000003") == ["1000002", "1000003"]
    assert _agent_list("1000002\n1000003；1000004") == ["1000002", "1000003", "1000004"]
    assert _agent_list("abc 1000002 x") == ["1000002"]
    assert _agent_list("") == []


def test_save_settings_roundtrip():
    mgr = _mgr()
    saved = mgr.save_settings({"enabled": True, "agent_ids": "1000002",
                               "interval": 10, "proxy": ""})
    assert saved["enabled"] is True
    assert saved["agent_ids"] == "1000002"
    assert saved["interval"] == 60
    assert mgr.settings()["enabled"] is True


# ------------------------------------------------------------------ 状态
def test_status_reports_missing_playwright():
    mgr = _mgr()
    mgr.available = False
    st = mgr.status()
    assert st["available"] is False
    assert "playwright" in st["hint"]
    assert st["login"] == "idle"
    assert st["logged_in"] is False


def test_update_requires_login():
    mgr = _mgr()
    got = mgr.request_update()
    assert got["ok"] is False and "扫码" in got["detail"]


def test_logout_clears_state_file():
    mgr = _mgr()
    mgr.state_file.parent.mkdir(parents=True, exist_ok=True)
    mgr.state_file.write_text("{}", encoding="utf-8")
    assert mgr.status()["logged_in"] is True
    assert mgr.logout()["ok"] is True
    assert mgr.status()["logged_in"] is False


# ------------------------------------------------------------ 检测 + 通知
def test_ip_change_notifies_when_auto_off():
    note = FakeNotifier()
    mgr = _mgr(notifier=note)
    mgr.save_settings({"enabled": True, "auto": False, "notify": True})
    mgr.current_ip = "1.1.1.1"
    mgr.detect_ip = lambda proxy="": "2.2.2.2"      # 不真的发请求
    mgr._check_and_update(mgr.settings(), force=False)
    assert mgr.current_ip == "2.2.2.2"
    assert len(note.sent) == 1
    assert "2.2.2.2" in note.sent[0][0]
    assert mgr.last_update == {}                    # 没动企微配置，不算一次更新


def test_ip_unchanged_does_nothing():
    note = FakeNotifier()
    mgr = _mgr(notifier=note)
    mgr.save_settings({"enabled": True, "auto": True, "notify": True})
    mgr.current_ip = "1.1.1.1"
    mgr.detect_ip = lambda proxy="": "1.1.1.1"
    mgr._check_and_update(mgr.settings(), force=False)
    assert note.sent == []
    assert mgr.last_update == {}


def test_detect_failure_keeps_last_ip():
    note = FakeNotifier()
    mgr = _mgr(notifier=note)
    mgr.save_settings({"enabled": True, "auto": True})
    mgr.current_ip = "1.1.1.1"
    mgr.detect_ip = lambda proxy="": ""
    mgr._check_and_update(mgr.settings(), force=False)
    assert mgr.current_ip == "1.1.1.1"
    assert "不通" in mgr.last_error
    assert note.sent == []


def test_auto_update_without_login_marks_expired():
    note = FakeNotifier()
    mgr = _mgr(notifier=note)
    mgr.save_settings({"enabled": True, "auto": True, "notify": True})
    mgr.current_ip = "1.1.1.1"
    mgr.detect_ip = lambda proxy="": "3.3.3.3"
    mgr._check_and_update(mgr.settings(), force=False)
    # 定时检查：只标记过期 + 提醒一次，不假装更新过
    assert mgr.login_state == "expired"
    assert mgr.last_update == {}
    assert len(note.sent) == 1 and "扫码" not in note.sent[0][0]

    # 手动点「立即检测并更新」：要留下失败记录
    mgr._check_and_update(mgr.settings(), force=True)
    assert mgr.last_update.get("ok") is False
    assert "登录" in str(mgr.last_update.get("detail"))


# ------------------------------------------------------- 登录判定（v1.18.1 修）
class _FakeCtx:
    def __init__(self, cookies):
        self._cookies = cookies
        self.saved: list[str] = []

    def cookies(self):
        return list(self._cookies)

    def storage_state(self, path: str):
        Path(path).write_text("{}", encoding="utf-8")
        self.saved.append(path)


def _page(url: str):
    return SimpleNamespace(url=url)


def test_logged_in_by_cookie_even_if_url_stays_loginpage():
    """扫码成功但主框架没跳走（或被打回登录页）时，认 cookie 不能认 URL。"""
    mgr = _mgr()
    mgr._context = _FakeCtx([{"name": "wwrtx.vst", "value": "abc123"}])
    assert mgr._has_vst() is True
    assert mgr._logged_in(_page("https://work.weixin.qq.com/wework_admin/loginpage_wx?from=myhome")) is True


def test_logged_in_by_url_when_cookie_missing():
    mgr = _mgr()
    mgr._context = _FakeCtx([{"name": "wwrtx.ref", "value": "1"}])
    assert mgr._logged_in(_page("https://work.weixin.qq.com/wework_admin/frame#/index")) is True
    assert mgr._logged_in(_page("https://work.weixin.qq.com/wework_admin/loginpage_wx")) is False
    assert mgr._logged_in(_page("")) is False


def test_save_state_rejects_fake_login():
    """没有 wwrtx.vst 的登录态 = 假登录，必须拒存并留提示。"""
    mgr = _mgr()
    mgr._context = _FakeCtx([{"name": "wwrtx.ref", "value": "1"}])
    try:
        mgr._save_state()
    except RuntimeError as exc:
        assert "確認" in str(exc) or "确认" in str(exc)
    else:
        raise AssertionError("没拿到 vst 时不该保存登录态")
    assert not mgr.state_file.exists()


def test_save_state_keeps_real_login():
    mgr = _mgr()
    ctx = _FakeCtx([{"name": "wwrtx.vst", "value": "abc"}])
    mgr._context = ctx
    mgr._save_state()
    assert mgr.state_file.exists() and ctx.saved


# ------------------------------------------------- 可信IP 读写（v1.18.2 修）
class _Stop:
    def wait(self, t=0):
        return False

    def is_set(self):
        return False


class _Textarea:
    def __init__(self, val=""):
        self.val = val

    def click(self, timeout=0):
        return None

    def input_value(self):
        return self.val

    def fill(self, v):
        self.val = v


def test_fill_ip_appends_and_keeps_existing():
    """必须追加，不能覆盖——老实现 fill(ip) 会把用户已配好的 IP 全删掉。"""
    mgr = _mgr()
    ta = _Textarea("1.2.3.4;5.6.7.8")
    mgr._find = lambda page, sels: ta
    assert mgr._fill_ip(None, "9.9.9.9") is True
    assert ta.val == "1.2.3.4;5.6.7.8;9.9.9.9"


def test_fill_ip_skips_when_already_present():
    mgr = _mgr()
    ta = _Textarea("1.2.3.4")
    mgr._find = lambda page, sels: ta
    assert mgr._fill_ip(None, "1.2.3.4") is True
    assert ta.val == "1.2.3.4"


def test_agent_name_resolves_to_discovered_id():
    """配置里写应用名（OECT）也能用：去后台列表里换成 AgentId。"""
    mgr = _mgr()
    mgr._stop = _Stop()
    mgr._ensure_browser = lambda: SimpleNamespace(url="https://x/frame", goto=lambda *a, **k: None)
    mgr._shutdown_browser = lambda: None
    mgr._discover_agents = lambda page: [("OECT", "5629503509663610")]
    seen: list[tuple[str, str]] = []

    def fake_update(page, aid, ip, name=""):
        seen.append((aid, name))
        return {"agent": aid, "name": name, "ok": True, "detail": "已更新"}

    mgr._update_one = fake_update
    r = mgr._update_agents(["OECT"], "1.1.1.1")
    assert seen == [("5629503509663610", "OECT")]
    assert r["ok"] is True and "OECT" in r["detail"]


def test_unknown_agent_name_reports_failure():
    mgr = _mgr()
    mgr._stop = _Stop()
    mgr._ensure_browser = lambda: SimpleNamespace(url="https://x/frame", goto=lambda *a, **k: None)
    mgr._shutdown_browser = lambda: None
    mgr._discover_agents = lambda page: [("OECT", "5629503509663610")]
    mgr._update_one = lambda page, aid, ip, name="": {"agent": aid, "name": name,
                                                     "ok": True, "detail": "已更新"}
    r = mgr._update_agents(["不存在的应用"], "1.1.1.1")
    assert r["ok"] is False
    assert "没找到" in r["detail"]


def test_numeric_agent_id_skips_discovery():
    """填了纯数字 AgentId 就不用去列表页瞎点。"""
    mgr = _mgr()
    mgr._stop = _Stop()
    mgr._ensure_browser = lambda: SimpleNamespace(url="https://x/frame", goto=lambda *a, **k: None)
    mgr._shutdown_browser = lambda: None

    def boom(page):
        raise AssertionError("填了 AgentId 不该再去发现列表")

    mgr._discover_agents = boom
    mgr._update_one = lambda page, aid, ip, name="": {"agent": aid, "name": name,
                                                     "ok": True, "detail": "已更新"}
    r = mgr._update_agents(["5629503509663610"], "1.1.1.1")
    assert r["ok"] is True
