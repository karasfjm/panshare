"""企微「企业可信 IP」插件后端。

重依赖（playwright + Chromium）由插件管理器装到 ``DATA_DIR/plugins/wxip/``，
所以在没装这个插件的机器上，``app/wxip.py`` 都不会被 import，更不会有浏览器。

发通知走「通知与联动」插件：``ctx.get("notify")``。没装通知插件也能用，
只是 IP 变了不会推送消息。
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Request


def setup(api: APIRouter, ctx: Any) -> Any:
    core = ctx.mod("wxip")
    manager = ctx.override or core.WxIpManager(ctx.cfg, ctx.db,
                                               notifier=ctx.get("notify"))
    build_routes(api, ctx, manager)
    return manager


def run(manager: Any, stop: threading.Event) -> None:
    manager.run_forever(stop)


def build_routes(api: APIRouter, ctx: Any, wxip: Any) -> None:
    def guard(request: Request, token: str = "") -> None:
        ctx.authorize(request, token)

    @api.get("/wxip")
    def wxip_status(request: Request, token: str = ""):
        guard(request, token)
        return wxip.status()

    @api.post("/wxip/settings")
    async def wxip_save(request: Request, token: str = ""):
        guard(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - 空 body 当空字典
            payload = {}
        wxip.save_settings(payload if isinstance(payload, dict) else {})
        return {"ok": True, "status": wxip.status()}

    @api.post("/wxip/login")
    def wxip_login(request: Request, token: str = ""):
        """开始扫码登录：后台开浏览器、截二维码，前端轮询 /api/wxip 取图。"""
        guard(request, token)
        return wxip.request_login()

    @api.post("/wxip/update")
    def wxip_update(request: Request, token: str = ""):
        guard(request, token)
        return wxip.request_update()

    @api.post("/wxip/shot")
    def wxip_shot(request: Request, token: str = ""):
        """诊断用：让浏览器截一张当前页面，稍后从 /api/wxip 的 shot 里取。"""
        guard(request, token)
        return wxip.request_shot()

    @api.post("/wxip/logout")
    def wxip_logout(request: Request, token: str = ""):
        guard(request, token)
        return wxip.logout()
