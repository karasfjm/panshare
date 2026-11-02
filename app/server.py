"""HTTP 服务：strm 播放地址（302 / 代理）、状态页、目录管理、手动触发接口。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from .auth import COOKIE_NAME, AuthManager
from .config import Config
from .copies import CopyManager
from .db import Database, strm_config_cids, strm_config_pairs, strm_config_paths
from .ed2k import (
    Ed2kError,
    Ed2kManager,
    Ed2kNotFound,
    Ed2kNotReady,
)
from .emby import EmbyProxy, fetch_public_users
from .instant import InstantError, InstantManager
from .p115api import P115API, DirNotFound, P115Error, WafBlocked
from .plugins import PluginError, PluginManager
from .scanner import Scanner
from .scheduler import StrmConfigScheduler
from .share import ShareError, ShareManager
from .strm import StrmStore, rel_join, safe_component
from .tmdb import BackdropSource
from .watcher import Watcher

log = logging.getLogger(__name__)

# 前端静态资源目录（web/index.html + web/js/*）
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# 115 STRM 配置的默认扩展名：命中这些后缀才生成 strm
DEFAULT_MEDIA_EXTS = "mp4,mkv,ts,iso,rmvb,avi,mov,wmv,m2ts"
DEFAULT_META_EXTS = "nfo,jpg,jpeg,png,srt,ass,sub,idx"


def _web_stamp() -> str:
    """前端资源的最新修改时间（秒），用于静态资源 URL 的版本号。"""
    newest = 0.0
    try:
        for path in WEB_DIR.rglob("*"):
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
    except OSError:
        pass
    return str(int(newest))


class _Static(StaticFiles):
    """静态资源：带 ETag 校验，但每次都回源确认（升级镜像后不会拿到旧 JS）。"""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        return response

# 代理时透传的响应头
PASS_HEADERS = {
    "content-type", "content-length", "content-range", "accept-ranges",
    "last-modified", "etag", "content-disposition", "cache-control",
}


def _split_pickcode(value: str) -> str:
    value = value.strip()
    if "." in value:
        head, _, tail = value.rpartition(".")
        if tail and len(tail) <= 10 and head:
            return head
    return value


def _needs_headers(url: str) -> bool:
    """115 直链的 f 参数：1 要求 UA，3 要求 UA + Cookie。"""
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return False
    return (query.get("f") or [""])[0] in ("1", "3")


def _needs_strict_headers(url: str) -> bool:
    """302 无法满足的头要求：目前只有 f=3（除了 UA 还要 Cookie）。

    f=1 只要求 UA，播放路由已经用请求方的 UA 取的链接，302 过去 UA 一致。
    """
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return False
    return (query.get("f") or [""])[0] == "3"


def _parse_dir_input(text: str) -> int:
    """把「目录 id」或「115 目录链接」解析成 cid。

    支持：``3027523183514719412``、``https://115.com/?cid=3027...``、
    以及直接粘一整段含 cid 的链接。空字符串返回 0（表示不修改）。
    """
    value = (text or "").strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    try:
        params = parse_qs(urlparse(value).query)
    except ValueError:
        params = {}
    for key in ("cid", "folder_id", "fid"):
        for item in params.get(key, []):
            if item.isdigit():
                return int(item)
    digits = re.findall(r"\d{10,}", value)
    if len(digits) == 1:
        return int(digits[0])
    raise ValueError(f"无法从「{value}」里识别出 115 目录 id，请直接填纯数字 id")


def _ancestor_ids(api: P115API, cid: int, limit: int = 64) -> set[int]:
    """一路向上收集祖先目录 id（含 0），用于判断两个目录的包含关系。"""
    ancestors: set[int] = set()
    current = int(cid)
    for _ in range(limit):
        if not current:
            ancestors.add(0)
            break
        try:
            node = api.get_node(current)
        except Exception:  # noqa: BLE001 - 查询失败按"无祖先"处理
            break
        if node is None:
            break
        parent = int(node.get("parent_id") or 0)
        ancestors.add(parent)
        current = parent
    return ancestors


def _as_list(value: Any) -> list[str]:
    """把「逗号分隔字符串 / 数组」统一成列表（网盘路径这类多值字段用）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [x.strip() for x in re.split(r"[,，;；\n]+", str(value)) if x.strip()]


def _local_prefix(local_dir: str, root_name: str = "strm") -> str:
    """把「本地 STRM 目录」规范化成相对 STRM_ROOT 的前缀。

    用户填的往往是容器里的挂载路径（``/strm``、``/strm/115-原档``），
    而 STRM_ROOT 本身就是 ``/strm``；直接 strip 掉斜杠会得到
    ``strm/115-原档``，文件就落到 ``/strm/strm/115-原档`` 里去了。
    这里把首段的 ``strm``（= STRM_ROOT 目录名）识别出来丢掉。
    """
    parts = [p for p in str(local_dir or "").replace("\\", "/").split("/") if p]
    if parts and parts[0] == (root_name or "strm"):
        parts = parts[1:]
    return "/".join(parts)


def _strm_config_jobs(row: dict[str, Any],
                      root_name: str = "strm") -> list[tuple[int, str, str]]:
    """一条 115 STRM 配置要扫的目录：``[(网盘 cid, 网盘路径, 本地前缀), ...]``。

    本地前缀完全由「本地 STRM 目录」决定，**不再自动加账号名前缀**。
    新结构是「一个网盘路径绑定一个目录」，每条用自己的目录；老配置
    （多个路径共用一个目录）没法区分，才按 115 目录名各自加一层子目录，
    否则同名文件会互相覆盖。
    """
    pairs = strm_config_pairs(row)
    fallback = str(row.get("name") or "")
    prefixes = [
        _local_prefix(p.get("dir") or fallback, root_name) for p in pairs
    ]
    # 有两条以上的路径却指向同一个目录 = 老结构，需要靠子目录区分
    share_dir = len(prefixes) > 1 and len(set(prefixes)) < len(prefixes)
    jobs: list[tuple[int, str, str]] = []
    for item, prefix in zip(pairs, prefixes):
        path = str(item.get("path") or "").strip()
        if share_dir:
            leaf = safe_component(path.rstrip("/").rsplit("/", 1)[-1] or item["cid"])
            prefix = rel_join(prefix, leaf)
        jobs.append((int(item["cid"]), path, prefix))
    return jobs


# ---------------------------------------------------------------------------
# 播放处理（模块级）：主账号路由和多账号路由 /p115/a/<名字>/... 共用
# ---------------------------------------------------------------------------
def _pickcode_from_tail(tail: str) -> str:
    """从 play 路由的尾部取出 pickcode。

    新格式：``<pickcode>/<URL 编码的文件名>/<115 文件 id>``；
    旧格式：``<pickcode>.mkv``（扫描时代没带文件名的那种）也继续认。
    """
    first = (tail or "").lstrip("/").split("/", 1)[0].strip()
    head, _, ext = first.rpartition(".")
    if head and ext and len(ext) <= 10:
        return head
    return first


def _play_tail_uid(tail: str) -> str:
    """从 play 路由尾部取出「115 用户 ID」（整串的最后一段，纯数字）。

    新格式：``<pickcode>/<URL 编码的文件名>/<uid>``。
    旧格式 ``<pickcode>.mkv`` 或 ``<pickcode>/<name>/<115 文件 id>`` 里
    最后一段也是数字，但那种 id 通常查不到账号 —— 查不到就回落默认账号，
    不影响播放（老 strm 下次全量会重写成新格式）。
    """
    parts = [p for p in (tail or "").lstrip("/").split("/") if p]
    if len(parts) < 2:
        return ""
    last = parts[-1].strip()
    return last if last.isdigit() else ""


def _check_play_token(cfg: Config, request: Request) -> None:
    """播放路由的鉴权：只认 AUTH_TOKEN（播放器没有 Cookie）。"""
    if not cfg.auth_token:
        return
    supplied = (
        request.query_params.get("token", "")
        or request.headers.get("x-token", "")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    if supplied != cfg.auth_token:
        raise HTTPException(status_code=401, detail="invalid token")


def _serve_cloud189(cfg: Config, cloud189: Any,
                    request: Request, tail: str):
    """天翼云盘播放入口（主服务端口与 Emby 反代端口共用）。

    支持三种 strm 内容（老版本继续可用）：

    * ``/p189/play/<fileId>/<名字>``            —— 老格式，账号靠索引反查
    * ``/p189/play/<账号>/<fileId>/<名字>``     —— 多账号
    * ``/p189/play/<账号>/fam/<家庭id>/<fileId>`` —— 家庭云

    每次请求都现取直链（有 120s 缓存），再按 PLAY_MODE 302 / 中转 ——
    天翼直链带签名且 13 分钟过期，缓存下来发给播放器会在拖动进度条时 403。
    """
    # 天翼云盘是插件：只有插件在跑才会走到这里，所以这里再 import 也不会白付代价
    from .cloud189 import PLAY_FAMILY, Cloud189Error

    _check_play_token(cfg, request)
    segs = [x for x in (tail or "").split("/") if x]
    if not segs:
        raise HTTPException(status_code=404, detail="缺少文件 id")
    account_id, family_id = "", ""
    if cloud189.is_account(segs[0]):
        account_id = segs.pop(0)
    if len(segs) >= 3 and segs[0] == PLAY_FAMILY:
        family_id = segs[1]
        file_id = segs[2]
    else:
        file_id = segs[0] if segs else ""
    if not file_id:
        raise HTTPException(status_code=404, detail="缺少文件 id")
    try:
        url = cloud189.resolve_link(file_id, account_id, family_id)
    except Cloud189Error as exc:
        raise HTTPException(status_code=502,
                            detail=f"天翼云盘直链解析失败: {exc}") from exc
    return _serve_direct(cfg, url, {}, request)


def _serve_direct(cfg: Config, url: str, headers: dict[str, str], request: Request):
    """按 PLAY_MODE 把直链返回给播放器（302 或容器中转）。"""
    if cfg.play_mode == "302":
        # 直链带 f=1/f=3 时，115 要求请求方携带指定 UA（f=3 还要 Cookie），
        # 302 无法把这些头带过去，会自动降级为本容器中转。
        if _needs_headers(url):
            log.debug("直链要求请求头，自动改用代理模式")
        else:
            return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-cache"})

    upstream_headers = {"User-Agent": cfg.play_user_agent}
    upstream_headers.update({k: v for k, v in (headers or {}).items() if k.lower() != "host"})
    range_header = request.headers.get("range")
    if range_header:
        upstream_headers["Range"] = range_header

    client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))
    try:
        req = client.build_request("GET", url, headers=upstream_headers)
        resp = client.send(req, stream=True)
    except Exception as exc:
        client.close()
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() in PASS_HEADERS}
    media_type = resp.headers.get("content-type", "application/octet-stream")

    def cleanup() -> None:
        try:
            resp.close()
        finally:
            client.close()

    if request.method == "HEAD":
        cleanup()
        return Response(status_code=resp.status_code, headers=out_headers, media_type=media_type)

    return StreamingResponse(
        resp.iter_raw(),
        status_code=resp.status_code,
        headers=out_headers,
        media_type=media_type,
        background=BackgroundTask(cleanup),
    )


