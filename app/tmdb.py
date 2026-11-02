"""登录页背景：随机取 TMDB 热门影片的剧照（backdrop）。

两种来源，按优先级：

1. ``TMDB_API_KEY``（v3 key）或 ``TMDB_READ_TOKEN``（v4 Bearer）—— 调
   ``/3/trending/movie/week`` 拿热门片单，取其中的 backdrop 图；
2. ``LOGIN_BACKDROPS`` —— 逗号分隔的图片直链，没有 TMDB key 时也能随机换图。

两者都没有时 ``available`` 为 False，前端保持原来的渐变背景。

国内网络直连 ``api.themoviedb.org`` / ``image.tmdb.org`` 往往 SSL 握手超时
（容器内的流量通常不走宿主机的透明代理），这时把代理填到 ``TMDB_PROXY``
（如 ``http://192.168.1.10:7890``）—— 只有这两类请求走代理，115 接口不受影响。

图片统一由本服务中转（``fetch_image``）：登录页和本服务同源，
不受 CDN 防盗链 / 混合内容影响，客户端也不需要能直连 image.tmdb.org。
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from collections import OrderedDict
from typing import Any

import httpx

log = logging.getLogger("app.tmdb")

API_BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p/original"
ALLOWED_IMG_HOSTS = ("image.tmdb.org",)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


class BackdropSource:
    """热门剧照列表 + 图片字节缓存（进程内，线程安全）。"""

    def __init__(self, api_key: str = "", read_token: str = "",
                 language: str = "zh-CN", ttl: float = 6 * 3600,
                 custom: tuple[str, ...] = (), proxy: str = ""):
        self.api_key = api_key or _env("TMDB_API_KEY")
        self.read_token = read_token or _env("TMDB_READ_TOKEN")
        self.language = language or _env("TMDB_LANGUAGE") or "zh-CN"
        self.ttl = float(ttl)
        # TMDB / 图片 CDN 在部分网络下连不上（SSL 握手超时），
        # 只给这两类请求单独走代理，不影响 115 接口的正常流量。
        self.proxy = (proxy or _env("TMDB_PROXY")).strip()
        extra = _env("LOGIN_BACKDROPS")
        self.custom = tuple(custom) or tuple(
            x.strip() for x in extra.replace(";", ",").split(",") if x.strip()
        )
        self._lock = threading.RLock()
        self._urls: list[str] = []
        self._fetched_at = 0.0
        self._images: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._error = ""

    # ------------------------------------------------------------------ 设置
    def configure(self, api_key: str | None = None, read_token: str | None = None,
                  proxy: str | None = None, custom: Any = None,
                  language: str | None = None) -> None:
        """网页上保存的设置：立刻生效（下一次拉片单就用新 key / 代理）。

        ``None`` = 这一项没被网页改过，保持环境变量带来的值；空字符串 = 明确清空。
        """
        with self._lock:
            if api_key is not None:
                self.api_key = api_key.strip()
            if read_token is not None:
                self.read_token = read_token.strip()
            if proxy is not None:
                self.proxy = proxy.strip()
            if custom is not None:
                items = custom if isinstance(custom, (list, tuple)) else str(custom).split(",")
                self.custom = tuple(
                    str(x).strip() for x in items if str(x).strip()
                )
            if language is not None and language.strip():
                self.language = language.strip()
            # 配置变了就把缓存清掉，否则「改完还是老样子」
            self._urls = []
            self._fetched_at = 0.0
            self._images.clear()
            self._error = ""

    # ------------------------------------------------------------------ 状态
    @property
    def available(self) -> bool:
        return bool(self.api_key or self.read_token or self.custom)

    def _tmdb_ready(self) -> bool:
        return bool(self.api_key or self.read_token)

    # ------------------------------------------------------------------ 片单
    def _headers(self) -> dict[str, str]:
        if self.read_token and not self.api_key:
            return {"Authorization": f"Bearer {self.read_token}"}
        return {}

    def _fetch_tmdb(self, client: httpx.Client, path: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"language": self.language}
        if self.api_key:
            params["api_key"] = self.api_key
        resp = client.get(API_BASE + path, params=params, headers=self._headers())
        resp.raise_for_status()
        data = resp.json()
        return list(data.get("results") or [])

    def _client(self, timeout: float, **kwargs: Any) -> httpx.Client:
        """建一个 Client（按需带 TMDB 专用代理）。"""
        return httpx.Client(timeout=httpx.Timeout(timeout),
                            proxy=self.proxy or None, **kwargs)

    def urls(self, refresh: bool = False) -> list[str]:
        """热门影片的剧照地址（按热度排序，缓存 ttl 秒）。"""
        with self._lock:
            fresh = self._urls and not refresh and (time.time() - self._fetched_at) < self.ttl
            if fresh:
                return self._urls
            found: list[str] = []
            if self._tmdb_ready():
                try:
                    with self._client(10.0) as client:
                        items: list[dict[str, Any]] = []
                        for path in ("/trending/movie/week", "/movie/popular"):
                            try:
                                items = self._fetch_tmdb(client, path)
                            except Exception as exc:  # noqa: BLE001 - 换下一个接口
                                self._error = f"{path}: {exc}"
                                log.warning("TMDB %s 拉取失败: %s", path, exc)
                                continue
                            if items:
                                # 拉到了就说明网络和 key 都正常，清掉上一轮的失败原因
                                self._error = ""
                                break
                        for item in items:
                            path = str(item.get("backdrop_path") or "")
                            if path.startswith("/"):
                                found.append(IMG_BASE + path)
                except Exception as exc:  # noqa: BLE001 - 网络问题不该影响登录
                    self._error = str(exc)
                    log.warning("TMDB 热门剧照获取失败: %s", exc)
            if not found and self.custom:
                found = list(self.custom)
            if found:
                self._urls = list(dict.fromkeys(found))
                self._fetched_at = time.time()
                # 注意：这里**不**清 _error —— 回退到自定义图片时，
                # 「TMDB 为什么没拉到」正是要给人看的诊断信息。
                log.info("登录页背景图库已更新：%d 张", len(self._urls))
            return self._urls

    def random_url(self) -> str:
        items = self.urls()
        if not items:
            return ""
        return random.choice(items)

    def pick(self, index: int = -1) -> str:
        items = self.urls()
        if not items:
            return ""
        if 0 <= index < len(items):
            return items[index]
        return random.choice(items)

    # ------------------------------------------------------------------ 图片
    def fetch_image(self, url: str) -> tuple[bytes, str]:
        """下载（并缓存）一张剧照，返回 (字节, content-type)。"""
        with self._lock:
            hit = self._images.get(url)
            if hit is not None:
                self._images.move_to_end(url)
                return hit
        host = ""
        try:
            host = httpx.URL(url).host or ""
        except Exception:  # noqa: BLE001 - 非法 URL 直接拒
            host = ""
        allowed = host in ALLOWED_IMG_HOSTS or url in self.custom
        if not allowed:
            raise ValueError("不支持的图片地址")
        with self._client(15.0, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            body = resp.content
            ctype = resp.headers.get("content-type", "image/jpeg")
        with self._lock:
            self._images[url] = (body, ctype)
            while len(self._images) > 8:
                self._images.popitem(last=False)
        return body, ctype

    def status(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "tmdb": self._tmdb_ready(),
            "custom": len(self.custom),
            "proxy": bool(self.proxy),
            "count": len(self._urls),
            "error": self._error,
        }

    def settings(self) -> dict[str, Any]:
        """给网页设置表单用（管理员接口，可以回填 key 本身，前端做遮挡）。"""
        return {
            "api_key": self.api_key,
            "read_token": self.read_token,
            "proxy": self.proxy,
            "custom": list(self.custom),
            "language": self.language,
        }
