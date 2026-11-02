"""登录页背景（TMDB 剧照）的回归测试。

线上问题：填了 ``TMDB_API_KEY`` 还是没背景 ——
1. 容器没重建，环境变量压根没进去；
2. 变量进去了也拉不到图：容器直连 ``api.themoviedb.org`` SSL 握手超时
   （宿主机有透明代理，docker bridge 的流量不走它）。
所以这里锁住「代理能配、只给 TMDB 的请求用」「TMDB 挂了要回退自定义图片」。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.tmdb as tmdb  # noqa: E402
from app.tmdb import BackdropSource  # noqa: E402


class _Resp:
    def __init__(self, payload: dict | None = None, body: bytes = b"",
                 ctype: str = "image/jpeg"):
        self._payload = payload or {}
        self.content = body
        self.headers = {"content-type": ctype}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_proxy_read_from_env(monkeypatch):
    monkeypatch.setenv("TMDB_PROXY", "http://192.168.2.124:7890")
    src = BackdropSource(api_key="k")
    assert src.proxy == "http://192.168.2.124:7890"
    assert src.status()["proxy"] is True


def test_proxy_empty_by_default(monkeypatch):
    monkeypatch.delenv("TMDB_PROXY", raising=False)
    src = BackdropSource(api_key="k")
    assert src.proxy == ""
    assert src.status()["proxy"] is False


def test_urls_client_gets_proxy(monkeypatch):
    """拉片单的请求必须带上 TMDB 代理（否则 115 之外的流量也被牵走/根本连不上）。"""
    seen: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None, headers=None):
            return _Resp({"results": [{"backdrop_path": "/a.jpg"},
                                      {"backdrop_path": "not-a-path"}]})

    monkeypatch.setattr(tmdb.httpx, "Client", FakeClient)
    src = BackdropSource(api_key="k", proxy="http://192.168.2.124:7890")
    assert src.urls() == [tmdb.IMG_BASE + "/a.jpg"]
    assert seen.get("proxy") == "http://192.168.2.124:7890"


def test_no_proxy_means_none(monkeypatch):
    seen: dict = {}

    class FakeClient:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None, headers=None):
            return _Resp({"results": []})

    monkeypatch.setattr(tmdb.httpx, "Client", FakeClient)
    BackdropSource(api_key="k").urls()
    assert seen.get("proxy") is None


def test_falls_back_to_custom_when_tmdb_fails(monkeypatch):
    """TMDB 连不上时用 LOGIN_BACKDROPS 的直链，登录页仍有图。"""

    class BoomClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None, headers=None):
            raise RuntimeError("_ssl.c:993: The handshake operation timed out")

    monkeypatch.setattr(tmdb.httpx, "Client", BoomClient)
    src = BackdropSource(api_key="k",
                         custom=("https://image.tmdb.org/t/p/original/x.jpg",))
    assert src.urls() == ["https://image.tmdb.org/t/p/original/x.jpg"]
    # 拉取失败的原因要留着，方便 /api/login/backdrop 的 detail 给人看
    assert "handshake" in src.status()["error"]


def test_fetch_image_rejects_foreign_host():
    src = BackdropSource(api_key="k")
    try:
        src.fetch_image("https://evil.example/x.jpg")
    except ValueError:
        return
    raise AssertionError("非 TMDB / 非自定义的图片地址必须拒绝")


def test_fetch_image_allows_tmdb_host(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            assert url == "https://image.tmdb.org/t/p/original/a.jpg"
            return _Resp(body=b"JPEGDATA", ctype="image/jpeg")

    monkeypatch.setattr(tmdb.httpx, "Client", FakeClient)
    src = BackdropSource(api_key="k", proxy="http://p:7890")
    body, ctype = src.fetch_image("https://image.tmdb.org/t/p/original/a.jpg")
    assert body == b"JPEGDATA" and ctype == "image/jpeg"


def test_available_false_without_anything(monkeypatch):
    for name in ("TMDB_API_KEY", "TMDB_READ_TOKEN", "LOGIN_BACKDROPS"):
        monkeypatch.delenv(name, raising=False)
    src = BackdropSource()
    assert src.available is False
