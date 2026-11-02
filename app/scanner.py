"""全量扫描：并发 BFS 遍历 115 目录树，落库 + 生成 strm。"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database, strm_config_cids
from .p115api import P115API, P115Error, WafBlocked
from .strm import StrmStore, pick_strm_rel, rel_join, safe_component

log = logging.getLogger(__name__)


@dataclass
class ScanStats:
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    dirs: int = 0
    files: int = 0
    written: int = 0
    unchanged: int = 0
    skipped: int = 0
    # 强制重写的数量（全量 / 全新生成：内容没变也重写）
    overwritten: int = 0
    removed: int = 0
    errors: int = 0
    throttled: int = 0
    aborted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": int(self.started_at),
            "finished_at": int(self.finished_at),
            "duration": round((self.finished_at or time.time()) - self.started_at, 1),
            "dirs": self.dirs,
            "files": self.files,
            "written": self.written,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "overwritten": self.overwritten,
            "removed": self.removed,
            "errors": self.errors,
            "throttled": self.throttled,
            "aborted": self.aborted,
        }


class Scanner:
    def __init__(self, cfg: Config, db: Database, api: P115API, store: StrmStore,
                 configs_db: Database | None = None, account: str | None = None):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.store = store
        # 115 STRM 配置存在主库里（带 account 字段），子账号的索引库是独立的；
        # 只读配置时要用主库，否则子账号扫描时找不到自己那条配置的请求头。
        self._configs_db = configs_db
        self._account = account
        self.stats = ScanStats()
        self._stop = False
        # 本次扫描中已被占用的 strm 路径（扫描期间索引还没入库，靠它防同名覆盖）
        self.claimed: dict[str, int] = {}
        # 全量扫描与子树扫描互斥，避免索引/统计互相踩踏
        self._run_lock = threading.Lock()
        # 监控根目录缓存（改根目录后调用 invalidate_roots()）
        self._roots_cache: dict[int, str] | None = None
        # 每条 115 STRM 配置自己的「STRM 请求头」：[(本地前缀, base_url), ...]
        self._base_urls: list[tuple[str, str]] | None = None
        # ed2k 转存目录：里面的文件是临时的，不参与全量生成，否则会和 ed2k 的 strm 重复
        self._skip_ids: set[int] = set()
        # 增量扫描时是否按目录指纹跳过没变化的子树（全量扫描永远不跳）
        self._smart = False
        # 强制重写 strm（不看内容是否相同）：全量 / 全新生成时打开，
        # 保证「全量」确实是一次完整重写，而不是「内容一样就跳过」。
        self._force = False
        # 本轮开扫前的索引快照：prune 只按它判断「哪些 strm 是我写的」
        self._prev_paths: set[str] = set()
        # 本轮是否因为 115 限流而中止（和用户手动停止区分开）
        self._throttled = False

    def _dir_stamp(self, node: dict[str, Any]) -> str:
        """目录指纹：115 会在子项变动时更新目录自身的修改时间。

        实测在子目录里新建条目会同时更新该目录的 mtime 和父目录里这一项的 mtime，
        所以「mtime 没变」基本等价于「这棵子树没动过」。
        名字也计入指纹，目录改名时一定重扫。
        """
        return f"{int(node.get('mtime') or 0)}|{node.get('name') or ''}"

    # ------------------------------------------------------------------ 过滤
    def _file_ok(self, name: str) -> bool:
        if not name:
            return False
        if not self.cfg.strm_all_files:
            suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
            if suffix not in self.cfg.include_ext:
                return False
        if self.cfg.include_regex and not re.search(self.cfg.include_regex, name, re.IGNORECASE):
            return False
        return not (
            self.cfg.exclude_regex and re.search(self.cfg.exclude_regex, name, re.IGNORECASE)
        )

    def _dir_ok(self, name: str) -> bool:
        if not self.cfg.exclude_dirs:
            return True
        low = name.lower()
        return not any(token.lower() in low for token in self.cfg.exclude_dirs)

    # ------------------------------------------------------------------ 根
    def root_prefix(self, cid: int, name: str = "") -> str:
        """监控根目录在本地 strm 目录里对应的前缀。"""
        if cid == 0:
            return ""
        if not self.cfg.include_root_name:
            return ""
        if not name:
            try:
                node = self.api.get_node(cid)
            except Exception:  # noqa: BLE001 - 名字拿不到就用 id 兜底
                node = None
            name = (node or {}).get("name") or ""
        return safe_component(name) if name else str(cid)

    def resolve_roots(self) -> dict[int, str]:
        """当前生效的监控根目录（来自数据库，网页上可增删）。

        值是目录在本地 strm 里的前缀：115 STRM 配置里显式指定了「本地 STRM 目录」
        就用它，否则按目录名推导。
        """
        if self._roots_cache is not None:
            return self._roots_cache
        roots: dict[int, str] = {}
        for row in self.db.list_roots():
            cid = int(row["cid"])
            if int(row.get("prefix_locked") or 0):
                # 前缀是明确指定过的（115 STRM 配置里的「本地 STRM 目录」）：
                # 空字符串也照用 —— 表示直接放在 STRM_ROOT 下
                roots[cid] = str(row.get("prefix") or "").strip("/")
                continue
            prefix = str(row.get("prefix") or "").strip("/")
            if not prefix:
                prefix = self.root_prefix(cid, row["name"])
            roots[cid] = prefix
        self._roots_cache = roots
        self.cfg.roots = roots
        return roots

    def invalidate_roots(self) -> None:
        """监控目录变更后调用，让下次解析重新读库。"""
        self._roots_cache = None
        self._base_urls = None
        self.cfg.roots = {}

    # ------------------------------------------------------------- 请求头（每条配置可不同）
    def base_url_map(self) -> list[tuple[str, str]]:
        """``[(本地目录前缀, STRM 请求头), ...]``，前缀长的排前面。

        115 STRM 的每条配置都能填自己的「STRM 请求头」（``strm_configs.base_url``）；
        以前这个字段只存不用，生成的 strm 永远用全局 ``PUBLIC_BASE_URL``，
        用户填了 172.17.0.1 却被写成局域网 IP 就是这么来的。
        """
        if self._base_urls is not None:
            return self._base_urls
        roots = self.resolve_roots()
        found: dict[str, str] = {}
        source_db = self._configs_db or self.db
        for row in source_db.list_strm_configs():
            # 子账号只看自己名下的配置：别的账号同前缀的请求头不能串过来
            if self._account is not None and str(row.get("account") or "") != self._account:
                continue
            base = str(row.get("base_url") or "").strip().rstrip("/")
            if not base:
                continue
            for cid in strm_config_cids(row):
                prefix = roots.get(int(cid))
                if prefix is None:
                    continue
                found[prefix] = base
        self._base_urls = sorted(found.items(), key=lambda kv: -len(kv[0]))
        return self._base_urls

    def base_url_for(self, rel_path: str) -> str:
        """按相对路径找出它属于哪条配置，返回那条配置的请求头（没有就返回空）。"""
        rel_path = (rel_path or "").strip("/")
        for prefix, base in self.base_url_map():
            if not prefix or rel_path == prefix or rel_path.startswith(prefix + "/"):
                return base
        return ""

    # ------------------------------------------------------------------ 主体
    def stop(self) -> None:
        self._stop = True

    def run(self, full: bool = True, force: bool = False) -> ScanStats:
        """跑一轮扫描。

        ``full=True`` 会清空本地索引后重扫；``force=True`` 表示**不看内容
        差异**，把所有 strm 重新写一遍（全量默认就带 force —— 监控目录点
        「全量」就是「全部重新生成覆盖」）。
        """
        with self._run_lock:
            return self._run(full, force=force or full)

    def _run(self, full: bool, force: bool = False) -> ScanStats:
        self._stop = False
        self._throttled = False
        self._force = bool(force)
        self.stats = ScanStats()
        self.claimed = {}
        # 还没登录 115（首次运行还没填 cookies）：直接跳过这一轮。
        # 不跳过的话每个目录都会抛登录错误、日志刷屏，而且会把索引误判成
        # 「文件全没了」——共用 STRM_ROOT 时还可能删掉别的账号的 strm。
        if not getattr(self.api, "authenticated", True):
            log.warning("还没有登录 115（未配置 cookies），跳过本轮扫描")
            self.stats.finished_at = time.time()
            return self.stats
        # 增量扫描默认开启指纹跳过：整棵树没变时几乎不产生接口请求
        self._smart = (not full) and self.cfg.smart_scan
        # 先把「上一轮我写过哪些 strm」记下来：多个账号共用 STRM_ROOT，
        # 只有这份快照能证明哪些文件是本账号的（见 prune）
        try:
            self._prev_paths = {str(Path(p).resolve()) for p in self.db.all_strm_paths()}
        except Exception:  # noqa: BLE001 - 快照失败就别删东西
            self._prev_paths = set()
        roots = self.resolve_roots()
        if not roots:
            # 没有登记任何监控目录：什么都不做（连索引都不清）。以前这里会
            # 「清空索引 → 扫 0 个目录 → 判定整棵树都失效」，把共用 STRM_ROOT
            # 里别的账号生成的 strm 全删掉。
            log.warning("没有监控目录，跳过本次扫描（不会清索引、不会删 strm）")
            self.stats.finished_at = time.time()
            self.db.set_meta("last_scan_stats", str(self.stats.as_dict()))
            return self.stats
        if full:
            log.info("清空本地索引，开始全量扫描")
            self.db.clear()
        else:
            log.info("开始增量扫描（%s）", "按目录指纹跳过未变化子树" if self._smart else "重新遍历全部目录")

        self._skip_ids = self.ed2k_dirs()
        log.info("扫描根目录: %s", ", ".join(f"{cid}->{prefix or '/'}" for cid, prefix in roots.items()))
        for cid, prefix in roots.items():
            self._register_root(cid, prefix)

        queue: list[tuple[int, str, str | None]] = [(cid, prefix, None) for cid, prefix in roots.items()]
        pending: set[Future] = set()
        last_log = time.time()

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.scan_workers),
                                thread_name_prefix="scan") as pool:
            def submit(cid: int, path: str, stamp: str | None) -> None:
                pending.add(pool.submit(self._process_dir, cid, path, stamp))

            for cid, path, stamp in queue:
                submit(cid, path, stamp)
            queue.clear()

            while pending:
                if self._stop:
                    self.stats.aborted = True
                    if self._throttled:
                        log.warning("115 限流，本轮扫描中止（已完成的目录仍然有效）")
                    else:
                        log.warning("收到停止信号，中断扫描")
                    for fut in pending:
                        fut.cancel()
                    break
                done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        subdirs = fut.result()
                    except Exception as exc:
                        self.stats.errors += 1
                        log.warning("遍历目录失败: %s", exc, exc_info=log.isEnabledFor(logging.DEBUG))
                        continue
                    for sub_id, sub_path, sub_stamp in subdirs:
                        submit(sub_id, sub_path, sub_stamp)
                if time.time() - last_log >= 10:
                    last_log = time.time()
                    log.info(
                        "扫描中… 目录 %d / 跳过 %d / 文件 %d / 新写入 %d / 失败 %d",
                        self.stats.dirs, self.stats.skipped, self.stats.files,
                        self.stats.written, self.stats.errors,
                    )

        # 限流时任务会很快失败并置 _stop，循环可能没走到 aborted 分支就退出了
        if self._stop and not self.stats.aborted:
            self.stats.aborted = True
        self.stats.finished_at = time.time()
        if not self.stats.aborted:
            self.db.set_meta("last_full_scan", int(self.stats.finished_at))
            if full and self.cfg.delete_orphan:
                self.stats.removed = self.prune()
        self.db.set_meta("last_scan_stats", str(self.stats.as_dict()))
        log.log(
            logging.WARNING if self.stats.aborted else logging.INFO,
            "%s: %s", "扫描中止" if self.stats.aborted else "扫描完成", self.stats.as_dict(),
        )
        return self.stats

    def _process_dir(self, cid: int, rel_path: str,
                     stamp: str | None = None) -> list[tuple[int, str, str]]:
        """遍历一个目录：写库、写 strm，返回待递归的子目录。

        ``stamp`` 是父目录里这一项的指纹（mtime|name）。增量扫描时若与上次一致，
        说明整棵子树没动过，直接跳过 —— 这是 115 关掉事件接口后
        「定时增量」能跑得动的原因。
        """
        if self._stop:
            return []
        if self._smart and stamp and self.db.get_dir_stamp(cid) == stamp:
            self.stats.skipped += 1
            return []

        subdirs: list[tuple[int, str, str]] = []
        file_rows: list[dict[str, Any]] = []
        dir_rows: list[tuple[int, int, str, str]] = []

        try:
            for node in self.api.iter_dir(
                cid,
                app=self.cfg.scan_app,
                page_workers=self.cfg.page_workers,
                cooldown=self.cfg.scan_cooldown,
            ):
                name = node["name"] or str(node["id"])
                if node["is_dir"]:
                    if node["id"] in self._skip_ids or not self._dir_ok(name):
                        continue
                    safe = safe_component(name)
                    child_rel = rel_join(rel_path, safe)
                    dir_rows.append((node["id"], node["parent_id"], name, child_rel))
                    subdirs.append((node["id"], child_rel, self._dir_stamp(node)))
                    continue

                if not self._file_ok(name):
                    continue
                safe = safe_component(name)
                child_rel = rel_join(rel_path, safe)
                pickcode = self.api.ensure_pickcode(node)
                strm_rel = pick_strm_rel(self.store, self.db, child_rel, node["id"], self.claimed)
                self.claimed[str(self.store.abspath(strm_rel))] = node["id"]
                strm_path, changed = ("", False)
                if pickcode:
                    strm_path, changed = self.store.write(
                        child_rel, pickcode, name, strm_rel=strm_rel,
                        base_url=self.base_url_for(child_rel),
                        force=self._force,
                    )
                file_rows.append(
                    {
                        "id": node["id"],
                        "parent_id": node["parent_id"],
                        "name": name,
                        "path": child_rel,
                        "pickcode": pickcode,
                        "size": node["size"],
                        "sha1": node["sha1"],
                        "ctime": node["ctime"],
                        "mtime": node["mtime"],
                        "strm_path": strm_path,
                    }
                )
                if changed:
                    self.stats.written += 1
                    if self._force:
                        self.stats.overwritten += 1
                else:
                    self.stats.unchanged += 1
        except WafBlocked as exc:
            # 账号被 WAF 限流：继续扫只会把封禁拖得更久，直接停掉本轮
            self.stats.throttled += 1
            self._throttled = True
            self._stop = True
            log.warning("列目录 %s (%s) 被限流，停止本轮扫描: %s", rel_path or "/", cid, exc)
            return subdirs
        except (P115Error, OSError) as exc:
            self.stats.errors += 1
            log.warning("列目录 %s (%s) 失败: %s", rel_path or "/", cid, exc)
            return subdirs
        except Exception as exc:
            self.stats.errors += 1
            log.warning("列目录 %s (%s) 异常: %s", rel_path or "/", cid, exc, exc_info=True)
            return subdirs

        self.db.upsert_dirs(dir_rows)
        self.db.upsert_files(file_rows)
        if stamp:
            self.db.set_dir_stamp(cid, stamp)
        self.stats.dirs += 1
        self.stats.files += len(file_rows)
        return subdirs

    # ------------------------------------------------------------------ 子树
    def ed2k_dirs(self) -> set[int]:
        """不该进索引的目录：ed2k 转存目录 + 同播副本目录。

        这两个目录里的文件是本服务自己放的「临时文件」，索引进来会变成
        一堆重复条目，还会给副本再生成一遍 strm。
        """
        cid = self.db.get_meta_int("ed2k_dir_cid") or self.cfg.ed2k_dir_cid
        cids = {cid} if cid else set()
        copy_cid = self.db.get_meta_int("copy_dir_cid")
        if copy_cid:
            cids.add(copy_cid)
        return cids

    def _register_root(self, cid: int, rel_path: str) -> None:
        """把目录自身写进 dirs 索引。

        不写的话，按 parent_id 收集子树时会在这里断链，
        「移除监控目录 / 删除整棵子树」就会漏掉这一支的文件。
        """
        parent_id = 0
        name = ""
        if cid != 0:
            try:
                node = self.api.get_node(cid)
            except Exception:  # noqa: BLE001 - 查不到就用 id 兜底登记
                node = None
            if node:
                parent_id = int(node.get("parent_id") or 0)
                name = node.get("name") or ""
            else:
                name = str(cid)
        self.db.upsert_dir(cid, parent_id, name, rel_path)

    def scan_subtree(self, cid: int, rel_path: str,
                     force: bool = False) -> ScanStats:
        """扫描单个目录（含子孙），用于增量事件里的新增/复制目录。

        ``force=True``：不看内容差异，全部重写（「全量 / 全新生成」用）。
        """
        with self._run_lock:
            self._skip_ids = self.ed2k_dirs()
            self._register_root(cid, rel_path)
            saved = self.stats
            saved_smart = self._smart
            saved_force = self._force
            self.stats = ScanStats()
            # 子树扫描一定是「有人明确说这里变了」，不做指纹跳过
            self._smart = False
            self._force = bool(force)
            pending: set[Future] = set()
            with ThreadPoolExecutor(max_workers=max(1, min(self.cfg.scan_workers, 4)),
                                    thread_name_prefix="subscan") as pool:
                pending.add(pool.submit(self._process_dir, cid, rel_path))
                while pending:
                    done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                    for fut in done:
                        try:
                            subdirs = fut.result()
                        except Exception:  # noqa: BLE001, S112 - 单个子树失败不影响其它
                            continue
                        for sub_id, sub_path, _stamp in subdirs:
                            pending.add(pool.submit(self._process_dir, sub_id, sub_path))
            result = self.stats
            self.stats = saved
            self._smart = saved_smart
            self._force = saved_force
            return result

    # ------------------------------------------------------------------ 清理
    def repair_missing(self, prefix: str = "") -> int:
        """把「索引里有、磁盘上已经被删掉」的 strm 重新写回来。

        场景：全量生成过一遍之后，用户在文件管理器里误删了若干 strm。
        115 那边没有任何变化，再跑一次全量时扫描器本来会因为「目录指纹/
        内容一致」而跳过，删掉的文件就一直缺着。这里直接按索引里的
        pickcode + 文件名重写，不需要再调一次 115 接口。
        """
        root = Path(self.store.root).resolve()
        fixed = 0
        try:
            rows = self.db.files_under(prefix)
        except Exception as exc:  # noqa: BLE001 - 补写失败不影响扫描结果
            log.warning("补写 strm 时读索引失败: %s", exc)
            return 0
        for row in rows:
            target = str(row.get("strm_path") or "")
            pickcode = str(row.get("pickcode") or "")
            if not target or not pickcode:
                continue
            if Path(target).exists():
                continue
            try:
                rel = Path(target).resolve().relative_to(root).as_posix()
            except (ValueError, OSError):
                continue
            rel_file = str(row.get("path") or rel)
            self.store.write(rel_file, pickcode, row.get("name") or "",
                             strm_rel=rel, base_url=self.base_url_for(rel_file))
            fixed += 1
        if fixed:
            log.info("补写被删掉的 strm: %d 个（%s）", fixed, prefix or "/")
        return fixed

    def prune(self) -> int:
        """删除本地存在、但已经不在索引里的 strm。

        多个账号现在**共用同一个 STRM_ROOT**（输出目录完全由每条 115 STRM
        配置里的「本地 STRM 目录」决定，不再按账号名分目录），所以这里绝不能
        遍历整个 STRM_ROOT 按「不在我的索引里」删文件 —— 那会把别的账号
        生成的 strm 一起清掉（实测丢过 8000+ 个）。
        只删「本引擎上一次索引里有、这次索引里没了」的文件。
        """
        if not self._prev_paths:
            # 没有上一轮的索引快照（刚启动/刚清过库）：无从判断归属，一律不删
            return 0
        known = {str(Path(p).resolve()) for p in self.db.all_strm_paths()}
        # ed2k 的 strm 不在 files 索引里，但同样是有效文件，不能当孤儿删掉
        known |= {
            str(Path(row["strm_path"]).resolve())
            for row in self.db.list_ed2k()
            if row.get("strm_path")
        }
        removed = 0
        for path in self._prev_paths - known:
            try:
                Path(path).unlink()
            except OSError:
                continue
            removed += 1
            self._drop_empty_dirs(Path(path).parent)
        if removed:
            log.info("清理失效 strm: %d 个", removed)
        return removed

    def _drop_empty_dirs(self, folder: Path) -> None:
        """删掉因为 strm 被删而变空的目录（不越过 STRM_ROOT）。"""
        try:
            root = self.store.root.resolve()
        except OSError:
            return
        cur = folder
        while True:
            try:
                cur_res = cur.resolve()
            except OSError:
                return
            if cur_res == root or root not in cur_res.parents:
                return
            try:
                cur.rmdir()          # 非空会抛 OSError，正好停下
            except OSError:
                return
            cur = cur.parent