def _serve_pickcode(cfg: Config, api: P115API, pickcode: str, request: Request):
    """拿直链并按 PLAY_MODE 返回 302 或中转。

    115 直链（f=1）校验「取链接时的 UA」==「请求 CDN 的 UA」，所以 302
    模式下要用**请求方的 UA** 去取链接，302 过去才能播放。只有 f=3
    （还要 Cookie）等 302 带不过去的场景才回退到固定 UA + 容器中转。
    """
    client_ua = (request.headers.get("user-agent") or "").strip()
    if cfg.play_mode == "302" and client_ua:
        try:
            url, _ = api.download_url(
                pickcode, user_agent=client_ua, cache_seconds=cfg.play_cache_seconds
            )
        except P115Error as exc:
            log.warning("按请求方 UA 取直链失败 %s: %s", pickcode, exc)
        else:
            if not _needs_strict_headers(url):
                return RedirectResponse(
                    url, status_code=302, headers={"Cache-Control": "no-cache"}
                )
            log.debug("直链需要 Cookie（f=3），回退中转 %s", pickcode)
    try:
        url, headers = api.download_url(
            pickcode, user_agent=cfg.play_user_agent, cache_seconds=cfg.play_cache_seconds
        )
    except P115Error as exc:
        log.warning("取直链失败 %s: %s", pickcode, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _serve_direct(cfg, url, headers, request)


def _play_pickcode(cfg: Config, api: P115API, pickcode: str, request: Request,
                   copies: Any | None = None):
    """strm 播放入口：必要时换成本次播放专属的副本 pickcode。"""
    pickcode = _split_pickcode(pickcode)
    if not pickcode:
        raise HTTPException(status_code=400, detail="empty pickcode")
    if copies is not None:
        # 同播复制是账号级能力：同一台设备用主文件，第二台起各用一份副本
        pickcode = copies.pick(pickcode, _client_fingerprint(request))
    return _serve_pickcode(cfg, api, pickcode, request)


def _client_fingerprint(request: Request) -> str:
    """播放设备指纹（来源 IP + UA），用于判断「是不是另一台设备」。

    Direct Play 时播放器会拿着 strm 里的地址直接来请求本服务，IP/UA 就是设备
    自己的，能区分开；如果是 Emby 服务端拉流转码，所有请求都来自 Emby 容器，
    指纹天然相同 —— 那种情况本服务确实认不出是谁在看，也就不会复制。
    """
    ip = ""
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    if not ip:
        ip = (request.client.host if request.client else "") or ""
    ua = (request.headers.get("user-agent") or "").strip()
    return f"{ip}|{ua}"[:200]


def _play_ed2k(cfg: Config, ed2k: Any, key: str, request: Request,
               copies: Any | None = None):
    """ed2k 的稳定播放入口：文件在就 302，不在就先转存再 302。"""
    if ed2k is None or not cfg.ed2k_enabled:
        raise HTTPException(status_code=404, detail="ed2k 功能未启用")
    key = _split_pickcode(key).lower()
    try:
        pickcode = ed2k.playable(key)
    except Ed2kNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Ed2kNotReady as exc:
        raise HTTPException(
            status_code=503, detail=str(exc), headers={"Retry-After": "10"}
        ) from exc
    except Ed2kError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if copies is not None:
        rec = ed2k.db.get_ed2k(key) or {}
        pickcode = copies.pick(
            pickcode, _client_fingerprint(request),
            file_id=int(rec.get("file_id") or 0), name=rec.get("name") or "",
        )
    return _serve_pickcode(cfg, ed2k.api, pickcode, request)


# Emby 用户列表缓存：60 秒内重复打开页面不重复请求 Emby
_EMBY_USER_CACHE: dict[str, Any] = {"at": 0.0, "url": "", "rows": []}


def _emby_all_users(cfg: Any, force: bool = False) -> list[dict[str, Any]]:
    """Emby 上的全部用户（带 60 秒缓存，``force=True`` 强刷）。"""
    url = str(getattr(cfg, "emby_url", "") or "")
    now = time.time()
    cache = _EMBY_USER_CACHE
    if not force and cache["url"] == url and now - float(cache["at"]) < 60:
        return list(cache["rows"])
    rows = fetch_public_users(url) if url else []
    _EMBY_USER_CACHE.update({"at": now, "url": url, "rows": rows})
    return list(rows)


def _known_emby_users(db: Any, cfg: Any = None, force: bool = False) -> list[dict[str, Any]]:
    """Emby 用户列表（供秒传页做下拉选择）。

    来源（合并去重，前者优先保留名字）：
    ① **Emby 服务本身**：``GET {EMBY_URL}/Users/Public`` —— 这是登录页列用户
       用的接口，不需要 API Key，也不需要谁先登录一次。以前下拉框里只有
       「登录过的用户」，就是因为没走这个接口；
    ② 已在秒传映射里的（名字更可信）；
    ③ 通过反代学到过 token 的（Emby 客户端播放过就会留下记录）。
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        # ① 先从 Emby 拉全量：拿不到（没配地址 / 连不上）就静默跳过
        for item in (_emby_all_users(cfg, force) if cfg is not None else []):
            uid = str(item.get("id") or "")
            if not uid:
                continue
            out[uid] = {
                "id": uid,
                "name": str(item.get("name") or ""),
                "mapped": False,
                "source": "emby",
                "has_password": bool(item.get("has_password")),
            }
        # ② 已映射的：名字以映射表为准，标 mapped
        for rec in db.list_emby_users():
            uid = str(rec.get("emby_user_id") or "")
            if not uid:
                continue
            row = out.get(uid) or {"id": uid, "name": "", "source": "mapped"}
            row["name"] = str(rec.get("emby_user_name") or "") or row.get("name", "")
            row["mapped"] = bool(rec.get("account"))
            out[uid] = row
        # ③ 学到过 token 但不在上面两处的（Emby 地址不可达时的兜底）
        for uid in db.list_emby_tokens().values():
            if not uid:
                continue
            row = out.get(uid) or {"id": uid, "name": "", "mapped": False}
            row["seen"] = True
            out[uid] = row
    except Exception:  # noqa: BLE001 - 查不到就返回空
        return []
    # 有名字的排前面，按名字排序，下拉框看着才顺
    return sorted(out.values(), key=lambda r: (not r.get("name"), str(r.get("name") or "")))


def _play_instant(cfg: Config, instant: Any, sha1: str, account: str,
                  request: Request, source_account: str = ""):
    """秒传播放：把文件秒传到指定账号后用它的直链播放。

    ``source_account`` = 文件原本所在的账号（115 STRM 的文件可能在子账号下）。
    取二次校验的数据要用**源账号**的直链 —— 用主账号去取会拿不到
    （文件不在主账号的网盘里）。
    """
    if instant is None:
        raise HTTPException(status_code=404, detail="秒传功能不可用")
    sha1 = sha1.strip().split(".")[0].upper()
    account = (account or "").strip()
    if not account:
        raise HTTPException(status_code=400, detail="缺少 account 参数")
    rec = instant.lookup(sha1, account)
    if rec is None:
        raise HTTPException(
            status_code=404,
            detail="还没有这个文件的秒传记录（通过 Emby 反代播放一次即可登记）",
        )
    # 记录里存的源账号优先（登记时写的更准），URL 参数是兜底
    src = str(rec.get("source_account") or source_account or "")
    try:
        rec = instant.resolve(rec["sha1"], rec["size"], rec["name"], account,
                              rec["source_pickcode"], src)
        url, headers = instant.direct_url(sha1, account)
    except (InstantError, P115Error) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log.info("秒传播放 %s（源 %s → 账号 %s，pickcode=%s）",
             rec.get("name") or sha1[:12], src or "主账号", account,
             rec.get("pickcode"))
    return _serve_direct(cfg, url, headers, request)



def _play_share(cfg: Config, share: Any, db: Database, api: P115API,
                my_share_code: str, file_id: str, request: Request):
    """分享的播放入口：从分享里解析该文件的直链。

    两种记录都走这里：

    * 转存模式：strm 里是**自己的**分享码，用 ``my_receive_code`` 解析；
    * 直接生成模式：strm 里是**原分享**的分享码，用它的 ``receive_code`` 解析
      （文件一直在别人网盘里，我们只是拿它的下载通道）。
    """
    if share is None or not cfg.share_enabled:
        raise HTTPException(status_code=404, detail="分享功能未启用")
    rec = db.get_share_by_my(my_share_code)
    if rec is not None and rec.get("my_receive_code"):
        share_code = my_share_code
        receive_code = str(rec["my_receive_code"])
    else:
        direct = db.get_share(my_share_code)
        if direct is None or not int(direct.get("direct") or 0):
            raise HTTPException(
                status_code=404,
                detail=f"没有这个分享记录: {my_share_code}（可能被删除，或由旧版本生成）",
            )
        rec = direct
        share_code = str(rec["share_code"])
        receive_code = str(rec.get("receive_code") or "")
    fid = file_id.strip().split(".")[0]
    if not fid.isdigit():
        raise HTTPException(status_code=400, detail=f"无效的文件 id: {file_id}")
    try:
        dl = api.client.share_download_url({
            "file_id": int(fid),
            "share_code": share_code,
            "receive_code": receive_code,
        })
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"分享直链解析失败: {exc}"
        ) from exc
    url = getattr(dl, "url", None) or (dl.get("url") if isinstance(dl, dict) else None)
    headers = dict(getattr(dl, "headers", None) or
                   ((dl or {}).get("headers") if isinstance(dl, dict) else None) or {})
    if not url:
        raise HTTPException(status_code=502, detail=f"分享直链解析失败: {dl}")
    return _serve_direct(cfg, url, headers, request)


def create_app(cfg: Config, db: Database, api: P115API, store: StrmStore,
               scanner: Scanner, watcher: Watcher,
               state: dict[str, Any], ed2k: Ed2kManager | None = None,
               share: ShareManager | None = None,
               instant: InstantManager | None = None,
               emby: EmbyProxy | None = None,
               hub: Any | None = None, spa_root: bool = True,
               plugins: PluginManager | None = None,
               copies: Any | None = None) -> FastAPI:
    app = FastAPI(title="PanShare", docs_url=None, redoc_url=None)
    # 同播复制（账号级）：主账号没有现成实例时自己建一个，
    # 单账号模式 / 测试都走这条路径
    if copies is None:
        copies = CopyManager(db, api, cfg)
    if ed2k is not None and getattr(ed2k, "copies", None) is None:
        ed2k.copies = copies
    lock = threading.Lock()
    auth = AuthManager(cfg.web_username, cfg.web_password, cfg.web_session_ttl)
    # 登录页背景：随机 TMDB 热门影片剧照（没配 key 时保持渐变，不影响登录）
    backdrop = BackdropSource()

    def load_backdrop_settings() -> None:
        """把网页上保存过的背景配置套上去（key / 代理 / 自定义直链）。

        环境变量仍然是基础值：网页里没动过的项保持 None，不会被清空。
        """
        raw = db.get_meta("login_backdrop", "")
        if not raw:
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("保存的登录页背景配置无法解析，已忽略")
            return
        if not isinstance(data, dict):
            return
        backdrop.configure(
            # 缺这个键 → None = 保持环境变量带来的值；空字符串 = 明确清空
            api_key=data.get("api_key"),
            read_token=data.get("read_token"),
            proxy=data.get("proxy"),
            custom=data.get("custom"),
            language=data.get("language"),
        )

    load_backdrop_settings()

    def session_user(request: Request) -> str | None:
        """从会话 Cookie 里认出登录用户；没登录返回 None。"""
        if not auth.enabled:
            return auth.username
        token = request.cookies.get(COOKIE_NAME, "")
        info = auth.verify(token)
        return str(info.get("username")) if info else None

    def require_login(request: Request) -> None:
        """网页与 API 的登录校验（播放路由不受影响，播放器没有 Cookie）。"""
        if not auth.enabled:
            return
        if session_user(request) is None:
            raise HTTPException(status_code=401, detail="需要登录")

    def check_token(request: Request, token: str = "") -> None:
        """播放路由用：只认 AUTH_TOKEN —— 播放器（Emby / 第三方播放器）没有 Cookie。"""
        if not cfg.auth_token:
            return
        supplied = (
            token
            or request.query_params.get("token", "")
            or request.headers.get("x-token", "")
            or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        )
        if supplied != cfg.auth_token:
            raise HTTPException(status_code=401, detail="invalid token")

    def authorize(request: Request, token: str = "") -> None:
        """管理类接口的鉴权：登录会话或 AUTH_TOKEN 二者其一即可。

        没配置 UI_PASSWORD 时不启用登录（保持旧的开放行为）；
        配置了密码就必须先登录，除非额外提供了 AUTH_TOKEN。
        """
        if auth.enabled and session_user(request) is not None:
            return
        if cfg.auth_token:
            check_token(request, token)
            return
        if auth.enabled:
            raise HTTPException(status_code=401, detail="需要登录")

    def plugin_obj(pid: str) -> Any:
        """插件运行时对象；没装 / 没启用就是 None。

        所有"可选功能"的接线都走这里：插件不在，功能自动消失，核心代码里
        不需要一堆 ``if xxx is not None`` 散落各处。
        """
        return plugins.get(pid) if plugins is not None else None

    def root_label(cid: int, prefix: str, names: dict[int, str]) -> str:
        if cid == 0:
            return "整个网盘"
        return names.get(cid) or prefix or str(cid)

    def account_engine(name: str):
        """按账号名取 (db, scanner, store, api)；主账号用当前这套。"""
        if not name:
            return db, scanner, store, api
        if hub is None:
            raise HTTPException(status_code=404, detail="没有多账号支持")
        eng = hub.get(name)
        if eng is None:
            raise HTTPException(status_code=404, detail=f"没有这个账号: {name}")
        return eng.db, eng.scanner, eng.store, eng.api

    # ------------------------------------------------------------------ 播放
    @app.api_route("/p115/play/{tail:path}", methods=["GET", "HEAD"])
    def play(tail: str, request: Request, token: str = ""):
        check_token(request, token)
        pickcode = _pickcode_from_tail(tail)
        # strm 末尾那串数字是 115 用户 ID：按它找回对应账号的引擎取直链
        eng = hub.by_uid(_play_tail_uid(tail)) if hub is not None else None
        if eng is not None:
            return _play_pickcode(eng.cfg, eng.api, pickcode, request, eng.copies)
        return _play_pickcode(cfg, api, pickcode, request, copies)

    @app.api_route("/p115/ed2k/{key}", methods=["GET", "HEAD"])
    @app.api_route("/p115/ed2k/{key}/{uid}", methods=["GET", "HEAD"])
    def play_ed2k(key: str, request: Request, uid: str = "", token: str = ""):
        check_token(request, token)
        eng = hub.by_uid(uid) if hub is not None else None
        if eng is not None:
            return _play_ed2k(eng.cfg, eng.ed2k, key, request, eng.copies)
        return _play_ed2k(cfg, ed2k, key, request, copies)

    @app.api_route("/p115/instant/{sha1}", methods=["GET", "HEAD"])
    def play_instant(sha1: str, request: Request, account: str = "",
                     size: int = 0, name: str = "", source_account: str = "",
                     token: str = ""):
        check_token(request, token)
        return _play_instant(cfg, instant, sha1, account, request,
                             source_account)

    @app.api_route("/p115/share/{my_share_code}/{file_id}", methods=["GET", "HEAD"])
    @app.api_route("/p115/share/{my_share_code}/{file_id}/{uid}", methods=["GET", "HEAD"])
    def play_share(my_share_code: str, file_id: str, request: Request,
                   uid: str = "", token: str = ""):
        check_token(request, token)
        eng = hub.by_uid(uid) if hub is not None else None
        if eng is not None:
            return _play_share(eng.cfg, eng.share, eng.db, eng.api,
                               my_share_code, file_id, request)
        return _play_share(cfg, share, db, api, my_share_code, file_id, request)

    # ------------------------------------------------------------ 天翼云盘播放
    # 地址写死在历史 strm 文件里（/p189/play/...），所以这条路由永远注册，
    # 插件没装时返回 404 —— 比让播放器拿到一个"反代上游 404"的页面清楚。
    @app.api_route("/p189/play/{tail:path}", methods=["GET", "HEAD"])
    def play_cloud189(tail: str, request: Request, token: str = ""):
        """天翼云盘的 strm 播放入口：换新鲜直链后按 PLAY_MODE 返回。"""
        check_token(request, token)
        mgr = plugin_obj("cloud189")
        if mgr is None:
            raise HTTPException(status_code=404,
                                detail="天翼云盘插件未安装，可在「插件」页安装后重试")
        return _serve_cloud189(cfg, mgr, request, tail)

    # ------------------------------------------------------------ 多账号播放
    if hub is not None:
        def _acc(name: str):
            eng = hub.get(name)
            if eng is None:
                raise HTTPException(status_code=404, detail=f"没有这个账号: {name}")
            return eng

        @app.api_route("/p115/a/{name}/play/{tail:path}", methods=["GET", "HEAD"])
        def acc_play(name: str, tail: str, request: Request, token: str = ""):
            check_token(request, token)
            eng = _acc(name)
            return _play_pickcode(eng.cfg, eng.api, _pickcode_from_tail(tail),
                                  request, eng.copies)

        @app.api_route("/p115/a/{name}/ed2k/{key}", methods=["GET", "HEAD"])
        def acc_play_ed2k(name: str, key: str, request: Request, token: str = ""):
            check_token(request, token)
            eng = _acc(name)
            return _play_ed2k(eng.cfg, eng.ed2k, key, request, eng.copies)

        @app.api_route("/p115/a/{name}/instant/{sha1}", methods=["GET", "HEAD"])
        def acc_play_instant(name: str, sha1: str, request: Request,
                             account: str = "", size: int = 0, name_q: str = "",
                             source_account: str = "", token: str = ""):
            check_token(request, token)
            eng = _acc(name)
            # 路径里的账号名就是源账号（子账号的 strm 走这里）。
            # 用**主服务的 instant**：用户→秒传账号的映射存在主库的
            # emby_users 表里，用子账号自己的 instant 就查不到映射。
            return _play_instant(eng.cfg, instant, sha1, account, request,
                                 source_account or name)

        @app.api_route("/p115/a/{name}/share/{my_share_code}/{file_id}",
                       methods=["GET", "HEAD"])
        def acc_play_share(name: str, my_share_code: str, file_id: str,
                           request: Request, token: str = ""):
            authorize(request, token)
            eng = _acc(name)
            return _play_share(eng.cfg, eng.share, eng.db, eng.api,
                               my_share_code, file_id, request)

    # ------------------------------------------------------------------ 分享转存
    @app.get("/api/share")
    def share_status(request: Request, token: str = ""):
        authorize(request, token)
        if share is None:
            raise HTTPException(status_code=404, detail="分享功能未启用")
        return {"settings": share.settings(), "items": db.list_shares()}

    @app.post("/api/share")
    def share_add(request: Request, url: str, name: str = "", dir_cid: str = "",
                  force: bool = False, direct: bool = False, token: str = ""):
        authorize(request, token)
        if share is None:
            raise HTTPException(status_code=404, detail="分享功能未启用")
        try:
            cid = _parse_dir_input(dir_cid) if dir_cid.strip() else 0
            rec = share.add(url, name=name, dir_cid=cid, force=force, direct=direct)
        except (ShareError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "share": rec}

    @app.post("/api/share/settings")
    def share_settings(request: Request, dir_cid: str = "", strm_dir: str = "",
                       dir_path: str = "", token: str = ""):
        authorize(request, token)
        if share is None:
            raise HTTPException(status_code=404, detail="分享功能未启用")
        try:
            cid = _parse_dir_input(dir_cid) if dir_cid.strip() else None
            # strm_dir 空 = 不改（要恢复默认就传默认目录名）
            return share.save_settings(cid, strm_dir=(strm_dir.strip() or None),
                                       dir_path=dir_path)
        except (ShareError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/share/auto-dir")
    def share_auto_dir(request: Request, token: str = ""):
        authorize(request, token)
        if share is None:
            raise HTTPException(status_code=404, detail="分享功能未启用")
        try:
            cid, name = share.target(create=True)
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"dir_cid": str(cid), "dir_name": name}

    @app.post("/api/share/refresh")
    def share_refresh(request: Request, share_code: str, token: str = ""):
        authorize(request, token)
        if share is None:
            raise HTTPException(status_code=404, detail="分享功能未启用")
        try:
            return {"ok": True, "share": share.refresh(share_code)}
        except ShareError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/share/probe")
    def share_probe(request: Request, url: str, token: str = ""):
        """诊断用：只看分享的内容结构，不做任何转存。

        返回顶层条目数、总文件数、顶层条目样例 —— 用来判断
        「只转存了部分内容」是快照遍历漏了还是 115 转存本身的问题。
        """
        authorize(request, token)
        if share is None:
            raise HTTPException(status_code=404, detail="分享功能未启用")
        try:
            from .share import parse_share

            payload = parse_share(url)
            tree = share.list_share_tree(payload["share_code"], payload["receive_code"])
        except (ShareError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {
            "ok": True,
            "title": tree["title"],
            "file_count": tree["file_count"],
            "top_count": len(tree["top_ids"]),
            "top_ids": tree["top_ids"],
            "sample": tree["items"][:20],
            "depth_dirs": sorted({
                item["rel_path"].count("/") for item in tree["items"]
            }),
        }

    @app.delete("/api/share/{share_code}")
    def share_delete(request: Request, share_code: str, delete_files: bool = True,
                     token: str = ""):
        authorize(request, token)
        if share is None:
            raise HTTPException(status_code=404, detail="分享功能未启用")
        try:
            rec = share.delete(share_code, delete_files=delete_files)
        except ShareError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True, "strm_removed": rec.get("_strm_removed", 0)}

    # ------------------------------------------------------------------ 秒传
    @app.get("/api/instant")
    def instant_status(request: Request, refresh: int = 0, token: str = ""):
        authorize(request, token)
        if instant is None:
            return {"accounts": [], "users": [], "records": 0, "known_users": []}
        data = instant.status()
        # 用户名单直接问 Emby（/Users/Public），不再依赖「谁登录过一次」
        data["known_users"] = _known_emby_users(db, cfg, force=bool(refresh))
        data["emby_url"] = str(getattr(cfg, "emby_url", "") or "")
        return data

    @app.post("/api/instant/accounts")
    def instant_add_account(request: Request, name: str, cookies: str, token: str = ""):
        authorize(request, token)
        if instant is None:
            raise HTTPException(status_code=404, detail="秒传功能不可用")
        try:
            return instant.add_account(name, cookies)
        except InstantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/instant/digest")
    def instant_digest_status(request: Request, token: str = ""):
        """秒传校验缓存的占用情况。"""
        authorize(request, token)
        if instant is None:
            raise HTTPException(status_code=404, detail="秒传功能不可用")
        return instant.digest_stats()

    @app.delete("/api/instant/digest")
    def instant_digest_clear(request: Request, token: str = ""):
        """清空秒传校验缓存。

        这层缓存只是用来省一次源盘读取，清掉最坏结果是下次多读一次，
        不会影响任何已经能播的文件。
        """
        authorize(request, token)
        if instant is None:
            raise HTTPException(status_code=404, detail="秒传功能不可用")
        return instant.digest_clear()

    @app.post("/api/instant/digest/sweep")
    def instant_digest_sweep(request: Request, token: str = ""):
        """立刻把校验缓存压回字节预算内（丢最久没用的那些）。

        后台线程本来会定期做这件事，这个接口是让用户不用等就能腾出空间。
        """
        authorize(request, token)
        if instant is None:
            raise HTTPException(status_code=404, detail="秒传功能不可用")
        return instant.digest_sweep()

    @app.delete("/api/instant/accounts/{name}")
    def instant_remove_account(request: Request, name: str, token: str = ""):
        authorize(request, token)
        if instant is None:
            raise HTTPException(status_code=404, detail="秒传功能不可用")
        if not instant.remove_account(name):
            raise HTTPException(status_code=404, detail=f"没有账号「{name}」")
        return {"ok": True}

    @app.post("/api/instant/user")
    def instant_set_user(request: Request, user_id: str, account: str = "",
                         name: str = "", token: str = ""):
        """设置 Emby 用户 → 秒传账号的映射。

        ``account`` 是**映射到哪个秒传账号**（空 = 主账号），不是「在哪个账号下操作」。
        映射表存在主库里，所以这个接口只在主服务上提供。
        """
        authorize(request, token)
        if instant is None:
            raise HTTPException(status_code=404, detail="秒传功能不可用")
        try:
            instant.set_user(user_id, name, account)
        except InstantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True}

    @app.delete("/api/instant/user/{user_id}")
    def instant_del_user(request: Request, user_id: str, token: str = ""):
        """移除一条用户映射 —— **真删掉这条记录**。

        之前只是把 account 置空（变成「未映射」），记录还留着，
        用户看到「移除后那个用户还在」。
        """
        authorize(request, token)
        if not db.delete_emby_user(user_id):
            raise HTTPException(status_code=404, detail="没有这个映射")
        return {"ok": True}

    @app.post("/api/instant/staging")
    def instant_set_staging(request: Request, account: str = "", path: str = "",
                            cid: int = 0, token: str = ""):
        """设置某个账号的秒传暂存目录（手动指定路径或目录 id）。

        path 与 cid 给一个即可；都不给 = 恢复默认目录
        （``INSTANT_DIR_NAME``，建在网盘根下）。
        """
        authorize(request, token)
        if instant is None:
            raise HTTPException(status_code=404, detail="秒传功能不可用")
        try:
            item = instant.set_staging_dir(account, path, cid)
        except (InstantError, P115Error) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "account": account, "staging": item}

    @app.post("/api/instant/prewarm")
    def instant_prewarm(request: Request, account: str = "", limit: int = 50,
                        token: str = ""):
        """预缓存：把索引里最近的文件提前秒传到目标账号。

        ``account`` 是**目标**账号（秒传到谁）。源文件来自各账号自己的索引库
        （115 STRM 生成的文件在对应账号的库里），所以要跨账号取。
        """
        authorize(request, token)
        if instant is None:
            raise HTTPException(status_code=404, detail="秒传功能不可用")
        account = (account or "").strip()
        if not account:
            raise HTTPException(status_code=400, detail="请选择要预缓存到的账号")
        # 校验用 instant.cookies_for()：它会问「115 助手」要 cookies。
        # 原来用的 db.get_account_cookies() 查的是旧「秒传账号」机制的
        # accounts 表 —— 115 助手的账号存在文件里，那张表是空的，
        # 会误报「没有账号「xxx」」。
        if not instant.cookies_for(account):
            raise HTTPException(status_code=404, detail=f"没有账号「{account}」")

        def run() -> None:
            try:
                result = instant.prewarm(account, limit)
                log.info("秒传预缓存完成（账号 %s）: %s", account, result)
                state["instant_prewarm"] = {**result, "account": account,
                                            "finished_at": int(time.time())}
            except Exception as exc:  # noqa: BLE001
                log.warning("秒传预缓存失败: %s", exc)
                state["instant_prewarm"] = {"error": str(exc), "account": account}

        threading.Thread(target=run, name="instant-prewarm", daemon=True).start()
        return {"accepted": True, "account": account, "limit": limit}

    @app.post("/api/instant/clean")
    def instant_clean(request: Request, account: str = "", token: str = ""):
        """清空目标账号的秒传暂存目录（下次播放会重新秒传）。"""
        authorize(request, token)
        if instant is None:
            raise HTTPException(status_code=404, detail="秒传功能不可用")
        account = (account or "").strip()
        if not account:
            raise HTTPException(status_code=400, detail="请选择要清空暂存目录的账号")
        try:
            return instant.clean(account)
        except (InstantError, P115Error) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ------------------------------------------------------------------ Emby 反代
    if emby is not None:
        # 同端口使用时的入口。路径完整透传给上游（不剥 /emby 前缀），
        # 这样上游 Emby 的 BaseUrl 无论配 / 还是 /emby 都能命中。
        @app.api_route("/emby/{path:path}",
                       methods=["GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS"])
        async def emby_gateway(path: str, request: Request):
            return await emby.handle(request, path)

        # 同端口时 Web 客户端会连 /emby/embywebsocket（带前缀）或 /embywebsocket
        # （BaseUrl 在客户端侧被忽略）。两个都接住，转给同一个桥。
        @app.websocket("/emby/{path:path}")
        async def emby_gateway_ws(websocket: WebSocket, path: str):
            await emby.handle_ws(websocket, path)

        @app.websocket("/embywebsocket")
        async def emby_ws_root(websocket: WebSocket):
            await emby.handle_ws(websocket, "embywebsocket")

    # ------------------------------------------------------------------ 状态
    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    def _login_state() -> dict[str, Any]:
        """115 登录状态：默认账号（配置文件那个）+ 已添加账号数。

        ``ok`` = 至少有一个可用身份（默认账号已登录，或「115 助手」里已添加
        账号）。首次运行没填 cookies 时它是 False，网页上据此显示引导。
        """
        info = dict(state.get("login") or {})
        if getattr(api, "authenticated", True):
            info["ok"] = True
        accounts = hub.count() if hub is not None else 0
        info["ok"] = bool(info.get("ok")) or accounts > 0
        info["accounts"] = accounts
        info["uid"] = str(info.get("uid") or "")
        info["detail"] = str(info.get("detail") or "")
        return info

    @app.get("/api/status")
    def status(request: Request, token: str = ""):
        authorize(request, token)
        roots = scanner.resolve_roots()
        names = {int(r["cid"]): r["name"] for r in db.list_roots()}
        # 通知 / 联动都在「通知与联动」插件里：插件没装这两项就是 None，
        # 前端据此不显示对应卡片
        ntf = plugin_obj("notify")
        lnk = getattr(ntf, "linker", None) if ntf is not None else None
        return {
            "version": state.get("version"),
            "mode": cfg.mode,
            "login": _login_state(),
            "scan": state.get("scan", {}),
            "watch": watcher.status(),
            "db": db.stats(),
            "roots": {
                str(cid): root_label(cid, prefix, names) for cid, prefix in roots.items()
            },
            "root_scans": state.get("root_scans", {}),
            "strm_root": str(cfg.strm_root),
            "play_mode": cfg.play_mode,
            "api": {
                "qps": cfg.api_qps,
                "blocked_for": max(0, int(getattr(api, "_blocked_until", 0.0) - time.time())),
            },
            "ed2k": ed2k.settings() if ed2k else {},
            "share": share.settings() if share else {"enabled": False},
            "instant": instant.status() if instant else {"accounts": []},
            "emby": {"enabled": emby is not None, "url": cfg.emby_url,
                     "proxy_port": cfg.emby_proxy_port if emby else 0},
            "notify": ntf.status() if ntf is not None else None,
            "link": lnk.status() if lnk is not None else None,
        }

    # ------------------------------------------------------------------ 目录管理
    @app.get("/api/roots")
    def list_roots(request: Request, token: str = ""):
        authorize(request, token)
        roots = scanner.resolve_roots()
        names = {int(r["cid"]): r["name"] for r in db.list_roots()}
        total_files = db.stats()["files"]
        out = []
        for cid, prefix in roots.items():
            out.append(
                {
                    # 115 的 cid 是 19 位，超过 JS 的 2^53 安全整数，必须按字符串下发，
                    # 否则浏览器 JSON.parse 会把它四舍五入，再传回来就是另一个 id。
                    "cid": str(cid),
                    "id": str(cid),
                    "name": root_label(cid, prefix, names),
                    "prefix": prefix,
                    "files": total_files if not prefix else len(db.files_under(prefix)),
                }
            )
        out.sort(key=lambda item: int(item["cid"]))
        return {"roots": out}

    @app.post("/api/roots")
    def add_root(request: Request, cid: int = 0, sync: bool = False, token: str = ""):
        authorize(request, token)
        cid = int(cid)
        if cid < 0:
            raise HTTPException(status_code=400, detail="目录 id 无效")
        if cid == 0:
            # 根目录在 115 上没有可查的节点详情，直接视为合法目录
            node: dict[str, Any] | None = {"id": 0, "parent_id": 0, "name": "整个网盘", "is_dir": True}
        else:
            try:
                node = api.get_node(cid)
            except P115Error as exc:
                raise HTTPException(status_code=502, detail=f"查询 115 失败: {exc}") from exc
        if node is None or not node["is_dir"]:
            raise HTTPException(status_code=404, detail=f"115 上不存在目录 {cid}")

        existing = scanner.resolve_roots()
        if cid in existing:
            raise HTTPException(status_code=409, detail="该目录已在监控列表中")

        ancestors = _ancestor_ids(api, cid)
        inside = [r for r in existing if r in ancestors]
        if inside:
            r = inside[0]
            label = root_label(r, existing[r], {})
            hint = (
                "。「整个网盘」会把所有目录都算作已覆盖，只想监控部分目录请先移除它"
                if r == 0
                else ""
            )
            raise HTTPException(
                status_code=400,
                detail=f"该目录已在「{label}」的监控范围内，无需重复添加{hint}",
            )
        for r in existing:
            if cid in _ancestor_ids(api, r):
                raise HTTPException(
                    status_code=400,
                    detail=f"已监控的「{root_label(r, existing[r], {})}」在该目录之下，请先移除它",
                )

        name = node.get("name") or str(cid)
        db.add_root(cid, name)
        scanner.invalidate_roots()
        prefix = scanner.root_prefix(cid, name)

        def do_scan() -> None:
            try:
                stats = scanner.scan_subtree(cid, prefix)
            except Exception as exc:
                log.warning("扫描新目录 %s 失败: %s", prefix or cid, exc, exc_info=True)
                return
            state.setdefault("root_scans", {})[str(cid)] = stats.as_dict()
            log.info("新目录「%s」扫描完成: %s", prefix or "整个网盘", stats.as_dict())

        if sync:
            do_scan()
        else:
            threading.Thread(target=do_scan, name=f"root-scan-{cid}", daemon=True).start()
        return {"accepted": True, "cid": str(cid), "name": name, "prefix": prefix}

    @app.delete("/api/roots/{cid}")
    def remove_root(cid: int, request: Request, token: str = ""):
        authorize(request, token)
        cid = int(cid)
        roots = scanner.resolve_roots()
        if cid not in roots:
            raise HTTPException(status_code=404, detail="该目录不在监控列表中")
        prefix = roots[cid]
        db.remove_root(cid)
        scanner.invalidate_roots()

        # 连带清掉已生成的 strm 和索引，保证「只对添加的目录生成」语义完整
        rows = db.delete_dir_tree(cid)
        for item in rows:
            if item.get("strm_path"):
                store.remove_abs(item["strm_path"])
            else:
                store.remove(item["path"])
        if prefix:
            store.remove_rel_dir(prefix, delete_empty=cfg.delete_empty_dirs)
        state.get("root_scans", {}).pop(str(cid), None)
        log.info("移除监控目录「%s」（清理 %d 个 strm）", prefix or "整个网盘", len(rows))
        return {"removed": True, "cid": str(cid), "prefix": prefix, "strm_removed": len(rows)}

    # ------------------------------------------------------------- 本地目录浏览
    @app.get("/api/local/dirs")
    def local_dirs(request: Request, path: str = "", token: str = ""):
        """列出**本地映射目录**（容器内 STRM_ROOT）下的子目录。

        「本地 STRM 目录」选择器用它：用户填的是容器内路径（如 ``/strm/影音``），
        这里只允许在 STRM_ROOT 里浏览 —— 手滑填成 ``/config`` 或 ``/``
        不会被列出来，更不会被写进去。

        参数 ``path`` 为空 = 根（STRM_ROOT）。只返回目录，跳过隐藏目录。
        """
        authorize(request, token)
        root = Path(str(getattr(cfg, "strm_root", "") or "/strm")).expanduser()
        try:
            root = root.resolve()
        except OSError:
            raise HTTPException(status_code=500, detail="STRM 根目录不可用") from None
        raw = (path or "").strip()
        target = Path(raw).expanduser() if raw else root
        if not target.is_absolute():
            target = root / target
        try:
            target = target.resolve()
        except OSError:
            target = root
        # 防越界：必须在 STRM_ROOT 之内（含根自身）
        if target != root and root not in target.parents:
            raise HTTPException(
                status_code=400, detail=f"只能浏览映射目录 {root} 内的路径")
        if not target.is_dir():
            raise HTTPException(status_code=404, detail=f"目录不存在：{target}")
        dirs: list[dict[str, Any]] = []
        try:
            entries = sorted(target.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            raise HTTPException(
                status_code=502, detail=f"读取目录失败：{exc}") from exc
        for item in entries:
            try:
                if not item.is_dir() or item.name.startswith("."):
                    continue
            except OSError:
                continue
            count = 0
            try:
                count = sum(1 for _ in item.glob("*.strm"))
            except OSError:
                count = 0
            dirs.append({
                "name": item.name,
                "path": str(item),
                "strm": count,
                "has_sub": any(
                    p.is_dir() for p in item.iterdir() if not p.name.startswith(".")
                ),
            })
        return {
            "root": str(root),
            "path": str(target),
            "parent": str(target.parent) if target != root else "",
            "dirs": dirs,
        }

    @app.get("/api/115/dirs")
    def browse_dirs(request: Request, cid: int = 0, limit: int = 200, token: str = ""):
        authorize(request, token)
        cid = int(cid)
        if cid < 0:
            raise HTTPException(status_code=400, detail="目录 id 无效")
        try:
            current = api.get_node(cid) if cid else None
            items: list[dict[str, Any]] = []
            for node in api.iter_dir(
                cid, app=cfg.scan_app, page_workers=1, cooldown=cfg.scan_cooldown
            ):
                if not node["is_dir"] or not scanner._dir_ok(node["name"]):
                    continue
                items.append({"id": str(node["id"]), "name": node["name"]})
                if len(items) >= max(1, min(limit, 500)):
                    break
        except DirNotFound as exc:
            log.info("浏览 115 目录 %s 失败: %s", cid, exc)
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except WafBlocked as exc:
            # 限流不是「目录坏了」，给前端一个能看懂的提示，别让人以为列表丢了
            log.warning("浏览 115 目录 %s 被限流: %s", cid, exc)
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except Exception as exc:
            log.warning("读取 115 目录 %s 失败: %s", cid, exc, exc_info=True)
            raise HTTPException(
                status_code=502, detail=f"读取 115 目录失败: {exc}"
            ) from exc
        managed = set(scanner.resolve_roots())
        for item in items:
            item["managed"] = int(item["id"]) in managed
        name = (current or {}).get("name") or ("整个网盘" if cid == 0 else str(cid))
        return {"cid": str(cid), "name": name, "dirs": items}

    # ------------------------------------------------------------------ ed2k
    @app.get("/api/ed2k")
    def ed2k_list(request: Request, token: str = ""):
        authorize(request, token)
        if ed2k is None or not cfg.ed2k_enabled:
            return {"enabled": False, "items": [], "settings": {}}
        items = []
        for row in db.list_ed2k():
            # 19 位的 cid / file_id 用字符串下发，避免前端丢精度
            row = dict(row)
            row["dir_cid"] = str(row.get("dir_cid") or "")
            row["file_id"] = str(row.get("file_id") or "")
            items.append(row)
        return {
            "enabled": True,
            "settings": ed2k.settings(),
            "items": items,
        }

    @app.post("/api/ed2k")
    def ed2k_add(request: Request, url: str = "", name: str = "", token: str = ""):
        authorize(request, token)
        if ed2k is None or not cfg.ed2k_enabled:
            raise HTTPException(status_code=400, detail="ed2k 功能未启用")
        try:
            rec = ed2k.add(url, name)
        except Ed2kError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "item": rec}

    @app.post("/api/ed2k/batch")
    def ed2k_add_batch(request: Request, urls: str = "", name: str = "",
                       token: str = ""):
        """批量添加：正文里所有 ed2k 链接都会被提取（支持每行一条或整段粘贴）。

        故意用同步 def（FastAPI 会丢到线程池执行），避免几十秒的转存提交把事件循环卡住、
        影响正在播放的请求。
        """
        authorize(request, token)
        if ed2k is None or not cfg.ed2k_enabled:
            raise HTTPException(status_code=400, detail="ed2k 功能未启用")
        try:
            result = ed2k.add_many(urls, limit=200)
        except Ed2kError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        log.info("批量添加 ed2k：成功 %d，失败 %d", result["added"], result["failed"])
        return {"ok": True, **result}

    @app.post("/api/ed2k/refresh")
    def ed2k_refresh(request: Request, key: str = "", token: str = ""):
        authorize(request, token)
        if ed2k is None:
            raise HTTPException(status_code=404, detail="ed2k 功能未启用")
        try:
            handled = ed2k.refresh(key.strip().lower() or None)
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "handled": handled, "items": db.list_ed2k()}

    @app.post("/api/ed2k/clean")
    def ed2k_clean(request: Request, force: bool = False, token: str = ""):
        authorize(request, token)
        if ed2k is None:
            raise HTTPException(status_code=404, detail="ed2k 功能未启用")
        try:
            return {"ok": True, **ed2k.clean(force=force)}
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/ed2k/settings")
    def ed2k_settings(request: Request, dir_cid: str = "", dir_path: str = "",
                      ttl_hours: float | None = None,
                      clean_interval: float | None = None,
                      strm_dir: str = "", auto_transfer: bool | None = None,
                      token: str = ""):
        authorize(request, token)
        if ed2k is None:
            raise HTTPException(status_code=404, detail="ed2k 功能未启用")
        target: int | None = None
        if (dir_cid or "").strip():
            try:
                target = _parse_dir_input(dir_cid)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            return {"ok": True, "settings": ed2k.save_settings(
                dir_cid=target, ttl_hours=ttl_hours, clean_interval=clean_interval,
                strm_dir=(strm_dir.strip() or None), dir_path=dir_path,
                auto_transfer=auto_transfer,
            )}
        except Ed2kError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/ed2k/export")
    def ed2k_export(request: Request, include_files: bool = True, token: str = ""):
        """导出任务 + 配置为 JSON（换设备时导入回去）。"""
        authorize(request, token)
        if ed2k is None or not cfg.ed2k_enabled:
            raise HTTPException(status_code=400, detail="ed2k 功能未启用")
        data = ed2k.export_data(include_files=include_files)
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime())
        return Response(
            content=json.dumps(data, ensure_ascii=False, indent=2),
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition":
                    f'attachment; filename="panshare-ed2k-{stamp}.json"',
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/ed2k/import")
    async def ed2k_import(request: Request, mode: str = "merge",
                          apply_settings: bool = False, token: str = ""):
        """导入备份。mode = merge（只加新的）/ update（覆盖已有）/ replace（先清空）。"""
        authorize(request, token)
        if ed2k is None or not cfg.ed2k_enabled:
            raise HTTPException(status_code=400, detail="ed2k 功能未启用")
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from exc
        try:
            result = ed2k.import_data(payload, mode=mode,
                                      apply_settings=apply_settings)
        except Ed2kError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        log.info(
            "导入 ed2k 备份：新增 %d，更新 %d，跳过 %d，失败 %d",
            result["added"], result["updated"], result["skipped"], result["failed"],
        )
        return result

    @app.post("/api/ed2k/auto-dir")
    def ed2k_auto_dir(request: Request, token: str = ""):
        authorize(request, token)
        if ed2k is None:
            raise HTTPException(status_code=404, detail="ed2k 功能未启用")
        try:
            cid, name = ed2k.target(create=True)
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "dir_cid": str(cid), "dir_name": name}

    @app.delete("/api/ed2k/{key}")
    def ed2k_delete(key: str, request: Request, delete_file: bool = True, token: str = ""):
        authorize(request, token)
        if ed2k is None:
            raise HTTPException(status_code=404, detail="ed2k 功能未启用")
        try:
            rec = ed2k.delete(_split_pickcode(key).lower(), delete_file=delete_file)
        except Ed2kNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "key": rec["key"], "name": rec["name"]}

    # ------------------------------------------------------------------ 手动触发
    @app.post("/api/scan")
    def trigger_scan(request: Request, full: bool = True, token: str = ""):
        authorize(request, token)
        if not getattr(api, "authenticated", True):
            raise HTTPException(
                status_code=400,
                detail="还没有 115 cookies：先在「115 助手」里添加账号，再扫描",
            )
        if not lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="扫描已在进行中")
        try:
            def run() -> None:
                try:
                    # 全量 = 清索引 + 全部重新写一遍（force 由 Scanner.run 内部保证）
                    stats = scanner.run(full=full)
                    state["scan"] = stats.as_dict()
                    try:
                        db.add_strm_event(
                            "scan_full" if full else "scan_inc",
                            f"监控目录{'全量' if full else '增量'}扫描完成："
                            f"{stats.files} 个文件，写入 {stats.written} 个"
                            f"（其中重写 {stats.overwritten}），跳过 {stats.unchanged}",
                            account="",
                            level="warn" if stats.aborted else "info",
                            detail=json.dumps(stats.as_dict(), ensure_ascii=False),
                        )
                    except Exception:  # noqa: BLE001 - 记日志失败不影响扫描
                        pass
                finally:
                    lock.release()

            threading.Thread(target=run, name="manual-scan", daemon=True).start()
        except Exception:
            lock.release()
            raise
        return {"accepted": True, "full": full}

    @app.post("/api/poll")
    def trigger_poll(request: Request, token: str = ""):
        """立刻同步一次变更。

        115 已经下线生活事件接口，这时 /api/poll 就等价于「跑一次增量扫描」，
        而不是返回 502 —— 用户在网页上点「立即同步」时不该看到一个报错。
        """
        authorize(request, token)
        if watcher.events_ok is False:
            stats = scanner.run(full=False)
            return {"accepted": True, "mode": "interval", "scan": stats.as_dict()}
        try:
            count = watcher.poll_once()
        except Exception as exc:
            if watcher._endpoint_gone(exc):
                watcher.events_ok = False
                stats = scanner.run(full=False)
                return {"accepted": True, "mode": "interval", "scan": stats.as_dict()}
            log.warning("手动拉取事件失败: %s", exc)
            raise HTTPException(status_code=502, detail=f"拉取事件失败: {exc}") from exc
        return {"accepted": True, "mode": "event", "handled": count}

    # ------------------------------------------------------------------ 多账号
    # 「115 助手」只负责记录已添加的账号：列表含主账号 + 所有子账号，
    # 显示名（display）可单独改，内部账号名 name 不动（它决定 strm 目录与 URL 前缀）。
    @app.get("/api/accounts")
    def accounts_list(request: Request, token: str = ""):
        authorize(request, token)
        # 只列「添加过的账号」。主账号位（内部名空串）是配置文件里那个账号，
        # 不在这里显示；「设为主账号」设置已移除，因此没有 main 标记。
        rows: list[dict[str, Any]] = []
        if hub is not None:
            for row in hub.status():
                name = str(row.get("name") or "")
                if not name:
                    continue          # 那个固定的槽位不显示
                row["display"] = hub.display_name(name)
                eng = hub.get(name)
                if eng is not None and getattr(eng, "copies", None) is not None:
                    copies_cfg = eng.copies.settings()
                    row["copy_enabled"] = copies_cfg["enabled"]
                    row["copy_count"] = copies_cfg["copies"]
                rows.append(row)
        return {"accounts": rows, "login": _login_state()}

    if hub is not None:
        @app.post("/api/accounts")
        def accounts_add(request: Request, name: str = "", cookies: str = "",
                         token: str = ""):
            authorize(request, token)
            from .hub import AccountError

            try:
                eng = hub.add(name, cookies)
            except AccountError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except P115Error as exc:
                raise HTTPException(
                    status_code=400, detail=f"登录校验失败，请检查 cookies: {exc}"
                ) from exc
            return {
                "ok": True,
                "name": eng.name,
                "strm_root": str(eng.cfg.strm_root),
                "manage_url": f"/a/{eng.name}/",
            }

        @app.delete("/api/accounts/{name}")
        def accounts_remove(name: str, request: Request, token: str = ""):
            authorize(request, token)
            acc = _acc_name(name)
            if not acc:
                # 主账号：清掉它的 cookies（等于删除主账号）
                hub.clear_main_cookies()
                return {"ok": True, "cleared": "main"}
            if hub.get(acc) is None and not any(
                item.get("name") == acc for item in hub.status()
            ):
                raise HTTPException(status_code=404, detail=f"没有这个账号: {acc}")
            hub.remove(acc)
            return {"ok": True}

    @app.put("/api/accounts/{name}/display")
    def accounts_display(name: str, request: Request, display: str = "",
                         token: str = ""):
        """改账号的显示名（不改内部账号名，strm 目录与 URL 前缀不受影响）。"""
        authorize(request, token)
        acc = _acc_name(name)
        if acc and hub is not None and hub.get(acc) is None:
            raise HTTPException(status_code=404, detail=f"没有这个账号: {acc}")
        db.set_account_display(acc, display)
        return {"ok": True, "name": acc, "display": display}

    # ---------------------------------------------------- 同播复制（账号级）
    # 开关挂在账号上：这个账号出去的所有播放（strm / ed2k / 以后的任何来源）
    # 都走同一套判断，不再只有 ed2k 享受。
    def _copies_of(acc: str):
        if hub is not None:
            eng = hub.default if not acc else hub.get(acc)
            if eng is None:
                raise HTTPException(status_code=404, detail=f"没有这个账号: {acc}")
            if getattr(eng, "copies", None) is not None:
                return eng.copies
        if acc:
            raise HTTPException(status_code=404, detail=f"没有这个账号: {acc}")
        return copies

    @app.get("/api/accounts/{name}/copies")
    def copies_get(name: str, request: Request, token: str = ""):
        authorize(request, token)
        acc = _acc_name(name)
        return {"ok": True, "name": acc, "settings": _copies_of(acc).settings()}

    @app.post("/api/accounts/{name}/copies")
    def copies_save(name: str, request: Request, enabled: bool | None = None,
                    ttl_minutes: float | None = None,
                    max_copies: int | None = None, token: str = ""):
        authorize(request, token)
        acc = _acc_name(name)
        manager = _copies_of(acc)
        return {"ok": True, "name": acc, "settings": manager.save_settings(
            enabled=enabled,
            ttl=(ttl_minutes * 60 if ttl_minutes else None),
            max_copies=max_copies,
        )}

    @app.post("/api/accounts/{name}/copies/clean")
    def copies_clean(name: str, request: Request, force: int = 0, token: str = ""):
        authorize(request, token)
        acc = _acc_name(name)
        manager = _copies_of(acc)
        try:
            result = manager.clean(force=bool(force))
        except P115Error as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "name": acc, **result,
                "settings": manager.settings()}

    # ---------------------------------------------------------------- 账号 cookie
    def _acc_name(raw: str) -> str:
        """URL 里的账号名 → 内部账号名。主账号用 main 表示（内部名是空串）。

        主账号的内部名是空串，直接拼进 URL 会变成 ``/api/accounts//display``
        这种双斜杠路径 —— 路由匹配不到（404），DELETE 更是直接 405。
        """
        name = (raw or "").strip()
        return "" if name.lower() == "main" else name

    if hub is not None:
        @app.get("/api/accounts/{name}/cookie")
        def accounts_cookie_get(name: str, request: Request, token: str = ""):
            """取某个账号当前的 cookies（网页上展示 / 复制 / 替换）。"""
            authorize(request, token)
            acc = _acc_name(name)
            if acc and hub.get(acc) is None:
                raise HTTPException(status_code=404, detail=f"没有这个账号: {acc}")
            return {"ok": True, "name": acc, "cookies": hub.cookies_for(acc)}

        @app.put("/api/accounts/{name}/cookie")
        async def accounts_cookie_put(name: str, request: Request, token: str = ""):
            """替换某个账号的 cookies。

            主账号直接换掉；子账号先校验登录态，通过后才写入。
            """
            authorize(request, token)
            acc = _acc_name(name)
            if acc and hub.get(acc) is None:
                raise HTTPException(status_code=404, detail=f"没有这个账号: {acc}")
            try:
                payload = await request.json()
            except Exception:  # noqa: BLE001 - 表单提交走查询参数
                payload = {}
            cookies = str(payload.get("cookies") or "").strip()
            if not cookies:
                raise HTTPException(status_code=400, detail="cookies 不能为空")
            try:
                if acc:
                    # 子账号：先校验再写，避免把坏 cookies 写进去导致账号起不来
                    probe = P115API(cookies=cookies, qps=cfg.api_qps)
                    probe.check_login()
                    eng = hub.get(acc)
                    eng.cfg.cookie_path.parent.mkdir(parents=True, exist_ok=True)
                    eng.cfg.cookie_path.write_text(cookies + "\n", encoding="utf-8")
                    fn = getattr(eng.api, "replace_cookies", None)
                    if callable(fn):
                        fn(cookies)
                else:
                    hub.set_main_cookies(cookies)
            except P115Error as exc:
                raise HTTPException(
                    status_code=400, detail=f"登录校验失败，请检查 cookies: {exc}"
                ) from exc
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {"ok": True, "name": acc}

        @app.post("/api/accounts/{name}/test-cookie")
        async def accounts_cookie_test(name: str, request: Request, token: str = ""):
            """测试 cookies 是否还能用（不改任何状态）。

            请求体里带 cookies 就测带的那串（可以先改再测），
            不带就测账号当前保存的那份。
            """
            authorize(request, token)
            acc = _acc_name(name)
            if acc and hub.get(acc) is None:
                raise HTTPException(status_code=404, detail=f"没有这个账号: {acc}")
            try:
                payload = await request.json()
            except Exception:  # noqa: BLE001 - 表单提交走查询参数
                payload = {}
            probe = str(payload.get("cookies") or "").strip()
            return hub.test_cookies(acc, probe or None)

        @app.post("/api/accounts/{name}/promote")
        def accounts_promote(name: str, request: Request, token: str = ""):
            """已移除：「设为主账号」不再支持（各工具页直接选账号即可）。"""
            authorize(request, token)
            raise HTTPException(
                status_code=410,
                detail="「设为主账号」功能已移除：请在各工具页里直接选择要用的账号",
            )

    # ------------------------------------------------------------------ 界面设置
    @app.get("/api/settings")
    def settings_get(request: Request, token: str = ""):
        """界面偏好（如各工具的默认账号），存在库里，换浏览器也保留。"""
        authorize(request, token)
        raw = db.get_meta("ui_settings", "{}")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {}
        return {"settings": data if isinstance(data, dict) else {}}

    @app.put("/api/settings")
    async def settings_put(request: Request, token: str = ""):
        authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        raw = db.get_meta("ui_settings", "{}")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        incoming = payload.get("settings")
        if not isinstance(incoming, dict):
            incoming = {k: v for k, v in payload.items() if k != "settings"}
        data.update(incoming)
        db.set_meta("ui_settings", json.dumps(data, ensure_ascii=False))
        return {"ok": True, "settings": data}

    # ------------------------------------------------------------------ 登录
    @app.post("/api/login")
    async def login(request: Request):
        """网页登录：账号密码由 compose 变量（UI_USERNAME / UI_PASSWORD）生成。"""
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - 表单提交也兼容
            payload = {}
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")
        if not auth.verify_password(username, password):
            log.info("登录失败：用户名或密码不对")
            raise HTTPException(status_code=401, detail="账号或密码不正确")
        token = auth.issue(username)
        response = JSONResponse({"ok": True, "username": username or auth.username})
        response.set_cookie(**auth.cookie_params(token))
        return response

    @app.post("/api/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.set_cookie(**auth.clear_cookie_params())
        return response

    # ------------------------------------------------------------------ 登录页背景
    @app.get("/api/login/backdrop")
    def login_backdrop(refresh: int = 0):
        """随机一张 TMDB 热门影片剧照给登录页当背景（不需要登录）。

        响应带 `no-store`：这里的结果会随配置/网络变化（先 no_tmdb_key、
        配好后就有图），被浏览器缓存住的话改完配置还是「没生效」。
        """
        no_store = {"Cache-Control": "no-store"}
        if not backdrop.available:
            return JSONResponse({"ok": False, "url": "", "reason": "no_tmdb_key"},
                                headers=no_store)
        urls = backdrop.urls(refresh=bool(refresh))
        if not urls:
            return JSONResponse(
                {"ok": False, "url": "", "reason": "fetch_failed",
                 "detail": backdrop.status().get("error", "")},
                headers=no_store,
            )
        index = random.randrange(len(urls))
        return JSONResponse({"ok": True, "url": f"/api/login/backdrop.jpg?i={index}"},
                            headers=no_store)

    @app.get("/api/login/backdrop.jpg")
    def login_backdrop_image(i: int = -1):
        """把选中的剧照中转给浏览器（同源，避免防盗链/混合内容）。"""
        url = backdrop.pick(i)
        if not url:
            raise HTTPException(status_code=404, detail="没有可用的背景图")
        try:
            body, ctype = backdrop.fetch_image(url)
        except Exception as exc:  # 单张失败就让前端保留渐变
            raise HTTPException(status_code=502, detail=f"背景图下载失败: {exc}") from exc
        return Response(content=body, media_type=ctype,
                        headers={"Cache-Control": "public, max-age=3600"})

    # ------------------------------------------------------- 登录页背景设置
    @app.get("/api/login/backdrop/settings")
    def backdrop_settings_get(request: Request, token: str = ""):
        """登录页背景的配置与当前状态（只给已登录的管理员看）。"""
        authorize(request, token)
        return {
            "settings": backdrop.settings(),
            "status": backdrop.status(),
            # 环境变量里配的也算「已有值」，前端据此提示「来自环境变量」
            "env": {
                "api_key": bool(os.environ.get("TMDB_API_KEY", "").strip()),
                "read_token": bool(os.environ.get("TMDB_READ_TOKEN", "").strip()),
                "proxy": bool(os.environ.get("TMDB_PROXY", "").strip()),
                "custom": bool(os.environ.get("LOGIN_BACKDROPS", "").strip()),
            },
        }

    @app.put("/api/login/backdrop/settings")
    async def backdrop_settings_put(request: Request, token: str = ""):
        """保存登录页背景配置：存 meta，立刻生效（不用重启容器）。"""
        authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        custom = payload.get("custom", [])
        if isinstance(custom, str):
            custom = [x.strip() for x in custom.replace(";", ",").split(",") if x.strip()]
        data = {
            "api_key": str(payload.get("api_key") or "").strip(),
            "read_token": str(payload.get("read_token") or "").strip(),
            "proxy": str(payload.get("proxy") or "").strip(),
            "custom": [str(x).strip() for x in (custom or []) if str(x).strip()],
            "language": str(payload.get("language") or "").strip() or "zh-CN",
        }
        db.set_meta("login_backdrop", json.dumps(data, ensure_ascii=False))
        backdrop.configure(
            api_key=data["api_key"], read_token=data["read_token"],
            proxy=data["proxy"], custom=data["custom"], language=data["language"],
        )
        log.info("登录页背景配置已更新（TMDB key=%s, 代理=%s, 自定义图片 %d 张）",
                 "有" if (data["api_key"] or data["read_token"]) else "无",
                 data["proxy"] or "-", len(data["custom"]))
        return {"ok": True, "settings": backdrop.settings(), "status": backdrop.status()}

    @app.post("/api/login/backdrop/test")
    def backdrop_test(request: Request, token: str = ""):
        """立刻拉一次片单，把结果（张数 / 失败原因）直接回给设置面板。"""
        authorize(request, token)
        if not backdrop.available:
            return JSONResponse(
                {"ok": False, "count": 0, "reason": "no_tmdb_key",
                 "status": backdrop.status()},
                headers={"Cache-Control": "no-store"},
            )
        urls = backdrop.urls(refresh=True)
        return JSONResponse(
            {"ok": bool(urls), "count": len(urls),
             "sample": (urls[0] if urls else ""),
             "reason": "" if urls else "fetch_failed",
             "status": backdrop.status()},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/session")
    def session(request: Request):
        """当前会话状态；前端据此决定显示登录页还是主界面。"""
        user = session_user(request)
        return {
            "authenticated": user is not None,
            "enabled": auth.enabled,
            "username": user or "",
            "site_name": cfg.web_site_name,
            "version": state.get("version", ""),
            "show_hint": cfg.web_show_hint,
        }

    # ------------------------------------------------------------------ 媒体服务器
    @app.get("/api/media-servers")
    def media_servers_list(request: Request, token: str = ""):
        authorize(request, token)
        return {"servers": db.list_media_servers()}

    @app.post("/api/media-servers")
    async def media_servers_save(request: Request, name: str = "", host: str = "",
                                 api_key: str = "", enabled: bool = True,
                                 note: str = "", sid: str = "", token: str = ""):
        authorize(request, token)
        # 表单与 JSON 都支持
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - 表单提交走查询参数
            payload = {}
        if isinstance(payload, dict):
            name = payload.get("name", name) or ""
            host = payload.get("host", host) or ""
            api_key = payload.get("api_key", api_key) or ""
            note = payload.get("note", note) or ""
            # 编辑已有配置时前端传 sid（也兼容 id）：两个都认，
            # 否则编辑一次就会因为 id 丢失而新增一条配置
            sid = payload.get("id") or payload.get("sid") or sid
            if "enabled" in payload:
                enabled = bool(payload["enabled"])
        host = (host or "").strip().rstrip("/")
        if not host:
            raise HTTPException(status_code=400, detail="Emby 地址不能为空")
        if not host.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="Emby 地址要以 http:// 或 https:// 开头")
        sid = (sid or "").strip() or ("srv_" + uuid.uuid4().hex[:10])
        db.upsert_media_server({
            "id": sid, "name": (name or "").strip() or host,
            "host": host, "api_key": (api_key or "").strip(),
            "enabled": bool(enabled), "note": (note or "").strip(),
            "created_at": int(time.time()),
        })
        return {"ok": True, "server": db.get_media_server(sid)}

    @app.delete("/api/media-servers/{sid}")
    def media_servers_delete(sid: str, request: Request, token: str = ""):
        authorize(request, token)
        if not db.delete_media_server(sid):
            raise HTTPException(status_code=404, detail="没有这个媒体服务器")
        return {"ok": True}

    # ------------------------------------------------------------------ Emby 反代
    def proxy_settings() -> dict[str, Any]:
        """反代配置：网页上改过就以网页为准，否则回落到环境变量。"""
        raw = db.get_meta("proxy_settings", "")
        saved: dict[str, Any] = {}
        if raw:
            try:
                saved = json.loads(raw)
            except json.JSONDecodeError:
                saved = {}
        base = {
            "enabled": bool(cfg.emby_enabled),
            "upstream": cfg.emby_url,
            # strm 挂载前缀：Emby 里 strm 目录的挂载路径，用于识别哪些
            # MediaSource 是本服务生成的 strm —— 与客户端地址无关
            "strm_prefix": cfg.emby_strm_prefix or "",
            "path_rules": list(cfg.emby_path_rules),
            "cache_seconds": max(60, cfg.play_cache_seconds),
            "proxy_port": cfg.emby_proxy_port,
            "log_level": "info",
            "base_url": str(store.base_url or ""),
        }
        base.update({k: v for k, v in saved.items() if k in base})
        return base

    @app.get("/api/proxy")
    def proxy_get(request: Request, token: str = ""):
        authorize(request, token)
        return {"settings": proxy_settings(), "servers": db.list_media_servers()}

    @app.put("/api/proxy")
    async def proxy_put(request: Request, token: str = ""):
        """手动修改反代配置（需求：反代变更为可手动修改）。"""
        authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        current = proxy_settings()
        for key in ("enabled", "upstream", "strm_prefix", "cache_seconds",
                    "proxy_port", "log_level"):
            if key in payload:
                current[key] = payload[key]
        if "path_rules" in payload:
            raw = payload["path_rules"]
            if isinstance(raw, str):
                current["path_rules"] = [x.strip() for x in raw.splitlines() if "=>" in x]
            else:
                current["path_rules"] = [str(x) for x in (raw or [])]
        current["upstream"] = str(current.get("upstream") or "").rstrip("/")
        current["strm_prefix"] = str(current.get("strm_prefix") or "")
        try:
            current["cache_seconds"] = max(0, int(current.get("cache_seconds") or 0))
        except (TypeError, ValueError):
            current["cache_seconds"] = 900
        try:
            current["proxy_port"] = max(0, int(current.get("proxy_port") or 0))
        except (TypeError, ValueError):
            current["proxy_port"] = 0
        if "base_url" in payload:
            current["base_url"] = str(payload.get("base_url") or "").rstrip("/")
        db.set_meta("proxy_settings", json.dumps(current, ensure_ascii=False))
        if emby is not None:
            emby.apply_settings(current)
            # 独立端口：网页上改完立即生效（不用重启容器）
            runner = getattr(emby, "port_runner", None)
            if runner is not None:
                runner.apply(current.get("proxy_port") or 0)
        # 主引擎的 strm 输出地址也跟着改（反代对外地址写在 strm 里）
        if payload.get("base_url"):
            store.base_url = str(payload["base_url"]).rstrip("/")
            db.set_meta("public_base_url", store.base_url)
        return {"ok": True, "settings": current}

    @app.get("/api/proxy/check")
    async def proxy_check(request: Request, token: str = ""):
        """检查反代链路是否真的通：上游 Emby + 独立端口监听状态。"""
        authorize(request, token)
        result: dict[str, Any] = {"ok": False, "upstream": {}, "port": {}}
        if emby is None:
            result["error"] = "反代未启用"
            return result
        upstream = emby.upstream
        result["upstream"]["url"] = upstream
        if not upstream:
            result["upstream"]["error"] = "没有配置上游 Emby 地址"
        else:
            try:
                resp = await emby.http_client().get(
                    upstream + "/System/Info/Public", timeout=8.0
                )
                result["upstream"]["status"] = resp.status_code
                result["upstream"]["ok"] = resp.status_code < 500
            except Exception as exc:  # noqa: BLE001
                result["upstream"]["error"] = f"上游不可达: {exc}"
        runner = getattr(emby, "port_runner", None)
        if runner is None:
            result["port"]["state"] = "未启动（反代对象不存在）"
        else:
            port = runner.port
            result["port"]["listening"] = port
            if port:
                result["port"]["state"] = f"已监听 {port}"
            else:
                result["port"]["state"] = "未启用独立端口（用本服务端口 /emby）"
        result["ok"] = bool(result["upstream"].get("ok"))
        return result

    # ------------------------------------------------------------------ 115 STRM 配置
    @app.get("/api/strm/events")
    def strm_events_list(request: Request, limit: int = 100, config_id: str = "",
                         token: str = ""):
        """115 STRM 的事件日志（生成 / 覆盖 / 补写 / 失败 / 配置变更）。

        以前只有前端内存日志，刷新页面就没了；现在落库，出问题能回查。
        """
        authorize(request, token)
        return {"events": db.list_strm_events(limit=limit, config_id=config_id)}

    @app.delete("/api/strm/events")
    def strm_events_clear(request: Request, config_id: str = "", token: str = ""):
        authorize(request, token)
        return {"ok": True, "removed": db.clear_strm_events(config_id)}

    @app.get("/api/strm/configs")
    def strm_configs_list(request: Request, token: str = ""):
        authorize(request, token)
        return {"configs": db.list_strm_configs()}

    @app.post("/api/strm/configs")
    async def strm_configs_save(request: Request, token: str = ""):
        """新增 / 修改一条 115 STRM 配置（账号 + 网盘路径 → 生成 strm）。"""
        authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        cid = str(payload.get("id") or "").strip()
        if not cid:
            cid = "cfg_" + uuid.uuid4().hex[:10]
        existing = db.get_strm_config(cid)
        prev = existing or {}
        # account：payload 没带时保留原值（改其它字段不该把账号清空）
        if "account" in payload:
            account = str(payload.get("account") or "").strip()
        else:
            account = str(prev.get("account") or "").strip()
        # 网盘路径：一条配置可以挂多个（remote_cids / remote_paths），
        # 旧字段 remote_cid / remote_path 保留第一个，兼容老前端与老库
        if "remote_cids" in payload or "remote_cid" in payload:
            raw_cids = payload.get("remote_cids")
            if raw_cids is None:
                raw_cids = payload.get("remote_cid")
            raw_cids = _as_list(raw_cids)
            if "remote_paths" in payload:
                raw_paths = _as_list(payload.get("remote_paths"))
            else:
                raw_paths = _as_list(payload.get("remote_path"))
        else:
            raw_cids = [str(c) for c in strm_config_cids(prev)]
            raw_paths = strm_config_paths(prev)
        cids: list[str] = []
        for item in raw_cids:
            item = str(item or "").strip()
            if not item:
                continue
            if not item.isdigit():
                raise HTTPException(status_code=400, detail=f"网盘路径 id 必须是数字：{item}")
            if item not in cids:
                cids.append(item)
        paths = [str(p or "").strip() for p in (raw_paths or [])]
        # 路径按位置和 cid 对齐（不足的位置留空，生成时按目录名兜底）
        paths = (paths + [""] * len(cids))[: len(cids)]
        if not cids:
            raise HTTPException(status_code=400, detail="请选择网盘路径")

        # 「一个网盘路径绑定一个本地目录」：前端带 path_dirs 时以它为准，
        # remote_cids/remote_paths/local_dir 只是兼容字段（取第一项）
        path_dirs: list[dict[str, str]] = []
        raw_pairs = payload.get("path_dirs")
        if isinstance(raw_pairs, list):
            seen: set[str] = set()
            for item in raw_pairs:
                if not isinstance(item, dict):
                    continue
                pid = str(item.get("cid") or "").strip()
                if not pid.isdigit() or pid in seen:
                    continue
                seen.add(pid)
                path_dirs.append({
                    "cid": pid,
                    "path": str(item.get("path") or "").strip(),
                    "dir": str(item.get("dir") or "").strip(),
                })
        if path_dirs:
            cids = [p["cid"] for p in path_dirs]
            paths = [p["path"] for p in path_dirs]
            # 本地目录可以自由选，但只能落在 STRM_ROOT 里（相对路径视为已在根内）
            root = str(cfg.strm_root).rstrip("/") or "/"
            for p in path_dirs:
                d = p["dir"].rstrip("/")
                if d.startswith("/") and d != root and not d.startswith(root + "/"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"本地目录必须在映射根 {root} 内：{p['dir']}",
                    )

        def keep(key: str, default: str = "") -> str:
            """payload 没带这个字段时保留原值（改别的字段不该把它清空）。"""
            if key in payload:
                return str(payload.get(key) or "").strip()
            return str(prev.get(key) or default).strip()

        row = {
            "id": cid,
            "account": account,
            "name": str(payload.get("name") or "").strip() or prev.get("name") or "",
            "remote_cid": cids[0],
            "remote_cids": ",".join(cids),
            "remote_path": paths[0] if paths else "",
            "remote_paths": ",".join(paths),
            "local_dir": (path_dirs[0]["dir"] if path_dirs else keep("local_dir")),
            "path_dirs": (json.dumps(path_dirs, ensure_ascii=False)
                          if path_dirs else keep("path_dirs")),
            "base_url": keep("base_url"),
            "media_exts": keep("media_exts") or DEFAULT_MEDIA_EXTS,
            "meta_exts": keep("meta_exts") or DEFAULT_META_EXTS,
            "download_meta": payload.get("download_meta", prev.get("download_meta", True)),
            "event_watch": payload.get("event_watch", prev.get("event_watch", True)),
            "api_pull": payload.get("api_pull", prev.get("api_pull", True)),
            "redirect": payload.get("redirect", prev.get("redirect", True)),
            # 定时巡检：开关 + 间隔（秒）。间隔 0 = 用默认值（1 小时）
            "auto_scan": 1 if payload.get("auto_scan", prev.get("auto_scan", 0)) else 0,
            "scan_interval": max(0, int(
                payload.get("scan_interval", prev.get("scan_interval", 0)) or 0)),
            "status": str(payload.get("status") or prev.get("status") or "idle"),
            "last_run": int(payload.get("last_run") or prev.get("last_run") or 0),
            "files": int(payload.get("files") or prev.get("files") or 0),
        }
        if not row["name"]:
            row["name"] = row["remote_path"] or f"配置 {row['remote_cid']}"
        row["created_at"] = int((existing or {}).get("created_at") or time.time())
        db.upsert_strm_config(row)
        try:
            db.add_strm_event(
                "config_save",
                ("更新" if existing else "新增") + f"配置：{row['name']} · "
                f"{len(cids)} 个网盘路径 → {row['local_dir'] or '/strm'}",
                config_id=cid, account=account,
                detail=json.dumps({"path_dirs": path_dirs, "base_url": row["base_url"]},
                                  ensure_ascii=False),
            )
        except Exception:  # noqa: BLE001 - 记日志失败不影响保存
            pass
        # 请求头/本地目录改了就清掉扫描器的缓存，否则这一轮还在用旧的
        try:
            scanner.invalidate_roots()
            if account:
                _a_db, a_scanner, _s, _a = account_engine(account)
                a_scanner.invalidate_roots()
        except HTTPException:
            pass
        return {"ok": True, "config": db.get_strm_config(cid)}

    @app.delete("/api/strm/configs/{cid}")
    def strm_configs_delete(cid: str, request: Request, token: str = ""):
        authorize(request, token)
        row = db.get_strm_config(cid) or {}
        if not db.delete_strm_config(cid):
            raise HTTPException(status_code=404, detail="没有这条配置")
        try:
            db.add_strm_event("config_delete",
                              f"删除配置：{row.get('name') or cid}",
                              config_id=cid, account=str(row.get("account") or ""),
                              level="warn")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True}

    def _trigger_strm_config(cid: str, mode: str) -> dict[str, Any]:
        """按配置跑一次生成（网页按钮、通知指令共用）。"""
        cfg_row = db.get_strm_config(cid)
        if cfg_row is None:
            raise HTTPException(status_code=404, detail="没有这条配置")
        # 用这条配置所属账号的引擎：子账号有独立的扫描器与索引库
        # （strm 输出目录是共用的，完全由配置里的「本地 STRM 目录」决定）
        acc_db, acc_scanner, acc_store, _acc_api = account_engine(
            str(cfg_row.get("account") or ""))
        # STRM_ROOT 的目录名（通常就是 strm）用来剥掉用户填的挂载路径首段，
        # 否则 /strm + 115-原档 会生成 /strm/strm/115-原档
        root_name = Path(str(getattr(acc_store, "root", "") or "strm")).name or "strm"
        jobs = _strm_config_jobs(cfg_row, root_name)
        if not jobs:
            raise HTTPException(status_code=400, detail="这条配置还没有网盘路径")
        # mode：
        #   inc     = 增量（按目录指纹跳过没变化的子树）
        #   full    = 全量（清索引重扫，strm 全部重新写一遍 —— 内容没变也重写）
        #   rebuild = 全新生成（先把旧 strm 删掉，再从头生成，文件的 mtime 全新）
        if mode not in ("inc", "full", "rebuild"):
            mode = "inc"
        full = mode in ("full", "rebuild")
        rebuild = mode == "rebuild"
        acc_name = str(cfg_row.get("account") or "")
        cfg_label = str(cfg_row.get("name") or cid)
        MODE_TEXT = {"inc": "增量生成", "full": "全量生成", "rebuild": "全新生成"}

        def event(kind: str, message: str, level: str = "info",
                  detail: str = "") -> None:
            try:
                db.add_strm_event(kind, message, config_id=cid, account=acc_name,
                                  level=level, detail=detail)
            except Exception:  # noqa: BLE001 - 记日志失败绝不影响主流程
                pass
            # 同一个事件再推一份到企业微信 / TG（渠道和事件开关在「通知与联动」里配）
            ntf = plugin_obj("notify")
            if ntf is not None and kind in ("run_start", "run_done", "run_error"):
                try:
                    ntf.send(f"{cfg_label}：{message}", event=kind, level=level)
                except Exception as exc:  # noqa: BLE001 - 通知失败不影响生成
                    log.debug("生成事件通知失败: %s", exc)

        def run() -> None:
            started = time.time()
            try:
                files = 0
                written = 0
                overwritten = 0
                fixed = 0
                cleared = 0
                removed_rows: list[dict] = []
                event("run_start", f"{MODE_TEXT[mode]}开始：{cfg_label}")
                for remote_cid, remote_path, prefix in jobs:
                    acc_db.add_root(remote_cid, remote_path or cfg_row["name"], prefix)
                    # 目录已存在时 INSERT OR IGNORE 不会改前缀，这里显式同步一次
                    acc_db.set_root_prefix(remote_cid, prefix)
                    acc_scanner.invalidate_roots()
                    # 生成前先把目标目录建出来（用户填的目录可能还不存在）
                    try:
                        target = (acc_store.abspath(prefix) if prefix
                                  else Path(acc_store.root))
                        target.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        log.warning("创建 strm 目录失败 %s: %s", prefix, exc)
                    if full:
                        # 全量：清掉这棵树的旧索引再重扫，避免改名/删除的残留
                        # （返回被删掉的文件行，全新生成要按里面的 strm_path 删文件）
                        removed_rows = acc_db.delete_dir_tree(remote_cid)
                    if rebuild:
                        # 全新生成：只删**本配置索引里记录过**的 strm，让「115 上
                        # 已删掉、盘上还剩」的残留也被清掉。这里刻意不按目录整片删：
                        # 多条配置常共用同一个输出目录（local_dir 填 /strm 时前缀
                        # 就是空串），整片删会把别的账号的 strm 一起带走。
                        try:
                            cleared += acc_store.remove_strm_paths(
                                [r.get("strm_path") for r in removed_rows])
                        except Exception as exc:  # noqa: BLE001 - 删不掉就退化成全量重写
                            log.warning("清理旧 strm 失败 %s: %s", prefix, exc)
                    stats = acc_scanner.scan_subtree(remote_cid, prefix, force=full)
                    files += stats.files
                    written += stats.written
                    overwritten += stats.overwritten
                    if stats.errors:
                        event("run_warn",
                              f"{cfg_label}：目录 {remote_path or remote_cid} 有 "
                              f"{stats.errors} 个目录读取失败", level="warn")
                    # 之前生成过、后来被手工删掉的 strm，全量/全新时补回来
                    try:
                        fixed += acc_scanner.repair_missing(prefix)
                    except Exception as exc:  # noqa: BLE001 - 补写失败不影响主流程
                        log.warning("补写 strm 失败 %s: %s", prefix, exc)
                db.update_strm_config(cid, status="idle", last_run=int(time.time()),
                                      files=files)
                cost = round(time.time() - started, 1)
                event("run_done",
                      f"{MODE_TEXT[mode]}完成：{cfg_label} · {files} 个文件 · "
                      f"写入 {written}（重写 {overwritten}）"
                      + (f" · 清理旧文件 {cleared}" if rebuild else "")
                      + (f" · 补写 {fixed}" if fixed else "")
                      + f" · 用时 {cost}s",
                      detail=json.dumps({
                          "mode": mode, "jobs": len(jobs), "files": files,
                          "written": written, "overwritten": overwritten,
                          "cleared": cleared, "repaired": fixed,
                          "seconds": cost,
                      }, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 - 后台任务失败只记状态
                log.warning("115 STRM 配置 %s 生成失败: %s", cid, exc)
                db.update_strm_config(cid, status="error")
                event("run_error", f"{MODE_TEXT[mode]}失败：{cfg_label} · {exc}",
                      level="err")

        threading.Thread(target=run, name=f"strm-cfg-{cid}", daemon=True).start()
        db.update_strm_config(cid, status="running")
        return {"ok": True, "accepted": True, "mode": mode}

    # 定时巡检器：只算时间，真跑还是走上面那份 _trigger_strm_config，
    # 这样「手动点增量」和「到点自动跑」走的完全是同一条链路。
    scheduler = StrmConfigScheduler(
        db, _trigger_strm_config, busy=lambda: lock.locked()
    )
    # 线程由 run_with_server 起来（create_app 在测试里也会被调用，不该自带后台线程）
    app.state.strm_scheduler = scheduler

    @app.post("/api/strm/configs/{cid}/run")
    async def strm_configs_run(cid: str, request: Request, token: str = ""):
        """按配置跑一次生成（full=全量 / 增量 / rebuild=全新）。"""
        authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        mode = str(payload.get("mode") or "").lower()
        if bool(payload.get("full")) or mode == "full":
            mode = "full"
        return _trigger_strm_config(cid, mode)

    # 定时巡检：每条配置到点自动跑一次增量。这里只建对象，
    # 真正起线程在 run_with_server 里（测试 / 纯扫描模式不该有这玩意在后台转）。
    @app.get("/api/strm/scheduler")
    def strm_scheduler_status(request: Request, token: str = ""):
        authorize(request, token)
        return {"scheduler": scheduler.status()}

    # ------------------------------------------------------------------ 插件
    # 天翼云盘 / 通知与联动 / 企微可信IP 都是插件：路由由插件自己在
    # plugins/<id>/backend.py 里注册，这里只挂「插件管理」这一层。
    # 插件没装 → 路由不存在 → 前端导航也没有入口。
    if plugins is not None:
        @app.get("/api/plugins")
        def plugins_status(request: Request, token: str = ""):
            authorize(request, token)
            return plugins.status()

        @app.post("/api/plugins/{pid}/install")
        def plugins_install(pid: str, request: Request, token: str = ""):
            authorize(request, token)
            try:
                return plugins.install(pid)
            except PluginError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.post("/api/plugins/{pid}/uninstall")
        def plugins_uninstall(pid: str, request: Request, purge: int = 0,
                              token: str = ""):
            """卸载。``purge=1`` 连下载好的依赖一起删（Chromium 有 600MB）。"""
            authorize(request, token)
            return plugins.uninstall(pid, purge=bool(purge))

        @app.post("/api/plugins/{pid}/enable")
        async def plugins_enable(pid: str, request: Request, token: str = ""):
            authorize(request, token)
            try:
                payload = await request.json()
            except Exception:  # noqa: BLE001 - 空 body 当空字典
                payload = {}
            try:
                return plugins.set_enabled(pid, bool(payload.get("enabled", True)))
            except PluginError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @app.get("/api/plugins/{pid}/log")
        def plugins_log(pid: str, request: Request, token: str = ""):
            authorize(request, token)
            return plugins.task_log(pid)

        @app.get("/plugin-assets/{pid}.js")
        def plugin_asset(pid: str):
            """插件的前端 JS。只认已安装的插件；纯静态代码，不校验登录。"""
            path = plugins.web_asset(pid)
            if path is None:
                raise HTTPException(status_code=404, detail="插件未安装")
            return FileResponse(str(path), media_type="application/javascript",
                                headers={"Cache-Control": "no-cache"})

        # 插件在这时候才挂上来：它们要用的 authorize 与核心对象已经齐了。
        # 顺序也重要 —— 通知插件先加载，企微插件才能拿到 notifier 发消息。
        plugins.attach(app, authorize)
        plugins.load_all()
        # 通知指令里的 /run 复用同一套生成触发逻辑（跟网页按钮行为一致）
        ntf = plugin_obj("notify")
        if ntf is not None:
            ntf.run_config = _trigger_strm_config
            ntf.status_provider = status

    # ------------------------------------------------------------------ 首页
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        """SPA 入口：经典管理面已移除，页面交给前端路由。"""
        if not spa_root:
            # 子账号挂在 /a/<名字>/ 下：统一跳回主页面，由前端内部切换账号
            return RedirectResponse("/", status_code=302)
        index_file = WEB_DIR / "index.html"
        if not index_file.exists():
            return HTMLResponse(
                "<h1>PanShare</h1><p>前端资源缺失（web/index.html），"
                "请重新构建镜像。</p>",
                status_code=500,
            )
        # 资源 URL 带版本号：升级镜像后浏览器不会继续用旧的 JS/CSS。
        # 版本号 = 应用版本 + 前端文件的最新修改时间：改了前端但没改版本号时，
        # 浏览器也会拿到新文件（否则改了代码刷新还是旧的）。
        version = str(state.get("version") or "0")
        version += "-" + _web_stamp()
        page = index_file.read_text(encoding="utf-8").replace("{{VERSION}}", version)
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    if hub is not None:
        hub.bind_app(app)

    # 静态资源（css / js）
    if spa_root and WEB_DIR.exists():
        app.mount("/static", _Static(directory=str(WEB_DIR)), name="static")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):  # pragma: no cover
        log.exception("未处理异常 %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    return app


def create_proxy_app(emby: EmbyProxy) -> FastAPI:
    """Emby 反代的独立端口入口。

    客户端把服务器地址直接填 ``http://<本机>:<端口>``（**不带任何路径前缀**），
    所有路径都原样转发到上游 Emby，并在 PlaybackInfo 上做 302 改写。

    上游 Emby 的 BaseUrl 可能是 ``/`` 也可能是 ``/emby``：路径完整透传时
    两种都能命中（Emby 客户端请求什么前缀就原样送过去），所以这里不做任何
    前缀剥离。

    另外这个端口顺带承担**天翼云盘(189)的直链换发**：反代改写 PlaybackInfo
    时把天翼 strm 的地址换成「本端口 + /p189/play/...」，播放器来这里要
    新鲜直链，这里 302 到天翼 CDN（视频流量不经过 NAS）。反代自己不认得
    ``/p189`` 的话，这个请求会被转给 Emby 然后 404。
    """
    from fastapi import FastAPI

    app = FastAPI(title="PanShare emby-proxy", docs_url=None, redoc_url=None,
                  openapi_url=None)
    methods = ["GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS"]

    @app.api_route("/p189/play/{tail:path}", methods=["GET", "HEAD"])
    async def proxy_cloud189(tail: str, request: Request):
        mgr = getattr(emby, "cloud189", None)
        if mgr is None:
            raise HTTPException(status_code=404, detail="天翼云盘未启用")
        # 换直链是同步 HTTP 调用，丢线程池里跑，别卡住 Emby 转发的循环
        return await asyncio.to_thread(
            _serve_cloud189, emby.cfg, mgr, request, tail
        )

    @app.websocket("/{path:path}")
    async def proxy_ws(websocket: WebSocket, path: str):
        """Web 客户端的 WebSocket（/embywebsocket）也要桥到上游。

        不做这一步时：网页能打开、能选账号，但登录后连不上服务器，
        播放页（#/videoosd）起不来 —— 看起来就像「反代没成功」。
        """
        await emby.handle_ws(websocket, path)

    @app.api_route("/{path:path}", methods=methods)
    async def proxy_root(path: str, request: Request):
        return await emby.handle(request, path)

    return app


class ProxyPortRunner:
    """Emby 反代独立端口的运行时管理器。

    网页上改「监听端口」后立即生效：先停掉旧端口的服务器，再在新端口起一个，
    不用重启容器。``uvicorn.Server`` 的 ``should_exit`` 置 True 就能优雅退出。
    """

    def __init__(self, cfg: Config, emby: EmbyProxy):
        self.cfg = cfg
        self.emby = emby
        self._lock = threading.Lock()
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._port = 0

    @property
    def port(self) -> int:
        """当前实际监听的端口（0 = 未启动）。"""
        with self._lock:
            return self._port

    def apply(self, port: int) -> None:
        """把独立端口切到 ``port``（0 = 关闭）。"""
        try:
            port = int(port or 0)
        except (TypeError, ValueError):
            port = 0
        if port < 0 or port > 65535:
            port = 0
        with self._lock:
            if port == self._port:
                return
            self._stop_locked()
            self._port = port
            if port:
                self._start_locked(port)

    def _start_locked(self, port: int) -> None:
        import uvicorn

        app = create_proxy_app(self.emby)
        config = uvicorn.Config(app, host=self.cfg.web_host, port=port,
                                log_config=None, access_log=False,
                                timeout_keep_alive=30)
        server = uvicorn.Server(config)
        self._server = server
        self._thread = threading.Thread(
            target=server.run, name="emby-proxy", daemon=True
        )
        self._thread.start()
        log.info("Emby 反代独立端口监听 %s:%d（客户端直填该端口即可，不用加前缀）",
                 self.cfg.web_host, port)

    def _stop_locked(self) -> None:
        server, thread = self._server, self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)
        if server is not None:
            # 端口换了：这个端口用过的 httpx 连接池跟着事件循环一起失效了，
            # 清掉免得越积越多
            try:
                self.emby.drop_clients()
            except Exception as exc:  # noqa: BLE001 - 清理失败不影响主流程
                log.debug("清理反代客户端失败: %s", exc)
            log.info("Emby 反代独立端口已关闭")
