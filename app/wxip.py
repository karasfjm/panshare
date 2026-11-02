"""企业微信「企业可信 IP」自动更新（移植 JYONG719/WXIPUpdaterr 的核心流程）。

为什么需要：企微自建应用配了「企业可信 IP」之后，家里的宽带出口 IP 一变，
应用消息发不出去、回调也收不到。这里用 Playwright 扫码登录企微管理后台，
定时比对公网 IP，变了就点进应用详情把可信 IP 改掉。

设计要点：
* **Playwright 是可选依赖**：没装 / Chromium 起不来时模块照样能 import，
  网页上会给出安装提示，不影响 PanShare 其它功能；
* **所有浏览器操作只在一条后台线程里做**（``run_forever``）：
  playwright 的同步 API 不能跨线程用，网页接口只置标志 + 读缓存状态；
* 登录态用 ``storage_state`` 存到 ``DATA_DIR/wxip/state.json``，过期要重新扫码；
* 页面结构一变定位就会失败，失败时把 ``page.content()`` 落到
  ``DATA_DIR/wxip/last.html``，方便照着改选择器。
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

META_KEY = "wxip_settings"
HISTORY_META = "wxip_history"
FRAME_URL = "https://work.weixin.qq.com/wework_admin/frame"
LOGIN_URL = "https://work.weixin.qq.com/wework_admin/loginpage_wx?from=myhome"

# 公网 IP 检测：随便哪个能通就行（原项目叫「6 接口容灾」）
IP_SERVICES: list[tuple[str, str]] = [
    ("ipify", "https://api.ipify.org"),
    ("ifconfig.me", "https://ifconfig.me/ip"),
    ("ip.sb", "https://api.ip.sb/ip"),
    ("3322", "http://ip.3322.net"),
    ("ipip.net", "https://myip.ipip.net"),
    ("cloudflare", "https://www.cloudflare.com/cdn-cgi/trace"),
]
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "auto": True,            # IP 变了自动改（关掉就只提醒）
    "agent_ids": "",         # 逗号 / 换行分隔；留空 = 自动从后台列表里抓
    "interval": 300,         # 检测间隔（秒），最小 60
    "notify": True,          # 变化 / 失败时发通知
    "proxy": "",             # 访问企微后台的代理（可选）
}

# 定位「企业可信IP」这一行的配置按钮：从最精确到最宽松。
# 实测企微把这个按钮做成 ``div.js_show_ipConfig_dialog``（不是 <a>），
# 早先只匹配 ``//a[...'配置']`` 会点到隔壁「可信域名」的配置链接，弹不出 IP 弹窗。
CONFIG_LOCATORS = [
    ".js_show_ipConfig_dialog",
    "xpath=//*[contains(@class,'js_show_ipConfig_dialog')]",
    ("xpath=//*[contains(normalize-space(.),'企业可信IP')]"
     "/ancestor::*[self::div or self::tr][1]"
     "//*[contains(normalize-space(.),'配置')]"),
]
TEXTAREA_LOCATORS = [
    ".js_ipConfig_textarea",
    ".app_ipConfig_dialog textarea",
    "div[class*=dialog] textarea",
    "textarea",
]
CONFIRM_LOCATORS = [
    ".js_ipConfig_confirmBtn",
    ("xpath=//*[contains(@class,'app_ipConfig_dialog')]"
     "//*[contains(normalize-space(.),'确定')]"),
    ("xpath=//*[contains(@class,'dialog') or contains(@class,'modal')]"
     "//*[contains(normalize-space(.),'确定')]"),
]


def _merged(raw: Any) -> dict[str, Any]:
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for key in DEFAULTS:
            if key in raw and raw[key] is not None:
                out[key] = raw[key]
    out["enabled"] = bool(out["enabled"])
    out["auto"] = bool(out["auto"])
    out["notify"] = bool(out["notify"])
    out["agent_ids"] = str(out["agent_ids"] or "").strip()
    out["proxy"] = str(out["proxy"] or "").strip()
    try:
        out["interval"] = max(60, int(float(out["interval"] or 300)))
    except (TypeError, ValueError):
        out["interval"] = 300
    return out


def wxip_settings(db: Database) -> dict[str, Any]:
    raw = db.get_meta(META_KEY) or ""
    if not raw:
        return _merged(None)
    try:
        return _merged(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        log.warning("企微可信IP 配置无法解析，使用默认值: %r", raw)
        return _merged(None)


def _agent_list(text: str) -> list[str]:
    return [x for x in re.split(r"[,，;；\s]+", str(text or "")) if x.isdigit()]


class WxIpManager:
    """检测公网 IP + 扫码登录企微后台 + 自动改可信 IP。"""

    def __init__(self, cfg: Config, db: Database, notifier: Any = None):
        self.cfg = cfg
        self.db = db
        self.notifier = notifier
        self.dir = Path(cfg.data_dir) / "wxip"
        self.state_file = self.dir / "state.json"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._jobs: dict[str, bool] = {"login": False, "update": False, "shot": False}
        self.login_state = "idle"          # idle / qr / waiting / ok / expired / error
        self.qr_b64 = ""
        self.qr_at = 0.0
        self.current_ip = ""
        self.ip_source = ""
        self.last_check = 0.0
        self.last_update: dict[str, Any] = {}
        self.last_error = ""
        self.last_shot = ""
        self.available = find_spec("playwright") is not None
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._next_check = 0.0

    # ------------------------------------------------------------------ 配置
    def settings(self) -> dict[str, Any]:
        return wxip_settings(self.db)

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        cur = self.settings()
        for key in DEFAULTS:
            if key in patch and patch[key] is not None:
                cur[key] = patch[key]
        cur = _merged(cur)
        self.db.set_meta(META_KEY, json.dumps(cur, ensure_ascii=False))
        self._next_check = 0.0            # 改了配置立刻按新值走一轮
        log.info("企微可信IP 配置已更新（启用=%s 自动=%s 间隔=%ss）",
                 cur["enabled"], cur["auto"], cur["interval"])
        return cur

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        with self._lock:
            hist = self._history()
            return {
                "settings": self.settings(),
                "available": self.available,
                "login": self.login_state,
                "qr": self.qr_b64,
                "qr_at": self.qr_at,
                "logged_in": self.state_file.exists(),
                "ip": self.current_ip,
                "ip_source": self.ip_source,
                "last_check": self.last_check,
                "last_update": dict(self.last_update),
                "last_error": self.last_error,
                "shot": self.last_shot,
                "history": hist,
                "hint": ("没装 playwright，先在容器里执行 "
                         "`pip install playwright && python -m playwright install chromium`"
                         if not self.available else ""),
            }

    def _history(self) -> list[dict[str, Any]]:
        try:
            raw = self.db.get_meta(HISTORY_META) or "[]"
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def _push_history(self, item: dict[str, Any]) -> None:
        hist = self._history()
        hist.append(item)
        self.db.set_meta(HISTORY_META, json.dumps(hist[-20:], ensure_ascii=False))

    # ------------------------------------------------------- 网页触发的动作
    def request_login(self) -> dict[str, Any]:
        self._jobs["login"] = True
        self._next_check = 0.0
        return {"ok": True, "login": self.login_state}

    def request_update(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"ok": False, "detail": "还没登录企微管理后台，先扫码"}
        self._jobs["update"] = True
        return {"ok": True}

    def request_shot(self) -> dict[str, Any]:
        self._jobs["shot"] = True
        return {"ok": True}

    def logout(self) -> dict[str, Any]:
        try:
            self.state_file.unlink(missing_ok=True)
        except OSError as exc:
            return {"ok": False, "detail": str(exc)}
        self.login_state = "idle"
        self.qr_b64 = ""
        return {"ok": True}

    # -------------------------------------------------------------- IP 检测
    def detect_ip(self, proxy: str = "") -> str:
        """依次试几个公开接口，第一个拿到 IP 的就用（记录来源）。"""
        for name, url in IP_SERVICES:
            try:
                with httpx.Client(timeout=8.0, proxy=proxy or None,
                                  follow_redirects=True) as client:
                    resp = client.get(url, headers={"user-agent": "PanShare"})
                if resp.status_code != 200:
                    continue
                body = resp.text or ""
                if name == "cloudflare":
                    match = re.search(r"^ip=(.+)$", body, re.MULTILINE)
                    text = match.group(1).strip() if match else ""
                else:
                    found = IP_RE.search(body)
                    text = found.group() if found else ""
                if text and IP_RE.fullmatch(text):
                    self.ip_source = name
                    return text
            except Exception as exc:  # noqa: BLE001 - 换下一个接口
                log.debug("IP 检测失败 %s: %s", name, exc)
        return ""

    # ------------------------------------------------------------- 后台线程
    def run_forever(self, stop: threading.Event) -> None:
        self._stop = stop
        # 登录态在内存里会被重启清掉，磁盘上却还在 —— 启动时先按“已登录”显示，
        # 真失效了下一轮检查会自己变成 expired（否则重启后网页一直挂着“未登录”）
        if self.state_file.exists() and self.login_state == "idle":
            self.login_state = "ok"
        log.info("企微可信IP 守护已启动（启用=%s 自动=%s 登录=%s）",
                 self.settings()["enabled"], self.settings()["auto"], self.login_state)
        while not stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("企微可信IP 任务异常: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            stop.wait(2.0)

    def _tick(self) -> None:
        s = self.settings()
        if self._jobs.pop("login", False):
            self._login_flow(s)
        if self._jobs.pop("shot", False):
            self._snapshot()
        if self._jobs.pop("update", False):
            self._check_and_update(s, force=True)
            self._next_check = time.time() + s["interval"]
            return
        if not s["enabled"]:
            return
        if time.time() >= self._next_check:
            self._next_check = time.time() + s["interval"]
            self._check_and_update(s, force=False)

    # ------------------------------------------------------------ IP + 更新
    def _check_and_update(self, s: dict[str, Any], force: bool) -> None:
        ip = self.detect_ip(s["proxy"])
        self.last_check = time.time()
        if not ip:
            self.last_error = "所有 IP 检测接口都不通（检查一下容器的网络）"
            log.warning("公网 IP 检测失败")
            return
        old = self.current_ip
        self.current_ip = ip
        self.last_error = ""
        if not force and old and old == ip:
            return
        if not (force or s["auto"]):
            if old and old != ip:
                self._notify(f"公网 IP 变了：{old} → {ip}（自动更新已关闭，需要手动点一下）")
            return
        if not self.state_file.exists():
            if not force:
                # 定时检查发现 IP 变了但还没登录：提示一次，别每轮都刷
                if old and old != ip:
                    self.login_state = "expired"
                    self._notify("公网 IP 变了，但还没登录企微后台，没法自动改可信IP",
                                 level="warn")
                return
            self.last_update = {"at": time.time(), "ok": False, "detail": "还没扫码登录"}
            self.login_state = "expired"
            return
        agents = _agent_list(s["agent_ids"])
        result = self._update_agents(agents, ip)
        self.last_update = {"at": time.time(), "ip": ip, "old": old, **result}
        self._push_history({"at": int(time.time()), "ip": ip, "old": old,
                            "ok": result.get("ok"), "detail": result.get("detail", "")})
        if result.get("ok"):
            self.login_state = "ok"
            if old and old != ip:
                self._notify(f"企微可信IP 已更新：{old} → {ip}")
        else:
            if "登录" in str(result.get("detail") or ""):
                self.login_state = "expired"
            self._notify(f"企微可信IP 更新失败：{result.get('detail')}", level="err")

    def _notify(self, text: str, level: str = "info") -> None:
        if not self.notifier or not self.settings()["notify"]:
            return
        try:
            self.notifier.send(text, event="wxip", level=level)
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响主流程
            log.warning("企微可信IP 通知发送失败: %s", exc)

    # ---------------------------------------------------------------- 浏览器
    def _ensure_browser(self):
        if self._page is not None:
            return self._page
        if not self.available:
            raise RuntimeError("没装 playwright：pip install playwright && "
                               "python -m playwright install chromium")
        from playwright.sync_api import sync_playwright

        self.dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        self._context = self._browser.new_context(
            locale="zh-CN", viewport={"width": 1440, "height": 900},
            storage_state=str(self.state_file) if self.state_file.exists() else None,
        )
        self._page = self._context.new_page()
        return self._page

    def _shutdown_browser(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                log.debug("关闭浏览器对象失败", exc_info=True)
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            log.debug("停止 playwright 失败", exc_info=True)
        self._pw = self._browser = self._context = self._page = None

    def _dump(self, page, tag: str) -> str:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / "last.html"
            path.write_text(page.content(), encoding="utf-8")
            return str(path)
        except Exception:
            log.debug("保存页面失败 %s", tag, exc_info=True)
            return ""

    def _snapshot(self) -> None:
        """给「诊断」按钮用：当前页面截一张图。"""
        try:
            page = self._ensure_browser()
            raw = page.screenshot(type="png", full_page=False)
            self.last_shot = "data:image/png;base64," + base64.b64encode(raw).decode()
            self._shutdown_browser()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"截图失败：{exc}"

    # ------------------------------------------------------------- 扫码登录
    def _login_flow(self, s: dict[str, Any]) -> None:
        self.login_state = "qr"
        self.last_error = ""
        try:
            page = self._ensure_browser()
            # 已经有登录态：先直接用已有 Cookie 开一次后台，能用就不折腾用户扫码。
            # （之前不判断，已登录的人点「扫码登录」会被跳到 frame#/，报「没找到二维码」）
            if self.state_file.exists():
                try:
                    page.goto(FRAME_URL, wait_until="domcontentloaded", timeout=45000)
                    self._stop.wait(3.0)
                    if self._logged_in(page):
                        self.login_state = "ok"
                        self.qr_b64 = ""
                        log.info("已有登录态，跳过扫码（url=%s）", (page.url or "")[:100])
                        self._shutdown_browser()
                        return
                    log.info("登录态已失效，重新扫码")
                    self.state_file.unlink(missing_ok=True)
                    # 旧 Cookie 还在 context 里，重开一个干净的浏览器再扫码
                    self._shutdown_browser()
                    page = self._ensure_browser()
                except OSError:
                    log.debug("清理失效登录态失败", exc_info=True)
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            if s["proxy"]:
                log.info("企微后台走代理 %s（代理在容器环境变量里配更稳）", s["proxy"])
            img = self._wait_qr(page)
            if img is None:
                self._dump(page, "no-qr")
                self.login_state = "error"
                self.last_error = "没找到登录二维码，可能页面结构变了（已保存 last.html）"
                log.warning("企微登录页没找到二维码：url=%s", (page.url or "")[:120])
                self._shutdown_browser()
                return
            self.qr_b64 = "data:image/png;base64," + base64.b64encode(img).decode()
            self.qr_at = time.time()
            self.login_state = "waiting"
            log.info("企微登录二维码已就绪，等待手机扫码确认（最多 5 分钟）")
            # 扫码后最多等 5 分钟（手机端还要点一次「确认登录」，3 分钟不够）
            deadline = time.time() + 300
            seen: list[str] = []
            last_log = 0.0
            while time.time() < deadline and not self._stop.is_set():
                if self._logged_in(page):
                    self._save_state()
                    self.login_state = "ok"
                    log.info("企微管理后台登录成功：url=%s", (page.url or "")[:120])
                    self._shutdown_browser()
                    return
                url = (page.url or "")[:120]
                if url and (not seen or seen[-1] != url):
                    seen.append(url)
                    seen[:] = seen[-4:]
                now = time.time()
                if now - last_log >= 15:
                    last_log = now
                    log.info("等扫码登录中：url=%s vst=%s", url, self._has_vst())
                self._stop.wait(2.0)
            self.login_state = "expired"
            self.last_error = ("二维码超时（手机上扫完要点「确认登录」）。"
                               f"浏览器最后停在：{seen[-1] if seen else '未知'}")
            log.warning("企微扫码超时，见过的 url：%s", " | ".join(seen))
            self._shutdown_browser()
        except Exception as exc:
            self.login_state = "error"
            self.last_error = str(exc) or f"{type(exc).__name__}"
            log.warning("企微扫码登录失败: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            self._shutdown_browser()

    def _has_vst(self) -> bool:
        """企微登录成功后一定会下发 ``wwrtx.vst``（登录票据）——比 URL 靠谱。"""
        try:
            for c in self._context.cookies():
                if c.get("name") == "wwrtx.vst" and str(c.get("value") or "").strip():
                    return True
        except Exception:  # 浏览器已关等情况
            log.debug("读 cookie 失败", exc_info=True)
        return False

    def _logged_in(self, page) -> bool:
        """是否已经登录。

        **不能只看 URL**：企微登录页的二维码在 iframe（``wwqrlogin/mng/login_qrcode``）里，
        扫码确认后有时主框架不整页跳转（或跳转被后台安全策略弹回登录页），
        只看 ``loginpage`` 会一直判成未登录、白等到超时。
        所以优先认 cookie 里的 ``wwrtx.vst``，URL 离开登录页只作为辅助判据。
        """
        if self._has_vst():
            return True
        try:
            url = (page.url or "").lower()
        except Exception:  # noqa: BLE001
            return False
        return bool(url) and "loginpage" not in url

    def _wait_qr(self, page) -> bytes | None:
        """等二维码出现并截它的图。

        企微登录页的二维码在 **iframe** 里（``wwqrlogin/mng/login_qrcode``），
        只在主 frame 找会一直扑空 —— 所以这里遍历所有 frame。
        """
        selectors = [
            "img.qrcode_login_img", "img.js_qrcode_img", "img[src*='qrcode']",
            ".qrcode img", "canvas.qrcode",
        ]
        for _ in range(25):
            loc = self._find(page, selectors)
            if loc is not None:
                try:
                    return loc.screenshot(type="png")
                except Exception:
                    log.debug("二维码截图失败，重试", exc_info=True)
            self._stop.wait(1.0)
        return None

    @staticmethod
    def _find(page, selectors: list[str]):
        """跨 frame 找第一个可见元素（企微后台把内容放在 iframe 里）。"""
        for frame in page.frames:
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if loc.count() and loc.is_visible():
                        return loc
                except Exception:
                    log.debug("选择器 %s 在某个 frame 不可用", sel, exc_info=True)
                    continue
        return None

    def _save_state(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._context.storage_state(path=str(self.state_file))
        except Exception as exc:  # noqa: BLE001
            log.warning("保存登录态失败: %s", exc)
            return
        # 存了但没有登录票据 = 存了个假的登录态，下次照样打不开后台，不如不存
        if not self._has_vst():
            log.warning("登录态里没有 wwrtx.vst，按未登录处理（没有真正登录成功）")
            try:
                self.state_file.unlink(missing_ok=True)
            except OSError:
                log.debug("清理无效登录态失败", exc_info=True)
            raise RuntimeError("登录没有真正成功（没拿到 wwrtx.vst），请在手机上点「确认登录」")

    # ----------------------------------------------------------- 改可信 IP
    def _update_agents(self, agents: list[str], ip: str) -> dict[str, Any]:
        try:
            page = self._ensure_browser()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"浏览器起不来：{exc}"}
        results: list[dict[str, Any]] = []
        try:
            page.goto(FRAME_URL, wait_until="domcontentloaded", timeout=45000)
            self._stop.wait(2.0)
            if "loginpage" in page.url.lower():
                self.login_state = "expired"
                return {"ok": False, "detail": "登录态已失效，请重新扫码", "agents": []}
            pairs: list[tuple[str, str]] = []
            if agents:
                # 配置里既能写 AgentId，也能写应用名（如 OECT）——名字要去列表里换 id
                numeric = [a for a in agents if a.isdigit()]
                pairs.extend((a, a) for a in numeric)
                named = [a for a in agents if not a.isdigit()]
                if named:
                    found = self._discover_agents(page)
                    by_name = {n.strip().lower(): aid for n, aid in found}
                    for nm in named:
                        hit = by_name.get(nm.strip().lower())
                        if hit:
                            pairs.append((nm, hit))
                        else:
                            results.append({"agent": nm, "name": nm, "ok": False,
                                            "detail": "后台里没找到这个应用名"})
            else:
                found = self._discover_agents(page)
                if not found:
                    return {"ok": False,
                            "detail": "没读到应用列表，请在配置里直接填 AgentId", "agents": []}
                names = "、".join(n for n, _ in found) or "（未命名）"
                log.info("自动发现 %d 个自建应用：%s", len(found), names)
                pairs = found
            for nm, agent_id in pairs:
                results.append(self._update_one(page, agent_id, ip, name=nm))
                self._stop.wait(1.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": f"更新失败：{exc}", "agents": results}
        finally:
            # 一轮跑完就关掉浏览器：常驻一个 Chromium 白吃 150MB 内存，
            # 下次要用的再开（冷启动两秒，比一直占着划算）
            self._shutdown_browser()
        ok = bool(results) and all(r["ok"] for r in results)
        detail = "；".join(
            f"{r.get('name') or r['agent']}：{'成功' if r['ok'] else r['detail']}"
            for r in results)
        return {"ok": ok, "detail": detail, "agents": results}

    def _discover_agents(self, page) -> list[tuple[str, str]]:
        """从「应用管理 → 应用」列表里抓自建应用的 ``(名字, AgentId)``。

        踩过的坑（v1.18.2）：
        * 列表页路由是 ``#/apps/list``，写 ``#apps`` 只会停在首页，什么都不渲染；
        * 列表项标题是 ``.app_index_item_title``，**没有 href、没有 data-id**，
          正则扫 HTML 一个 id 都扫不到 —— 只能点进去再从地址栏读
          ``#apps/modApiApp/<id>``（详情页的 hash 不带斜杠）；
        * 页面靠前的「基础」区有 20+ 个应用（打卡/审批/汇报…），只取前 N 个标题
          会全部耗在它们身上，压根到不了「自建」区 —— 所以只扫
          ``.js_app_section_openapi_app`` 这个分区；
        * 「消息推送」点进去是群机器人页（``#roomRobot``），没有 AgentId，跳过即可。
        """
        try:
            page.goto(f"{FRAME_URL}#/apps/list", wait_until="domcontentloaded", timeout=45000)
            self._stop.wait(6.0)
        except Exception as exc:  # noqa: BLE001
            log.warning("打开应用列表失败: %s", exc)
            return []

        script = (
            "() => {const out=[];"
            "document.querySelectorAll('.js_app_section_openapi_app .app_index_item_title')"
            ".forEach(e=>{const t=(e.innerText||'').trim();"
            "if(t&&!t.startsWith('创建应用')) out.push(t);});return out;}"
        )
        try:
            titles = page.evaluate(script) or []
        except Exception:  # noqa: BLE001
            titles = []
        log.info("自建应用分区：%s", "、".join(titles) or "（空）")

        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for name in titles:
            if self._stop.is_set():
                break
            try:
                page.goto(f"{FRAME_URL}#/apps/list",
                          wait_until="domcontentloaded", timeout=45000)
                self._stop.wait(3.0)
                page.get_by_text(name, exact=True).last.click(timeout=3000)
                self._stop.wait(1.5)
                match = re.search(r"modApiApp/(\d{3,})", page.url or "")
                if match and match.group(1) not in seen:
                    seen.add(match.group(1))
                    out.append((name, match.group(1)))
                elif not match:
                    log.info("应用「%s」没有 AgentId（跳到 %s），跳过", name, (page.url or "")[:80])
            except Exception as exc:  # noqa: BLE001 - 单个点不开就跳过
                log.debug("点应用「%s」失败: %s", name, exc)
                continue
        return out

    def _update_one(self, page, agent_id: str, ip: str,
                    name: str = "") -> dict[str, Any]:
        label = name or agent_id

        def ret(ok: bool, detail: str, old: str = "") -> dict[str, Any]:
            return {"agent": agent_id, "name": label, "ok": ok,
                    "detail": detail, "old": old}

        try:
            page.goto(f"{FRAME_URL}#apps/modApiApp/{agent_id}",
                      wait_until="domcontentloaded", timeout=45000)
            self._stop.wait(3.0)
            if "loginpage" in page.url.lower():
                return ret(False, "登录态已失效")
            try:
                page.wait_for_function(
                    "() => document.body && document.body.innerText.includes('可信')",
                    timeout=8000)
            except Exception:  # noqa: BLE001 - 没等到也继续试，可能只是文案不同
                log.debug("等「可信」文案超时 agent=%s", agent_id)
            if not self._open_config(page):
                self._dump(page, "no-config")
                return ret(False, "没找到「企业可信IP」的配置按钮（已存 last.html）")
            # 已配置的 IP 在弹窗的 textarea 里，必须先开弹窗再读
            old = self._read_trusted(page)
            if ip in (old or ""):
                self._close_config(page)
                return ret(True, f"已经是 {ip}，跳过", old)
            if not self._fill_ip(page, ip):
                self._dump(page, "no-textarea")
                return ret(False, "没找到 IP 输入框", old)
            if not self._confirm(page):
                self._dump(page, "no-confirm")
                return ret(False, "没点到「确定」", old)
            # 关掉弹窗后重新打开读一次：比“认为点成功了”可靠
            self._stop.wait(2.0)
            now = ""
            if self._open_config(page):
                now = self._read_trusted(page)
                self._close_config(page)
            ok = ip in (now or "")
            return ret(ok, "已更新" if ok else f"页面显示的还是 {now or '（读不到）'}", old)
        except Exception as exc:  # noqa: BLE001 - 单个应用失败不影响其它
            return ret(False, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _read_trusted(page) -> str:
        """读弹窗里已配置的可信 IP。

        只认弹窗的 textarea —— 用整页 ``inner_text`` 正则找 IP 会误读页面上
        其它地址（可信域名、回调地址等），而且 textarea 的 value 根本不在
        innerText 里，等于永远读不到。
        """
        for sel in TEXTAREA_LOCATORS:
            for frame in page.frames:
                try:
                    loc = frame.locator(sel).first
                    if loc.count() and loc.is_visible():
                        val = loc.input_value() or ""
                        found = IP_RE.findall(val)
                        if found:
                            return ";".join(dict.fromkeys(found))
                    elif loc.count() and sel == TEXTAREA_LOCATORS[0]:
                        # 弹窗在但一条都没配：返回空串（不是“读不到”）
                        return ""
                except Exception:  # 浏览器已关 / 元素不可用
                    log.debug("读 textarea 失败 sel=%s", sel, exc_info=True)
                    continue
        return ""

    def _open_config(self, page) -> bool:
        loc = self._find(page, CONFIG_LOCATORS)
        if loc is None:
            return False
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            log.debug("滚动到配置按钮失败", exc_info=True)
        try:
            loc.click(timeout=5000)
        except Exception:
            log.warning("点「配置」按钮失败", exc_info=True)
            return False
        # 等弹窗真的出来，否则后面读 textarea 会扑空
        try:
            page.wait_for_selector(TEXTAREA_LOCATORS[0], state="visible", timeout=8000)
        except Exception:
            log.debug("等可信IP 弹窗超时", exc_info=True)
        return True

    def _close_config(self, page) -> None:
        """关掉弹窗但不保存（已经配好了就别乱动）。"""
        try:
            page.keyboard.press("Escape")
            self._stop.wait(0.5)
        except Exception:  # 关不掉也无所谓，浏览器马上就关了
            log.debug("关闭弹窗失败", exc_info=True)

    def _fill_ip(self, page, ip: str) -> bool:
        """把 IP **追加**到已有列表里（企微用英文分号分隔）。

        老实现是 ``fill(ip)`` 直接覆盖 —— 会把用户已经配好的 IP 全删掉，
        换 IP 之后其它服务器的接口立刻就调不通了。
        """
        loc = self._find(page, TEXTAREA_LOCATORS)
        if loc is None:
            return False
        try:
            loc.click(timeout=3000)
            cur = (loc.input_value() or "").strip()
            items = [x for x in re.split(r"[;；,，\s]+", cur) if x]
            if ip in items:
                return True
            items.append(ip)
            if len(items) > 50:      # 企微上限 120，留点余量，别一直涨
                items = items[-50:]
            loc.fill(";".join(items))
            return True
        except Exception:
            log.warning("填 IP 失败", exc_info=True)
            return False

    def _confirm(self, page) -> bool:
        loc = self._find(page, CONFIRM_LOCATORS)
        if loc is None:
            return False
        try:
            loc.click(timeout=4000)
            return True
        except Exception:
            log.warning("点「确定」失败", exc_info=True)
            return False

    # ---------------------------------------------------------------- 退出
    def close(self) -> None:
        self._stop.set()
        self._shutdown_browser()
