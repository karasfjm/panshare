"""本地 strm ⇄ 网盘文件的双向联动。

两个方向：

1. **本地 → 网盘**（``local_to_remote``）
   用户在文件管理器 / Emby 里删掉某个 ``*.strm``，认为这部片不要了，
   这里定期比对索引，确认删除后把 115 上对应的原文件一起删掉（进回收站，
   可恢复）。

2. **网盘 → 本地**（``remote_to_local``）
   115 上删了文件，``Watcher.on_delete`` 会删本地 strm；这里补上
   「连带清掉刮削文件」（nfo / 海报 / 字幕），否则 Emby 里会留一条
   没有视频的幽灵条目。

安全设计（删网盘是不可逆操作，方向 1 默认**关闭**）：

* 宽限期：strm 消失后不会立刻删，默认等 5 分钟 —— 挪文件、重新生成、
  挂载抖动都会在这个窗口里恢复；
* 比例熔断：一轮里缺失比例超过阈值（默认 20% 且至少 5 个）就整体跳过，
  典型场景是 NAS 挂载掉了 / 容器重启中 STRM_ROOT 是空的；
* 只认索引：只处理「我们生成的、索引里记着 file_id + strm_path」的文件，
  用户自己放进 STRM_ROOT 的 strm 一律不碰；
* 干跑：``dry_run`` 只看不动，先在页面上确认待删列表。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database

log = logging.getLogger(__name__)

META_KEY = "link_settings"

DEFAULTS: dict[str, Any] = {
    # 方向 1：本地删 strm → 删网盘原文件（危险，默认关）
    "local_to_remote": False,
    # 方向 2：网盘删文件 → 删本地 strm（Watcher 负责）+ 刮削文件（这里给开关）
    "remote_to_local": True,
    "scrape_clean": True,
    # strm 消失多久后才认定是「用户删的」（秒）
    "grace_seconds": 300,
    # 检查间隔（秒）
    "interval": 120,
    # 熔断：单轮缺失比例上限 / 最少几个才触发熔断 / 最少几个才开始检查
    "max_ratio": 0.2,
    "min_missing": 5,
    # 只看不动
    "dry_run": False,
    # 联动删除时发通知
    "notify": True,
}


def _merged(raw: Any) -> dict[str, Any]:
    out = dict(DEFAULTS)
    if isinstance(raw, dict):
        for key in DEFAULTS:
            if key in raw and raw[key] is not None:
                out[key] = raw[key]
    # 类型兜底，避免网页上填了字符串把线程搞崩
    out["local_to_remote"] = bool(out["local_to_remote"])
    out["remote_to_local"] = bool(out["remote_to_local"])
    out["scrape_clean"] = bool(out["scrape_clean"])
    out["dry_run"] = bool(out["dry_run"])
    out["notify"] = bool(out["notify"])
    out["grace_seconds"] = max(0, int(float(out["grace_seconds"] or 0)))
    out["interval"] = max(30, int(float(out["interval"] or 120)))
    out["max_ratio"] = min(1.0, max(0.0, float(out["max_ratio"] or 0)))
    out["min_missing"] = max(1, int(float(out["min_missing"] or 5)))
    return out


def link_settings(db: Database) -> dict[str, Any]:
    """读联动配置（主库 meta）。Watcher 也用它决定要不要清刮削。"""
    try:
        raw = db.get_meta(META_KEY)
    except Exception:  # noqa: BLE001 - 读配置失败按默认值走
        raw = ""
    if not raw:
        return dict(DEFAULTS)
    try:
        return _merged(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        log.warning("联动配置无法解析，使用默认值: %r", raw)
        return dict(DEFAULTS)


class LinkManager:
    """双向联动的后台检查器（网页可配置，默认只做「网盘 → 本地」）。"""

    def __init__(self, cfg: Config, db: Database,
                 engines: Callable[[], list] | None = None,
                 notifier: Any = None):
        self.cfg = cfg
        self.db = db
        self._engines = engines or list
        self.notifier = notifier
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._wake = threading.Event()
        # strm_path -> 第一次发现它不存在的时间（进程内，重启后重新计时更安全）
        self._missing: dict[str, float] = {}
        self._last: dict[str, Any] = {
            "at": 0, "checked": 0, "missing": 0, "pending": 0,
            "deleted": 0, "aborted": "", "dry_run": False,
        }
        self.last_error = ""

    # ------------------------------------------------------------------ 配置
    def settings(self) -> dict[str, Any]:
        return link_settings(self.db)

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        cur = self.settings()
        for key in DEFAULTS:
            if key in patch and patch[key] is not None:
                cur[key] = patch[key]
        cur = _merged(cur)
        self.db.set_meta(META_KEY, json.dumps(cur, ensure_ascii=False))
        self._wake.set()          # 改完立刻按新间隔跑，不用等下一轮
        log.info("联动配置已更新: %s", cur)
        return cur

    def status(self) -> dict[str, Any]:
        with self._lock:
            last = dict(self._last)
            pending = len(self._missing)
        try:
            indexed = self.db.count_files_with_strm()
        except Exception:  # 统计失败不该影响页面
            log.debug("统计 strm 行数失败", exc_info=True)
            indexed = 0
        return {
            "settings": self.settings(),
            "last": last,
            "pending": pending,
            "indexed": indexed,
            "last_error": self.last_error,
            "strm_root": str(self.cfg.strm_root),
            "strm_root_ok": Path(self.cfg.strm_root).is_dir(),
        }

    # ------------------------------------------------------------------ 主体
    def run_forever(self, stop: threading.Event) -> None:
        log.info("联动检查已启动（本地删 strm → 删网盘：%s）",
                 "开" if self.settings()["local_to_remote"] else "关")
        while not stop.is_set():
            try:
                if self.settings()["local_to_remote"]:
                    self.scan_once()
            except Exception as exc:  # 后台线程不能死
                self.last_error = str(exc)
                log.warning("联动检查失败: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            interval = max(30, int(self.settings()["interval"]))
            # 分段等：网页上改了间隔 / 开关，最多 5 秒就生效
            waited = 0
            while waited < interval and not stop.is_set():
                if self._wake.wait(5.0):
                    self._wake.clear()
                    break
                waited += 5

    def scan_once(self, force: bool = False) -> dict[str, Any]:
        """跑一轮检查；``force=True`` 忽略比例熔断（用户明确要执行时）。"""
        if not self._scan_lock.acquire(blocking=False):
            # 定时线程和网页手动检查可能撞在一起：后到的直接走开，别双删
            with self._lock:
                last = dict(self._last)
            last["aborted"] = "上一轮检查还在进行"
            return last
        try:
            return self._scan_once(force=force)
        finally:
            self._scan_lock.release()

    def _scan_once(self, force: bool = False) -> dict[str, Any]:
        settings = self.settings()
        root = Path(self.cfg.strm_root)
        if not root.is_dir():
            # 挂载没准备好：绝不能把「全都读不到」当成「用户全删了」
            return self._finish(checked=0, missing=0, pending=0, deleted=0,
                                aborted="STRM_ROOT 不可用（挂载未就绪？）",
                                dry_run=bool(settings["dry_run"]))
        if not settings["local_to_remote"]:
            return self._finish(checked=0, missing=0, pending=0, deleted=0,
                                aborted="联动关闭", dry_run=bool(settings["dry_run"]))

        now = time.time()
        deleted = 0
        checked = 0
        missing_total = 0
        aborted = ""
        seen: set[str] = set()
        messages: list[str] = []

        for eng in self._engines():
            db = getattr(eng, "db", None)
            api = getattr(eng, "api", None)
            if db is None or api is None:
                continue
            try:
                rows = db.files_with_strm()
            except Exception as exc:  # noqa: BLE001
                log.warning("读取索引失败（%s）: %s", getattr(eng, "display", "?"), exc)
                continue
            checked += len(rows)
            missing = [r for r in rows
                       if not self._exists(r.get("strm_path"))]
            missing_total += len(missing)
            if not missing:
                continue

            # 熔断：缺失比例过高通常是挂载/权限问题，不是用户删片
            ratio = len(missing) / max(1, len(rows))
            if (not force and len(rows) >= settings["min_missing"]
                    and len(missing) >= settings["min_missing"]
                    and ratio >= settings["max_ratio"]):
                who = (getattr(eng, "display", None)
                       or getattr(eng, "name", "") or "主账号")
                text = (f"「{who}」缺失 {len(missing)}/{len(rows)}（{ratio:.0%}）"
                        f"超过阈值 {settings['max_ratio']:.0%}，本轮跳过")
                aborted = (aborted + "；" + text) if aborted else text
                log.warning("联动熔断（%s）: %s", who, text)
                self._event("link_abort", f"联动熔断：{text}", level="warn")
                self._notify(f"联动熔断：{text}\n如果确实删了很多，请在网页上点「强制执行」",
                             event="link")
                continue

            for row in missing:
                strm_path = str(row.get("strm_path") or "")
                seen.add(strm_path)
                first = self._missing.get(strm_path)
                if first is None:
                    self._missing[strm_path] = now
                    first = now
                if now - first < settings["grace_seconds"]:
                    continue
                if settings["dry_run"]:
                    messages.append(f"（试运行）将删除：{row.get('path') or row.get('name')}")
                    continue
                name = str(row.get("name") or row.get("path") or row.get("id"))
                try:
                    api.delete_files([int(row["id"])])
                except Exception as exc:  # noqa: BLE001 - 单个失败不影响其它
                    log.warning("删除 115 文件失败 %s: %s", name, exc)
                    self._event("link_error", f"删除网盘文件失败：{name} · {exc}", level="err")
                    continue
                db.delete_file(int(row["id"]))
                self._missing.pop(strm_path, None)
                deleted += 1
                messages.append(f"已删除：{name}（strm 已不在本地）")
                log.info("本地 strm 已删除，同步删除网盘文件: %s", name)
                self._event("link_delete", f"本地 strm 已删，同步删除网盘文件：{name}")

        # 已经恢复的（用户又生成回来了）从待删名单里摘掉
        for path in list(self._missing):
            if path not in seen:
                self._missing.pop(path, None)

        if messages:
            title = "🔗 联动删除" if not settings["dry_run"] else "🔗 联动试运行"
            body = "\n".join(messages[:10])
            if len(messages) > 10:
                body += f"\n… 还有 {len(messages) - 10} 条"
            self._notify(f"{title}（{deleted} 个）\n{body}", event="link")

        self.last_error = ""
        return self._finish(checked=checked, missing=missing_total,
                            pending=len(self._missing), deleted=deleted,
                            aborted=aborted, dry_run=bool(settings["dry_run"]))

    def pending(self, limit: int = 200) -> list[dict[str, Any]]:
        """待删除列表：当前 strm 不在索引里记的位置上的那些。

        ``ready`` 为假表示还在宽限期内（下一轮才会自动删），网页上也列出来，
        这样用户能提前看到「再等几分钟就要删这些」。
        """
        settings = self.settings()
        grace = settings["grace_seconds"]
        now = time.time()
        out: list[dict[str, Any]] = []
        for eng in self._engines():
            db = getattr(eng, "db", None)
            if db is None:
                continue
            try:
                rows = db.files_with_strm()
            except Exception as exc:  # noqa: BLE001 - 一个账号读不了不影响其它
                log.debug("读取索引失败（%s）: %s", getattr(eng, "name", ""), exc)
                continue
            for row in rows:
                path = str(row.get("strm_path") or "")
                first = self._missing.get(path)
                if first is None or self._exists(path):
                    continue
                out.append({
                    "account": getattr(eng, "name", "") or "主账号",
                    "file_id": str(row.get("id")),
                    "name": row.get("name") or "",
                    "path": row.get("path") or "",
                    "strm_path": path,
                    "missing_since": int(first),
                    "age": int(now - first),
                    "ready": (now - first) >= grace,
                })
                if len(out) >= limit:
                    return out
        return out

    def commit(self, limit: int = 0) -> dict[str, Any]:
        """立刻执行待删列表（网页上「确认删除」按钮），不等宽限期。"""
        rows = self.pending(limit=limit or 1000)
        deleted = 0
        errors = 0
        for item in rows:
            eng = next((e for e in self._engines()
                        if (getattr(e, "name", "") or "主账号") == item["account"]), None)
            if eng is None:
                continue
            name = item["name"]
            try:
                eng.api.delete_files([int(item["file_id"])])
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.warning("删除 115 文件失败 %s: %s", name, exc)
                continue
            eng.db.delete_file(int(item["file_id"]))
            self._missing.pop(item["strm_path"], None)
            deleted += 1
            self._event("link_delete", f"手动联动删除网盘文件：{name}")
        if deleted or errors:
            self._notify(f"🔗 手动联动删除：成功 {deleted} 个"
                         + (f"，失败 {errors} 个" if errors else ""), event="link")
        return {"ok": True, "deleted": deleted, "errors": errors}

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _exists(path: Any) -> bool:
        if not path:
            return True          # 没有 strm_path 的行不算「被删了」
        try:
            return Path(str(path)).exists()
        except OSError:
            return True          # 读不了就当作还在，绝不误删

    def _finish(self, *, checked: int, missing: int, pending: int,
                deleted: int, aborted: str, dry_run: bool = False) -> dict[str, Any]:
        last = {
            "at": int(time.time()),
            "checked": checked,
            "missing": missing,
            "pending": pending,
            "deleted": deleted,
            "aborted": aborted,
            "dry_run": dry_run,
        }
        with self._lock:
            self._last = last
        return dict(last)

    def _event(self, kind: str, message: str, level: str = "info") -> None:
        try:
            self.db.add_strm_event(kind, message, level=level)
        except Exception as exc:  # noqa: BLE001 - 记日志失败不影响删除
            log.debug("联动事件写入失败: %s", exc)

    def _notify(self, text: str, event: str = "link") -> None:
        if not self.settings().get("notify", True) or self.notifier is None:
            return
        try:
            self.notifier.send(text, event=event)
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响联动
            log.debug("联动通知发送失败: %s", exc)
