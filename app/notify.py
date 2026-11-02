"""通知与指令交互：企业微信 / Telegram Bot。

* **推送**：任务开始 / 完成 / 失败、联动删除、扫描异常都可以发出去。
  企业微信优先用「群机器人 webhook」（零配置），没有就退回「自建应用消息」；
  Telegram 用 Bot API。
* **接收指令**：
  - Telegram 用 ``getUpdates`` 长轮询，**不需要公网**，配好 token + chat_id 就能用；
  - 企业微信用「接收消息服务器」回调，需要公网地址 + token/aeskey，
    加解密在 :mod:`app.wxcrypt` 里自己实现（容器没有 crypto 依赖）。

指令（两个渠道共用一套）：``/help /status /ls /run /find /pending /link /notify``。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

import httpx

from . import __version__ as APP_VERSION
from . import wxcrypt
from .config import Config
from .db import Database

log = logging.getLogger(__name__)

META_KEY = "notify_settings"
WECOM_TOKEN_META = "wecom_token_cache"
TG_API = "https://api.telegram.org"
WECOM_API = "https://qyapi.weixin.qq.com"

DEFAULT_EVENTS = {
    "run_start": False,     # 每次生成开始都推送太吵，默认关
    "run_done": True,
    "run_error": True,
    "link": True,
    "scan": False,
    "login": True,
    "wxip": True,           # 企微可信 IP 变化 / 更新失败
}

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "events": dict(DEFAULT_EVENTS),
    "wecom": {
        "enabled": False,
        # 群机器人（最简单）：https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
        "webhook": "",
        # 自建应用（支持接收指令，需要公网回调）
        "corp_id": "",
        "agent_id": "",
        "secret": "",
        "token": "",
        "aes_key": "",
        "to_user": "@all",
    },
    "tg": {
        "enabled": False,
        "token": "",
        "chat_id": "",
        "poll": True,
        # 国内直连不通时填代理，如 http://192.168.2.1:7890
        "proxy": "",
        # 允许发指令的用户（逗号分隔 user id 或 @username），留空 = 只认 chat_id
        "allow": "",
    },
    # 通用 Webhook（Emby / n8n / 任何收 JSON 的自动化）
    "webhook": {
        "enabled": False,
        "url": "",
        # 选了媒体服务器就自动带 Emby 的 api_key（X-Emby-Token），
        # 地址留空时默认推到 <Emby>/emby/Notifications/Admin（消息中心）
        "server": "",
        # 每行一个 "Key: Value"
        "headers": "",
        # 留空用默认模板；模板里可以用 {{title}} {{text}} {{event}} {{level}} {{date}} {{version}}
        "template": "",
        "timeout": 8.0,
    },
}

# 默认 payload：同时给 Emby Webhooks 插件风格（Event/Title/Description/Date/Server）
# 和 Emby 消息中心需要的 Name/Description/Level，两边都能认
DEFAULT_WEBHOOK_TEMPLATE = """{
  "Event": "panshare.{{event}}",
  "Name": "{{title}}",
  "Title": "{{title}}",
  "Description": "{{text}}",
  "Level": "{{level}}",
  "Date": "{{date}}",
  "Server": {"Name": "PanShare", "Id": "panshare", "Version": "{{version}}"}
}"""

HELP_TEXT = """PanShare 指令
/status   运行状态（索引、扫描、联动）
/ls       115 STRM 配置列表
/run <配置名|all> [inc|full|rebuild]  触发生成
/find <关键词>   搜索已索引的文件
/pending  联动待删除列表
/link     联动状态；/link on|off 开关「本地删 strm → 删网盘」
/notify on|off   通知开关
/help     这条帮助"""


def _merged(raw: Any) -> dict[str, Any]:
    out = json.loads(json.dumps(DEFAULTS))     # 深拷贝，别改到默认值
    if isinstance(raw, dict):
        if "enabled" in raw:
            out["enabled"] = bool(raw["enabled"])
        events = raw.get("events")
        if isinstance(events, dict):
            for key in DEFAULT_EVENTS:
                if key in events:
                    out["events"][key] = bool(events[key])
        for section in ("wecom", "tg", "webhook"):
            block = raw.get(section)
            if isinstance(block, dict):
                for key in out[section]:
                    if key in block and block[key] is not None:
                        out[section][key] = block[key]
    for section in ("wecom", "tg", "webhook"):
        out[section]["enabled"] = bool(out[section]["enabled"])
    out["tg"]["poll"] = bool(out["tg"]["poll"])
    out["wecom"]["agent_id"] = str(out["wecom"]["agent_id"] or "").strip()
    try:
        out["webhook"]["timeout"] = min(60.0, max(1.0, float(out["webhook"]["timeout"] or 8)))
    except (TypeError, ValueError):
        out["webhook"]["timeout"] = 8.0
    out["webhook"]["server"] = str(out["webhook"]["server"] or "").strip()
    return out


def notify_settings(db: Database) -> dict[str, Any]:
    raw = db.get_meta(META_KEY) or ""
    if not raw:
        return _merged(None)
    try:
        return _merged(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        log.warning("通知配置无法解析，使用默认值: %r", raw)
        return _merged(None)


def _fill(node: Any, vals: dict[str, str]) -> Any:
    """替换模板里的 ``{{key}}`` 占位符。

    只动 JSON 的字符串值，绝不把正文拼进 JSON 文本 —— 通知正文里带引号 /
    换行也不会让 payload 变成非法 JSON。
    """
    if isinstance(node, dict):
        return {key: _fill(val, vals) for key, val in node.items()}
    if isinstance(node, list):
        return [_fill(item, vals) for item in node]
    if isinstance(node, str):
        out = node
        for key, value in vals.items():
            out = out.replace("{{" + key + "}}", str(value))
        return out
    return node


def _chunks(text: str, max_bytes: int = 2000) -> list[str]:
    """按 UTF-8 字节切分（企业微信单条 2048 字节、TG 4096 字符）。"""
    text = text or ""
    if not text:
        return []
    out: list[str] = []
    buf = ""
    used = 0
    for ch in text:
        size = len(ch.encode("utf-8"))
        if used + size > max_bytes:
            out.append(buf)
            buf, used = ch, size
        else:
            buf += ch
            used += size
    if buf:
        out.append(buf)
    return out


class Notifier:
    """通知发送 + 指令处理。构造后由 ``main`` 注入依赖并启动轮询线程。"""

    def __init__(self, cfg: Config, db: Database, hub: Any = None,
                 linker: Any = None, engines: Callable[[], list] | None = None):
        self.cfg = cfg
        self.db = db
        self.hub = hub
        self.linker = linker
        self._engines = engines or list
        # 由 server 注入：status_provider() -> dict；run_config(cid, mode) -> dict
        self.status_provider: Callable[[], dict[str, Any]] | None = None
        self.run_config: Callable[[str, str], Any] | None = None
        self._stop = threading.Event()
        self.last_error = ""
        self.last_sent = 0
        self.sent_count = 0
        self.tg_offset = 0
        self.tg_state = "off"      # off | polling | error
        self.last_command = ""

    # ------------------------------------------------------------------ 配置
    def settings(self) -> dict[str, Any]:
        return notify_settings(self.db)

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        cur = self.settings()
        if "enabled" in patch:
            cur["enabled"] = bool(patch["enabled"])
        if isinstance(patch.get("events"), dict):
            for key in DEFAULT_EVENTS:
                if key in patch["events"]:
                    cur["events"][key] = bool(patch["events"][key])
        for section in ("wecom", "tg", "webhook"):
            block = patch.get(section)
            if isinstance(block, dict):
                for key in cur[section]:
                    if key in block and block[key] is not None:
                        cur[section][key] = block[key]
        cur = _merged(cur)
        self.db.set_meta(META_KEY, json.dumps(cur, ensure_ascii=False))
        log.info("通知配置已更新（企微=%s TG=%s Webhook=%s 启用=%s）",
                 cur["wecom"]["enabled"], cur["tg"]["enabled"],
                 cur["webhook"]["enabled"], cur["enabled"])
        return cur

    def status(self) -> dict[str, Any]:
        s = self.settings()
        return {
            "settings": self._masked(s),
            "tg_state": self.tg_state,
            "last_error": self.last_error,
            "last_sent": int(self.last_sent),
            "sent": self.sent_count,
            "last_command": self.last_command,
            "wecom_callback": (f"{self.cfg.base_url}/api/notify/wecom"
                               if s["wecom"]["token"] and s["wecom"]["aes_key"] else ""),
        }

    @staticmethod
    def _masked(s: dict[str, Any]) -> dict[str, Any]:
        """下发给前端时把密钥打码：能看出「配过」，但拿不到原文。"""
        out = json.loads(json.dumps(s))

        def mask(value: str) -> str:
            value = str(value or "")
            if not value:
                return ""
            return value[:6] + "••••••" + value[-4:] if len(value) > 12 else "••••••"

        out["wecom"]["secret"] = mask(out["wecom"]["secret"])
        out["wecom"]["aes_key"] = mask(out["wecom"]["aes_key"])
        out["tg"]["token"] = mask(out["tg"]["token"])
        if out["wecom"]["webhook"]:
            wh = out["wecom"]["webhook"]
            out["wecom"]["webhook"] = wh.split("key=")[0] + "key=" + mask(
                wh.split("key=")[-1] if "key=" in wh else "")
        # webhook 的自定义 Header 不打码：多行值打码后无法还原，
        # 前端保存时会把打码串写回去（宁可少一层遮掩，也不能吃配置）
        return out

    # ------------------------------------------------------------------ 发送
    def send(self, text: str, event: str = "", level: str = "info",
             force: bool = False) -> dict[str, Any]:
        s = self.settings()
        if not force:
            if not s["enabled"]:
                return {"ok": False, "skipped": "通知未启用"}
            if event and not s["events"].get(event, False):
                return {"ok": False, "skipped": f"事件 {event} 未开启"}
        prefix = {"err": "[错误] ", "warn": "[注意] "}.get(str(level), "")
        body = prefix + (text or "")
        channels: dict[str, Any] = {}
        if s["wecom"]["enabled"]:
            channels["wecom"] = self._send_wecom(body, s["wecom"])
        if s["tg"]["enabled"]:
            channels["tg"] = self._send_tg(body, s["tg"])
        if s["webhook"]["enabled"]:
            channels["webhook"] = self._send_webhook(body, s["webhook"], event, level)
        if not channels:
            return {"ok": False, "skipped": "没有启用任何渠道"}
        ok = any(v.get("ok") for v in channels.values())
        if ok:
            self.sent_count += 1
            self.last_sent = time.time()
        return {"ok": ok, "channels": channels}

    def test(self) -> dict[str, Any]:
        s = self.settings()
        text = (f"PanShare 通知测试\n服务：{self.cfg.web_site_name}\n"
                f"时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"版本：{getattr(self.cfg, 'site_version', '') or ''}".rstrip())
        if not (s["wecom"]["enabled"] or s["tg"]["enabled"] or s["webhook"]["enabled"]):
            return {"ok": False, "skipped": "先在页面上启用一个渠道并保存"}
        return self.send(text, force=True)

    # ---------------------------------------------------------- 企业微信
    def _wecom_webhook(self, hook: str, text: str) -> dict[str, Any]:
        chunks = _chunks(text, 2000)
        try:
            with httpx.Client(timeout=10.0) as client:
                for part in chunks:
                    resp = client.post(hook, json={
                        "msgtype": "text", "text": {"content": part},
                    })
                    data = resp.json() if resp.content else {}
                    if data.get("errcode") not in (0, None):
                        return {"ok": False, "detail": f"企微返回 {data}"}
            return {"ok": True, "detail": f"群机器人 · {len(chunks)} 条"}
        except Exception as exc:  # noqa: BLE001 - 网络错误只记录，不抛出
            return {"ok": False, "detail": f"群机器人发送失败：{exc}"}

    def _wecom_access_token(self, conf: dict[str, Any]) -> str:
        corp_id = str(conf.get("corp_id") or "").strip()
        secret = str(conf.get("secret") or "").strip()
        if not corp_id or not secret:
            raise ValueError("缺少 corp_id / secret")
        try:
            cache = json.loads(self.db.get_meta(WECOM_TOKEN_META) or "{}")
        except json.JSONDecodeError:
            cache = {}
        if cache.get("corp_id") == corp_id and cache.get("exp", 0) > time.time() + 60:
            return str(cache.get("token") or "")
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{WECOM_API}/cgi-bin/gettoken",
                              params={"corpid": corp_id, "corpsecret": secret})
            data = resp.json()
        if data.get("errcode") not in (0, None) or not data.get("access_token"):
            raise ValueError(f"获取 access_token 失败：{data}")
        token = str(data["access_token"])
        self.db.set_meta(WECOM_TOKEN_META, json.dumps({
            "corp_id": corp_id, "token": token,
            "exp": int(time.time()) + int(data.get("expires_in") or 7200),
        }))
        return token

    def _send_wecom(self, text: str, conf: dict[str, Any]) -> dict[str, Any]:
        hook = str(conf.get("webhook") or "").strip()
        if hook:
            return self._wecom_webhook(hook, text)
        try:
            token = self._wecom_access_token(conf)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}
        agent = conf.get("agent_id") or ""
        try:
            agent_id = int(str(agent).strip())
        except ValueError:
            return {"ok": False, "detail": "agent_id 必须是数字"}
        try:
            with httpx.Client(timeout=10.0) as client:
                for part in _chunks(text, 2000):
                    resp = client.post(
                        f"{WECOM_API}/cgi-bin/message/send",
                        params={"access_token": token},
                        json={"touser": conf.get("to_user") or "@all",
                              "msgtype": "text", "agentid": agent_id,
                              "text": {"content": part}},
                    )
                    data = resp.json() if resp.content else {}
                    if data.get("errcode") not in (0, None):
                        return {"ok": False, "detail": f"应用消息返回 {data}"}
            return {"ok": True, "detail": "企业微信应用消息"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"应用消息发送失败：{exc}"}

    # ------------------------------------------------------------- Telegram
    def _tg_client(self) -> httpx.Client:
        s = self.settings()["tg"]
        proxy = str(s.get("proxy") or "").strip()
        kwargs: dict[str, Any] = {"timeout": 40.0}
        if proxy:
            kwargs["proxy"] = proxy
        return httpx.Client(**kwargs)

    def _send_tg(self, text: str, conf: dict[str, Any] | None = None,
                 chat_id: str = "") -> dict[str, Any]:
        s = conf or self.settings()["tg"]
        token = str(s.get("token") or "").strip()
        target = str(chat_id or s.get("chat_id") or "").strip()
        if not token or not target:
            return {"ok": False, "detail": "缺少 token 或 chat_id"}
        chunks = _chunks(text, 3500)
        try:
            with self._tg_client() as client:
                for part in chunks:
                    resp = client.post(f"{TG_API}/bot{token}/sendMessage", json={
                        "chat_id": target, "text": part,
                        "disable_web_page_preview": True,
                    })
                    data = resp.json() if resp.content else {}
                    if not data.get("ok"):
                        return {"ok": False, "detail": f"TG 返回 {data}"}
            return {"ok": True, "detail": f"Telegram · {len(chunks)} 条"}
        except Exception as exc:  # noqa: BLE001 - 国内直连不通很常见，给出可读提示
            return {"ok": False,
                    "detail": f"Telegram 发送失败：{exc}（国内网络请配置代理）"}

    # -------------------------------------------------------------- Webhook
    def _webhook_payload(self, conf: dict[str, Any], text: str, event: str,
                         level: str) -> Any:
        """按模板拼 JSON；模板里只有字符串值做占位替换，不会被正文里的引号搞坏。"""
        body = (text or "").strip()
        title = body.splitlines()[0][:80] if body else "PanShare 通知"
        vals = {
            "title": title,
            "text": body,
            "event": str(event or "notify"),
            "level": {"err": "error", "warn": "warning"}.get(str(level), "info"),
            "date": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "version": APP_VERSION,
        }
        raw = str(conf.get("template") or "").strip() or DEFAULT_WEBHOOK_TEMPLATE
        try:
            node = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("webhook 模板不是合法 JSON，改用默认模板")
            node = json.loads(DEFAULT_WEBHOOK_TEMPLATE)
        return _fill(node, vals)

    def _send_webhook(self, text: str, conf: dict[str, Any], event: str = "",
                      level: str = "info") -> dict[str, Any]:
        url = str(conf.get("url") or "").strip()
        headers = {"content-type": "application/json"}
        for line in str(conf.get("headers") or "").splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                if key.strip():
                    headers[key.strip()] = val.strip()
        note = ""
        sid = str(conf.get("server") or "").strip()
        if sid:
            row = None
            try:
                row = next((x for x in self.db.list_media_servers()
                            if str(x.get("id")) == sid), None)
            except Exception:  # 读媒体服务器失败不该挡住发送
                log.debug("读取媒体服务器失败", exc_info=True)
            if row:
                if not url:
                    url = str(row.get("host") or "").rstrip("/") + "/emby/Notifications/Admin"
                token = str(row.get("api_key") or "").strip()
                if token:
                    headers.setdefault("X-Emby-Token", token)
                note = f" · Emby {row.get('name') or ''}".rstrip()
            else:
                note = "（媒体服务器已删除，按自定义地址发送）"
        if not url:
            return {"ok": False, "detail": "没填 Webhook 地址"}
        payload = self._webhook_payload(conf, text, event, level)
        try:
            timeout = float(conf.get("timeout") or 8)
        except (TypeError, ValueError):
            timeout = 8.0
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
            if 200 <= resp.status_code < 300:
                return {"ok": True, "detail": f"{resp.status_code} · {url}{note}"}
            return {"ok": False,
                    "detail": f"{resp.status_code} {(resp.text or '')[:120]}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"Webhook 发送失败：{exc}"}

    def run_forever(self, stop: threading.Event) -> None:
        """Telegram 长轮询：接收指令并回复（不需要公网回调）。"""
        log.info("通知指令轮询已启动")
        while not stop.is_set():
            s = self.settings()
            tg = s["tg"]
            if not (s["enabled"] and tg["enabled"] and tg["poll"] and tg["token"]):
                self.tg_state = "off"
                stop.wait(15)
                continue
            self.tg_state = "polling"
            try:
                updates = self._tg_updates(str(tg["token"]), tg["proxy"])
            except Exception as exc:  # noqa: BLE001 - 网络抖动重试即可
                self.tg_state = "error"
                self.last_error = f"TG 轮询失败：{exc}"
                log.warning("TG 轮询失败: %s", exc)
                stop.wait(20)
                continue
            for item in updates:
                self.tg_offset = max(self.tg_offset, int(item.get("update_id", 0)) + 1)
                self._tg_handle(item)
            # 长轮询本身会阻塞 ~25s，这里不用额外等待
            stop.wait(1)

    def _tg_updates(self, token: str, proxy: str = "") -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"timeout": 45.0}
        if str(proxy or "").strip():
            kwargs["proxy"] = str(proxy).strip()
        with httpx.Client(**kwargs) as client:
            resp = client.get(f"{TG_API}/bot{token}/getUpdates", params={
                "offset": self.tg_offset, "timeout": 25,
                "allowed_updates": json.dumps(["message"]),
            })
            data = resp.json() if resp.content else {}
        if not data.get("ok"):
            raise RuntimeError(str(data.get("description") or data))
        return list(data.get("result") or [])

    def _tg_handle(self, item: dict[str, Any]) -> None:
        msg = item.get("message") or {}
        text = str(msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        user = msg.get("from") or {}
        tg = self.settings()["tg"]
        if not self._tg_allowed(chat_id, user, tg):
            log.warning("忽略未授权 TG 指令（chat=%s user=%s）", chat_id, user.get("id"))
            self._send_tg("没有权限：请把下面的 chat_id 填进通知设置\nchat_id: "
                          + chat_id, tg, chat_id=chat_id)
            return
        who = str(user.get("username") or user.get("first_name") or user.get("id") or "")
        self.last_command = f"{time.strftime('%H:%M:%S')} {who}: {text}"
        reply = self.handle_command(text, who=who)
        self._send_tg(reply, tg, chat_id=chat_id)

    @staticmethod
    def _tg_allowed(chat_id: str, user: dict[str, Any], tg: dict[str, Any]) -> bool:
        allow = [x.strip().lstrip("@").lower()
                 for x in str(tg.get("allow") or "").split(",") if x.strip()]
        if allow:
            uid = str(user.get("id") or "")
            uname = str(user.get("username") or "").lstrip("@").lower()
            return uid in allow or (uname and uname in allow)
        # 没配白名单：只认配置里的 chat_id（防止别人把 bot 拉进群乱发指令）
        return not tg.get("chat_id") or str(tg.get("chat_id")) == chat_id

    # ---------------------------------------------------------- 企业微信回调
    def wecom_verify(self, query: dict[str, str]) -> str:
        """GET 验证：签名校验 + 解密 echostr。"""
        conf = self.settings()["wecom"]
        token = str(conf.get("token") or "")
        aes_key = str(conf.get("aes_key") or "")
        if not token or not aes_key:
            raise ValueError("还没有配置企业微信 Token / EncodingAESKey")
        sig = str(query.get("msg_signature") or "")
        timestamp = str(query.get("timestamp") or "")
        nonce = str(query.get("nonce") or "")
        echo = str(query.get("echostr") or "")
        if sig != wxcrypt.signature(token, timestamp, nonce, echo):
            raise ValueError("签名校验失败")
        return wxcrypt.verify_url(aes_key, echo, str(conf.get("corp_id") or ""))

    def wecom_receive(self, body: bytes, query: dict[str, str]) -> str:
        """POST 回调：解密消息 → 执行指令 → 用应用消息回复。返回 "success"。"""
        conf = self.settings()["wecom"]
        token = str(conf.get("token") or "")
        aes_key = str(conf.get("aes_key") or "")
        if not token or not aes_key:
            return "success"
        try:
            root = ET.fromstring(body)
            encrypt = (root.findtext("Encrypt") or "").strip()
        except ET.ParseError:
            return "success"
        sig = str(query.get("msg_signature") or "")
        timestamp = str(query.get("timestamp") or "")
        nonce = str(query.get("nonce") or "")
        if encrypt and sig != wxcrypt.signature(token, timestamp, nonce, encrypt):
            log.warning("企业微信回调签名不匹配，已忽略")
            return "success"
        try:
            plain = wxcrypt.decrypt_message(aes_key, encrypt, str(conf.get("corp_id") or ""))
            msg = ET.fromstring(plain)
        except Exception as exc:  # noqa: BLE001 - 解密失败别把回调打死
            log.warning("企业微信回调解密失败: %s", exc)
            return "success"
        content = (msg.findtext("Content") or "").strip()
        user = (msg.findtext("FromUserName") or "").strip()
        if content.startswith("/"):
            self.last_command = f"{time.strftime('%H:%M:%S')} {user}: {content}"
            reply = self.handle_command(content, who=user)
            self._send_wecom(reply, conf)
        return "success"

    # ------------------------------------------------------------------ 指令
    def handle_command(self, text: str, who: str = "") -> str:
        raw = (text or "").strip()
        if not raw:
            return HELP_TEXT
        parts = raw.split()
        cmd = parts[0].lower().lstrip("/").split("@")[0]
        args = parts[1:]
        try:
            if cmd in ("help", "h", "开始", "帮助"):
                return HELP_TEXT
            if cmd in ("status", "s", "状态"):
                return self._cmd_status()
            if cmd in ("ls", "list", "配置"):
                return self._cmd_ls()
            if cmd in ("run", "生成"):
                return self._cmd_run(args)
            if cmd in ("find", "search", "搜索"):
                return self._cmd_find(args)
            if cmd in ("pending", "待删"):
                return self._cmd_pending()
            if cmd in ("link", "联动"):
                return self._cmd_link(args)
            if cmd in ("notify", "通知"):
                return self._cmd_notify(args)
            return f"不认识的指令：{cmd}\n\n{HELP_TEXT}"
        except Exception as exc:  # noqa: BLE001 - 指令出错也要回一句话
            log.warning("处理指令失败 %s: %s", raw, exc)
            return f"执行失败：{exc}"

    def _cmd_status(self) -> str:
        st: dict[str, Any] = {}
        if self.status_provider is not None:
            try:
                st = self.status_provider() or {}
            except Exception as exc:  # noqa: BLE001
                st = {"error": str(exc)}
        dbstats = st.get("db") or self.db.stats()
        scan = st.get("scan") or {}
        watch = st.get("watch") or {}
        running = [c for c in self.db.list_strm_configs() if c.get("status") == "running"]
        lines = [
            f"PanShare {st.get('version') or ''}".strip(),
            f"文件索引：{dbstats.get('files', 0)} 个 · 目录 {dbstats.get('dirs', 0)} 个",
            f"已生成 strm：{dbstats.get('strm', 0)} 个",
        ]
        if scan.get("finished_at"):
            lines.append("上次扫描：" + time.strftime(
                "%m-%d %H:%M", time.localtime(int(scan["finished_at"])))
                + f"（文件 {scan.get('files', 0)}，写入 {scan.get('written', 0)}）")
        mode_text = {"event": "生活事件", "interval": "定时增量", "probing": "探测中"}
        lines.append("增量方式：" + mode_text.get(str(watch.get("mode")), "—"))
        if running:
            lines.append("生成中：" + "、".join(
                str(c.get("name") or c.get("id")) for c in running))
        if self.linker is not None:
            link = self.linker.status()
            s = link.get("settings") or {}
            last = link.get("last") or {}
            lines.append(
                "联动：" + ("本地→网盘 开" if s.get("local_to_remote") else "本地→网盘 关")
                + ("（试运行）" if s.get("dry_run") else "")
                + f" · 待删 {link.get('pending', 0)}"
                + (f" · 上次删 {last.get('deleted', 0)}" if last.get("at") else ""))
        accs = self._engines()
        if accs:
            lines.append(f"账号：{len(accs)} 个（" + "、".join(
                getattr(a, "display", getattr(a, "name", "")) or "主账号" for a in accs) + "）")
        return "\n".join(lines)

    def _cmd_ls(self) -> str:
        rows = self.db.list_strm_configs()
        if not rows:
            return "还没有 115 STRM 配置"
        lines = [f"115 STRM 配置（{len(rows)} 条）"]
        for row in rows[:30]:
            status = {"running": "运行中", "error": "失败"}.get(str(row.get("status")), "空闲")
            lines.append(
                f"· {row.get('name') or row.get('id')}  [{status}]"
                f"\n  {row.get('remote_paths') or row.get('remote_path') or '—'}"
                f" → {row.get('local_dir') or '/strm'} · {row.get('files') or 0} 个文件")
        if len(rows) > 30:
            lines.append(f"… 还有 {len(rows) - 30} 条")
        return "\n".join(lines)

    def _cmd_run(self, args: list[str]) -> str:
        if self.run_config is None:
            return "当前环境不支持触发生成（没有可用的配置执行器）"
        name = args[0] if args else "all"
        mode = (args[1].lower() if len(args) > 1 else "inc")
        mode = {"增量": "inc", "全量": "full", "全新": "rebuild"}.get(mode, mode)
        if mode not in ("inc", "full", "rebuild"):
            return "模式只能是 inc / full / rebuild"
        rows = self.db.list_strm_configs()
        if name in ("all", "全部", "*"):
            targets = rows
        else:
            targets = [c for c in rows
                       if name == str(c.get("id")) or name in str(c.get("name") or "")]
        if not targets:
            return f"没找到配置：{name}（用 /ls 看列表）"
        done = []
        for row in targets:
            if row.get("status") == "running":
                done.append(f"{row.get('name')}：正在运行，跳过")
                continue
            try:
                self.run_config(str(row["id"]), mode)
                done.append(f"{row.get('name')}：已提交 {mode}")
            except Exception as exc:  # noqa: BLE001
                done.append(f"{row.get('name')}：提交失败 {exc}")
        return "\n".join(done)

    def _cmd_find(self, args: list[str]) -> str:
        keyword = " ".join(args).strip()
        if not keyword:
            return "用法：/find 关键词"
        lines = []
        for eng in self._engines():
            db = getattr(eng, "db", None)
            if db is None:
                continue
            try:
                hits = db.search_files(keyword, limit=10)
            except Exception as exc:  # noqa: BLE001 - 单个账号查不了不影响其它
                log.debug("搜索索引失败（%s）: %s", getattr(eng, "name", ""), exc)
                continue
            for row in hits:
                lines.append(f"· {row.get('path') or row.get('name')}")
                if len(lines) >= 20:
                    break
            if len(lines) >= 20:
                break
        if not lines:
            return f"没有找到包含「{keyword}」的文件"
        return f"包含「{keyword}」的文件（最多 20 条）\n" + "\n".join(lines)

    def _cmd_pending(self) -> str:
        if self.linker is None:
            return "联动功能未启用"
        rows = self.linker.pending(limit=20)
        if not rows:
            return "没有待删除的文件（strm 都还在，或还在宽限期内）"
        lines = [f"待删除 {len(rows)} 个（本地 strm 已不存在）："]
        for row in rows:
            lines.append(f"· {row['name']}  · {row['age']}s")
        lines.append("确认后可在网页「通知与联动」里点「立即执行」")
        return "\n".join(lines)

    def _cmd_link(self, args: list[str]) -> str:
        if self.linker is None:
            return "联动功能未启用"
        cur = self.linker.settings()
        act = (args[0].lower() if args else "")
        if act in ("on", "off", "开", "关"):
            on = act in ("on", "开")
            cur = self.linker.save_settings({"local_to_remote": on})
        elif act in ("dry", "试运行"):
            cur = self.linker.save_settings({"dry_run": not cur["dry_run"]})
        lines = [
            "联动状态",
            f"本地删 strm → 删网盘：{'开' if cur['local_to_remote'] else '关'}",
            f"网盘删 → 同步本地：{'开' if cur['remote_to_local'] else '关'}",
            f"清理刮削文件：{'开' if cur['scrape_clean'] else '关'}",
            (f"试运行：{'开' if cur['dry_run'] else '关'} · 宽限 "
             f"{cur['grace_seconds']}s · 间隔 {cur['interval']}s"),
            "用 /link on | off 切换开关",
        ]
        return "\n".join(lines)

    def _cmd_notify(self, args: list[str]) -> str:
        act = (args[0].lower() if args else "")
        if act in ("on", "off", "开", "关"):
            self.save_settings({"enabled": act in ("on", "开")})
            return "通知已" + ("开启" if act in ("on", "开") else "关闭")
        s = self.settings()
        return ("通知状态：" + ("开" if s["enabled"] else "关")
                + f"\n企微：{'开' if s['wecom']['enabled'] else '关'}"
                + f" · TG：{'开' if s['tg']['enabled'] else '关'}")
