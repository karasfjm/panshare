"""Emby 反代的 WebSocket 桥接 + 天翼（189）Path 已是 URL 的改写。

线上两个真实现象：

1. 打开 ``http://<host>:7259/web/index.html`` 后登录不了/播放页起不来，
   日志里刷 ``WebSocket /embywebsocket ... 403`` —— 反代只做了 HTTP，
   客户端握手拿不到 upgrade，Starlette 对未匹配的 WS 直接 403。
2. 天翼的 strm 播放地址始终是 ``http://172.17.0.1:9527/p189/play/...``
   （docker 网关），局域网播放器连不上。原因是 Emby 侧的 strm 插件会把
   strm **内容**当作 ``MediaSource.Path`` 返回，Path 根本不是文件路径，
   「文件路径 → 读 strm 内容」那条分支命中不了。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.emby import EmbyProxy

PAYLOAD_PATH = ("http://172.17.0.1:9527/p189/play/c18987563bb8/"
                "625233228867873015/SONE-501.mkv")
ORIGIN = "http://192.168.2.124:7259"


def _proxy(base_url: str = "http://172.17.0.1:9527") -> EmbyProxy:
    cfg = SimpleNamespace(
        emby_enabled=True, emby_url="http://172.17.0.1:8096", emby_strm_prefix="",
        emby_path_rules=(), play_cache_seconds=600, emby_proxy_port=7259,
        strm_root=Path("/strm"),
    )
    store = SimpleNamespace(base_url=base_url, route_prefix="")
    return EmbyProxy(cfg, None, None, store)


# ------------------------------------------------------------------ 天翼 Path 是 URL
def test_189_url_path_is_swapped_to_client_origin():
    """Path 已经是天翼播放地址时，把 origin 换成客户端访问的反代端口。"""
    proxy = _proxy()
    got = proxy.resolve_path(PAYLOAD_PATH, "u1", {}, ORIGIN)
    assert got == ("http://192.168.2.124:7259/p189/play/c18987563bb8/"
                   "625233228867873015/SONE-501.mkv")


def test_189_url_path_falls_back_to_public_base_url():
    """拿不到客户端 origin 时退回 PUBLIC_BASE_URL，别把 docker 网关发出去。"""
    proxy = _proxy(base_url="http://192.168.2.124:9527")
    got = proxy.resolve_path(PAYLOAD_PATH, "u1", {}, "")
    assert got.startswith("http://192.168.2.124:9527/p189/play/")


def test_189_url_path_keeps_same_origin_untouched():
    """origin 本来就对（客户端直接访问 9527）时原样返回。"""
    url = PAYLOAD_PATH.replace("172.17.0.1:9527", "192.168.2.124:9527")
    proxy = _proxy()
    assert proxy.resolve_path(url, "u1", {}, "http://192.168.2.124:9527") == url


def test_playbackinfo_rewrite_sets_direct_stream_url():
    """整条改写：MediaSource 的 Path/DirectStreamUrl 都指到反代端口。"""
    proxy = _proxy()
    payload = {"PlaySessionId": "x", "MediaSources": [{"Path": PAYLOAD_PATH, "Name": "n"}]}
    request = SimpleNamespace(headers={"user-agent": "Infuse/7.0",
                                       "host": "192.168.2.124:7259"})
    out = proxy.rewrite_playback_info(payload, request, b"{}")
    source = out["MediaSources"][0]
    assert source["Path"].startswith(ORIGIN + "/p189/play/")
    assert source["DirectStreamUrl"] == source["Path"]
    assert source["SupportsDirectPlay"] is True


# ------------------------------------------------------------------ WebSocket 桥
class _FakeUpstream:
    def __init__(self, incoming: list[str]):
        self.subprotocol = None
        self.sent: list[object] = []
        self.closed = False
        self._incoming = list(incoming)

    def __aiter__(self):
        async def gen():
            for item in self._incoming:
                yield item
        return gen()

    async def send(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True


class _FakeWebSocket:
    def __init__(self, received: list[dict], path: str = "/embywebsocket",
                 query: str = "api_key=abc"):
        self.headers = {
            "user-agent": "Mozilla/5.0 EmbyWeb/4.9.3",
            "x-emby-token": "tok",
            "sec-websocket-key": "should-be-dropped",
        }
        self.url = SimpleNamespace(path=path, query=query)
        self._received = list(received)
        self.accepted: str | None = None
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed: int | None = None

    async def accept(self, subprotocol=None):
        self.accepted = subprotocol or ""

    async def receive(self):
        return self._received.pop(0) if self._received else {"type": "websocket.disconnect"}

    async def send_text(self, text):
        self.sent_text.append(text)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def close(self, code=1000):
        self.closed = code


def test_ws_target_keeps_path_and_query():
    proxy = _proxy()
    ws = _FakeWebSocket([], path="/emby/embywebsocket", query="api_key=abc&deviceId=d1")
    assert proxy._ws_target(ws, "x") == (
        "ws://172.17.0.1:8096/emby/embywebsocket?api_key=abc&deviceId=d1")


def test_ws_headers_drop_handshake_fields():
    proxy = _proxy()
    ws = _FakeWebSocket([])
    headers = proxy._ws_headers(ws)
    assert headers["x-emby-token"] == "tok"
    assert "sec-websocket-key" not in headers


def test_ws_bridge_roundtrip(monkeypatch):
    """客户端消息发给上游、上游消息回给客户端，断开后两边都关。"""
    upstream = _FakeUpstream(["hello-from-emby"])
    captured: dict[str, object] = {}

    async def fake_connect(target, **kwargs):
        captured["target"] = target
        captured["headers"] = kwargs.get("additional_headers")
        captured["ua"] = kwargs.get("user_agent_header")
        return upstream

    import websockets.asyncio.client as wsclient
    monkeypatch.setattr(wsclient, "connect", fake_connect)

    ws = _FakeWebSocket([
        {"type": "websocket.receive", "text": "hello-from-client"},
        {"type": "websocket.disconnect"},
    ])
    asyncio.run(_proxy().handle_ws(ws, "embywebsocket"))

    assert captured["target"] == "ws://172.17.0.1:8096/embywebsocket?api_key=abc"
    assert captured["ua"] == "Mozilla/5.0 EmbyWeb/4.9.3"
    assert "sec-websocket-key" not in (captured["headers"] or {})
    assert upstream.sent == ["hello-from-client"]
    assert ws.sent_text == ["hello-from-emby"]
    assert upstream.closed is True
    assert ws.accepted == ""


def test_ws_closes_when_upstream_unreachable(monkeypatch):
    """上游连不上：客户端收到 1011，而不是挂在半开状态。"""
    async def boom(target, **kwargs):
        raise OSError("connection refused")

    import websockets.asyncio.client as wsclient
    monkeypatch.setattr(wsclient, "connect", boom)

    ws = _FakeWebSocket([])
    asyncio.run(_proxy().handle_ws(ws, "embywebsocket"))
    assert ws.closed == 1011
