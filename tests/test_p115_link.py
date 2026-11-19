"""115 取直链的接口顺序与缓存时长。

对齐 MoviePilot 的 p115strmhelper：直链优先走 App 端接口
（``proapi.115.com/android/2.0/ufile/download``），Web 端 ``files/download``
只作最后的兜底；缓存时长按链接自带的 ``t`` 参数折算，别把快过期的链接
302 出去。

这些用例不联网，用一个只记录调用的假 client 验证「先问谁、失败退到谁」。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.p115api import P115API, P115Error, _url_ttl


class FakeClient:
    """只实现取直链的四个候选接口，记录调用顺序。"""

    def __init__(self, fail: tuple[str, ...] = (), url: str = "") -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail = set(fail)
        self.url = url or "http://cdnfhnfile.115.com/x.mkv?t=9999999999&f=1"

    def _record(self, name: str, ua: str) -> None:
        self.calls.append((name, ua))
        if name in self.fail:
            raise RuntimeError(f"{name} 405")

    def download_url_app(self, pickcode: str, user_agent: str = "", app: str = "chrome"):
        self._record(f"app:{app}", user_agent)
        return {"state": True, "data": {"url": self.url}, "headers": {"user-agent": user_agent}}

    def download_url(self, pickcode: str, user_agent: str = "", **kwargs):
        self._record("os_windows", user_agent)
        return {"state": True, "data": {"url": self.url}, "headers": {"user-agent": user_agent}}

    def download_url_web(self, pickcode: str, user_agent: str = "", **kwargs):
        self._record("web", user_agent)
        return {"state": True, "data": {"url": self.url}, "headers": {"user-agent": user_agent}}


def _api(client: FakeClient) -> P115API:
    api = object.__new__(P115API)
    api.client = client
    api._url_cache = {}
    api._url_lock = threading.Lock()
    return api


def test_android_app_endpoint_first():
    """第一条就打 App 端接口，并且把请求方的 UA 透传下去。"""
    client = FakeClient()
    api = _api(client)
    url, headers = api.download_url("abcdefghij1234567", user_agent="Emby/4.8")
    assert url.startswith("http://cdnfhnfile.115.com/")
    assert client.calls == [("app:android", "Emby/4.8")]
    assert headers.get("user-agent") == "Emby/4.8"


def test_fallback_order_when_android_fails():
    """App 端被挡（405）后退桌面端，再退浏览器、Web 端。"""
    client = FakeClient(fail=("app:android", "os_windows", "app:chrome"))
    api = _api(client)
    url, _ = api.download_url("abcdefghij1234567", user_agent="UA")
    assert url
    assert [name for name, _ in client.calls] == [
        "app:android", "os_windows", "app:chrome", "web",
    ]


def test_all_endpoints_fail_raises():
    client = FakeClient(fail=("app:android", "os_windows", "app:chrome", "web"))
    api = _api(client)
    with pytest.raises(P115Error) as excinfo:
        api.download_url("abcdefghij1234567", user_agent="UA")
    assert "获取下载直链失败" in str(excinfo.value)


def test_cache_keyed_by_user_agent():
    """不同 UA 必须是不同的缓存项：115 直链是绑 UA 的。"""
    client = FakeClient()
    api = _api(client)
    api.download_url("abcdefghij1234567", user_agent="UA-1")
    api.download_url("abcdefghij1234567", user_agent="UA-1")   # 命中缓存
    api.download_url("abcdefghij1234567", user_agent="UA-2")   # 换 UA 必须重取
    assert [ua for _, ua in client.calls] == ["UA-1", "UA-2"]


def test_ttl_follows_link_expiry():
    """链接 t 只剩 10 分钟：缓存不能超过 t-300。"""
    soon = int(time.time()) + 600
    ttl = _url_ttl(f"http://x/1.mkv?t={soon}", 900.0)
    assert 240 <= ttl <= 360


def test_ttl_uses_config_when_link_is_long_lived():
    """t 还有两小时，但配置只让缓存 900 秒：按配置来。"""
    far = int(time.time()) + 7200
    assert _url_ttl(f"http://x/1.mkv?t={far}", 900.0) == 900.0


def test_ttl_falls_back_without_t_param():
    assert _url_ttl("http://x/1.mkv", 600.0) == 600.0
    assert _url_ttl("http://x/1.mkv?t=abc", 600.0) == 600.0
    assert _url_ttl(f"http://x/1.mkv?t={int(time.time()) - 10}", 600.0) == 600.0
