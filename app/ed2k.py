"""ed2k 链接 → 115 离线转存 → strm。

链路：

1. 添加 ed2k：**先写 strm**（内容指向 ``/p115/ed2k/<hash>``），播放器里马上就有条目；
2. 转存有两种时机，由「添加后立即转存」开关决定：
   - 关闭（默认，懒转存）：只写 strm，不提交任何离线任务，播放时才转存；
   - 打开：添加时立刻提交成 115 的离线下载任务，保存目录 = 「转存目录」；
3. 后台轮询任务（只轮询真的在转/已清理的记录），文件落地后记下 file_id / pickcode；
4. 播放时按 hash 找当前文件；文件不在就**当场**提交转存，等它就位再 302；
5. 定时把超过保留时长的文件从转存目录删掉 —— strm 保留，下次播放自动重新转存。

为什么 strm 不直接写 pickcode：转存目录会被定期清空，重新转存后 pickcode 变了，
写死的地址就 404 了。所以 strm 指向本服务的稳定路由，播放时再解析。

懒转存（``auto_transfer=False``）的好处：批量加几百条 ed2k 也不会往 115 里塞
几百个文件，只给真正看过的片子占空间；代价是第一次播放要等一会儿转存。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .p115api import P115API, P115Error, cloud_state
from .strm import StrmStore

log = logging.getLogger(__name__)

# ed2k://|file|名字|字节数|MD4-HASH|/
# 名字里可能带 "|" 以外的任意字符，用非贪婪匹配把 3 段固定结构捞出来
ED2K_RE = re.compile(
    r"ed2k://\|file\|(?P<name>.+?)\|(?P<size>\d+)\|(?P<hash>[0-9A-Fa-f]{32})\|",
    re.IGNORECASE,
)

# 同播复制已上移到账号级（见 app/copies.py）：ed2k 的播放只是把 pickcode
# 交给同一个 CopyManager 判断，这里不再保留自己的会话表与副本参数。


class Ed2kError(P115Error):
    """ed2k 功能相关的可读错误。"""


class Ed2kNotFound(Ed2kError):
    pass


class Ed2kNotReady(Ed2kError):
    """文件还在转存，暂时不能播放。"""


def parse_ed2k(url: str) -> dict[str, Any] | None:
    """解析 ed2k 链接；不是 ed2k 就返回 None。"""
    match = ED2K_RE.search((url or "").strip())
    if not match:
        return None
    return {
        "key": match.group("hash").lower(),
        "url": (url or "").strip(),
        "name": match.group("name"),
        "size": int(match.group("size")),
    }


def extract_ed2k_links(text: str, limit: int = 0) -> list[str]:
    """从一大段文本里把 ed2k 链接都捞出来（去重，保持原顺序）。

    支持「每行一条」，也支持粘贴一整段杂乱文本（只认 ``ed2k://|file|...|`` 结构）。
    """
    seen: set[str] = set()
    out: list[str] = []
    for match in ED2K_RE.finditer(text or ""):
        url = match.group(0).strip()
        if not url.endswith("|/"):
            url += "/"
        key = match.group("hash").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if limit and len(out) >= limit:
            break
    return out


class Ed2kManager:
    def __init__(self, cfg: Config, db: Database, api: P115API, store: StrmStore):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.store = store
        self._dir_cache: tuple[int, str, float] | None = None
        # 同一条 ed2k 可能被播放器同时开好几个连接（探测 + 播放）：
        # 加个按 hash 的锁，只让第一个去提交转存，其余等它出结果，
        # 否则会连着 cloud_add 两次 —— 115 对重复任务是「删掉旧的再建」，
        # 正在跑的转存会被自己打断。
        self._play_locks: dict[str, threading.Lock] = {}
        self._play_locks_guard = threading.Lock()
        # 最近一次提交转存的时间：115 那边要几十秒才落地，
        # 期间再提交只会「删掉旧任务重建」，反而更慢
        self._last_submit: dict[str, float] = {}
        # 同播复制由账号级 CopyManager 负责（app/copies.py），hub 建好后注入
        self.copies: Any = None
        # 网页上改过「本地 strm 目录」后，strm 写出器要按新目录拼相对路径
        self._sync_store_dir()

    def _play_lock(self, key: str) -> threading.Lock:
        with self._play_locks_guard:
            lock = self._play_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._play_locks[key] = lock
            return lock

    def _recently_submitted(self, key: str, within: float = 30.0) -> bool:
        """这条 ed2k 是不是刚提交过转存（避免播放器连点 / 多连接时反复提交）。"""
        with self._play_locks_guard:
            last = self._last_submit.get(key, 0.0)
        return bool(last) and (time.time() - last) < within

    # ------------------------------------------------------------------ 配置
    @property
    def strm_dir(self) -> str:
        """ed2k 的 strm 输出子目录（相对 STRM_ROOT，网页上可改）。

        以前的目录是固定的 ``ED2K_STRM_DIR``（默认 ``ed2k``），用户在容器里
        换目录就得改环境变量重启；现在存 meta，改完立即生效。
        """
        raw = self.db.get_meta("ed2k_strm_dir") or self.cfg.ed2k_strm_dir
        return (raw or "ed2k").strip("/") or "ed2k"

    def _sync_store_dir(self) -> None:
        try:
            self.store.ed2k_dir = self.strm_dir
        except Exception as exc:  # noqa: BLE001 - 同步失败不该影响转存
            log.warning("同步 ed2k strm 目录失败: %s", exc)
    def _meta_float(self, key: str, default: float) -> float:
        raw = self.db.get_meta(key)
        if not raw:
            return default
        try:
            return float(raw)
        except ValueError:
            return default

    def _meta_bool(self, key: str, default: bool) -> bool:
        raw = (self.db.get_meta(key) or "").strip().lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes", "on", "y")

    @property
    def auto_transfer(self) -> bool:
        """添加后是否立刻提交离线转存；False = 只写 strm，播放时再转存。"""
        default = bool(getattr(self.cfg, "ed2k_auto_transfer", False))
        return self._meta_bool("ed2k_auto_transfer", default)

    # ------------------------------------------------------------ 同播复制配置
    # 已上移：账号设置里的「同播复制」（app/copies.py）对所有播放生效，
    # ed2k 页面不再单独提供开关，避免同一个功能两处配置、以谁为准说不清。

    def settings(self) -> dict[str, Any]:
        # 读设置不查 115：WAF 限流时 get_node 失败会误显示成「未设置」
        cid = self.db.get_meta_int("ed2k_dir_cid") or self.cfg.ed2k_dir_cid
        name = self.db.get_meta("ed2k_dir_name") or ""
        if not name and self._dir_cache and self._dir_cache[0] == cid:
            name = self._dir_cache[1]
        return {
            "enabled": self.cfg.ed2k_enabled,
            # 字符串：19 位 cid 超过 JS 安全整数，前端拿到数字会丢精度
            "dir_cid": str(cid) if cid else "",
            "dir_name": name,
            # 完整路径（保存时记下）：输入框直接显示 /影音/ed2k
            "dir_path": self.db.get_meta("ed2k_dir_path") or "",
            "dir_default_name": self.cfg.ed2k_dir_name,
            "strm_dir": self.strm_dir,
            "ttl_hours": self._meta_float("ed2k_ttl_hours", self.cfg.ed2k_ttl_hours),
            "clean_interval": self._meta_float(
                "ed2k_clean_interval", self.cfg.ed2k_clean_interval
            ),
            # False = 添加时只生成 strm，播放时才转存（懒转存）
            "auto_transfer": self.auto_transfer,
            "tasks": self.db.ed2k_count(),
        }

    def save_settings(self, dir_cid: int | None = None, ttl_hours: float | None = None,
                      clean_interval: float | None = None,
                      strm_dir: str | None = None,
                      dir_path: str = "",
                      auto_transfer: bool | None = None) -> dict[str, Any]:
        if auto_transfer is not None:
            self.db.set_meta("ed2k_auto_transfer", "1" if auto_transfer else "0")
        if dir_cid is not None:
            cid = int(dir_cid)
            if cid:
                node = self.api.get_node(cid)
                if node is None or not node["is_dir"]:
                    raise Ed2kError(f"115 上不存在目录 {cid}")
                self.db.set_meta("ed2k_dir_name", node.get("name") or "")
                self.db.set_meta("ed2k_dir_path",
                                 (dir_path or "").strip() or (node.get("name") or ""))
            else:
                self.db.set_meta("ed2k_dir_path", "")
            self.db.set_meta("ed2k_dir_cid", cid)
            self._dir_cache = None
        if ttl_hours is not None:
            self.db.set_meta("ed2k_ttl_hours", max(0.0, float(ttl_hours)))
        if clean_interval is not None:
            self.db.set_meta("ed2k_clean_interval", max(60.0, float(clean_interval)))
        if strm_dir is not None:
            # 本地 strm 输出目录：只影响「之后生成」的 strm，已生成的不搬
            self.db.set_meta("ed2k_strm_dir", (strm_dir or "").strip("/"))
            self._sync_store_dir()
        return self.settings()

    @property
    def dir_cid(self) -> int:
        return self.db.get_meta_int("ed2k_dir_cid") or self.cfg.ed2k_dir_cid

    def target(self, create: bool = False) -> tuple[int, str]:
        """返回 (转存目录 cid, 名称)；create=True 时没配置就自动建一个。

        读取（create=False）不访问 115：有配置就直接返回，名字取 meta。
        """
        now = time.time()
        if self._dir_cache and self._dir_cache[2] > now:
            return self._dir_cache[0], self._dir_cache[1]

        cid = self.dir_cid
        if cid:
            name = self.db.get_meta("ed2k_dir_name") or ""
            if not create:
                return cid, name
            node = self.api.get_node(cid)
            if node is not None and node["is_dir"]:
                name = node.get("name") or name or str(cid)
                self.db.set_meta("ed2k_dir_name", name)
                self._dir_cache = (cid, name, now + 300)
                return cid, name
            log.warning("ed2k 转存目录 %s 已失效，稍后重新创建", cid)

        if not create:
            return cid, self.db.get_meta("ed2k_dir_name") or ""
        cid = self.api.find_child_dir(0, self.cfg.ed2k_dir_name)
        if not cid:
            cid = self.api.mkdir(0, self.cfg.ed2k_dir_name)
        self.db.set_meta("ed2k_dir_cid", cid)
        self.db.set_meta("ed2k_dir_name", self.cfg.ed2k_dir_name)
        self._dir_cache = (cid, self.cfg.ed2k_dir_name, now + 300)
        log.info("ed2k 转存目录就绪: %s (%s)", self.cfg.ed2k_dir_name, cid)
        return cid, self.cfg.ed2k_dir_name

    # ------------------------------------------------------------------ 添加
    def add(self, url: str, name: str = "", refresh: bool = True) -> dict[str, Any]:
        """添加一条 ed2k：**先写 strm**，再决定是否立刻转存。

        「添加后立即转存」关掉时（默认），这里连 115 都不碰：不查目录、不建目录、
        不提交离线任务，只把 strm 写出去，所以批量导入几百条也只是几百个小文件，
        网盘空间要等真正播放时才占用。
        """
        if not self.cfg.ed2k_enabled:
            raise Ed2kError("ed2k 功能已关闭（ED2K_ENABLED=0）")
        parsed = parse_ed2k(url)
        if parsed is None:
            raise Ed2kError("不是有效的 ed2k 链接，格式应为 ed2k://|file|名字|大小|HASH|/")
        key = parsed["key"]
        title = (name or "").strip() or parsed["name"] or key
        auto = self.auto_transfer

        dir_cid = 0
        if auto:
            dir_cid, _ = self.target(create=True)
            if not dir_cid:
                raise Ed2kError("转存目录不可用，请先选择一个 115 目录")
        else:
            # 只记下当前配置的转存目录（不联网、不建目录），播放时再确认
            dir_cid = int(self.dir_cid or 0)

        rec = self.db.get_ed2k(key)
        if rec is None:
            self.db.upsert_ed2k(
                {
                    "key": key,
                    "url": parsed["url"],
                    "name": title,
                    "dir_cid": dir_cid,
                    "size": parsed["size"],
                    "status": "pending" if auto else "idle",
                    "message": "" if auto else "等待播放时转存",
                }
            )
        else:
            # 已存在的记录只更新链接/名字；dir_cid 为空时不要覆盖掉原来的值
            fields: dict[str, Any] = {"url": parsed["url"], "name": title}
            if dir_cid:
                fields["dir_cid"] = dir_cid
            self.db.update_ed2k(key, **fields)
        rec = self.db.get_ed2k(key)
        assert rec is not None

        # 先把 strm 写出来（播放地址是稳定的，不依赖转存是否完成）
        self._write_strm(rec)
        if auto:
            self._submit(rec)
            if refresh:
                self.refresh(key)
        log.info("新增 ed2k: %s (%s)%s", title, key, "" if auto else "，等待播放时转存")
        return self.db.get_ed2k(key) or rec

    def add_many(self, text: str, limit: int = 200) -> dict[str, Any]:
        """批量添加 ed2k 链接，返回逐条结果。

        逐条提交（115 的离线接口对突发请求比较敏感），最后统一刷新一次状态，
        避免 N 条任务把转存目录翻 N 遍。
        """
        urls = extract_ed2k_links(text, limit=limit)
        if not urls:
            raise Ed2kError("没有识别到 ed2k 链接（格式 ed2k://|file|名字|大小|HASH|/）")
        results: list[dict[str, Any]] = []
        for url in urls:
            parsed = parse_ed2k(url) or {}
            try:
                rec = self.add(url, refresh=False)
                results.append(
                    {"ok": True, "key": rec["key"], "name": rec["name"],
                     "status": rec.get("status", "pending")}
                )
            except (Ed2kError, P115Error) as exc:
                results.append(
                    {"ok": False, "key": parsed.get("key", ""),
                     "name": parsed.get("name", ""), "error": str(exc)}
                )
                log.warning("批量添加失败 %s: %s", parsed.get("name", ""), exc)
            time.sleep(0.2)
        try:
            self.refresh()
        except P115Error as exc:
            log.warning("批量添加后刷新状态失败（不影响已提交的任务）: %s", exc)
        added = sum(1 for item in results if item["ok"])
        return {"added": added, "failed": len(results) - added, "items": results}

    def _submit(self, rec: dict[str, Any]) -> None:
        """提交离线转存。懒转存的记录这里是**第一次**真正碰 115 的目录。"""
        key = rec["key"]
        cid = int(rec.get("dir_cid") or 0)
        if not cid:
            # 懒转存时记录里的 dir_cid 可能是 0（添加时还没配目录）：
            # 到这里才去确认 / 自动创建转存目录
            try:
                cid, _ = self.target(create=True)
            except P115Error as exc:
                self.db.update_ed2k(key, status="failed", message=f"转存目录不可用：{exc}")
                log.warning("ed2k 提交转存失败 %s: 转存目录不可用 %s", rec.get("name"), exc)
                return
            if not cid:
                self.db.update_ed2k(
                    key, status="failed", message="转存目录不可用，请先选择一个 115 目录"
                )
                return
            self.db.update_ed2k(key, dir_cid=cid)
            rec["dir_cid"] = cid
        result = self.api.cloud_add(rec["url"], cid, key)
        self._last_submit[key] = time.time()
        if result["ok"]:
            self.db.update_ed2k(key, status="pending", message="已提交转存")
        else:
            message = result["message"] or f"提交转存失败（errcode={result['errcode']}）"
            self.db.update_ed2k(key, status="failed", message=message)
            log.warning("ed2k 提交转存失败 %s: %s", rec["name"], message)

    # ------------------------------------------------------------------ 刷新
    def refresh(self, key: str | None = None) -> int:
        """刷新转存状态；文件到位就记下 file_id / pickcode 并补 strm。"""
        if key:
            rec = self.db.get_ed2k(key)
            recs = [rec] if rec else []
        else:
            recs = self.db.list_ed2k(("pending", "cleared"))
        if not recs:
            return 0

        # 同一个目录只列一次，避免 N 个任务把目录翻 N 遍
        listed: dict[int, dict[str, dict[str, Any]]] = {}
        handled = 0
        for rec in recs:
            if not rec:
                continue
            cid = int(rec["dir_cid"] or 0)
            if not cid:
                continue
            try:
                if cid not in listed:
                    _, files = self.api.list_dir(
                        cid, app=self.cfg.scan_app, page_workers=0, cooldown=self.cfg.scan_cooldown
                    )
                    listed[cid] = {f["name"]: f for f in files}
                if self._refresh_one(rec, listed[cid]):
                    handled += 1
            except Exception as exc:  # noqa: BLE001 - 单个任务失败不影响其他任务
                log.warning("刷新 ed2k 任务 %s 失败: %s", rec.get("key"), exc)
        return handled

    def _refresh_one(self, rec: dict[str, Any], files: dict[str, dict[str, Any]]) -> bool:
        key = rec["key"]
        node = self._pick(rec, files)
        if node is not None:
            self._mark_ready(rec, node)
            return True

        task = self.api.cloud_task(key)
        state, percent, text = cloud_state(task)
        if state == "running":
            self.db.update_ed2k(key, status="pending", message=f"{text} {percent}%".strip())
            return False
        if state == "failed":
            self.db.update_ed2k(key, status="failed", message=text or "转存失败")
            return False
        if state == "done":
            # 任务说完成了，但目录里没有文件：可能被人手动移走/删掉了
            self.db.update_ed2k(key, status="pending", message="任务已完成，等待文件落盘")
            return False
        # 任务记录不存在：只有「已清理」才需要重新转存；
        # 刚提交的任务（pending）这里不重复提交，避免触发「任务已存在」的删-建循环。
        if rec["status"] == "cleared":
            self._submit(rec)
        return False

    def _pick(self, rec: dict[str, Any],
              files: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """在转存目录里找到这个 ed2k 对应的文件。"""
        for node in files.values():
            if rec["file_id"] and int(node["id"]) == int(rec["file_id"]):
                return node
        node = files.get(rec["name"])
        if node is not None:
            return node
        stem = str(rec["name"]).rsplit(".", 1)[0]
        for name, item in files.items():
            if name.rsplit(".", 1)[0] == stem:
                return item
        return None

    def _mark_ready(self, rec: dict[str, Any], node: dict[str, Any]) -> None:
        key = rec["key"]
        pickcode = node.get("pickcode") or self.api.ensure_pickcode(node)
        if not pickcode:
            return
        self.db.update_ed2k(
            key,
            file_id=int(node["id"]),
            pickcode=pickcode,
            size=int(node.get("size") or rec.get("size") or 0),
            status="ready",
            message="",
        )
        if not rec.get("strm_path") or not Path(rec["strm_path"]).exists():
            self._write_strm(rec)
        log.info("ed2k 转存完成: %s", rec["name"])

    # ------------------------------------------------------------------ 播放
    def playable(self, key: str, wait: float | None = None) -> str:
        """拿到可用于播放的 pickcode；文件不在就重新转存并等一会儿。

        懒转存下这是「第一次真正转存」的入口：同一个 hash 的并发请求用进程内
        锁排队，第一个去提交，后面的等它出结果，不会各自 cloud_add 一遍。
        """
        if not self.cfg.ed2k_enabled:
            raise Ed2kError("ed2k 功能已关闭")
        rec = self.db.get_ed2k(key)
        if rec is None:
            raise Ed2kNotFound(f"没有这个 ed2k 任务: {key}")

        with self._play_lock(key):
            return self._playable_locked(key, rec, wait)

    def _playable_locked(self, key: str, rec: dict[str, Any],
                         wait: float | None) -> str:
        deadline = time.time() + (self.cfg.ed2k_play_wait if wait is None else max(0.0, wait))
        submitted = False
        while True:
            node = self._locate(rec)
            if node is not None:
                pickcode = node.get("pickcode") or self.api.ensure_pickcode(node)
                if pickcode:
                    self._mark_ready(rec, node)
                    self.db.update_ed2k(key, last_play_at=int(time.time()))
                    return pickcode

            if not submitted:
                state, percent, text = cloud_state(self.api.cloud_task(key))
                if state == "running":
                    self.db.update_ed2k(key, status="pending",
                                        message=f"{text} {percent}%".strip())
                else:
                    # missing / failed / done 但文件不见了 → 重新提交转存
                    # （刚提交过就只等着，别再删一次重建）
                    if self._recently_submitted(key):
                        log.debug("ed2k 刚提交过转存，继续等待: %s", rec["name"])
                    else:
                        log.info("ed2k 文件不在转存目录，重新转存: %s", rec["name"])
                        self._submit(rec)
                submitted = True

            if time.time() >= deadline:
                latest = self.db.get_ed2k(key) or rec
                raise Ed2kNotReady(
                    f"正在转存到 115（{latest.get('message') or '排队中'}），请稍后重试"
                )
            time.sleep(3)
            rec = self.db.get_ed2k(key) or rec

    def _locate(self, rec: dict[str, Any]) -> dict[str, Any] | None:
        """在转存目录里定位文件；顺带核对 file_id 是否还有效。"""
        cid = int(rec["dir_cid"] or self.dir_cid)
        if not cid:
            return None
        if rec["file_id"]:
            node = self.api.get_node(int(rec["file_id"]))
            if node is not None and not node["is_dir"] and int(node["parent_id"]) == cid:
                return node
        _, files = self.api.list_dir(
            cid, app=self.cfg.scan_app, page_workers=0, cooldown=self.cfg.scan_cooldown
        )
        return self._pick(rec, {f["name"]: f for f in files})

    # ------------------------------------------------------------------ 清理
    def clean(self, force: bool = False) -> dict[str, Any]:
        """清理转存目录。

        默认只删本服务转存、且超过保留时长的文件；
        force=True 清空整个目录（包含目录里不是本服务放的文件）。

        同播复制产生的副本由账号级 CopyManager 一起回收（副本放在它自己的
        目录里，不占转存目录）。
        """
        copies: dict[str, Any] = {"removed": 0, "files": []}
        if self.copies is not None:
            try:
                copies = self.copies.clean(force=force)
            except P115Error as exc:
                log.warning("清理同播副本失败: %s", exc)
        cid, name = self.target()
        if not cid:
            return {"removed": 0, "files": [], "dir_cid": 0,
                    "copies_removed": copies["removed"]}
        ttl = self.settings()["ttl_hours"]
        if not force and ttl <= 0:
            return {"removed": 0, "files": [], "dir_cid": cid,
                    "copies_removed": copies["removed"]}

        _, files = self.api.list_dir(
            cid, app=self.cfg.scan_app, page_workers=0, cooldown=self.cfg.scan_cooldown
        )
        known = {int(r["file_id"]): r for r in self.db.list_ed2k() if r["file_id"]}
        now = time.time()
        expired: list[dict[str, Any]] = []
        for node in files:
            rec = known.get(int(node["id"]))
            if rec is None:
                if force:
                    expired.append(node)
                continue
            base = int(rec.get("last_play_at") or rec.get("created_at") or 0)
            if force or (base and now - base >= ttl * 3600):
                expired.append(node)

        if not expired:
            return {"removed": 0, "files": [], "dir_cid": cid,
                    "copies_removed": copies["removed"]}

        self.api.delete_files([int(node["id"]) for node in expired])
        for node in expired:
            rec = known.get(int(node["id"]))
            if rec is not None:
                self.db.update_ed2k(
                    rec["key"], status="cleared", file_id=0, pickcode="", message="已清理"
                )
        titles = [node["name"] for node in expired]
        log.info("清理 ed2k 转存目录「%s」: %d 个文件", name or cid, len(titles))
        return {"removed": len(titles), "files": titles, "dir_cid": cid,
                "copies_removed": copies["removed"]}

    # ------------------------------------------------------------------ 维护
    def delete(self, key: str, delete_file: bool = True) -> dict[str, Any]:
        """删掉一条 ed2k 记录（连带 strm、同播副本，可选删除 115 上的文件）。"""
        rec = self.db.delete_ed2k(key)
        if rec is None:
            raise Ed2kNotFound(f"没有这个 ed2k 任务: {key}")
        if rec.get("strm_path"):
            self.store.remove_abs(rec["strm_path"])
        else:
            self.store.remove_ed2k(rec["key"], rec["name"])
        # 副本跟着主记录一起消失：不然 115 上会留下无主的同名文件
        copies: list[dict[str, Any]] = []
        pickcode = str(rec.get("pickcode") or "")
        if pickcode:
            copies = self.db.delete_play_copies_for("pc:" + pickcode)
        if self.copies is not None and pickcode:
            self.copies.drop(pickcode)
        if delete_file:
            ids = [int(c["file_id"]) for c in copies if c.get("file_id")]
            if rec.get("file_id"):
                ids.append(int(rec["file_id"]))
            if ids:
                try:
                    self.api.delete_files(ids)
                except P115Error as exc:
                    log.warning("删除 115 文件失败（记录已删）: %s", exc)
        return rec

    # ------------------------------------------------------------ 导入 / 导出
    EXPORT_KIND = "ed2k"
    EXPORT_VERSION = 1

    def export_data(self, include_files: bool = True) -> dict[str, Any]:
        """把 ed2k 任务 + 配置导成一份 JSON（换设备时导入回去）。

        默认带上 ``file_id`` / ``pickcode``：同一个 115 账号换机器时它们仍然有效，
        导入后第一次播放就能直接命中，不用重新转存。换的是**另一个 115 账号**
        时这两个值必然失效 —— 导入时会被重置成 ``idle``，播放时自动重新转存。
        """
        settings = self.settings()
        items: list[dict[str, Any]] = []
        for rec in self.db.list_ed2k():
            item: dict[str, Any] = {
                "key": rec["key"],
                "url": rec.get("url") or "",
                "name": rec.get("name") or "",
                "size": int(rec.get("size") or 0),
                "status": rec.get("status") or "idle",
                "created_at": int(rec.get("created_at") or 0),
                "last_play_at": int(rec.get("last_play_at") or 0),
            }
            if include_files:
                # 19 位 id 用字符串，避免 JS 侧丢精度
                item["file_id"] = str(rec.get("file_id") or "")
                item["pickcode"] = rec.get("pickcode") or ""
            items.append(item)
        return {
            "app": "panshare",
            "kind": self.EXPORT_KIND,
            "version": self.EXPORT_VERSION,
            "exported_at": int(time.time()),
            "settings": {
                "dir_cid": settings["dir_cid"],
                "dir_name": settings["dir_name"],
                "dir_path": settings["dir_path"],
                "strm_dir": settings["strm_dir"],
                "ttl_hours": settings["ttl_hours"],
                "clean_interval": settings["clean_interval"],
                "auto_transfer": settings["auto_transfer"],
            },
            "items": items,
        }

    def import_data(self, payload: dict[str, Any], mode: str = "merge",
                    apply_settings: bool = False) -> dict[str, Any]:
        """导入备份。mode：

        * ``merge``（默认）：只加新的，已存在的 key 原样跳过；
        * ``update``：已存在的用备份里的链接/名字/大小覆盖，其余不动；
        * ``replace``：先清空本地记录（**不删 115 上的文件**），再全量导入。

        导入的记录一审置 ``idle``：换账号后 file_id/pickcode 可能已经指向别人的
        文件，等播放时由 ``_locate`` 自己校验并纠正，比盲信备份安全。
        """
        mode = (mode or "merge").strip().lower()
        if mode not in ("merge", "update", "replace"):
            raise Ed2kError(f"不支持的导入模式：{mode}")
        if not isinstance(payload, dict):
            raise Ed2kError("导入内容不是合法的 JSON 对象")
        items = payload.get("items")
        if not isinstance(items, list):
            inner = payload.get("data")
            if isinstance(inner, dict) and isinstance(inner.get("items"), list):
                items = inner["items"]
        if not isinstance(items, list):
            raise Ed2kError("导入文件里没有 items 列表（请用本功能导出的 JSON）")

        removed = 0
        if mode == "replace":
            for rec in self.db.list_ed2k():
                self.delete(rec["key"], delete_file=False)
                removed += 1

        added = updated = skipped = failed = 0
        errors: list[str] = []
        for raw in items:
            if not isinstance(raw, dict):
                failed += 1
                continue
            parsed = parse_ed2k(str(raw.get("url") or ""))
            if parsed is None:
                failed += 1
                if len(errors) < 10:
                    errors.append(str(raw.get("name") or raw.get("key") or "")[:60])
                continue
            key = parsed["key"]
            name = str(raw.get("name") or "").strip() or parsed["name"] or key
            size = int(raw.get("size") or parsed["size"] or 0)
            exists = self.db.get_ed2k(key)
            if exists is not None and mode == "merge":
                skipped += 1
                continue
            if exists is None:
                self.db.upsert_ed2k({
                    "key": key,
                    "url": parsed["url"],
                    "name": name,
                    "size": size,
                    "dir_cid": int(self.dir_cid or 0),
                    "file_id": int(raw.get("file_id") or 0),
                    "pickcode": str(raw.get("pickcode") or ""),
                    "status": "idle",
                    "message": "已导入，等待播放时确认",
                    "created_at": int(raw.get("created_at") or 0),
                    "last_play_at": int(raw.get("last_play_at") or 0),
                })
                added += 1
            else:
                self.db.update_ed2k(key, url=parsed["url"], name=name, size=size)
                updated += 1

        if apply_settings:
            saved = payload.get("settings")
            if isinstance(saved, dict):
                try:
                    self.save_settings(
                        ttl_hours=saved.get("ttl_hours"),
                        clean_interval=saved.get("clean_interval"),
                        strm_dir=(str(saved.get("strm_dir") or "").strip("/") or None),
                        auto_transfer=saved.get("auto_transfer"),
                    )
                except (Ed2kError, P115Error, TypeError, ValueError) as exc:
                    # 配置只是附带项：导不进去也不该让任务导入失败
                    log.warning("导入 ed2k 配置失败（任务已导入）: %s", exc)

        strm = self.restore_strm()
        log.info(
            "导入 ed2k：新增 %d，更新 %d，跳过 %d，失败 %d，补 strm %d",
            added, updated, skipped, failed, strm,
        )
        return {
            "ok": True,
            "mode": mode,
            "total": len(items),
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "failed": failed,
            "removed": removed,
            "strm": strm,
            "errors": errors,
        }

    def restore_strm(self) -> int:
        """补写缺失的 strm（strm 卷被清空 / 容器重建后）。

        懒转存的记录（idle）也要补：它们可能一次都没播放过，strm 却是用户
        在 Emby 里能看到的唯一入口。
        """
        count = 0
        for rec in self.db.list_ed2k(("idle", "ready", "cleared")):
            if rec.get("strm_path") and Path(rec["strm_path"]).exists():
                continue
            self._write_strm(rec)
            count += 1
        if count:
            log.info("补齐 ed2k strm: %d 个", count)
        return count

    def _write_strm(self, rec: dict[str, Any]) -> str:
        old = rec.get("strm_path") or ""
        path, _ = self.store.write_ed2k(rec["key"], rec["name"], self.db, old)
        if old and old != path:
            # 名字变了（或撞名改了带 hash 的后缀），旧文件要清掉
            self.store.remove_abs(old)
        self.db.update_ed2k(rec["key"], strm_path=path)
        rec["strm_path"] = path
        return path

    # ------------------------------------------------------------------ 循环
    def run_forever(self, stop: threading.Event) -> None:
        if not self.cfg.ed2k_enabled:
            return
        cur = self.settings()
        log.info(
            "ed2k 后台任务启动：保留 %.1f 小时，每 %.0f 分钟检查清理，转存时机=%s",
            cur["ttl_hours"],
            cur["clean_interval"] / 60,
            "添加时立即转存" if cur["auto_transfer"] else "播放时再转存",
        )
        try:
            self.restore_strm()
        except Exception as exc:  # noqa: BLE001
            log.warning("补齐 ed2k strm 失败: %s", exc)

        last_clean = 0.0
        while not stop.is_set():
            try:
                if self.db.list_ed2k(("pending", "cleared")):
                    self.refresh()
            except Exception as exc:  # noqa: BLE001
                log.warning("刷新 ed2k 任务失败: %s", exc)
            now = time.time()
            interval = self.settings()["clean_interval"]
            if now - last_clean >= interval:
                last_clean = now
                try:
                    self.clean()
                except Exception as exc:  # noqa: BLE001
                    log.warning("清理 ed2k 转存目录失败: %s", exc)
            stop.wait(self.cfg.ed2k_poll_interval)


__all__ = [
    "Ed2kError",
    "Ed2kManager",
    "Ed2kNotFound",
    "Ed2kNotReady",
    "extract_ed2k_links",
    "parse_ed2k",
]
