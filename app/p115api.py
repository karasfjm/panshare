"""对 p115client 的薄封装：登录、列目录、取节点信息、取直链。

这一层刻意保持“宽容”：115 的接口返回字段在不同 app / 版本下键名不一致，
这里用多键回退解析，避免因为某个字段改名而整体失败。
"""

from __future__ import annotations

import errno
import itertools
import logging
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from p115client import P115Client
from p115client.tool.iterdir import iterdir

log = logging.getLogger(__name__)


class P115Error(RuntimeError):
    pass


class DirNotFound(P115Error):
    """115 上找不到这个目录（已删除 / 已移动 / id 不完整）。

    单独一个类型是为了上层能给出「目录不存在」这种明确提示，
    而不是笼统地说「设备接口都不可用」。
    """


class WafBlocked(P115Error):
    """请求被 115 前面的阿里云 WAF 拦了（HTTP 405 + HTML 拦截页）。

    这不是接口下线：同一个接口不带 cookies 请求会返回正常的 JSON，
    带上 cookies 才 405 —— 说明是按账号/频率触发的人机校验。
    正确做法是**退避等待**，而不是换设备接着打（那只会延长封禁）。
    """

    def __init__(self, message: str, retry_after: float = 60.0):
        super().__init__(message)
        self.retry_after = retry_after


def _is_waf_block(exc: BaseException) -> bool:
    """区分「WAF 405」和「接口真的没了」：WAF 拦截页是 HTML。"""
    text = f"{type(exc).__name__}: {exc}"
    return "405" in text and "METHOD NOT ALLOWED" in text.upper()


