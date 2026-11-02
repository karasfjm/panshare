"""通知与联动插件后端。

一个插件管两件事：``Notifier``（企微 / TG / Webhook 推送 + 聊天指令）和
``LinkManager``（双向联动删除）。两者本来就耦合 —— 联动发现有文件要删时
要靠通知发消息 —— 所以放一个插件里，卸载时一起收走。

返回的对象是 ``Notifier`` 本身（它带 ``linker`` 属性）：主服务拿它当通知器
用，企微可信IP 插件也通过 ``ctx.get("notify")`` 拿到它来发消息。
"""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse


def setup(api: APIRouter, ctx: Any) -> Any:
    core = ctx.mod("notify")
    linker_core = ctx.mod("linker")
    engines = ctx.engines or (list)
    linker = linker_core.LinkManager(ctx.cfg, ctx.db, engines=engines)
    notifier = core.Notifier(ctx.cfg, ctx.db, hub=ctx.hub, linker=linker,
                             engines=engines)
    linker.notifier = notifier
    build_routes(api, ctx, notifier, linker)
    return notifier


def run(notifier: Any, stop: threading.Event) -> None:
    """后台线程：联动巡检 + 通知指令长轮询（TG）。"""
    linker = getattr(notifier, "linker", None)
    if linker is not None:
        threading.Thread(target=linker.run_forever, args=(stop,),
                         name="linker", daemon=True).start()
    notifier.run_forever(stop)


def build_routes(api: APIRouter, ctx: Any, notifier: Any, linker: Any) -> None:
    # ------------------------------------------------------------------ 通知
    @api.get("/notify")
    def notify_status(request: Request, token: str = ""):
        ctx.authorize(request, token)
        return notifier.status()

    @api.post("/notify/settings")
    async def notify_save(request: Request, token: str = ""):
        ctx.authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - 空 body 当空字典
            payload = {}
        notifier.save_settings(payload if isinstance(payload, dict) else {})
        return {"ok": True, "status": notifier.status()}

    @api.post("/notify/test")
    def notify_test(request: Request, token: str = ""):
        ctx.authorize(request, token)
        return notifier.test()

    # 企业微信「接收消息服务器」回调：由微信服务器直接访问，
    # 不能要求登录态，安全性靠 msg_signature（token + aeskey）保证。
    @api.get("/notify/wecom")
    def wecom_verify(request: Request):
        try:
            return PlainTextResponse(
                notifier.wecom_verify(dict(request.query_params)))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.post("/notify/wecom")
    async def wecom_receive(request: Request):
        body = await request.body()
        try:
            text = notifier.wecom_receive(body, dict(request.query_params))
        except Exception:  # noqa: BLE001 - 回调不能 500，否则企微会一直重试
            text = "success"
        return PlainTextResponse(text)

    # ------------------------------------------------------------------ 联动
    @api.get("/link")
    def link_status(request: Request, token: str = ""):
        ctx.authorize(request, token)
        return linker.status()

    @api.post("/link/settings")
    async def link_save(request: Request, token: str = ""):
        ctx.authorize(request, token)
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            payload = {}
        return {"ok": True,
                "settings": linker.save_settings(payload if isinstance(payload, dict) else {})}

    @api.post("/link/check")
    def link_check(request: Request, token: str = "", force: int = 0,
                   background: int = 1):
        """立即检查一轮：默认后台跑（删 115 文件要逐条调接口，会慢）。"""
        ctx.authorize(request, token)
        if background:
            def work() -> None:
                try:
                    linker.scan_once(force=bool(force))
                except Exception:  # noqa: BLE001 - 后台任务失败只记日志
                    ctx.mod("linker").log.warning("手动联动检查失败", exc_info=True)

            threading.Thread(target=work, name="link-check", daemon=True).start()
            return {"ok": True, "accepted": True}
        return {"ok": True, "result": linker.scan_once(force=bool(force))}

    @api.get("/link/pending")
    def link_pending(request: Request, token: str = "", limit: int = 200):
        ctx.authorize(request, token)
        return {"items": linker.pending(limit=max(1, min(limit, 1000)))}

    @api.post("/link/commit")
    def link_commit(request: Request, token: str = "", limit: int = 0):
        """确认删除待删列表（不等宽限期）。"""
        ctx.authorize(request, token)

        def work() -> None:
            try:
                linker.commit(limit=limit)
            except Exception:  # noqa: BLE001
                ctx.mod("linker").log.warning("联动删除执行失败", exc_info=True)

        threading.Thread(target=work, name="link-commit", daemon=True).start()
        return {"ok": True, "accepted": True}
