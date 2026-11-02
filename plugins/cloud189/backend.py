"""天翼云盘（189）插件后端。

路由全部挂在 ``/api/cloud189*`` 上（老前端不用改），播放地址
``/p189/play/...`` 留在主服务里：它已经写进了历史 strm 文件，不能挪走。

依赖只有标准库 + httpx（核心本来就有），所以这个插件装起来是**瞬间完成**的
——「安装」在这里的意义是「启用」，让不想用天翼云盘的人眼前干净点。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request

# 生成 strm 时默认抓哪些媒体后缀（和 115 那套保持一致）
DEFAULT_MEDIA_EXTS = "mp4,mkv,ts,iso,rmvb,avi,mov,wmv,m2ts"


def setup(api: APIRouter, ctx: Any) -> Any:
    """建管理器 + 注册路由。返回的管理器会被主服务拿去处理 /p189/play。"""
    core = ctx.mod("cloud189")
    manager = ctx.override or core.Cloud189Manager(ctx.cfg, ctx.db, ctx.store)
    if ctx.override is None:
        manager.start_auto_refresh()
    build_routes(api, ctx, manager, core)
    return manager


def build_routes(api: APIRouter, ctx: Any, cloud189: Any, core: Any) -> None:
    db, store = ctx.db, ctx.store
    root_id = core.ROOT_FOLDER_ID

    def guard(exc: Exception) -> HTTPException:
        return HTTPException(status_code=502, detail=str(exc))

    def body_account(payload: dict, account: str = "") -> str:
        """请求体里的账号；没带就用 query 上的。"""
        return str(payload.get("account") or account or "").strip()

    # ------------------------------------------------------------------ 状态
    @api.get("/cloud189")
    def cloud189_status(request: Request, token: str = ""):
        ctx.authorize(request, token)
        return cloud189.status()

    @api.get("/cloud189/accounts")
    def cloud189_accounts(request: Request, token: str = ""):
        ctx.authorize(request, token)
        return {"accounts": cloud189.status()["accounts"],
                "default": cloud189.default_id()}

    # ------------------------------------------------------------------ 登录
    @api.post("/cloud189/login/start")
    def cloud189_login_start(request: Request, token: str = ""):
        """取登录二维码：返回 uuid + 二维码矩阵（前端自己画 SVG）。"""
        ctx.authorize(request, token)
        try:
            return cloud189.login_start()
        except core.Cloud189Error as exc:
            raise guard(exc) from exc

    @api.get("/cloud189/login/poll")
    def cloud189_login_poll(request: Request, uuid: str = "",
                            account: str = "", token: str = ""):
        """轮询扫码。``account`` 为空 = 新建账号，非空 = 给这个账号换 token。"""
        ctx.authorize(request, token)
        try:
            return cloud189.login_poll(uuid, account)
        except core.Cloud189Error as exc:
            raise guard(exc) from exc

    @api.get("/cloud189/login/debug")
    def cloud189_login_debug(request: Request, token: str = ""):
        """扫码轮询的原始往来（最近 40 次），扫码没反应时用它定位。"""
        ctx.authorize(request, token)
        return {"log": cloud189.qr_log()}

    @api.post("/cloud189/cookie")
    async def cloud189_set_cookie(request: Request, token: str = ""):
        """用天翼网页版的 COOKIE_LOGIN_USER 换 token（能自动续期的那条路）。"""
        ctx.authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - 空 body 当空字典
            payload = {}
        try:
            return cloud189.set_cookie(str(payload.get("cookie") or ""),
                                       body_account(payload))
        except core.Cloud189Error as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/cloud189/token")
    async def cloud189_set_token(request: Request, token: str = ""):
        """手动填 accessToken（扫码不可用时的兜底入口，不能自动续期）。"""
        ctx.authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        try:
            return cloud189.set_token(
                str(payload.get("token") or ""),
                payload.get("expires_in") or 0,
                body_account(payload))
        except core.Cloud189Error as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/cloud189/logout")
    def cloud189_logout(request: Request, account: str = "", token: str = ""):
        """退出登录（默认账号）：清 token 与会话，账号记录和配置都留着。"""
        ctx.authorize(request, token)
        return cloud189.logout(account)

    # ------------------------------------------------------------------ 账号
    @api.post("/cloud189/accounts/{aid}/logout")
    def cloud189_account_logout(aid: str, request: Request, token: str = ""):
        ctx.authorize(request, token)
        return cloud189.logout(aid)

    @api.post("/cloud189/accounts/{aid}/default")
    def cloud189_account_default(aid: str, request: Request, token: str = ""):
        ctx.authorize(request, token)
        try:
            return cloud189.set_default(aid)
        except core.Cloud189Error as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.post("/cloud189/accounts/{aid}/refresh")
    def cloud189_account_refresh(aid: str, request: Request, token: str = ""):
        """手动续期一次（用保存的 sessionKey 换新 token）。"""
        ctx.authorize(request, token)
        try:
            cloud189.refresh_account(aid, reason="manual")
        except core.Cloud189Error as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        row = next((x for x in cloud189.status()["accounts"] if x["id"] == aid), None)
        return {"ok": True, "account": row}

    @api.post("/cloud189/accounts/{aid}/auto-refresh")
    async def cloud189_account_auto_refresh(aid: str, request: Request,
                                            token: str = ""):
        ctx.authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        try:
            return cloud189.set_auto_refresh(aid, bool(payload.get("enabled", True)))
        except core.Cloud189Error as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @api.delete("/cloud189/accounts/{aid}")
    def cloud189_account_delete(aid: str, request: Request, force: int = 0,
                                token: str = ""):
        """删账号。``force=1`` 时名下配置一起删（已生成的 strm 保留）。"""
        ctx.authorize(request, token)
        try:
            return cloud189.delete_account(aid, force=bool(force))
        except core.Cloud189Error as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------ 目录
    @api.get("/cloud189/dirs")
    def cloud189_dirs(request: Request, folder_id: str = "",
                      account: str = "", token: str = ""):
        """列云端目录（个人云 + 家庭云）。

        ``folder_id`` 可以是 ``-11``（个人云根）、数字目录 id、
        ``fam``（家庭云列表）、``fam:<家庭id>[:<目录id>]``。
        """
        ctx.authorize(request, token)
        try:
            return cloud189.list_dirs(folder_id or root_id, account)
        except core.Cloud189Error as exc:
            raise guard(exc) from exc

    # ------------------------------------------------------------------ 配置
    @api.get("/cloud189/configs")
    def cloud189_configs(request: Request, token: str = ""):
        ctx.authorize(request, token)
        return {"configs": db.list_cloud189_configs()}

    @api.post("/cloud189/configs")
    async def cloud189_configs_save(request: Request, token: str = ""):
        """新增 / 修改一条天翼云盘生成配置（云端目录 → 本地映射目录）。"""
        ctx.authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        cid = str(payload.get("id") or "").strip()
        if not cid:
            cid = "c189_" + uuid.uuid4().hex[:10]
        prev = db.get_cloud189_config(cid) or {}

        def keep(key: str, default: str = "") -> str:
            if key in payload:
                return str(payload.get(key) or "").strip()
            return str(prev.get(key) or default).strip()

        remote_id = keep("remote_id", root_id) or root_id
        remote_path = keep("remote_path")
        name = keep("name")
        local_dir = keep("local_dir")
        account_id = keep("account") or cloud189.default_id()
        if account_id and not cloud189.is_account(account_id):
            raise HTTPException(status_code=400, detail="选中的天翼云盘账号不存在")
        if not local_dir:
            raise HTTPException(status_code=400, detail="请选择本地 STRM 输出目录")
        if "all_files" in payload:
            all_files = bool(payload.get("all_files"))
        else:
            all_files = bool(prev.get("all_files"))
        row = {
            "id": cid,
            "account": account_id,
            "name": name or remote_path or f"天翼目录 {remote_id}",
            "remote_id": remote_id,
            "remote_path": remote_path,
            "local_dir": local_dir,
            "base_url": keep("base_url"),
            "media_exts": keep("media_exts") or DEFAULT_MEDIA_EXTS,
            "all_files": all_files,
            "status": str(prev.get("status") or "idle"),
            "last_run": int(prev.get("last_run") or 0),
            "files": int(prev.get("files") or 0),
        }
        db.upsert_cloud189_config(row)
        cloud189.event(
            "config_save",
            ("更新" if prev else "新增")
            + f"配置：{row['name']} · {remote_path or remote_id} → {local_dir}",
            config_id=cid,
            detail=json.dumps({"remote_id": remote_id, "local_dir": local_dir,
                               "all_files": all_files,
                               "account": account_id}, ensure_ascii=False),
        )
        return {"ok": True, "config": db.get_cloud189_config(cid)}

    @api.delete("/cloud189/configs/{cid}")
    def cloud189_configs_delete(cid: str, request: Request,
                                purge: int = 0, token: str = ""):
        """删除配置。``purge=1`` 时把它生成过的 strm 一起删掉。"""
        ctx.authorize(request, token)
        row = db.get_cloud189_config(cid)
        if row is None:
            raise HTTPException(status_code=404, detail="没有这条配置")
        removed = 0
        if purge:
            removed = store.remove_strm_paths(
                [r.get("strm_path") for r in db.list_cloud189_files(cid)])
        db.delete_cloud189_config(cid)
        cloud189.event("config_delete",
                       f"删除配置：{row.get('name') or cid}"
                       + (f"（同时删除 {removed} 个 strm）" if removed else ""),
                       config_id=cid)
        return {"ok": True, "removed": removed}

    @api.post("/cloud189/configs/{cid}/run")
    async def cloud189_configs_run(cid: str, request: Request, token: str = ""):
        """跑一次生成：mode = inc（增量）/ full（全量）/ rebuild（全新）。"""
        ctx.authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        mode = str(payload.get("mode") or "inc").lower()
        try:
            return cloud189.run_config(cid, mode)
        except core.Cloud189Error as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/cloud189/events")
    def cloud189_events(request: Request, limit: int = 100,
                        config_id: str = "", token: str = ""):
        ctx.authorize(request, token)
        return {"events": db.list_cloud189_events(limit, config_id)}

    @api.delete("/cloud189/events")
    def cloud189_events_clear(request: Request, config_id: str = "",
                              token: str = ""):
        ctx.authorize(request, token)
        return {"ok": True, "removed": db.clear_cloud189_events(config_id)}
