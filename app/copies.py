"""同播复制（账号级）：多设备同时播放同一个文件时，各走一份独立的 115 副本。

为什么要复制：115 对**同一个文件**的并发拉流是共享的（同一条直链、同一份带宽），
两台设备同时看同一部片会互相抢带宽，也更容易撞上并发限制。复制一份之后每台
设备拿到自己的 pickcode 和直链，互不影响。

这里刻意做成**账号级**而不是某个工具的功能：strm 播放、ed2k 播放、以后任何
通过这个账号出网的播放，都走同一套判断。开关和参数存在账号自己的库里，
换账号就是换配置。

几条设计约定：

- **会话只存内存**：指纹 = 来源 IP + User-Agent。Direct Play 时播放器直接来
  请求本服务，指纹就是设备自己；如果是 Emby 服务端拉流转码，所有请求都来自
  Emby 容器，指纹天然相同 —— 那种情况确实认不出是谁在看，也就不会复制。
- **副本放在专用目录**（默认 115 根目录下的 ``PanShare同播副本``），扫描器会
  跳过它，不会把副本当成新片再生成一遍 strm。
- **复制只是优化**：任何一步失败都静默回退主文件，绝不因为「复制不出来」让
  播放失败。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .p115api import P115API, P115Error

log = logging.getLogger(__name__)

# 副本保留时长：最后一次被播放之后多久删掉（秒）
DEFAULT_TTL = 1800.0
MIN_TTL = 300.0
# 同一个文件最多几份副本（含正在用的）
DEFAULT_MAX = 3
MAX_MAX = 10
# 会话多久没动静就算「已经不在看了」，腾出它占的那份副本
SESSION_WINDOW = 1800.0
# 过期副本的自动清理节流
CLEAN_INTERVAL = 600.0
# 副本专用目录名（放在 115 根目录下，扫描器会跳过）
COPY_DIR_NAME = "PanShare同播副本"


class CopyError(Exception):
    """同播复制相关的可预期错误。"""


class CopyManager:
    """一个 115 账号的同播复制能力。"""

    def __init__(self, db: Any, api: P115API, cfg: Any = None):
        self.db = db
        self.api = api
        self.cfg = cfg
        # 文件 key -> {指纹: {"at": 时间, "pickcode": ..., "copy_id": ...}}
        self._sessions: dict[str, dict[str, dict[str, Any]]] = {}
        self._guard = threading.Lock()
        self._dir_lock = threading.Lock()
        self._dir_cid = 0
        self._last_clean = 0.0
        self._clean_guard = threading.Lock()

    # ------------------------------------------------------------------ 配置
    def _meta_float(self, key: str, default: float) -> float:
        raw = self.db.get_meta(key)
        if not raw:
            return default
        try:
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _meta_bool(self, key: str, default: bool) -> bool:
        raw = (self.db.get_meta(key) or "").strip().lower()
        if not raw:
            return default
        return raw in ("1", "true", "yes", "on", "y")

    @property
    def enabled(self) -> bool:
        """这个账号是否开启同播复制（默认开，和 ed2k 之前的默认一致）。"""
        return self._meta_bool("copy_enabled", True)

    @property
    def ttl(self) -> float:
        return max(MIN_TTL, self._meta_float("copy_ttl", DEFAULT_TTL))

    @property
    def max_copies(self) -> int:
        return min(MAX_MAX, max(1, int(self._meta_float("copy_max", DEFAULT_MAX))))

    def settings(self) -> dict[str, Any]:
        rows = self.db.list_play_copies()
        return {
            "enabled": self.enabled,
            "ttl": self.ttl,
            "ttl_minutes": round(self.ttl / 60, 1),
            "max": self.max_copies,
            "copies": len(rows),
            "size": sum(int(r.get("size") or 0) for r in rows),
            "dir_cid": str(self._dir_cid or self.db.get_meta_int("copy_dir_cid") or ""),
            "dir_name": self.db.get_meta("copy_dir_name") or COPY_DIR_NAME,
        }

    def save_settings(self, enabled: bool | None = None,
                      ttl: float | None = None,
                      max_copies: int | None = None) -> dict[str, Any]:
        if enabled is not None:
            self.db.set_meta("copy_enabled", "1" if enabled else "0")
        if ttl is not None:
            self.db.set_meta("copy_ttl", str(max(MIN_TTL, float(ttl))))
        if max_copies is not None:
            self.db.set_meta(
                "copy_max", str(min(MAX_MAX, max(1, int(max_copies))))
            )
        return self.settings()

    # ------------------------------------------------------------------ 播放
    def pick(self, pickcode: str, fingerprint: str, file_id: int = 0,
             name: str = "") -> str:
        """返回这次播放该用的 pickcode。

        正常情况下就是主文件；同一个文件在窗口期内被**另一台**设备播放时，
        回来的是副本的 pickcode。
        """
        if not self.enabled or not fingerprint or not pickcode:
            return pickcode
        key = "pc:" + pickcode
        try:
            fid = int(file_id or 0) or self._file_id(pickcode)
            if not fid:
                return pickcode
            return self._pick_locked(key, pickcode, fid, name, fingerprint)
        except Exception as exc:  # noqa: BLE001 - 复制是优化，失败绝不能让播放失败
            log.warning("同播复制失败，回退主文件 %s: %s", name or pickcode, exc)
            return pickcode

    def _pick_locked(self, key: str, main_pickcode: str, fid: int,
                     name: str, fingerprint: str) -> str:
        now = time.time()
        with self._guard:
            sessions = self._sessions.setdefault(key, {})
            for stale in [f for f, s in sessions.items() if now - s["at"] > SESSION_WINDOW]:
                sessions.pop(stale, None)
            mine = sessions.get(fingerprint)
            if mine is not None and mine.get("pickcode"):
                mine["at"] = now
                if mine.get("copy_id"):
                    self.db.update_play_copy(mine["copy_id"], last_used_at=int(now))
                return str(mine["pickcode"])
            others = [f for f in sessions if f != fingerprint]
            if not others:
                # 只有这一台在放：用主文件，不复制（省一份空间）
                sessions[fingerprint] = {"at": now, "pickcode": main_pickcode, "copy_id": ""}
                return main_pickcode
            taken = {
                s.get("copy_id") for f, s in sessions.items()
                if f != fingerprint and s.get("copy_id")
            }

        copy = self._pick_reusable(key, taken)
        if copy is None:
            copy = self._make_copy(key, fid, name)
        if copy is None:
            log.info("同播复制：%s 已到上限或复制失败，沿用主文件", name or main_pickcode)
            with self._guard:
                self._sessions.setdefault(key, {})[fingerprint] = {
                    "at": now, "pickcode": main_pickcode, "copy_id": "",
                }
            return main_pickcode

        pickcode = str(copy.get("pickcode") or "")
        if not pickcode:
            return main_pickcode
        self.db.update_play_copy(copy["id"], last_used_at=int(now))
        with self._guard:
            self._sessions.setdefault(key, {})[fingerprint] = {
                "at": now, "pickcode": pickcode, "copy_id": copy["id"],
            }
        log.info("同播复制：%s 第 %s 份副本 → %s",
                 name or main_pickcode, copy.get("seq"), fingerprint[:24])
        self._maybe_clean()
        return pickcode

    def _file_id(self, pickcode: str) -> int:
        try:
            return int(self.db.file_id_by_pickcode(pickcode) or 0)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------ 副本
    def ensure_dir(self) -> int:
        """副本专用目录（默认 115 根目录下的 ``PanShare同播副本``）。"""
        with self._dir_lock:
            if self._dir_cid:
                return self._dir_cid
            cid = self.db.get_meta_int("copy_dir_cid")
            if cid:
                self._dir_cid = cid
                return cid
            try:
                cid = self.api.find_child_dir(0, COPY_DIR_NAME) or 0
                if not cid:
                    cid = self.api.mkdir(0, COPY_DIR_NAME)
            except P115Error as exc:
                raise CopyError(f"创建副本目录失败: {exc}") from exc
            if not cid:
                raise CopyError("创建副本目录失败")
            self.db.set_meta("copy_dir_cid", int(cid))
            self.db.set_meta("copy_dir_name", COPY_DIR_NAME)
            self._dir_cid = int(cid)
            log.info("同播复制：副本目录 = %s（%s）", COPY_DIR_NAME, cid)
            return self._dir_cid

    def _pick_reusable(self, key: str, taken: set[Any]) -> dict[str, Any] | None:
        """找一份用过、没过期、也没被别的设备占着的副本。"""
        now = time.time()
        ttl = self.ttl
        for row in self.db.list_play_copies(key):
            if row["id"] in taken or not row.get("pickcode"):
                continue
            used = int(row.get("last_used_at") or row.get("created_at") or 0)
            if used and now - used < ttl:
                return row
        return None

    def _make_copy(self, key: str, file_id: int, name: str) -> dict[str, Any] | None:
        existing = self.db.list_play_copies(key)
        if len(existing) >= self.max_copies:
            return None
        cid = self.ensure_dir()
        made = self.api.copy_file(
            int(file_id), cid, cooldown=float(getattr(self.cfg, "scan_cooldown", 0) or 0)
        )
        if not made or not made.get("pickcode"):
            return None
        seq = max([int(r.get("seq") or 0) for r in existing] or [0]) + 1
        now = int(time.time())
        row = {
            "id": f"{key}:{seq}",
            "key": key,
            "kind": "play",
            "seq": seq,
            "file_id": int(made["file_id"]),
            "pickcode": str(made["pickcode"]),
            "name": str(made.get("name") or name or ""),
            "size": int(made.get("size") or 0),
            "dir_cid": cid,
            "created_at": now,
            "last_used_at": now,
        }
        self.db.upsert_play_copy(row)
        log.info("同播复制：为 %s 复制了第 %d 份副本（%s）",
                 name or key, seq, row["name"])
        return row

    def clean(self, force: bool = False) -> dict[str, Any]:
        """删掉过期副本（连 115 上的文件一起）。

        正在某台设备上用的副本不删：会话表里有它的 copy_id 就跳过，
        否则删掉后那台设备下一次 seek 会 404。
        """
        now = time.time()
        ttl = self.ttl
        with self._guard:
            active = set()
            for sessions in self._sessions.values():
                for state in sessions.values():
                    if state.get("copy_id") and now - state["at"] <= SESSION_WINDOW:
                        active.add(state["copy_id"])
        expired = [
            row for row in self.db.list_play_copies()
            if row["id"] not in active and (
                force
                or now - int(row.get("last_used_at") or row.get("created_at") or 0) >= ttl
            )
        ]
        if not expired:
            return {"removed": 0, "files": []}
        ids = [int(row["file_id"]) for row in expired if row.get("file_id")]
        if ids:
            try:
                self.api.delete_files(ids)
            except P115Error as exc:
                log.warning("删除 115 副本失败（记录仍会清掉）: %s", exc)
        for row in expired:
            self.db.delete_play_copy(row["id"])
        names = [row.get("name") or row["id"] for row in expired]
        log.info("清理同播副本: %d 个", len(names))
        return {"removed": len(names), "files": names}

    def _maybe_clean(self) -> None:
        """播放时顺手清一次过期副本（10 分钟最多一次，放后台线程）。"""
        now = time.time()
        with self._clean_guard:
            if now - self._last_clean < CLEAN_INTERVAL:
                return
            self._last_clean = now

        def work() -> None:
            try:
                self.clean()
            except Exception as exc:  # noqa: BLE001 - 清理失败不影响播放
                log.warning("自动清理同播副本失败: %s", exc)

        threading.Thread(target=work, name="copy-clean", daemon=True).start()

    def drop(self, pickcode: str) -> None:
        """文件被删时清掉它的会话（副本交给 TTL 清理）。"""
        if not pickcode:
            return
        with self._guard:
            self._sessions.pop("pc:" + pickcode, None)