class RateLimiter:
    """全局请求节流：所有 115 请求共用一个最小间隔。

    p115client 自己的 cooldown 是「每个迭代器」的，多个目录并发时会叠加成
    数倍 QPS，正是这样把账号打成 405 的。这里在 client 入口统一限速。
    """

    def __init__(self, min_interval: float = 0.4):
        self.min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next_at = 0.0

    def set_interval(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_at = now + self.min_interval


def _is_missing_dir(exc: BaseException) -> bool:
    """判断异常是不是「请求的 cid 不是有效目录」。

    p115client 在 115 返回的 cid 与请求不一致时会抛 ``NotADirectoryError``，
    （见 tool/fs_files.py：拿不存在的 cid 列目录，115 会退回根目录，cid 就对不上了）。
    """
    if isinstance(exc, NotADirectoryError):
        return True
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in (
        errno.ENOTDIR,
        errno.ENOENT,
    )


# 列目录的设备/域名链。115 有几个不同的入口，WAF 也是按域名分别限流的，
# 所以一个域名被拦时可以换另一个试试（但别指望换域名能绕过「整体封号」）。
#
# 2026-10 实测踩坑记录（很重要，别再改回去）：
#   最初看到 webapi 的 /files 返回 405，以为是 115 下线了接口；
#   实际上是**请求频率过高触发了阿里云 WAF**——同一接口不带 cookies 返回 200，
#   带 cookies 返回的是 WAF 的 HTML 拦截页。控制好频率（见 RateLimiter）后，
#   webapi 和事件接口都会自己恢复。aps 也会被同样地拦，只是封的是另一个域名。
APP_CHAIN = ("web", "aps", "chrome")

# 分享快照（share/snap）被 WAF 拦时的退避重试：只有 webapi 一个域名，
# 换不了入口，只能等。次数与间隔都要够大分享能递归完。
SHARE_SNAP_ATTEMPTS = 6
SHARE_SNAP_BACKOFF = 5.0

# 离线任务「链接已存在」的错误码
ERR_TASK_EXISTS = 10008


def _first(raw: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in raw and raw[key] not in (None, "", []):
            return raw[key]
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ""):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_node(raw: dict[str, Any]) -> dict[str, Any]:
    """把 115 的原始字段整理成统一结构。"""
    if not isinstance(raw, dict):
        raise P115Error(f"无法解析节点信息: {raw!r}")

    # 有些接口把真正的数据再包一层
    for wrapper in ("data", "file", "info"):
        if isinstance(raw.get(wrapper), dict):
            raw = raw[wrapper]
            break

    is_dir_raw = _first(raw, "is_dir", "is_folder", "folder")
    if is_dir_raw is None:
        fc = _first(raw, "fc", "file_category")
        sha = _first(raw, "sha", "sha1", "file_sha1")
        if fc is not None:
            is_dir_raw = _as_int(fc, 1) == 0
        elif sha is not None:
            is_dir_raw = not sha
        else:
            is_dir_raw = "fid" not in raw and "file_id" not in raw

    if isinstance(is_dir_raw, str):
        is_dir = is_dir_raw.lower() in ("1", "true", "yes")
    else:
        is_dir = bool(is_dir_raw)

    node_id = _as_int(_first(raw, "id", "fid", "file_id", "cid", "category_id"))
    parent_id = _as_int(_first(raw, "parent_id", "pid", "cid", "category_id", "p_cid"))
    if is_dir and parent_id == node_id:
        parent_id = _as_int(_first(raw, "pid", "parent_id"))

    name = str(_first(raw, "name", "file_name", "fn", "n", default="") or "")
    pickcode = str(_first(raw, "pickcode", "pick_code", "pc", default="") or "")

    return {
        "id": node_id,
        "parent_id": parent_id,
        "name": name,
        "is_dir": is_dir,
        "pickcode": pickcode,
        "size": _as_int(_first(raw, "size", "file_size", "fs", "s")),
        "sha1": str(_first(raw, "sha1", "sha", "file_sha1", default="") or ""),
        "ctime": _as_int(_first(raw, "ctime", "ptime", "tp", "create_time")),
        "mtime": _as_int(_first(raw, "mtime", "utime", "te", "update_time")),
    }


def first_dict(payload: Any) -> dict[str, Any] | None:
    """把接口返回的 data 统一成单个节点 dict。

    115 的 files/details 实际返回的是 `{"data": [ {...} ]}`（列表包一层），
    也有接口返回 `{"data": {"list": [...]}}`，这里一并抹平。
    """
    while True:
        if isinstance(payload, (list, tuple)):
            if not payload:
                return None
            payload = payload[0]
            continue
        if not isinstance(payload, dict):
            return None
        moved = False
        for key in ("list", "data", "file", "info"):
            if key not in payload:
                continue
            inner = payload[key]
            if isinstance(inner, (list, tuple, dict)):
                if not inner:
                    return None
                payload = inner[0] if isinstance(inner, (list, tuple)) else inner
                moved = True
                break
        if not moved:
            return payload


class P115API:
    # 所有入口都被 WAF 拦下后静默多久再试（秒）。
    # 连续被封时按 3 倍递增（60 → 180 → 540 → 1620，封顶 1 小时），成功一次就重置。
    _WAF_COOLDOWN = 60.0
    _WAF_COOLDOWN_MAX = 3600.0

    def __init__(self, cookies: str = "", cookie_file: Path | str | None = None,
                 qps: float = 2.0):
        self.cookie_file = Path(cookie_file) if cookie_file else None
        raw = (cookies or "").strip()
        if not raw and self.cookie_file is not None and self.cookie_file.exists():
            try:
                raw = self.cookie_file.read_text(encoding="utf-8").strip()
            except OSError:
                raw = ""
        # 允许「没有 cookies」：首次运行时服务先起来，用户在网页「115 助手」里
        # 粘贴 cookies 添加账号即可。未登录时 authenticated=False，需要登录的
        # 调用会给出明确提示（而不是让整个进程起不来）。
        self.authenticated = bool(raw)
        self.client = P115Client(raw) if raw else P115Client("")
        self._uid = ""
        self._url_cache: dict[str, tuple[str, dict[str, str], float]] = {}
        self._url_lock = threading.Lock()
        # 上一次列目录成功的设备类型：下次优先用它，省掉每回都先撞一次 405
        self._app_hint = ""
        # 被 WAF 限流到这个时刻为止，期间不再发列目录请求
        self._blocked_until = 0.0
        # 连续被拦次数：用来延长冷却时间
        self._waf_streak = 0
        # 全局限流：QPS 太高会被 WAF 按账号拦成 405（实测 20 QPS 十几秒就中招）
        self.limiter = RateLimiter(1.0 / qps if qps > 0 else 0.0)
        self._install_throttle()

    def _install_throttle(self) -> None:
        """给 client.request 套一层全局限速。

        打的是实例属性，p115client 内部所有接口（列目录、取直链、离线任务）
        都会走这里，不用担心漏掉某个并发路径。
        """
        original = self.client.request

        def throttled(*args, **kwargs):
            self.limiter.wait()
            return original(*args, **kwargs)

        self.client.request = throttled  # type: ignore[method-assign]

    # ---------------------------------------------------------------- 基础
    def replace_cookies(self, cookies: str) -> None:
        """换一份 cookies（添加账号 / 更新 cookie 后调用，立即生效）。

        重新建一个 client 并重新装上限速：限速是打在 client 上的，
        换 client 不重装的话新 client 就没限速了。

        传空串等于「退出登录」：回到未登录状态，服务照常运行。
        """
        cookies = (cookies or "").strip()
        client = P115Client(cookies) if cookies else P115Client("")
        self.client = client
        self.authenticated = bool(cookies)
        self._url_cache.clear()
        self._app_hint = ""
        self._blocked_until = 0.0
        self._waf_streak = 0
        self._uid = ""          # 换了 cookies，缓存的 uid 必须丢掉
        self._install_throttle()

    def check_login(self) -> None:
        """用一次最小的目录请求验证 cookies 是否有效。

        不能只用 webapi 的 /files 判断：那个接口 115 已经关了（405），
        换个能用的接口探，否则 cookies 明明是好的也会被判成登录失败。
        """
        if not self.authenticated:
            raise P115Error(
                "还没有 115 cookies：请在网页「115 助手」里粘贴 cookies 添加账号"
            )
        errors: list[str] = []
        for name, call in (
            ("webapi", lambda: self.client.fs_files(
                {"cid": 0, "show_dir": 1, "limit": 1, "offset": 0}
            )),
            ("aps", lambda: self.client.fs_files_aps(
                {"cid": 0, "show_dir": 1, "limit": 1, "offset": 0}
            )),
        ):
            try:
                resp = call()
            except Exception as exc:  # noqa: BLE001 - 换下一个接口
                errors.append(f"{name}: {exc}")
                continue
            if isinstance(resp, dict) and resp.get("state") is not False:
                uid = getattr(self.client, "user_id", None)
                log.info("115 登录校验通过（uid=%s，接口=%s）", uid, name)
                return
            errors.append(f"{name}: {resp}")
        joined = " | ".join(errors)
        if errors and all("405" in item for item in errors):
            raise WafBlocked(f"115 拒绝请求（HTTP 405，多为限流/风控）: {joined}")
        raise P115Error(f"115 cookies 校验失败（所有接口均不可用）: {joined}")

    def user_id(self) -> str:
        """当前 cookies 对应的 115 用户 ID（uid）。

        用途：strm 末尾用它标识「这个 strm 属于哪个账号」，
        播放时按 uid 找到对应账号的引擎取直链。拿不到返回空串
        （调用方回落成默认账号，不影响播放）。
        """
        cached = getattr(self, "_uid", "")
        if cached:
            return cached
        for getter in (lambda: self.client.user_id,
                       lambda: self.client.user_id(),
                       lambda: self.client.user_info().get("user_id", "")):
            try:
                value = getter()
            except Exception:  # noqa: BLE001 - 拿不到就算了
                continue
            if value:
                self._uid = str(value).strip()
                return self._uid
        return ""

    def ensure_pickcode(self, node: dict[str, Any]) -> str:
        """拿到可用于播放地址的 pickcode。

        115 只给文件发 pickcode（字段 `pc`），目录不需要。
        极少数接口不返回 pc，这时用 files/details 单查一次；
        **不要**用本地按 id 推导的算法兜底 —— 实测与 115 现行规则不一致，
        会生成打得开但 404 的 strm。
        """
        pickcode = node.get("pickcode") or ""
        if pickcode or node.get("is_dir"):
            return pickcode
        detail = self.get_node(node["id"])
        if detail and detail.get("pickcode"):
            node["pickcode"] = detail["pickcode"]
            return detail["pickcode"]
        log.warning("节点 %s(%s) 拿不到 pickcode，本次跳过", node.get("id"), node.get("name"))
        return ""

    # ---------------------------------------------------------------- 目录
    def _app_order(self, app: str) -> list[str]:
        """列目录时依次尝试的设备类型。

        顺序：上次成功的 → 调用方指定 → 内置链。
        换 app 之所以有用，是因为不同 app 走不同域名（webapi / aps / proapi），
        而 WAF 是按域名分别限流的。
        """
        order: list[str] = []
        for candidate in (self._app_hint, app, *APP_CHAIN):
            if candidate and candidate not in order:
                order.append(candidate)
        return order

    def iter_dir(self, cid: int, app: str = "web", page_workers: int = 0,
                 cooldown: float = 0.0):
        """逐项产出目录内容（带并发翻页），供扫描器和目录浏览使用。

        115 的接口前面有一层阿里云 WAF：QPS 一高就按账号返回 405 拦截页
        （不带 cookies 请求同一接口返回的是正常 JSON）。WAF 按域名分别限流，
        所以 405 时先换一个入口（webapi → aps）试，全部都被拦才停下来冷却。
        平时靠 RateLimiter 把速率压住，正常情况不会走到这条路。
        """
        blocked = getattr(self, "_blocked_until", 0.0) - time.time()
        if blocked > 0:
            raise WafBlocked(
                f"115 正在限流（还需等待约 {int(blocked)} 秒），跳过本次列目录"
                "（建议把 API_QPS 调到 1、SCAN_WORKERS 调到 1）",
                retry_after=blocked,
            )

        last_error: Exception | None = None
        waf_hit = False
        last_app = ""
        for candidate in self._app_order(app):
            last_app = candidate
            gen = None
            try:
                gen = iterdir(
                    self.client,
                    cid,
                    show_dir=1,
                    cur=1,
                    type=99,
                    app=candidate,
                    max_workers=page_workers,
                    cooldown=cooldown,
                )
                first = next(gen)
            except StopIteration:
                self._app_hint = candidate
                return
            except Exception as exc:
                last_error = exc
                if gen is not None:
                    gen.close()
                if _is_missing_dir(exc):
                    # 换入口也没用：115 认不出这个 cid
                    raise DirNotFound(
                        f"115 上找不到目录 {cid}（可能已被删除、移动，或 id 少了几位）"
                    ) from exc
                if _is_waf_block(exc):
                    waf_hit = True
                    log.warning(
                        "目录 %s 用 app=%s 被 WAF 拦了（HTTP 405，请求太快），换个入口再试",
                        cid, candidate,
                    )
                    continue
                log.warning("目录 %s 用 app=%s 拉取失败，改用备用入口: %s", cid, candidate, exc)
                continue
            self._app_hint = candidate
            self._waf_streak = 0
            if candidate != app:
                log.info("目录 %s 已回退到 app=%s", cid, candidate)
            try:
                for attr in itertools.chain((first,), gen):
                    node = normalize_node(attr)
                    if node["id"]:
                        yield node
            finally:
                gen.close()
            return

        if waf_hit:
            streak = getattr(self, "_waf_streak", 0) + 1
            self._waf_streak = streak
            cooldown = min(
                self._WAF_COOLDOWN * (3 ** (streak - 1)), self._WAF_COOLDOWN_MAX
            )
            self._blocked_until = time.time() + cooldown
            raise WafBlocked(
                f"115 限流中（HTTP 405）：列目录 {cid} 的每个入口都被 WAF 拦了"
                f"（最后试的是 {last_app}），{int(cooldown)} 秒内不再发请求"
                f"（连续第 {streak} 次）。请把 API_QPS 调到 1、SCAN_WORKERS 调到 1；"
                "如果一直不恢复，去 115 网页端重新登录换一份 cookies",
                retry_after=cooldown,
            )
        raise P115Error(f"列目录失败（所有入口都不可用）: {last_error}")

    def list_dir(self, cid: int, app: str = "web", page_workers: int = 0,
                 cooldown: float = 0.0) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """列出单个目录的直接子项，返回 (子目录, 文件)。"""
        dirs: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        for node in self.iter_dir(cid, app=app, page_workers=page_workers, cooldown=cooldown):
            if node["is_dir"]:
                dirs.append(node)
            else:
                node["pickcode"] = self.ensure_pickcode(node)
                files.append(node)
        return dirs, files

    # ---------------------------------------------------------------- 分享快照
    def share_snap(self, payload: dict[str, Any]) -> dict[str, Any]:
        """读分享快照（带 WAF 退避重试）。

        分享接口只有 webapi.115.com 一个域名，没法像列目录那样「换入口」，
        只能靠**降低频率 + 405 时退避重试**。115 前面有阿里云 WAF：
        QPS 一高就返回 405 拦截页（不带 cookies 请求同一接口是正常 JSON）。
        递归遍历大分享时请求很密集，很容易被拦 —— 之前一被拦就整棵遍历中断，
        表现为「只转存了部分内容」。
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                return self.client.share_snap(payload)
            except Exception as exc:
                if not _is_waf_block(exc) or attempts >= SHARE_SNAP_ATTEMPTS:
                    raise
                wait = SHARE_SNAP_BACKOFF * attempts
                log.warning(
                    "分享快照被 WAF 拦了（HTTP 405），%s 秒后重试（第 %d 次）",
                    wait, attempts,
                )
                time.sleep(wait)

    # ---------------------------------------------------------------- 单节点
    def file_by_pickcode(self, pickcode: str) -> dict[str, Any] | None:
        """按 pickcode 查文件信息（含 sha1 / size）。

        用来回答「源盘上这个文件现在是什么样」：115 拒绝一次秒传，往往是
        因为源盘把文件换了 —— 记住的 SHA1 已经过时。这时候重新问一次
        115 拿当前的 SHA1，才能把这一集救回来。
        """
        pickcode = str(pickcode or "").strip()
        if not pickcode:
            return None
        try:
            resp = self.client.fs_supervision({"pickcode": pickcode})
        except Exception as exc:  # noqa: BLE001 - 查不到就当没有
            log.debug("按 pickcode %s 查文件信息失败: %s", pickcode, exc)
            return None
        if not isinstance(resp, dict) or resp.get("state") is False:
            return None
        payload = first_dict(resp.get("data")) or first_dict(resp)
        if not isinstance(payload, dict):
            return None
        return {
            "sha1": str(_first(payload, "sha1", "sha", "file_sha1", default="") or "").upper(),
            "size": _as_int(_first(payload, "size", "file_size", "fs", "s")),
            "name": str(_first(payload, "file_name", "name", "fn", default="") or ""),
            "pickcode": str(_first(payload, "pickcode", "pick_code", "pc", default="") or ""),
        }

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        """按 id 查询文件或目录信息（用于增量事件补全）。"""
        node_id = int(node_id)
        for call in (
            lambda: self.client.fs_file({"file_id": node_id}),
            lambda: self.client.fs_file_skim({"file_id": node_id}),
            lambda: self.client.fs_category_get({"fid": node_id}),
        ):
            try:
                resp = call()
            except Exception:  # noqa: BLE001, S112 - 逐个接口试，失败就换下一个
                continue
            if not isinstance(resp, dict) or resp.get("state") is False:
                continue
            payload = first_dict(resp.get("data")) or first_dict(resp)
            if payload is None:
                continue
            try:
                node = normalize_node(payload)
            except P115Error:
                continue
            if node["id"]:
                return node
        # 删除事件常常查不到，属于正常情况
        return None

    # ---------------------------------------------------------------- 直链
    def _extract_url(self, obj: Any, depth: int = 0) -> tuple[str, dict[str, str]] | None:
        if depth > 6:
            return None
        if isinstance(obj, str):
            if not obj.startswith("http"):
                return None
            headers = getattr(obj, "headers", None)
            # P115URL 是 str 子类但覆写了 __getitem__（切片返回 None），
            # 后续任何 url[...] / split 都会踩坑，这里统一转成普通 str
            return str(obj), dict(headers) if isinstance(headers, dict) else {}
        if isinstance(obj, dict):
            for key in ("url", "download_url", "downurl"):
                value = obj.get(key)
                if isinstance(value, str) and value.startswith("http"):
                    headers = obj.get("headers") or {}
                    return value, dict(headers) if isinstance(headers, dict) else {}
                if isinstance(value, dict):
                    inner = self._extract_url(value, depth + 1)
                    if inner:
                        url, _ = inner
                        headers = value.get("headers") or obj.get("headers") or {}
                        return url, dict(headers) if isinstance(headers, dict) else {}
            for value in obj.values():
                inner = self._extract_url(value, depth + 1)
                if inner:
                    return inner
        if isinstance(obj, (list, tuple)):
            for value in obj:
                inner = self._extract_url(value, depth + 1)
                if inner:
                    return inner
        return None

    def download_url(self, pickcode: str, user_agent: str = "",
                     cache_seconds: int = 600) -> tuple[str, dict[str, str]]:
        """拿一个可播放直链，带短时缓存。返回 (url, headers)。

        115 直链（f=1）要求「取链接时用的 UA」和「请求 CDN 的 UA」一致，
        缓存键必须带上 UA —— 播放路由会用请求方的 UA 取链接再 302。
        """
        now = time.time()
        ua = user_agent or (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        key = f"{pickcode}|{ua}"
        with self._url_lock:
            cached = self._url_cache.get(key)
            if cached and cached[2] > now:
                return cached[0], cached[1]

        candidates = (
            lambda: self.client.download_url(pickcode, user_agent=ua),
            lambda: self.client.download_url_app(pickcode, user_agent=ua, app="chrome"),
            lambda: self.client.download_url_web(pickcode, user_agent=ua),
        )
        errors: list[str] = []
        for call in candidates:
            try:
                resp = call()
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
                continue
            found = self._extract_url(resp)
            if found:
                url, headers = found
                url = str(url)
                with self._url_lock:
                    self._url_cache[key] = (url, headers, now + max(30, cache_seconds))
                return url, headers
            errors.append(f"响应中未找到直链: {str(resp)[:200]}")
        raise P115Error("获取下载直链失败: " + " | ".join(errors))

    # ---------------------------------------------------------------- 离线下载
    def cloud_task(self, info_hash: str) -> dict[str, Any]:
        """查单个离线任务；不存在时返回 {"state": False}，不抛异常。"""
        try:
            resp = self.client.clouddownload_task({"info_hash": info_hash})
        except Exception as exc:
            raise P115Error(f"查询离线任务失败: {exc}") from exc
        return resp if isinstance(resp, dict) else {}

    def cloud_add(self, url: str, dir_cid: int, info_hash: str = "") -> dict[str, Any]:
        """提交离线任务（保存到 dir_cid）。

        115 按 info_hash 去重，重复链接会返回 10008。这时先删掉旧的任务记录
        再提交一次：任务记录不等于文件，删记录不会动已经转存好的文件，
        而「清理后又想再看」正是需要重新提交的场景。
        """
        payload = {"url": url, "wp_path_id": int(dir_cid)}

        def submit() -> dict[str, Any]:
            try:
                resp = self.client.clouddownload_task_add_url(payload)
            except Exception as exc:
                raise P115Error(f"提交离线任务失败: {exc}") from exc
            return resp if isinstance(resp, dict) else {}

        resp = submit()
        if _as_int(resp.get("errcode")) == ERR_TASK_EXISTS and info_hash:
            log.info("离线任务已存在，删除旧记录后重新提交: %s", info_hash)
            self.cloud_del(info_hash)
            time.sleep(1.0)
            resp = submit()
        ok = resp.get("state") is not False and _as_int(resp.get("errcode")) == 0
        return {
            "ok": ok,
            "message": str(resp.get("error_msg") or ""),
            "errcode": _as_int(resp.get("errcode")),
        }

    def cloud_del(self, info_hash: str) -> bool:
        if not info_hash:
            return False
        try:
            resp = self.client.clouddownload_task_del(info_hash)
        except Exception as exc:  # noqa: BLE001
            log.warning("删除离线任务 %s 失败: %s", info_hash, exc)
            return False
        return isinstance(resp, dict) and resp.get("state") is not False

    def delete_files(self, fids: Iterable[int]) -> bool:
        ids = [int(f) for f in fids if _as_int(f)]
        if not ids:
            return True
        try:
            resp = self.client.fs_delete(ids)
        except Exception as exc:
            raise P115Error(f"删除 115 文件失败: {exc}") from exc
        return isinstance(resp, dict) and resp.get("state") is not False

    def mkdir(self, parent_id: int, name: str) -> int:
        try:
            resp = self.client.fs_mkdir({"pid": int(parent_id), "cname": name})
        except Exception as exc:
            raise P115Error(f"创建 115 目录失败: {exc}") from exc
        cid = _as_int((resp or {}).get("cid") or (resp or {}).get("file_id"))
        if not isinstance(resp, dict) or resp.get("state") is False or not cid:
            raise P115Error(f"创建 115 目录失败: {resp}")
        return cid

    def find_child_dir(self, parent_id: int, name: str) -> int | None:
        """在 parent_id 下按名字找一个子目录。"""
        for node in self.iter_dir(parent_id, app="web", page_workers=0, cooldown=0.0):
            if node["is_dir"] and node["name"] == name:
                return int(node["id"])
        return None

    def copy_file(self, file_id: int, parent_id: int,
                  cooldown: float = 0.0) -> dict[str, Any] | None:
        """把文件复制到 parent_id（通常是它自己所在的目录）。

        115 没有「复制成新文件并告诉我 id」的原子接口：``fs_copy`` 只回一个
        是否成功的状态，新文件的 id 要自己再翻一次目录。同目录复制时 115 会
        自动改名成 ``xxx(1).mkv``，所以这里用「复制前后目录 id 的差集」找新文件，
        不能靠名字猜。
        """
        parent_id = int(parent_id)
        before = {int(n["id"]) for n in self.list_dir(
            parent_id, page_workers=0, cooldown=cooldown)[1]}
        try:
            resp = self.client.fs_copy([int(file_id)], pid=parent_id)
        except Exception as exc:
            raise P115Error(f"复制 115 文件失败: {exc}") from exc
        if isinstance(resp, dict) and resp.get("state") is False:
            raise P115Error(f"复制 115 文件失败: {resp}")

        # 115 建副本是异步的：偶发要过一两秒才在目录里出现，这里重试几次
        for attempt in range(6):
            files = self.list_dir(parent_id, page_workers=0, cooldown=cooldown)[1]
            fresh = [f for f in files if int(f["id"]) not in before]
            if fresh:
                node = max(fresh, key=lambda f: int(f.get("id") or 0))
                pickcode = node.get("pickcode") or self.ensure_pickcode(node)
                return {
                    "file_id": int(node["id"]),
                    "pickcode": pickcode,
                    "name": node.get("name") or "",
                    "size": int(node.get("size") or 0),
                }
            if attempt < 5:
                time.sleep(0.8)
        log.warning("115 复制成功但目录里没找到新文件: %s", file_id)
        return None


def cloud_state(task: dict[str, Any]) -> tuple[str, int, str]:
    """把离线任务的返回统一成 (状态, 进度, 说明)。

    状态取 missing / running / done / failed。
    注意：任务列表接口和单任务接口的 `status` 数字并不一致
    （同一个已完成的 ed2k，列表里是 2、单查是 1），只认 display_status / 文字。
    """
    if not task or task.get("state") is False:
        return "missing", 0, str(task.get("error_msg") or "")
    display = str(task.get("display_status") or "").lower()
    text = str(task.get("status_text") or "")
    percent = _as_int(task.get("percentDone") or task.get("display_percent"))
    if display in ("finished", "done", "success") or text in ("下载成功", "已完成"):
        return "done", 100, text or "下载成功"
    if display in ("failed", "error", "fail") or "失败" in text:
        return "failed", percent, text or "下载失败"
    return "running", percent, text or "转存中"


__all__ = [
    "P115API", "DirNotFound", "P115Error", "RateLimiter", "WafBlocked",
    "cloud_state", "normalize_node",
]
