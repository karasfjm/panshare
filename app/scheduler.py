"""115 STRM 配置的「定时巡检」。

每条配置可以在网页上单独打开「定时巡检」并给一个间隔（分钟）。到点就跑一次
**增量**生成：只补新增/改名的文件，已经有 strm 的不会重写（不会把 Emby 里
的条目刷成「刚加入」）。和「全量 / 全新」按钮的区别就在这里。

设计上的几个约束：

* 一次只跑一条：多条配置同时扫 115 很容易触发 WAF 405，宁可排到下一轮；
* 正在跑（status=running）的配置不重复触发，避免同一棵树被两遍扫描；
* 手动全量扫描在跑的时候让路（``busy`` 回调），别抢同一份 API 配额；
* 时间只存在内存里：重启后按 ``last_run + 间隔`` 重新排，过期的下一拍就补跑。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# 间隔下限：比这更频繁等于把 115 当靶子打
MIN_INTERVAL = 60.0
# 配置里没写间隔时用的默认值（1 小时）
DEFAULT_INTERVAL = 3600.0
# 多久看一次「谁到点了」。15 秒足够准，又不会一直在查库
TICK = 15.0


class StrmConfigScheduler:
    def __init__(self, db: Any, runner: Callable[[str, str], Any],
                 busy: Callable[[], bool] | None = None, tick: float = TICK):
        self.db = db
        self.runner = runner          # (config_id, mode) -> None，实际干活的是 server 里的那份
        self.busy = busy              # 其它扫描是否在跑（True 就跳过这一拍）
        self.tick = max(5.0, float(tick))
        self._due: dict[str, float] = {}
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ 生命周期
    def start(self, stop: threading.Event | None = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        stop = stop or threading.Event()
        self._thread = threading.Thread(
            target=self._loop, args=(stop,), name="strm-scheduler", daemon=True
        )
        self._thread.start()
        log.info("115 STRM 定时巡检已启动（每 %.0f 秒检查一次）", self.tick)

    def _loop(self, stop: threading.Event) -> None:
        # 启动时先等一下：让服务把引擎、索引都拉起来，别一上来就抢配额
        if stop.wait(min(self.tick, 20.0)):
            return
        while not stop.is_set():
            try:
                self.run_pending()
            except Exception as exc:  # noqa: BLE001 - 轮询线程不能死
                log.warning("定时巡检失败: %s", exc)
            stop.wait(self.tick)

    # ------------------------------------------------------------------ 调度
    @staticmethod
    def interval_of(row: dict[str, Any]) -> float:
        try:
            value = float(row.get("scan_interval") or 0)
        except (TypeError, ValueError):
            value = 0.0
        return max(MIN_INTERVAL, value or DEFAULT_INTERVAL)

    def run_pending(self, now: float | None = None) -> int:
        """跑一条到点的配置，返回触发的数量（0 或 1）。"""
        now = time.time() if now is None else now
        rows = self.db.list_strm_configs()
        active = {str(r["id"]): r for r in rows if r.get("auto_scan")}

        # 配置被删掉（或关了开关）就忘掉它的排期
        for cid in [c for c in self._due if c not in active]:
            self._due.pop(cid, None)

        if not active:
            return 0
        if any(str(r.get("status") or "") == "running" for r in rows):
            return 0
        if self.busy is not None and self.busy():
            return 0

        for cid, row in active.items():
            interval = self.interval_of(row)
            due = self._due.get(cid)
            if due is None:
                # 从没跑过 → 立刻补一次；跑过 → 按上次完成时间排下一拍
                last = float(row.get("last_run") or 0)
                due = (last + interval) if last else now
                self._due[cid] = due
            if now < due:
                continue
            self._due[cid] = now + interval
            try:
                self.runner(cid, "inc")
            except Exception as exc:  # noqa: BLE001 - 单条失败不影响其它配置
                log.warning("定时巡检 %s 触发失败: %s", row.get("name") or cid, exc)
                continue
            log.info(
                "定时巡检触发：%s（每 %.0f 分钟）", row.get("name") or cid, interval / 60
            )
            return 1
        return 0

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "tick": self.tick,
            "due": {cid: int(at) for cid, at in self._due.items()},
        }


__all__ = ["DEFAULT_INTERVAL", "MIN_INTERVAL", "StrmConfigScheduler"]
