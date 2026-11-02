"""增量同步：基于 115「生活事件」拉取变更，增量维护 strm。

限制（115 侧的能力边界，非本工具问题）：
- 没有「复制文件」「复制目录」「从回收站还原」的完整事件；
- 第三方客户端上传有时没有上传事件。
因此建议配合 RECONCILE_HOURS 定期做一次全量对账。
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from p115client.tool.life import iter_life_behavior_once

from .config import Config
from .db import Database
from .p115api import P115API
from .scanner import Scanner
from .strm import StrmStore, pick_strm_rel, rel_join, safe_component, strm_rel_path

log = logging.getLogger(__name__)

# 浏览 / 星标 / 标签类事件，不改变文件内容或位置
IGNORED_TYPES = {1, 3, 4, 5, 7, 8, 9, 10, 19}
# 删除文件或目录
DELETE_TYPES = {22}
# 只作用于目录的事件：新建目录(17)、目录改名(20)
DIR_ONLY_TYPES = {17, 20}
# 只作用于文件的事件：文件改名(24)
FILE_ONLY_TYPES = {24}


class Watcher:
    def __init__(self, cfg: Config, db: Database, api: P115API,
                 store: StrmStore, scanner: Scanner,
                 shared_db: Database | None = None):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.store = store
        self.scanner = scanner
        # 联动配置存在主库 meta 里（子账号的库是独立的，读不到）
        self._shared_db = shared_db or db
        self.last_poll = 0.0
        self.last_event_at = 0.0
        self.processed = 0
        # None=还没探测；False=事件接口不可用，走定时增量扫描
        self.events_ok: bool | None = None
        self.last_fallback = 0.0
        self.fallback_runs = 0

    # ------------------------------------------------------------------ 循环
    def run_forever(self, stop_event: threading.Event) -> None:
        if self.cfg.event_enabled:
            log.info("增量监听启动：事件间隔 %.1fs（app=%s）", self.cfg.event_interval, self.cfg.event_app)
        self._ensure_life_enabled()
        self.events_ok = self.cfg.event_enabled and self._probe_events()
        if not self.cfg.event_enabled:
            log.info("EVENT_ENABLED=0，只做定时增量扫描")
        elif not self.events_ok:
            log.warning(
                "115 生活事件接口不可用（实测各设备均返回 405，115 已下线该接口），"
                "改为每 %.0f 秒做一次指纹增量扫描", self.cfg.fallback_interval
            )

        while not stop_event.is_set():
            if self.events_ok:
                try:
                    count = self.poll_once()
                except Exception as exc:
                    if self._endpoint_gone(exc):
                        self.events_ok = False
                        log.warning(
                            "生活事件接口已被 115 下线（%s），切换为每 %.0f 秒一次的增量扫描",
                            exc, self.cfg.fallback_interval,
                        )
                        continue
                    log.warning("拉取生活事件失败: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
                else:
                    if count:
                        log.info("处理事件 %d 条", count)
                self._maybe_reconcile()
                stop_event.wait(self.cfg.event_interval)
                continue

            self._fallback_scan()
            self._maybe_reconcile()
            stop_event.wait(self.cfg.fallback_interval)

    @staticmethod
    def _endpoint_gone(exc: BaseException) -> bool:
        """判断异常是不是「115 把这个接口关了」，而不是临时网络错误。"""
        code = getattr(exc, "code", None)
        if code == 405:
            return True
        text = f"{type(exc).__name__}: {exc}".upper()
        return "405" in text and "METHOD NOT ALLOWED" in text

    def _probe_events(self) -> bool:
        """启动时探一次事件接口，别让 405 每 30 秒刷一条日志。"""
        try:
            for _ in iter_life_behavior_once(
                self.api.client,
                app=self.cfg.event_app,
                cooldown=0,
                first_batch_size=1,
                yield_latest=False,
                from_time=int(time.time()) - 120,
            ):
                break
        except Exception as exc:  # noqa: BLE001
            log.warning("探测生活事件接口失败: %s", exc)
            self.events_ok = False
            return False
        log.info("生活事件接口可用，使用事件增量模式")
        self.events_ok = True
        return True

    def _fallback_scan(self) -> None:
        """事件不可用时的替代方案：定时跑一次指纹增量扫描。

        只重列「mtime 变了」的目录（见 Scanner._process_dir），
        大多数轮次只需几次请求，代价远小于全量扫描。
        如果还没成功跑过一次全量（比如启动时正好被限流 abort 了），这里补跑全量。
        """
        self.last_fallback = time.time()
        self.last_poll = self.last_fallback
        self.fallback_runs += 1
        full = not self.db.get_meta("last_full_scan") or self.db.stats()["files"] == 0
        try:
            stats = self.scanner.run(full=full)
        except Exception as exc:
            log.warning("增量扫描失败: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
            return
        if stats.aborted:
            log.warning("本轮%s被中断，%.0f 秒后重试", "全量扫描" if full else "增量扫描",
                        self.cfg.fallback_interval)
            return
        if full or stats.written or stats.files:
            log.info(
                "%s完成：目录 %d（跳过 %d）/ 文件 %d / 新写入 %d / 失败 %d",
                "全量扫描" if full else "增量扫描",
                stats.dirs, stats.skipped, stats.files, stats.written, stats.errors,
            )

    def _ensure_life_enabled(self) -> None:
        """115 的「最近记录」关闭时不会有任何事件，启动时顺手打开。"""
        try:
            from p115client.tool.life import life_show

            life_show(self.api.client)
        except Exception as exc:  # noqa: BLE001
            log.debug("确认生活事件开关失败（不影响已有记录）: %s", exc)

    def _maybe_reconcile(self) -> None:
        hours = self.cfg.reconcile_hours
        if hours <= 0:
            return
        last = self.db.get_meta_int("last_full_scan")
        if last and (time.time() - last) < hours * 3600:
            return
        log.info("距离上次全量已超过 %.1f 小时，执行一次对账扫描", hours)
        try:
            self.scanner.run(full=True)
        except Exception as exc:
            log.warning("对账扫描失败: %s", exc, exc_info=True)

    # ------------------------------------------------------------------ 轮询
    def poll_once(self) -> int:
        cursor = self.db.get_meta_int("event_cursor")
        now = time.time()
        if cursor:
            kwargs: dict[str, Any] = {"from_id": cursor, "from_time": -1}
        else:
            since = self.db.get_meta_int("event_from_time") or int(now - 120)
            kwargs = {"from_time": since}

        events: list[dict[str, Any]] = list(
            iter_life_behavior_once(
                self.api.client,
                app=self.cfg.event_app,
                cooldown=self.cfg.event_cooldown,
                first_batch_size=self.cfg.event_batch,
                yield_latest=False,
                **kwargs,
            )
        )

        self.last_poll = time.time()
        if not events:
            if not cursor:
                # 保留 5 秒重叠，避免轮询间隙漏事件
                self.db.set_meta("event_from_time", int(now) - 5)
            return 0

        events.sort(key=lambda e: int(e.get("id") or 0))
        max_id = cursor
        max_time = self.db.get_meta_int("event_from_time")
        handled = 0

        for event in events:
            try:
                event_id = int(event.get("id") or 0)
                if event_id <= cursor:
                    continue
                max_id = max(max_id, event_id)
                if "update_time" in event:
                    max_time = max(max_time, int(event["update_time"]))
                if self._handle(event):
                    handled += 1
            except Exception as exc:
                log.warning("处理事件失败 %s: %s", event, exc, exc_info=log.isEnabledFor(logging.DEBUG))

        if max_id:
            self.db.set_meta("event_cursor", max_id)
        if max_time:
            self.db.set_meta("event_from_time", max_time)
        self.processed += handled
        self.last_event_at = time.time()
        return handled

    # ------------------------------------------------------------------ 分发
    def _handle(self, event: dict[str, Any]) -> bool:
        etype = int(event.get("type") or 0)
        fid = int(event.get("file_id") or 0)
        if not fid or etype in IGNORED_TYPES:
            return False
        name = str(event.get("event_name") or etype)
        log.debug("事件 #%s %s file_id=%s", event.get("id"), name, fid)

        if etype in DELETE_TYPES:
            self.on_delete(fid)
            return True

        # 以 files/details 为准：事件里的 pick_code 与文件真实 pickcode 并不一致
        # （实测同一个文件两者完全不同），拿它写 strm 会 404。
        node = self.api.get_node(fid)
        if node is None:
            # 详情查不到时，目录还能用事件数据补登记；文件没有可信 pickcode，跳过。
            node = self._node_from_event(event)
            if node is None or not node["is_dir"]:
                log.debug("事件 %s 的节点 %s 查询不到，跳过", name, fid)
                return False

        if node["is_dir"]:
            self.on_dir(node, scan_subtree=etype in (2, 18))
        else:
            self.on_file(node)
        return True

    @staticmethod
    def _node_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
        """把事件记录转成节点结构（仅用于详情接口查不到时的兜底）。

        注意：事件里的 `pick_code` 与文件实际 pickcode 不同，这里刻意丢弃，
        只保留目录登记所需的字段，避免生成打不开的 strm。
        """
        fid = int(event.get("file_id") or 0)
        if not fid:
            return None
        name = str(event.get("file_name") or "")
        category = event.get("file_category")
        if not name or category in (None, ""):
            return None

        etype = int(event.get("type") or 0)
        if etype in DIR_ONLY_TYPES:
            is_dir = True
        elif etype in FILE_ONLY_TYPES:
            is_dir = False
        else:
            # 115 用 file_category: 0=目录，1=文件
            is_dir = str(category) in ("0", "folder", "dir")

        return {
            "id": fid,
            "parent_id": int(event.get("parent_id") or 0),
            "name": name,
            "is_dir": is_dir,
            "pickcode": "",
            "size": int(event.get("file_size") or 0),
            "sha1": str(event.get("sha1") or ""),
            "ctime": int(event.get("create_time") or 0),
            "mtime": int(event.get("update_time") or 0),
        }

    # ------------------------------------------------------------------ 新增 / 移动
    def on_dir(self, node: dict[str, Any], scan_subtree: bool = False) -> None:
        if node["id"] in self.scanner.ed2k_dirs():
            return
        parent_path = self.resolve_dir_path(node["parent_id"])
        if parent_path is None:
            log.debug("目录 %s 不在监控范围内", node["id"])
            return
        name = node["name"] or str(node["id"])
        rel = rel_join(parent_path, safe_component(name))
        old = self.db.get_dir(node["id"])
        if old and old["path"] and old["path"] != rel:
            log.info("目录移动/改名: %s -> %s", old["path"], rel)
            self.store.move_dir(old["path"], rel)
            self.db.rename_prefix(old["path"], rel)
            self._refresh_strm_paths(rel)
        self.db.upsert_dir(node["id"], node["parent_id"], name, rel)
        if scan_subtree or not old:
            stats = self.scanner.scan_subtree(node["id"], rel)
            log.info(
                "目录 %s 已扫描: 文件 %d（新写入 %d）", rel or "/", stats.files, stats.written
            )

    def on_file(self, node: dict[str, Any]) -> None:
        parent_path = self.resolve_dir_path(node["parent_id"])
        if parent_path is None:
            return
        name = node["name"] or str(node["id"])
        old = self.db.get_file(node["id"])

        # 改名/移动后不再符合过滤条件（例如 .mkv -> .txt）：清掉旧 strm
        if not self.scanner._file_ok(name):
            if old:
                self.store.remove(old["path"])
                self.db.delete_file(node["id"])
                log.info("文件不再匹配规则，已移除 strm: %s", old["path"])
            return

        rel = rel_join(parent_path, safe_component(name))
        pickcode = self.api.ensure_pickcode(node)
        if not pickcode:
            log.debug("文件 %s 缺少 pickcode，跳过", node["id"])
            return

        if old and old["path"] and old["path"] != rel:
            self.store.move(old["path"], rel)

        strm_rel = pick_strm_rel(self.store, self.db, rel, node["id"], self.scanner.claimed)
        self.scanner.claimed[str(self.store.abspath(strm_rel))] = node["id"]
        strm_path, changed = self.store.write(
            rel, pickcode, name, strm_rel=strm_rel,
            base_url=self.scanner.base_url_for(rel),
        )
        self.db.upsert_file(
            {
                "id": node["id"],
                "parent_id": node["parent_id"],
                "name": name,
                "path": rel,
                "pickcode": pickcode,
                "size": node["size"],
                "sha1": node["sha1"],
                "ctime": node["ctime"],
                "mtime": node["mtime"],
                "strm_path": strm_path,
            }
        )
        log.info("%s strm: %s", "写入" if changed else "更新", rel)

    def on_delete(self, fid: int) -> None:
        found = self.db.get_node(fid)
        if found is None:
            return
        kind, row = found
        links = self._link_settings()
        sync_local = links.get("remote_to_local", True) is not False
        if kind == "file":
            if sync_local:
                self._remove_local_file(row, links)
            self.db.delete_file(fid)
            log.info("删除 strm: %s", row["path"])
            return

        rel_dir = row["path"]
        removed = self.db.delete_dir_tree(fid)
        if sync_local:
            for item in removed:
                self._remove_local_file(item, links)
            if rel_dir:
                # 整目录删除：nfo / 海报 / 字幕本来就在这棵树里，一起清掉
                self.store.remove_rel_dir(rel_dir, delete_empty=self.cfg.delete_empty_dirs)
        log.info("删除目录 %s（连带 %d 个 strm）", rel_dir or "/", len(removed))

    def _remove_local_file(self, row: dict[str, Any], links: dict[str, Any],
                           prune: bool = True) -> None:
        """删掉一个文件对应的本地 strm（顺带清刮削文件）。"""
        strm_path = str(row.get("strm_path") or "")
        if strm_path:
            self.store.remove_abs(strm_path)
        else:
            self.store.remove(str(row.get("path") or ""))
        if not strm_path:
            return
        if links.get("scrape_clean", True) is False:
            return
        try:
            self.store.remove_scraped(strm_path)
        except Exception as exc:  # noqa: BLE001 - 刮削清理失败不影响主流程
            log.debug("清理刮削文件失败 %s: %s", strm_path, exc)

    def _link_settings(self) -> dict[str, Any]:
        """联动开关（网盘删除是否同步本地 / 是否清刮削）。读不到就按默认全开。"""
        try:
            from .linker import link_settings

            return link_settings(self._shared_db)
        except Exception:  # noqa: BLE001 - 配置读不到时保持原有行为
            return {}

    # ------------------------------------------------------------------ 路径解析
    def resolve_dir_path(self, cid: int) -> str | None:
        """把目录 id 解析为监控根下的相对路径；不在监控范围内返回 None。"""
        roots = self.scanner.resolve_roots()
        cid = int(cid)
        # ed2k 转存目录里的文件是临时的（会被定时清理），不生成 strm
        skip = self.scanner.ed2k_dirs()
        if cid in skip:
            return None
        if cid in roots:
            return roots[cid]

        chain: list[tuple[int, str, int]] = []
        seen: set[int] = set()
        current = cid
        base: str | None = None

        while True:
            if current in roots:
                base = roots[current]
                break
            if current in skip:
                return None
            if not current or current in seen or len(chain) >= 64:
                return None
            seen.add(current)

            row = self.db.get_dir(current)
            if row is not None:
                base = row["path"]
                break

            node = self.api.get_node(current)
            if node is None or not node["is_dir"]:
                return None
            chain.append(
                (node["id"], safe_component(node["name"] or str(node["id"])), node["parent_id"])
            )
            current = node["parent_id"]

        # 自底向上补回索引，顺便把路径算出来
        path = base or ""
        for node_id, name, parent_id in reversed(chain):
            path = rel_join(path, name)
            self.db.upsert_dir(node_id, parent_id, name, path)
        return path

    # ------------------------------------------------------------------ 辅助
    def _refresh_strm_paths(self, prefix: str) -> None:
        """目录整体搬家后，重算其下文件的 strm_path。"""
        rows = self.db.files_under(prefix)
        updates = []
        for row in rows:
            # 之前因同名冲突用过「带后缀」的命名，搬家后要保持一致
            old_name = Path(row["strm_path"]).name if row.get("strm_path") else ""
            keep_ext = bool(old_name) and old_name == Path(row["path"]).name + ".strm"
            new_path = str(self.store.abspath(strm_rel_path(row["path"], keep_ext=keep_ext)))
            updates.append((new_path, row["id"]))
        if updates:
            self.db.update_strm_paths(updates)

    def status(self) -> dict[str, Any]:
        return {
            "mode": "event" if self.events_ok else ("interval" if self.events_ok is False else "probing"),
            "events_ok": self.events_ok,
            "interval": self.cfg.fallback_interval,
            "last_poll": int(self.last_poll),
            "last_fallback": int(self.last_fallback),
            "fallback_runs": self.fallback_runs,
            "last_event_at": int(self.last_event_at),
            "cursor": self.db.get_meta_int("event_cursor"),
            "processed": self.processed,
        }
