"""Emby 302 反代：把 Emby 的 PlaybackInfo 改写成 115 最终直链，一个容器搞定。

用法：Emby 客户端地址填本服务 ``http://<host>:9527/emby``，
本服务把请求原样转发给上游 Emby（EMBY_URL），但在「PlaybackInfo」响应里
把每条 MediaSource 的路径改写成 **115 的最终直链** —— 改写时就用发起播放的
设备的 UA 向 115 取好直链，直接发给播放设备，播放器零跳转直连 115，
Emby 服务器不用再中转视频流。直链预解析失败时（ed2k 未就绪、限流、
f=3 要 Cookie 等）回退为本服务的播放地址，播放器请求它时再 302 / 中转。

MediaSource 路径的识别规则（按顺序尝试，命中即止）：

1. **本服务的 strm**：路径在 STRM_ROOT 下（或以 EMBY_STRM_PREFIX 开头），
   按 strm 相对路径查索引拿 pickcode；ed2k / 分享的 strm 走各自的稳定路由；
2. **Pickcode**：路径或 URL 里带 ``pickcode=``；
3. **FileId**：路径形如 ``/115/<file_id>`` 或带 ``file_id=``；
4. **自定义规则**（EMBY_PATH_RULES）：每行 ``正则=>模板``，模板里可用
   ``$1..$9`` 引用捕获组、``{base}`` 是本服务地址；模板以 ``dbpath:`` 开头
   时表示「按 115 相对路径在本地索引里找文件」（可覆盖 123 等其它目录树
   挂到 115 索引的场景）。

直链解析结果带 TTL 缓存，并按 key 单飞（同一文件的并发请求只解析一次，
其余复用结果 —— 即「同播复制」）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.datastructures import Headers

from .config import Config
from .db import Database
from .instant import InstantManager
from .p115api import P115API
from .strm import StrmStore

log = logging.getLogger(__name__)

PASS_HEADERS = {
    "content-type", "content-length", "content-range", "accept-ranges",
    "last-modified", "etag", "content-disposition", "cache-control",
    # gzip/br 压缩的响应体必须带上编码头，否则客户端把压缩字节当明文解析
    # （Emby 登录接口就吃了这个亏：报「处理请求时出错」）
    "content-encoding", "vary", "set-cookie",
}

# /emby/Items/{id}/PlaybackInfo
PLAYBACKINFO_RE = re.compile(r"/Items/[\w-]+/PlaybackInfo/?$", re.IGNORECASE)

# Emby 登录接口：响应里带 AccessToken + User.ID，记下来用于播放时反查用户
AUTH_RE = re.compile(
    r"/(?:Users/)?AuthenticateByName/?$|/Users/Authenticate/?$", re.IGNORECASE
)

# 本服务自己生成的直链（strm 的内容就是这种 URL）：可预解析成 115 最终直链；
# 子账号的 URL 带 /a/<账号名> 前缀
OUR_URL_RE = re.compile(
    r"https?://[^/]+/p115(?:/a/[^/?#]+)?/(?:play|share|ed2k|instant)/", re.IGNORECASE
)

# 本服务播放路由的路径形状：[/a/<账号名>]/<route>/<tail>
OUR_PATH_RE = re.compile(
    r"^/p115(?:/a/([^/?#]+))?/(play|ed2k|share|instant)/(.+)$", re.IGNORECASE
)


def _split_ext(value: str) -> str:
    """去掉播放器补的扩展名（``pc1234.mkv`` -> ``pc1234``）。"""
    head, _, tail = value.rpartition(".")
    if tail and 0 < len(tail) <= 10 and head:
        return head
    return value


def _play_tail_pickcode(tail: str) -> str:
    """从 play 路由尾部取 pickcode。

    新格式 ``<pickcode>/<URL 编码的文件名>/<115 文件 id>``，旧格式
    ``<pickcode>.mkv`` 也认 —— 取第一段再去掉可能的扩展名。
    """
    first = (tail or "").lstrip("/").split("/", 1)[0]
    return _split_ext(first)


def _play_tail_uid(tail: str) -> str:
    """新格式尾部最后一段是 115 用户 ID（旧格式没有 / 不是纯数字，返回空）。"""
    parts = [p for p in (tail or "").split("/") if p]
    if len(parts) >= 2 and parts[-1].isdigit():
        return parts[-1]
    return ""


def _needs_strict_headers(url: str) -> bool:
    """f=3 的直链除了 UA 还要 Cookie，交给设备播不了，回退本服务地址中转。"""
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return False
    return (query.get("f") or [""])[0] == "3"


def fetch_public_users(emby_url: str, timeout: float = 6.0) -> list[dict[str, Any]]:
    """从 Emby 的 ``/Users/Public`` 取全部用户（**不需要 API Key**）。

    Emby 自己的登录页就是靠这个接口列出可登录用户的，所以它对未认证请求
    开放。之前秒传页只能看到「登录过一次」的用户 —— 因为名单来自
    反代过程中捕获的 token，没登录过的用户自然不在里面。

    失败（Emby 地址没配 / 连不上 / 返回格式不对）一律返回空列表：
    名单只是给下拉框用的，拿不到就退回「已知用户」，不能影响页面。
    """
    base = (emby_url or "").strip().rstrip("/")
    if not base:
        return []
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout), follow_redirects=True) as client:
            resp = client.get(f"{base}/Users/Public")
            resp.raise_for_status()
            items = resp.json()
    except Exception as exc:  # noqa: BLE001 - 网络问题不该影响页面
        log.debug("获取 Emby 用户列表失败（%s）: %s", base, exc)
        return []
    if isinstance(items, dict):
        items = items.get("Items") or []
    out: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("Id") or "").strip()
        if not uid:
            continue
        out.append({
            "id": uid,
            "name": str(item.get("Name") or ""),
            "has_password": bool(item.get("HasPassword")),
        })
    return out


def _run_bounded(func, seconds: float) -> tuple[bool, Any]:
    """在守护线程里跑 ``func``，最多等 ``seconds`` 秒。

    返回 ``(是否跑完, 结果)``。预解析会顺手做一次秒传（十几秒），
    万一 115 那边卡住，不能让 Emby 的 PlaybackInfo 跟着一起卡死：
    超时就先回退，秒传继续在后台跑完，下一次请求就快了。
    ``seconds <= 0`` 表示不限制（同步等）。
    """
    if not seconds or seconds <= 0:
        return True, func()
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - 交给调用线程重新抛
            box["error"] = exc

    thread = threading.Thread(target=runner, name="p115-preresolve", daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        return False, None
    if "error" in box:
        raise box["error"]
    return True, box.get("value")


class LinkCache:
    """带 TTL 和单飞的直链缓存（同播复制：同一 key 并发只解析一次）。"""

    def __init__(self, ttl: float = 600.0):
        self.ttl = ttl
        self._data: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: str) -> str | None:
        item = self._data.get(key)
        if item and item[1] > time.time():
            return item[0]
        return None

    def lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def put(self, key: str, url: str) -> None:
        self._data[key] = (url, time.time() + self.ttl)


class EmbyProxy:
    def __init__(self, cfg: Config, db: Database, api: P115API, store: StrmStore,
                 instant: InstantManager | None = None,
                 ed2k: Any | None = None, hub: Any | None = None,
                 account: str = "", cloud189: Any | None = None):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.store = store
        self.instant = instant
        self.ed2k = ed2k
        self.hub = hub
        # 天翼云盘管理器：天翼的 strm 也要在这里改写成「客户端可达的直链」，
        # 否则 strm 里的 docker 网关地址（172.17.0.1）局域网播放器连不上。
        self.cloud189 = cloud189
        # 这个反代代表哪个账号（"" = 主账号）：秒传二次校验要拿**源账号**的直链
        self.account = account
        # 独立端口的运行时管理器（由 main.py 注入）：网页上改端口后立即生效
        self.port_runner: Any = None
        # 反代配置可以在网页上手动改（需求：反代全部字段可编辑），
        # 所以放在独立字典里，改完调 apply_settings() 立即生效
        self.settings: dict[str, Any] = {
            # 有上游地址就默认开着；网页上可以随时关掉（关掉后 /emby 返回 503）
            "enabled": bool(cfg.emby_enabled or cfg.emby_url),
            "upstream": cfg.emby_url.rstrip("/"),
            "strm_prefix": cfg.emby_strm_prefix or "",
            "path_rules": list(cfg.emby_path_rules),
            "cache_seconds": max(60, cfg.play_cache_seconds),
            "proxy_port": cfg.emby_proxy_port,
            "log_level": "info",
        }
        self.cache = LinkCache(ttl=max(60, cfg.play_cache_seconds))
        # 115 最终直链按（直链路由, UA）缓存 —— 直链与 UA 绑定
        self.direct_cache = LinkCache(ttl=max(60, cfg.play_cache_seconds))
        # httpx 客户端按事件循环分别缓存：连接池会绑定到首次使用它的事件循环，
        # 而主服务与独立反代端口是两个事件循环，共用一个必然报
        # "bound to a different event loop"
        self._clients: dict[Any, httpx.AsyncClient] = {}
        self._rules: list[tuple[re.Pattern[str], str, bool]] = []
        self._compile_rules()

    # ------------------------------------------------------------------ 配置
    def http_client(self) -> httpx.AsyncClient:
        """当前事件循环对应的 httpx 客户端（按循环缓存，循环换了就换客户端）。"""
        try:
            loop: Any = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        client = self._clients.get(loop)
        if client is None:
            # 循环已关闭的客户端（比如独立端口换过端口）直接丢掉，避免越积越多
            for key in [k for k in self._clients
                        if k is not None and getattr(k, "is_closed", False)]:
                stale = self._clients.pop(key, None)
                if stale is not None:
                    try:
                        stale.close()
                    except Exception as exc:  # noqa: BLE001 - 清理失败不影响主流程
                        log.debug("关闭失效的 httpx 客户端失败: %s", exc)
            client = httpx.AsyncClient(
                follow_redirects=False, timeout=httpx.Timeout(30.0, read=300.0)
            )
            self._clients[loop] = client
        return client

    def drop_clients(self) -> None:
        """丢弃已关闭事件循环对应的客户端（独立端口换端口时调用）。"""
        for key in [k for k in self._clients
                    if k is not None and getattr(k, "is_closed", False)]:
            stale = self._clients.pop(key, None)
            if stale is not None:
                try:
                    stale.close()
                except Exception as exc:  # noqa: BLE001 - 清理失败不影响主流程
                    log.debug("关闭失效的 httpx 客户端失败: %s", exc)

    def _compile_rules(self) -> None:
        rules: list[tuple[re.Pattern[str], str, bool]] = []
        for line in self.settings.get("path_rules") or []:
            pattern, _, template = str(line).partition("=>")
            try:
                compiled = re.compile(pattern.strip())
            except re.error as exc:
                log.warning("EMBY_PATH_RULES 正则无效（忽略该行）: %s (%s)", line, exc)
                continue
            rules.append((compiled, template.strip(), template.strip().startswith("dbpath:")))
        self._rules = rules

    def apply_settings(self, settings: dict[str, Any]) -> None:
        """应用网页上改过的反代配置（立即生效，不用重启容器）。"""
        merged = dict(self.settings)
        for key in ("enabled", "upstream", "strm_prefix", "path_rules",
                    "cache_seconds", "proxy_port", "log_level"):
            if key in settings and settings[key] is not None:
                merged[key] = settings[key]
        merged["upstream"] = str(merged.get("upstream") or "").rstrip("/")
        merged["strm_prefix"] = str(merged.get("strm_prefix") or "")
        merged["path_rules"] = [str(x) for x in (merged.get("path_rules") or [])]
        try:
            merged["cache_seconds"] = max(0, int(merged.get("cache_seconds") or 0))
        except (TypeError, ValueError):
            merged["cache_seconds"] = 900
        try:
            merged["proxy_port"] = max(0, int(merged.get("proxy_port") or 0))
        except (TypeError, ValueError):
            merged["proxy_port"] = 0
        self.settings = merged
        ttl = max(60, merged["cache_seconds"] or 900)
        self.cache = LinkCache(ttl=ttl)
        self.direct_cache = LinkCache(ttl=ttl)
        self._compile_rules()
        log.info("反代配置已更新: upstream=%s strm前缀=%s 独立端口=%s rules=%d",
                 merged["upstream"] or "(未配置)",
                 merged["strm_prefix"] or "(自动识别)",
                 merged["proxy_port"] or "(用本服务端口)",
                 len(self._rules))

    @property
    def upstream(self) -> str:
        """上游 Emby 地址（网页上可改）。"""
        return str(self.settings.get("upstream") or "").rstrip("/")

    # ------------------------------------------------------------------ 反代
    async def handle(self, request: Request, path: str) -> Response:
        """把请求原样转发到上游 Emby。

        路径**完整透传**：客户端请求 ``/emby/Items/x/PlaybackInfo``，上游就收到
        同样的路径。这样不管上游 Emby 的 BaseUrl 配的是 ``/`` 还是 ``/emby``
        都能命中 —— 剥掉前缀再转发反而会在 BaseUrl=/emby 时 404。
        """
        if not self.settings.get("enabled", True):
            raise HTTPException(status_code=503, detail="Emby 反代已关闭")
        if not self.upstream:
            raise HTTPException(status_code=503, detail="没有配置上游 Emby 地址")
        url = f"{self.upstream}{self._request_path(request, path)}"
        if request.url.query:
            url += f"?{request.url.query}"
        body = await request.body()
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "connection")
        }

        try:
            client = self.http_client()
            upstream_req = client.build_request(
                request.method, url, headers=headers, content=body or None
            )
            upstream_resp = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Emby 上游不可达: {exc}") from exc

        # PlaybackInfo 响应要改写；其余原样透传
        if PLAYBACKINFO_RE.search("/" + path) and request.method == "POST":
            try:
                payload = await self._read_json(upstream_resp)
            finally:
                await upstream_resp.aclose()
            # 取直链是同步 115 调用，丢线程池里跑，别卡事件循环
            rewritten = await asyncio.to_thread(
                self.rewrite_playback_info, payload, request, body
            )
            return Response(
                content=_dump(rewritten),
                status_code=upstream_resp.status_code,
                media_type="application/json",
            )

        # 登录响应里带 AccessToken + User.ID —— 记下来，播放时用 token 反查用户。
        # 播放请求不一定带 UserId（Emby 客户端只在部分接口带），但一定带 token；
        # 没有这张映射表就认不出是谁在播，秒传账号映射也就用不上。
        if AUTH_RE.search("/" + path) and request.method == "POST":
            # 登录响应要整个读完（要拿到 AccessToken）。
            # 用 aiter_raw 拿**未解压**的原始字节：转发给客户端的必须还是
            # 压缩过的（content-encoding 头会原样带回，客户端自己会解）。
            raw_chunks: list[bytes] = []
            try:
                async for chunk in upstream_resp.aiter_raw():
                    raw_chunks.append(chunk)
            except Exception as exc:  # noqa: BLE001 - 读不到就当没 token
                log.debug("读 Emby 登录响应失败: %s", exc)
            body_bytes = b"".join(raw_chunks)
            try:
                raw_body = body_bytes
                if raw_body[:2] == b"\x1f\x8b":
                    try:
                        import gzip

                        raw_body = gzip.decompress(raw_body)
                    except OSError:
                        raw_body = body_bytes
                auth_payload = json.loads(raw_body)
            except (ValueError, TypeError):
                auth_payload = {}
            if not isinstance(auth_payload, dict):
                auth_payload = {}
            token = str(auth_payload.get("AccessToken") or "")
            user = auth_payload.get("User") or {}
            user_id = str(user.get("Id") or "") if isinstance(user, dict) else ""
            if token and user_id:
                try:
                    self.db.remember_emby_token(token, user_id)
                    log.info("记住 Emby 登录令牌 → 用户 %s", user_id)
                except Exception as exc:  # noqa: BLE001 - 记不住不影响登录
                    log.debug("记录 Emby 登录令牌失败: %s", exc)
            out_headers = Headers(
                raw=[
                    (k.encode("latin-1"), v.encode("latin-1"))
                    for k, v in upstream_resp.headers.multi_items()
                    if k.lower() in PASS_HEADERS
                    or k.lower() == "location"
                    or k.lower().startswith("x-emby")
                ]
            )
            async def auth_stream():
                yield body_bytes

            return StreamingResponse(
                auth_stream(),
                status_code=upstream_resp.status_code,
                headers=out_headers,
                media_type=upstream_resp.headers.get("content-type")
                or "application/json",
                background=BackgroundTask(upstream_resp.aclose),
            )

        out_headers = Headers(
            raw=[
                (k.encode("latin-1"), v.encode("latin-1"))
                for k, v in upstream_resp.headers.multi_items()
                if k.lower() in PASS_HEADERS
                or k.lower() == "location"
                or k.lower().startswith("x-emby")
            ]
        )

        async def stream():
            try:
                async for chunk in upstream_resp.aiter_raw():
                    yield chunk
            finally:
                await upstream_resp.aclose()

        return StreamingResponse(
            stream() if request.method != "HEAD" else _empty(),
            status_code=upstream_resp.status_code,
            headers=out_headers,
            background=BackgroundTask(upstream_resp.aclose),
        )

    # ------------------------------------------------------------------ WebSocket
    async def handle_ws(self, websocket: Any, path: str) -> None:
        """把客户端的 WebSocket 原样桥到上游 Emby。

        Emby 的 web 客户端（``/web/index.html``）登录后必须连 ``/embywebsocket``，
        播放状态、遥控、实时刷新全走它。只做 HTTP 反代的话，页面能打开、账户能
        选中，但握手拿不到 upgrade → 一直被拒（浏览器表现为「反代不成功」、
        界面转圈或播放器起不来）。这里用 websockets 建一条上游连接，
        然后双向搬帧。
        """
        if not self.settings.get("enabled", True) or not self.upstream:
            await websocket.close(code=1011)
            return
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError:  # pragma: no cover - uvicorn[standard] 一定带 websockets
            log.warning("缺少 websockets 库，无法转发 Emby 的 WebSocket")
            await websocket.close(code=1011)
            return

        target = self._ws_target(websocket, path)
        subs = [s.strip() for s in
                (self._header(websocket, "sec-websocket-protocol") or "").split(",")
                if s.strip()]
        # websockets 会用自己的 UA 覆盖同名头 —— Emby 靠 UA 认设备（网页版尤其），
        # 必须显式把客户端的 UA 带过去，不然上游看到的是 Python/websockets。
        try:
            upstream = await ws_connect(
                target,
                additional_headers=self._ws_headers(websocket),
                user_agent_header=self._header(websocket, "user-agent") or None,
                subprotocols=subs or None,
                max_size=None,
                # 心跳交给 Emby 自己的 KeepAlive 消息，双端各自 ping 容易互相打断
                ping_interval=None,
                open_timeout=15,
                close_timeout=5,
            )
        except Exception as exc:  # noqa: BLE001 - 上游不可达就断掉，别卡住客户端
            log.warning("Emby WebSocket 上游连接失败 %s: %s", target[:160], exc)
            await websocket.close(code=1011)
            return

        await websocket.accept(subprotocol=upstream.subprotocol)
        log.debug("Emby WebSocket 已桥接: %s", target[:160])
        up_task = asyncio.ensure_future(self._ws_up_to_client(websocket, upstream))
        down_task = asyncio.ensure_future(self._ws_client_to_up(websocket, upstream))
        try:
            done, pending = await asyncio.wait(
                [up_task, down_task], return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            for task in done:
                exc = task.exception() if not task.cancelled() else None
                if exc is not None:
                    log.debug("Emby WebSocket 转发结束: %s", exc)
        except Exception as exc:  # noqa: BLE001 - 客户端断开是常态
            log.debug("Emby WebSocket 转发异常: %s", exc)
        finally:
            for task in (up_task, down_task):
                if not task.done():
                    task.cancel()
            try:
                await upstream.close()
            except Exception as exc:  # noqa: BLE001 - 关不掉也不影响
                log.debug("关闭上游 WebSocket 失败: %s", exc)
            try:
                await websocket.close()
            except Exception as exc:  # noqa: BLE001 - 客户端可能已经走了
                log.debug("关闭客户端 WebSocket 失败: %s", exc)

    @staticmethod
    async def _ws_up_to_client(websocket: Any, upstream: Any) -> None:
        async for message in upstream:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)

    @staticmethod
    async def _ws_client_to_up(websocket: Any, upstream: Any) -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            text = message.get("text")
            if text is not None:
                await upstream.send(text)
                continue
            data = message.get("bytes")
            if data is not None:
                await upstream.send(data)

    def _ws_target(self, websocket: Any, path: str) -> str:
        """上游 ws 地址（保留客户端的路径前缀与查询串）。"""
        scheme = "wss" if self.upstream.startswith("https://") else "ws"
        rest = self.upstream.split("://", 1)[1] if "://" in self.upstream else self.upstream
        url = getattr(websocket, "url", None)
        raw = getattr(url, "path", "") if url is not None else ""
        if not raw:
            raw = "/" + path.lstrip("/")
        query = str(getattr(url, "query", "") or "")
        if not query:
            query = str(getattr(url, "query_string", "") or "")
        return f"{scheme}://{rest}{raw}" + (f"?{query}" if query else "")

    @staticmethod
    def _ws_headers(websocket: Any) -> dict[str, str]:
        """转发客户端头（去掉 WS 握手专有字段，它们由 websockets 重新生成）。"""
        skip = {
            "host", "connection", "upgrade", "sec-websocket-key",
            "sec-websocket-version", "sec-websocket-extensions",
            "sec-websocket-protocol", "sec-websocket-accept", "content-length",
        }
        headers = getattr(websocket, "headers", None) or {}
        try:
            items = headers.items()
        except Exception:  # noqa: BLE001 - 测试桩可能没有 items
            return {}
        return {k: v for k, v in items if k.lower() not in skip}

    @staticmethod
    def _request_path(request: Any, path: str) -> str:
        """要转发给上游的路径。

        优先用请求的原始路径（含客户端用的前缀），这样上游 Emby 的 BaseUrl
        无论配成 ``/`` 还是 ``/emby`` 都能命中；取不到时退回路由里的 path。
        """
        url = getattr(request, "url", None)
        raw = getattr(url, "path", "") if url is not None else ""
        if raw:
            return raw
        return "/" + path.lstrip("/")

    async def _read_json(self, resp: httpx.Response) -> dict[str, Any]:
        try:
            # 流式响应必须先读完才能解析 JSON
            if not resp.is_closed and not getattr(resp, "is_stream_consumed", True):
                await resp.aread()
            return resp.json()
        except Exception:  # noqa: BLE001 - 不是 JSON 就当空响应处理
            return {}

    # ------------------------------------------------------------------ 改写
    def rewrite_playback_info(self, payload: dict[str, Any],
                              request: Request, body: bytes) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        sources = payload.get("MediaSources") or []
        if not isinstance(sources, list):
            return payload

        user_id = self._detect_user(request, body, payload)
        origin = self._client_origin(request)
        changed = 0
        for source in sources:
            if not isinstance(source, dict):
                continue
            path = str(source.get("Path") or "")
            if not path:
                continue
            try:
                url = self.resolve_path(path, user_id, source, origin)
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其余
                log.warning("改写 MediaSource 失败 %s: %s", path[:120], exc)
                continue
            if not url:
                log.debug("PlaybackInfo 未改写: path=%s cloud189=%s strm_rel=%s",
                          path[:120], self.cloud189 is not None,
                          self._strm_rel(path))
                continue
            # 预解析成 115 最终直链，把直链直接发给播放设备
            url = self.direct_link(url, self._header(request, "user-agent"))
            self._apply(source, url)
            changed += 1
            log.info("Emby 播放改写: %s -> %s", path[:100], url[:100])
        if changed:
            # 直接播放优先，别让 Emby 再转码
            payload["PlaySessionId"] = payload.get("PlaySessionId", "")
        return payload

    def _apply(self, source: dict[str, Any], url: str) -> None:
        source["Path"] = url
        source["Protocol"] = "http"
        source["SupportsDirectPlay"] = True
        source["SupportsDirectStream"] = True
        source["DirectStreamUrl"] = url
        source.pop("TranscodingUrl", None)

    @staticmethod
    def _header(request: Any, name: str) -> str:
        """兼容真实 Request / 测试桩的取头方式。"""
        headers = getattr(request, "headers", None)
        if not headers:
            return ""
        try:
            return (headers.get(name) or "").strip()
        except Exception:  # noqa: BLE001 - 头取不到就当没有
            return ""

    def _engine(self, name: str) -> EmbyProxy | Any:
        """按 URL 里的账号前缀取引擎；主引擎就是 self（字段同名可互换）。"""
        if not name or self.hub is None:
            return self
        eng = self.hub.get(name)
        if eng is None:
            raise LookupError(f"没有这个账号: {name}")
        return eng

    def _engine_by_uid(self, uid: str) -> EmbyProxy | Any:
        """按 strm 末尾的 115 用户 ID 取引擎；查不到就用主引擎。

        新生成的 strm 不再带 /a/<账号名>，账号身份完全靠这串 uid。
        """
        if not uid or self.hub is None:
            return self
        eng = self.hub.by_uid(uid)
        return self if eng is None else eng

    # -------------------------------------------------------------- 直链预解析
    def direct_link(self, our_url: str, user_agent: str) -> str:
        """把本服务的播放地址解析成 115 最终直链，直接发给播放设备。

        115 直链与 UA 绑定（f=1 校验「取链 UA == 请求 CDN 的 UA」），
        所以必须用**发起播放的设备的 UA** 去取。取不到时回退本服务地址，
        播放器请求它时仍会 302 / 中转，不影响播放。
        """
        if not user_agent or not OUR_URL_RE.search(our_url):
            return our_url
        key = f"{user_agent}|{our_url}"
        cached = self.direct_cache.get(key)
        if cached:
            return cached
        with self.direct_cache.lock(key):
            cached = self.direct_cache.get(key)
            if cached:
                return cached
            direct = self._direct_uncached(our_url, user_agent)
            if direct:
                log.info("已预解析 115 直链: %s", direct[:100])
                self.direct_cache.put(key, direct)
                return direct
        return our_url

    def _direct_uncached(self, our_url: str, user_agent: str) -> str | None:
        try:
            parts = urlsplit(our_url)
            match = OUR_PATH_RE.match(parts.path)
            if not match:
                return None
            acc, route, tail = (
                match.group(1), match.group(2).lower(), match.group(3)
            )
            # 新 strm 没有 /a/<账号名>：账号身份在尾部那段 uid 上
            eng = self._engine(acc) if acc else self._engine_by_uid(_play_tail_uid(tail))
            if route == "play":
                return self._direct_play(eng, _play_tail_pickcode(tail), user_agent)
            if route == "ed2k":
                key = _split_ext(tail.split("/", 1)[0]).lower()
                return self._direct_ed2k(eng, key, user_agent)
            if route == "share":
                code, _, rest = tail.partition("/")
                fid = rest.split("/", 1)[0] if rest else ""
                if not code or not fid:
                    return None
                return self._direct_share(eng, code, _split_ext(fid), user_agent)
            # instant：/p115[/a/<账号>]/instant/<sha1>.<ext>?account=..&size=..
            account = (parse_qs(parts.query).get("account") or [""])[0]
            if not account or eng.instant is None:
                return None
            sha1 = _split_ext(tail).upper()
            if getattr(eng.cfg, "instant_preresolve", True):
                # 只登记过、还没秒传的文件：这里当场秒传一次再取链。
                # 否则 direct_url 会抛「还没有秒传」，回退成**本服务地址**，
                # 而那个地址（容器内网/宿主机 IP）播放器根本连不上。
                wait = float(getattr(eng.cfg, "instant_preresolve_timeout", 25.0) or 0)
                done, got = _run_bounded(
                    lambda: eng.instant.ensure_direct(
                        sha1, account, user_agent=user_agent
                    ),
                    wait,
                )
                if not done:
                    log.info(
                        "预解析 %s 超过 %.0f 秒未完成（秒传继续在后台跑），"
                        "本次先回退本服务地址", sha1[:12], wait,
                    )
                    return None
                url = got[0]
            else:
                url, _ = eng.instant.direct_url(
                    sha1, account, user_agent=user_agent
                )
            return None if _needs_strict_headers(url) else url
        except Exception as exc:  # noqa: BLE001 - 预解析失败就走回退，别挡播放
            log.info("预解析 115 直链失败（回退本服务地址）: %s: %s",
                     our_url[:100], exc)
            return None

    def _direct_play(self, eng: Any, pickcode: str, user_agent: str) -> str | None:
        if not pickcode:
            return None
        url, _headers = eng.api.download_url(
            pickcode, user_agent=user_agent,
            cache_seconds=eng.cfg.play_cache_seconds,
        )
        return None if _needs_strict_headers(url) else url

    def _direct_ed2k(self, eng: Any, key: str, user_agent: str) -> str | None:
        if eng.ed2k is None or not eng.cfg.ed2k_enabled or not key:
            return None
        # 文件被清理时会在这里等转存（与播放路由行为一致）
        pickcode = eng.ed2k.playable(key)
        return self._direct_play(eng, pickcode, user_agent)

    def _direct_share(self, eng: Any, my_share_code: str, file_id: str,
                      user_agent: str) -> str | None:
        del user_agent  # 分享直链与 UA 无关，仅为签名一致
        rec = eng.db.get_share_by_my(my_share_code)
        if rec is None or not rec.get("my_receive_code") or not file_id.isdigit():
            return None
        dl = eng.api.client.share_download_url({
            "file_id": int(file_id),
            "share_code": my_share_code,
            "receive_code": rec["my_receive_code"],
        })
        url = getattr(dl, "url", None) or (
            dl.get("url") if isinstance(dl, dict) else None
        )
        if not url or _needs_strict_headers(url):
            return None
        return url

    def _detect_user(self, request: Request, body: bytes,
                     payload: dict[str, Any]) -> str:
        """从请求里认出 Emby 用户。

        三条路，按可靠性排：

        1. ``UserId``（PlaybackInfo 的 body / query 都带）—— 最直接；
        2. ``X-Emby-Token`` 反查（Emby 登录时记下的 token → 用户 ID）：
           **播放请求不一定带 UserId**（Emby 客户端只在部分接口带），
           但一定带 token。认不出用户是谁，秒传账号映射就用不上 ——
           参考项目 junction 用的就是这张表；
        3. 都拿不到就当匿名用户（走主账号播放）。
        """
        try:
            if body:
                data = json.loads(body)
                uid = str((data or {}).get("UserId") or "")
                if uid:
                    return uid
        except Exception:  # noqa: BLE001, S110 - body 不是 JSON 时回退到 query 参数
            pass
        params = getattr(request, "query_params", None) or {}
        try:
            uid = params.get("UserId") or ""
        except AttributeError:
            uid = ""
        if uid:
            return uid
        uid = str(payload.get("UserId") or "")
        if uid:
            return uid
        # 没有 UserId：用登录令牌反查
        token = self._request_token(request)
        if token:
            uid = self.db.emby_user_by_token(token)
            if uid:
                return uid
        return ""

    @staticmethod
    def _request_token(request: Request) -> str:
        """取请求里的 Emby 登录令牌。"""
        headers = getattr(request, "headers", None)
        if headers is None:
            return ""
        for key in ("X-Emby-Token", "X-MediaBrowser-Token", "Emby-Token",
                    "X-Emby-Authorization"):
            try:
                value = headers.get(key) or ""
            except AttributeError:
                return ""
            if not value:
                continue
            # Authorization 可能是 "MediaBrowser Token=xxx, Client=..."
            if "=" in value and " " not in value:
                value = value.split("=", 1)[1].strip()
            if value:
                return value.strip()
        return ""

    # ------------------------------------------------------------------ 路径解析
    def resolve_path(self, path: str, user_id: str,
                     source: dict[str, Any], origin: str = "") -> str | None:
        path = path.replace("\\", "/").strip()

        cache_key = f"{user_id}|{origin}|{path}"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        with self.cache.lock(cache_key):
            cached = self.cache.get(cache_key)
            if cached:
                return cached
            url = self._resolve_uncached(path, user_id, source, origin)
            if url:
                self.cache.put(cache_key, url)
            return url

    @staticmethod
    def _client_origin(request: Any) -> str:
        """客户端正在用的那个地址（``scheme://host[:port]``）。

        反代改写出来的播放地址必须用这个 —— 客户端能连到哪里，就用哪个
        地址回连。Emby 客户端填的是反代端口，这里拿到的就是反代端口。
        """
        headers = getattr(request, "headers", None)
        if headers is None:
            return ""
        try:
            host = headers.get("host") or ""
        except AttributeError:
            return ""
        if not host:
            return ""
        scheme = ""
        try:
            scheme = (headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        except AttributeError:
            scheme = ""
        if not scheme:
            try:
                scheme = request.url.scheme or ""
            except AttributeError:
                scheme = ""
        return f"{scheme or 'http'}://{host}"

    def _normalize_origin(self, url: str) -> str:
        """把本服务直链的 origin 换成 PUBLIC_BASE_URL（客户端可达地址）。"""
        return self._swap_origin(url, "")

    def _swap_origin(self, url: str, origin: str = "") -> str:
        """把播放地址的 origin 换成客户端真正访问得到的地址。

        优先用调用方观察到的 ``origin``（客户端请求反代时用的 scheme+Host），
        没有就用 PUBLIC_BASE_URL。天翼（189）的路径也走这里 —— 它的 strm
        内容是 ``<请求头>/p189/play/...``，请求头往往是 docker 网关
        ``172.17.0.1``，局域网播放器连不上。
        """
        base = origin or self.store.base_url
        if not base:
            return url
        try:
            parts = urlsplit(url)
            bparts = urlsplit(base)
        except ValueError:
            return url
        if not bparts.netloc:
            return url
        if (parts.scheme, parts.netloc) == (bparts.scheme, bparts.netloc):
            return url
        fixed = urlunsplit(
            (bparts.scheme, bparts.netloc, parts.path, parts.query, parts.fragment)
        )
        log.info("直链 origin 不可达，已规范化: %s -> %s",
                 f"{parts.scheme}://{parts.netloc}", f"{bparts.scheme}://{bparts.netloc}")
        return fixed

    def _resolve_uncached(self, path: str, user_id: str,
                          source: dict[str, Any], origin: str = "") -> str | None:
        # 0. 天翼云盘（189）：Path 可能已经是播放地址本身 —— Emby 侧的 strm
        #    插件会把 strm 内容当作 MediaSource.Path 返回（并不总是给文件路径），
        #    这时按下面的 strm 文件分支完全命中不了。把 origin 换成客户端正在
        #    访问的地址（反代端口），播放器来反代现取新鲜直链再 302 到天翼 CDN。
        if "/p189/play/" in path:
            return self._swap_origin(path, origin)

        # 0. 本服务自己的直链：strm 内容就是 /p115[/a/<账号>]/... 播放地址，URL 不用改，
        #    但必须强制直连标记 —— 否则客户端拿 Emby 的 /videos/{id}/original.*
        #    相对地址去播，视频流量就从中转（NAS）走了。
        #    另外 strm 可能是旧扫描用 docker 内网地址（如 172.17.0.1）生成的，
        #    客户端访问不到，这里把 origin 规范化成 PUBLIC_BASE_URL。
        if OUR_URL_RE.search(path):
            # **但如果这个用户映射了秒传账号，这里不能就这么返回**：
            # strm 里存的是**源账号**的播放地址，直接给客户端就等于「源盘直出」，
            # 秒传这一步被整个跳过了 —— 用户看到的现象就是「秒传不生效」。
            # 所以要先反查这个 pickcode 对应的文件（拿 sha1/size），能秒传就改写成
            # 秒传路由，让文件先落到用户自己的网盘再播。
            instant_url = self._instant_url_for(path, user_id, source)
            if instant_url:
                return instant_url
            return self._normalize_origin(path)

        # 1. 本服务的 strm（绝对路径匹配 / 挂载前缀匹配）；
        #    多账号时子账号的 strm 在 STRM_ROOT/<账号名>/ 子树，主库查不到
        #    就交给对应子账号的反代解析
        rel = self._strm_rel(path)
        if rel is not None:
            # 1.0 天翼云盘（189）的 strm：内容和 115 一样是「本服务的播放地址」，
            #     但换直链要问 189 的账号（不是 115 的 pickcode），所以单独走一条。
            #     strm 里的请求头通常是 docker 网关（172.17.0.1），局域网播放器
            #     连不上 —— 换成客户端正在用的地址（反代端口），播放器再来这里
            #     现取新鲜直链，302 到天翼 CDN。
            if self.cloud189 is not None:
                played = self.cloud189.strm_play_url(
                    str(self.store.abspath(rel)), origin)
                if played:
                    return played
            url = self._from_strm_rel(rel, user_id)
            if url:
                return url
            if self.hub is not None:
                url = self.hub.resolve_strm_rel(rel, user_id)
                if url:
                    return url

        # 2. pickcode
        match = re.search(r"pick_?code=([0-9a-zA-Z]{6,24})", path, re.IGNORECASE)
        if match:
            return self.store.play_url(match.group(1).lower(), source.get("Name") or "")

        # 3. file id（/115/<id> 或 file_id=）
        match = re.search(r"(?:/115/|[?&]file_?id=)(\d{6,})", path, re.IGNORECASE)
        if match:
            node = self.api.get_node(int(match.group(1)))
            if node is not None and not node["is_dir"]:
                pickcode = self.api.ensure_pickcode(node)
                if pickcode:
                    return self.store.play_url(pickcode, node.get("name") or "")
            return None

        # 4. 自定义规则（可覆盖 123 等其它目录树）
        for compiled, template, is_dbpath in self._rules:
            match = compiled.search(path)
            if not match:
                continue
            expanded = template
            for index, value in enumerate(match.groups(), start=1):
                expanded = expanded.replace(f"${index}", value or "")
            expanded = expanded.replace("{base}", self.store.base_url)
            if is_dbpath:
                rel_path = expanded.removeprefix("dbpath:").strip("/")
                url = self._from_db_path(rel_path, user_id)
                if url:
                    return url
                continue
            return expanded
        return None

    # -------------------------------------------------------------- 秒传改写
    def _instant_url_for(self, url: str, user_id: str,
                         source: dict[str, Any]) -> str | None:
        """strm 里已经是本服务的播放地址时，尝试改写成**秒传**路由。

        strm 的内容是源账号的播放地址：
        ``/p115/play/<pickcode>/<URL 编码的文件名>/<115 用户 ID>``
        （旧格式 ``/p115/a/<账号名>/play/<pickcode>`` 依然认）。
        直接把它交给客户端就等于「源盘直出」—— 秒传这一步被整个跳过，
        文件字节直接从源账号出去。用户看到的现象就是「秒传不生效」。

        所以这里反查这个 pickcode 对应的文件（要 sha1 + size，115 按这两个
        认文件），如果这个用户映射了秒传账号，就改写成秒传路由：
        先把文件秒传进用户**自己**的网盘，再用那个账号的直链播。

        拿不到索引信息（文件不在任何账号的库里 / 缺 sha1）就返回 None，
        交给调用方按原来的地址播 —— 宁可源盘直出，也不要把播放搞坏。
        """
        if self.instant is None or not user_id:
            return None
        # 用户映射到哪个秒传账号；没映射就没有秒传这回事
        account = self.instant.account_for_user(user_id)
        if not account:
            return None
        # 只处理播放路由：ed2k / share / instant 本身已经是最终地址了
        match = OUR_PATH_RE.match(urlsplit(url).path)
        if not match or match.group(2).lower() != "play":
            return None
        tail = match.group(3)
        # 文件原本在哪个账号：老 strm 从 /a/<账号名> 拿，
        # 新 strm 没有那段前缀，账号身份全在尾部的 115 用户 ID 上。
        # **这一步漏了就会去主库里找一个子账号的文件，查不到 → 秒传被静默跳过。**
        source_account = match.group(1) or self._uid_account(_play_tail_uid(tail))
        pickcode = _play_tail_pickcode(tail)
        if not pickcode:
            return None

        # 反查索引：pickcode 属于哪个账号，就去哪个账号的库里找
        found = self._file_by_pickcode(pickcode, source_account)
        if not found:
            return None
        row, source_account = found
        sha1 = str(row.get("sha1") or "").upper()
        size = int(row.get("size") or 0)
        if not sha1 or not size:
            return None
        name = str(row.get("name") or source.get("Name") or "")

        # 登记映射（源 pickcode 供播放时做二次校验取数据），再给秒传路由
        self.instant.register(sha1, size, name, account,
                              str(row.get("pickcode") or pickcode),
                              source_account)
        log.info("Emby 播放改写: 命中秒传路由（用户 %s → 账号 %s，源账号 %s，sha1=%s）",
                 user_id, account, source_account or "默认账号", sha1[:12])
        return self._instant_route(sha1, size, name, account, source_account)

    def _uid_account(self, uid: str) -> str:
        """strm 末尾的 115 用户 ID → 账号名（查不到返回空串 = 主账号）。"""
        uid = str(uid or "").strip()
        if not uid or self.hub is None:
            return ""
        try:
            eng = self.hub.by_uid(uid)
        except Exception as exc:  # noqa: BLE001 - 反查失败就当主账号
            log.debug("按 uid 反查账号失败 %s: %s", uid, exc)
            return ""
        return str(getattr(eng, "name", "") or "") if eng is not None else ""

    def _file_by_pickcode(self, pickcode: str, source_account: str
                          ) -> tuple[dict[str, Any], str] | None:
        """按 pickcode 反查索引行，返回 ``(文件行, 它所属的账号名)``。

        子账号的文件只在自己那个索引库里。账号名判定错（或拿不到）就会
        在主库里空手而归 —— 秒传因此整段跳过，表现成「秒传不成功」。
        所以这里最后再按所有账号的索引兜底找一遍，找到谁算谁。
        """
        if source_account and self.hub is not None:
            eng = self.hub.get(source_account)
            if eng is not None:
                row = eng.db.file_by_pickcode(pickcode)
                if row:
                    return row, source_account
                # 子账号的库里没有就再看主库（同一个 pickcode 不会同时属于两个账号，
                # 但源盘直链可能是主账号生成的）
        row = self.db.file_by_pickcode(pickcode)
        if row:
            # 本库（= 这个反代所属账号的库）里查到，归属就是它自己
            return row, self.source_account()
        if self.hub is not None:
            try:
                engines = list(self.hub.engines().items())
            except Exception:  # noqa: BLE001 - 拿不到账号列表就只查主库
                engines = []
            for name, eng in engines:
                if eng.db is self.db or name == source_account:
                    continue
                try:
                    row = eng.db.file_by_pickcode(pickcode)
                except Exception as exc:  # noqa: BLE001 - 单个库坏了不影响其余
                    log.debug("账号「%s」按 pickcode 查索引失败: %s", name, exc)
                    continue
                if row:
                    return row, name
        return None

    # -------------------------------------------------------------- strm 路径
    def _strm_rel(self, path: str) -> str | None:
        """把 Emby 里的文件路径映射成相对 STRM_ROOT 的路径；不是 strm 则 None。"""
        prefix = str(self.settings.get("strm_prefix") or "").replace("\\", "/").rstrip("/")
        if prefix and path.lower().startswith(prefix.lower()):
            return path[len(prefix):].lstrip("/")
        root = str(self.cfg.strm_root).replace("\\", "/").rstrip("/")
        low = path.lower()
        if low.startswith(root.lower() + "/"):
            return path[len(root):].lstrip("/")
        # Emby 挂载点名字和 STRM_ROOT basename 不一致时，按 basename 切
        base = root.rsplit("/", 1)[-1]
        idx = low.find(f"/{base.lower()}/")
        if idx >= 0:
            return path[idx + len(base) + 2:]
        return None

    def _from_strm_rel(self, rel: str, user_id: str) -> str | None:
        rel = rel.strip("/")
        if not rel.lower().endswith(".strm"):
            return None
        abs_path = str(self.store.abspath(rel))
        # ed2k：稳定路由
        rec = self.db.ed2k_by_strm_path(abs_path)
        if rec:
            return self.store.ed2k_url(rec["key"], rec["name"])
        # 分享：直接 pickcode 播放
        share = self.db.share_by_strm_path(abs_path)
        if share and share.get("pickcode"):
            return self.store.play_url(share["pickcode"], share.get("name") or "")
        # 普通扫描 strm
        file_id = self.db.file_id_by_strm_path(abs_path)
        row = self.db.get_file(file_id) if file_id else None
        if row and row.get("pickcode"):
            return self._play_url_for(row, user_id)
        return None

    def _from_db_path(self, rel_path: str, user_id: str) -> str | None:
        """按 115 相对路径查索引（自定义规则 dbpath: 用）。"""
        rel_path = "/" + rel_path.strip("/")
        rows = self.db._conn().execute(
            "SELECT * FROM files WHERE path = ? OR path = ? LIMIT 1",
            (rel_path, rel_path.lstrip("/")),
        ).fetchall()
        for row in rows:
            item = dict(row)
            if item.get("pickcode"):
                return self._play_url_for(item, user_id)
        return None

    def _play_url_for(self, row: dict[str, Any], user_id: str) -> str | None:
        """普通文件 → 播放地址；该 Emby 用户映射了秒传账号则走秒传路由。"""
        account = self.instant.account_for_user(user_id) if self.instant else ""
        if account:
            sha1 = str(row.get("sha1") or "").upper()
            size = int(row.get("size") or 0)
            if sha1 and size:
                # 源账号 = 这个文件原本所在的账号（主账号或 /a/<名字>/ 子账号）。
                # 取二次校验的数据要用**源账号**的直链 —— 文件在子账号网盘里，
                # 用主账号去取直链会拿不到。
                source_account = self.source_account()
                # 先登记映射（源 pickcode 供播放时做二次校验取数据）
                self.instant.register(sha1, size, row.get("name") or "", account,
                                      row.get("pickcode") or "", source_account)
                return self._instant_route(sha1, size, row.get("name") or "",
                                           account, source_account)
            log.warning(
                "用户 %s 映射了秒传账号「%s」，但文件 %s 缺少 sha1/size，回退默认账号",
                user_id, account, row.get("name"),
            )
        return self.store.play_url(row["pickcode"], row.get("name") or "")

    def source_account(self) -> str:
        """当前这个 proxy 对应的源账号名（空串 = 主账号）。"""
        if self.account:
            return self.account
        # 兜底：老对象没设 account 时按 strm 写出器的 uid 反查
        uid = str(getattr(self.store, "account_uid", "") or "")
        if uid and self.hub is not None:
            eng = self.hub.by_uid(uid)
            if eng is not None:
                return eng.name
        prefix = str(getattr(self.store, "route_prefix", "") or "")
        name = prefix.split("/a/", 1)[1] if "/a/" in prefix else ""
        return name.split("/", 1)[0] if name else ""

    def _instant_route(self, sha1: str, size: int, name: str, account: str,
                       source_account: str = "") -> str:
        ext = ""
        if name and "." in name:
            candidate = name.rsplit(".", 1)[-1].lower()
            if 0 < len(candidate) <= 10:
                ext = "." + candidate
        url = (
            f"{self.store.base_url}/p115"
            f"/instant/{sha1}{ext}"
            f"?account={quote(account)}&size={size}&name={quote(name)}"
        )
        if source_account:
            url += f"&source_account={quote(source_account)}"
        if self.store.auth_token:
            url += f"&token={quote(self.store.auth_token)}"
        return url


async def _empty():
    yield b""


def _dump(payload: Any) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return b"{}"


__all__ = ["EmbyProxy", "LinkCache"]
