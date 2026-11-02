"""SQLite 状态库：目录索引、文件索引、增量游标。

只存元数据，不存文件内容；删除 .strm 时依赖这里的索引定位。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# 表名白名单（table_has_rows 要把它拼进 SQL）
_TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS dirs (
    id        INTEGER PRIMARY KEY,
    parent_id INTEGER NOT NULL DEFAULT 0,
    name      TEXT    NOT NULL DEFAULT '',
    path      TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    parent_id  INTEGER NOT NULL DEFAULT 0,
    name       TEXT    NOT NULL DEFAULT '',
    path       TEXT    NOT NULL DEFAULT '',
    pickcode   TEXT    NOT NULL DEFAULT '',
    size       INTEGER NOT NULL DEFAULT 0,
    sha1       TEXT    NOT NULL DEFAULT '',
    ctime      INTEGER NOT NULL DEFAULT 0,
    mtime      INTEGER NOT NULL DEFAULT 0,
    strm_path  TEXT    NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS roots (
    cid      INTEGER PRIMARY KEY,
    name     TEXT    NOT NULL DEFAULT '',
    added_at INTEGER NOT NULL DEFAULT 0,
    prefix   TEXT    NOT NULL DEFAULT '',
    prefix_locked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS media_servers (
    id         TEXT PRIMARY KEY,
    name       TEXT    NOT NULL DEFAULT '',
    host       TEXT    NOT NULL DEFAULT '',
    api_key    TEXT    NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    note       TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS strm_configs (
    id            TEXT PRIMARY KEY,
    account       TEXT    NOT NULL DEFAULT '',
    name          TEXT    NOT NULL DEFAULT '',
    remote_cid    TEXT    NOT NULL DEFAULT '',
    remote_cids   TEXT    NOT NULL DEFAULT '',
    remote_path   TEXT    NOT NULL DEFAULT '',
    remote_paths  TEXT    NOT NULL DEFAULT '',
    local_dir     TEXT    NOT NULL DEFAULT '',
    path_dirs     TEXT    NOT NULL DEFAULT '',
    base_url      TEXT    NOT NULL DEFAULT '',
    media_exts    TEXT    NOT NULL DEFAULT '',
    meta_exts     TEXT    NOT NULL DEFAULT '',
    download_meta INTEGER NOT NULL DEFAULT 1,
    event_watch   INTEGER NOT NULL DEFAULT 1,
    api_pull      INTEGER NOT NULL DEFAULT 1,
    redirect      INTEGER NOT NULL DEFAULT 1,
    auto_scan     INTEGER NOT NULL DEFAULT 0,
    scan_interval INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'idle',
    last_run      INTEGER NOT NULL DEFAULT 0,
    files         INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ed2k (
    key          TEXT PRIMARY KEY,
    url          TEXT    NOT NULL DEFAULT '',
    name         TEXT    NOT NULL DEFAULT '',
    dir_cid      INTEGER NOT NULL DEFAULT 0,
    file_id      INTEGER NOT NULL DEFAULT 0,
    pickcode     TEXT    NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'pending',
    message      TEXT    NOT NULL DEFAULT '',
    strm_path    TEXT    NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL DEFAULT 0,
    last_play_at INTEGER NOT NULL DEFAULT 0
);
-- 同播复制：同一部片被多台设备同时播放时，在 115 里复制出来的副本。
-- 副本只是「另一份同名文件」，用来给第二台设备一条独立的 pickcode / 直链，
-- 避免两台设备共用一个直链互相抢带宽。TTL 到了就连同 115 文件一起清掉。
-- 同播副本：多设备同时播放同一个文件时给后来者用的那份拷贝。
-- 一张表通用于 strm 播放和 ed2k 播放，key = "pc:<主文件 pickcode>"。
CREATE TABLE IF NOT EXISTS play_copies (
    id           TEXT PRIMARY KEY,
    key          TEXT    NOT NULL DEFAULT '',
    kind         TEXT    NOT NULL DEFAULT '',
    seq          INTEGER NOT NULL DEFAULT 0,
    file_id      INTEGER NOT NULL DEFAULT 0,
    pickcode     TEXT    NOT NULL DEFAULT '',
    name         TEXT    NOT NULL DEFAULT '',
    size         INTEGER NOT NULL DEFAULT 0,
    dir_cid      INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL DEFAULT 0,
    last_used_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dir_stamps (
    cid        INTEGER PRIMARY KEY,
    stamp      TEXT    NOT NULL DEFAULT '',
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS shares (
    share_code   TEXT PRIMARY KEY,
    receive_code TEXT    NOT NULL DEFAULT '',
    url          TEXT    NOT NULL DEFAULT '',
    name         TEXT    NOT NULL DEFAULT '',
    dir_cid      INTEGER NOT NULL DEFAULT 0,
    direct       INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'pending',
    message      TEXT    NOT NULL DEFAULT '',
    total        INTEGER NOT NULL DEFAULT 0,
    done         INTEGER NOT NULL DEFAULT 0,
    my_share_code   TEXT NOT NULL DEFAULT '',
    my_receive_code TEXT NOT NULL DEFAULT '',
    shared_ids      TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS share_files (
    share_code TEXT    NOT NULL,
    rel_path   TEXT    NOT NULL DEFAULT '',
    file_id    INTEGER NOT NULL DEFAULT 0,
    pickcode   TEXT    NOT NULL DEFAULT '',
    name       TEXT    NOT NULL DEFAULT '',
    size       INTEGER NOT NULL DEFAULT 0,
    sha1       TEXT    NOT NULL DEFAULT '',
    strm_path  TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (share_code, rel_path)
);
CREATE TABLE IF NOT EXISTS accounts (
    name       TEXT PRIMARY KEY,
    cookies    TEXT    NOT NULL DEFAULT '',
    uid        TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS emby_users (
    emby_user_id TEXT PRIMARY KEY,
    emby_user_name TEXT NOT NULL DEFAULT '',
    account      TEXT NOT NULL DEFAULT '',
    updated_at   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS emby_tokens (
    token       TEXT PRIMARY KEY,
    emby_user_id TEXT NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS instant (
    sha1           TEXT NOT NULL,
    account        TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    size           INTEGER NOT NULL DEFAULT 0,
    source_pickcode TEXT NOT NULL DEFAULT '',
    source_account TEXT NOT NULL DEFAULT '',
    file_id        INTEGER NOT NULL DEFAULT 0,
    pickcode       TEXT NOT NULL DEFAULT '',
    created_at     INTEGER NOT NULL DEFAULT 0,
    last_play_at   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sha1, account)
);
CREATE TABLE IF NOT EXISTS file_digest (
    sha1     TEXT PRIMARY KEY,
    size     INTEGER NOT NULL DEFAULT 0,
    ranges   TEXT    NOT NULL DEFAULT '',
    at       INTEGER NOT NULL DEFAULT 0,
    used_at  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS digest_block (
    sha1    TEXT    NOT NULL,
    off     INTEGER NOT NULL,
    data    BLOB    NOT NULL,
    at      INTEGER NOT NULL DEFAULT 0,
    used_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sha1, off)
);
CREATE TABLE IF NOT EXISTS upload_cred (
    id      TEXT PRIMARY KEY,
    userkey TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    at      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS strm_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id  TEXT    NOT NULL DEFAULT '',
    account    TEXT    NOT NULL DEFAULT '',
    kind       TEXT    NOT NULL DEFAULT '',
    level      TEXT    NOT NULL DEFAULT 'info',
    message    TEXT    NOT NULL DEFAULT '',
    detail     TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cloud189_accounts (
    id            TEXT PRIMARY KEY,
    name          TEXT    NOT NULL DEFAULT '',
    login_name    TEXT    NOT NULL DEFAULT '',
    access_token  TEXT    NOT NULL DEFAULT '',
    session_key   TEXT    NOT NULL DEFAULT '',
    expires_at    INTEGER NOT NULL DEFAULT 0,
    login_at      INTEGER NOT NULL DEFAULT 0,
    source        TEXT    NOT NULL DEFAULT '',
    is_default    INTEGER NOT NULL DEFAULT 0,
    auto_refresh  INTEGER NOT NULL DEFAULT 1,
    status        TEXT    NOT NULL DEFAULT 'ok',
    last_refresh  INTEGER NOT NULL DEFAULT 0,
    refresh_count INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT    NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cloud189_configs (
    id          TEXT PRIMARY KEY,
    account     TEXT    NOT NULL DEFAULT '',
    name        TEXT    NOT NULL DEFAULT '',
    remote_id   TEXT    NOT NULL DEFAULT '-11',
    remote_path TEXT    NOT NULL DEFAULT '',
    local_dir   TEXT    NOT NULL DEFAULT '',
    base_url    TEXT    NOT NULL DEFAULT '',
    media_exts  TEXT    NOT NULL DEFAULT '',
    all_files   INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'idle',
    last_run    INTEGER NOT NULL DEFAULT 0,
    files       INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS cloud189_files (
    config_id  TEXT    NOT NULL,
    file_id    TEXT    NOT NULL,
    account    TEXT    NOT NULL DEFAULT '',
    family_id  TEXT    NOT NULL DEFAULT '',
    name       TEXT    NOT NULL DEFAULT '',
    rel_path   TEXT    NOT NULL DEFAULT '',
    strm_path  TEXT    NOT NULL DEFAULT '',
    size       INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (config_id, file_id)
);
CREATE TABLE IF NOT EXISTS cloud189_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    config_id  TEXT    NOT NULL DEFAULT '',
    kind       TEXT    NOT NULL DEFAULT '',
    level      TEXT    NOT NULL DEFAULT 'info',
    message    TEXT    NOT NULL DEFAULT '',
    detail     TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_dirs_parent  ON dirs(parent_id);
CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent_id);
CREATE INDEX IF NOT EXISTS idx_files_path   ON files(path);
CREATE INDEX IF NOT EXISTS idx_strm_events_at ON strm_events(created_at);
CREATE INDEX IF NOT EXISTS idx_c189_files_cfg ON cloud189_files(config_id);
CREATE INDEX IF NOT EXISTS idx_c189_files_fid ON cloud189_files(file_id);
CREATE INDEX IF NOT EXISTS idx_c189_events_at ON cloud189_events(created_at);
"""

# 事件日志最多保留多少条（老记录按时间淘汰，避免库里越攒越大）
STRM_EVENT_KEEP = 2000

# 校验缓存里每一块的大小。它必须和秒传读源盘的块大小一致，否则每次查找
# 都会落空 —— 缓存按「块起点」索引，读的时候也按块起点对齐，两边对不上
# 就等于没缓存。
DIGEST_BLOCK_SIZE = 512 * 1024

_SPLIT_LIST = re.compile(r"[,，;；\s]+")


def _split_list(raw: Any) -> list[str]:
    return [x for x in _SPLIT_LIST.split(str(raw or "")) if x]


def strm_config_cids(row: dict[str, Any]) -> list[int]:
    """一条 115 STRM 配置覆盖的所有网盘目录 id。

    新字段 ``remote_cids``（逗号分隔，一条配置可挂多个网盘路径）优先；
    老配置只有 ``remote_cid``，照样能用。
    """
    out: list[int] = []
    for part in _split_list(row.get("remote_cids")):
        if part.isdigit():
            out.append(int(part))
    if not out:
        legacy = str(row.get("remote_cid") or "").strip()
        if legacy.isdigit():
            out.append(int(legacy))
    return list(dict.fromkeys(out))


def strm_config_paths(row: dict[str, Any]) -> list[str]:
    """与 :func:`strm_config_cids` 一一对应的网盘路径（只用于展示/命名）。"""
    paths = _split_list(row.get("remote_paths"))
    if not paths:
        legacy = str(row.get("remote_path") or "").strip()
        paths = [legacy] if legacy else []
    cids = strm_config_cids(row)
    # 路径数量对不上就按顺序补空，避免下标错位
    while len(paths) < len(cids):
        paths.append(paths[-1] if paths else "")
    return paths[: len(cids)] if cids else paths



def strm_config_pairs(row: dict[str, Any]) -> list[dict[str, str]]:
    """一条配置的「网盘路径 ↔ 本地 STRM 目录」绑定表。

    新结构存在 ``path_dirs``（JSON 数组，每项 ``{"cid","path","dir"}``），
    一个网盘路径严格绑定一个本地目录。老配置没有这个字段时按
    ``remote_cids`` + ``local_dir`` 拼出等价结构（多个路径共用同一个目录，
    由调用方再各自加子目录区分）。
    """
    raw = str(row.get("path_dirs") or "").strip()
    if raw:
        try:
            items = json.loads(raw)
        except (ValueError, TypeError):
            items = None
        if isinstance(items, list):
            out: list[dict[str, str]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("cid") or "").strip()
                if not cid.isdigit():
                    continue
                out.append({
                    "cid": cid,
                    "path": str(item.get("path") or "").strip(),
                    "dir": str(item.get("dir") or "").strip(),
                })
            if out:
                return out
    cids = strm_config_cids(row)
    paths = strm_config_paths(row)
    base = str(row.get("local_dir") or "").strip()
    return [
        {"cid": str(cid), "path": (paths[i] if i < len(paths) else ""), "dir": base}
        for i, cid in enumerate(cids)
    ]


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.RLock()
        self._open: set = set()  # 各线程的连接都登记在这，close() 时全部关闭
        self._init()

    # ------------------------------------------------------------------ 基础
    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            with self._lock:
                self._open.add(conn)
        return conn

    def _init(self) -> None:
        with self._lock:
            conn = self._conn()
            conn.executescript(SCHEMA)
            # 轻量列迁移：老库没有的新列直接 ALTER 补上
            cols = {r[1] for r in conn.execute("PRAGMA table_info(shares)")}
            for col, ddl in (
                ("my_share_code", "TEXT NOT NULL DEFAULT ''"),
                ("my_receive_code", "TEXT NOT NULL DEFAULT ''"),
                ("shared_ids", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in cols:
                    conn.execute(f"ALTER TABLE shares ADD COLUMN {col} {ddl}")
            # instant.source_account：文件原本在哪个账号（老库没有这列）
            inst_cols = {r[1] for r in conn.execute("PRAGMA table_info(instant)")}
            if "source_account" not in inst_cols:
                conn.execute(
                    "ALTER TABLE instant ADD COLUMN source_account TEXT NOT NULL DEFAULT ''")
            # roots.prefix：115 STRM 配置可以指定本地输出目录（相对 strm 根）
            root_cols = {r[1] for r in conn.execute("PRAGMA table_info(roots)")}
            if "prefix" not in root_cols:
                conn.execute("ALTER TABLE roots ADD COLUMN prefix TEXT NOT NULL DEFAULT ''")
            # roots.prefix_locked：前缀是「明确指定过的」，哪怕为空也要照用
            # （空 = 直接放 STRM_ROOT 下，别用目录名兜底）
            if "prefix_locked" not in root_cols:
                conn.execute(
                    "ALTER TABLE roots ADD COLUMN prefix_locked INTEGER NOT NULL DEFAULT 0")
            # strm_configs 多网盘路径：remote_cids / remote_paths 逗号分隔
            sc_cols = {r[1] for r in conn.execute("PRAGMA table_info(strm_configs)")}
            for col in ("remote_cids", "remote_paths", "path_dirs"):
                if col not in sc_cols:
                    conn.execute(
                        f"ALTER TABLE strm_configs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            # strm_configs 定时巡检：auto_scan=1 时每 scan_interval 秒跑一次增量
            for col, ddl in (
                ("auto_scan", "INTEGER NOT NULL DEFAULT 0"),
                ("scan_interval", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in sc_cols:
                    conn.execute(f"ALTER TABLE strm_configs ADD COLUMN {col} {ddl}")
            # shares.direct：分享「直接生成」模式（不转存、不建自己的分享）
            share_cols = {r[1] for r in conn.execute("PRAGMA table_info(shares)")}
            if "direct" not in share_cols:
                conn.execute(
                    "ALTER TABLE shares ADD COLUMN direct INTEGER NOT NULL DEFAULT 0")
            # cloud189_configs.account：一条配置归属哪个天翼云盘账号（多账号）
            c189_cfg_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(cloud189_configs)")}
            if "account" not in c189_cfg_cols:
                conn.execute(
                    "ALTER TABLE cloud189_configs ADD COLUMN account TEXT NOT NULL DEFAULT ''")
            # cloud189_files.account / family_id：播放时按 file_id 反查归属账号，
            # family_id 非空说明这个文件在家庭云里（取直链要带 familyId）
            c189_file_cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(cloud189_files)")}
            for col in ("account", "family_id"):
                if col not in c189_file_cols:
                    conn.execute(
                        f"ALTER TABLE cloud189_files ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            # v1.22.0：同播副本从 ed2k 专属改成账号级，旧表的数据搬过来
            # （key 加 legacy: 前缀，只为让 TTL 清理还能找到这些 115 文件）
            old = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ed2k_copies'"
            ).fetchone()
            if old:
                conn.execute(
                    "INSERT OR IGNORE INTO play_copies"
                    "(id,key,kind,seq,file_id,pickcode,name,size,dir_cid,created_at,last_used_at) "
                    "SELECT id,'legacy:'||key,'ed2k',seq,file_id,pickcode,name,0,dir_cid,"
                    "created_at,last_used_at FROM ed2k_copies"
                )
                conn.execute("DROP TABLE ed2k_copies")
            conn.commit()

    def close(self) -> None:
        """关闭所有线程的连接（多账号删除引擎时必须全关，否则文件被锁删不掉）。"""
        with self._lock:
            for conn in list(self._open):
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._open.clear()
        self._local = threading.local()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str = "") -> str:
        row = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )
            conn.commit()

    def get_meta_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get_meta(key, str(default)) or default)
        except ValueError:
            return default

    def table_has_rows(self, table: str) -> bool:
        """表里有没有数据（插件升级迁移时判断「这个功能以前用过没有」）。

        表名只允许字母数字下划线：这里拼进 SQL 了，不能接受任意字符串。
        """
        if not _TABLE_NAME.match(table):
            return False
        try:
            row = self._conn().execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return row is not None

    # ------------------------------------------------------------------ 监控根目录
    def list_roots(self) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM roots ORDER BY cid").fetchall()
        return [dict(r) for r in rows]

    def add_root(self, cid: int, name: str = "", prefix: str = "") -> bool:
        """登记一个监控根目录；已存在时返回 False。

        ``prefix`` 是该目录在本地 strm 里的输出前缀（相对 STRM_ROOT）；
        留空则用目录名。
        """
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                "INSERT OR IGNORE INTO roots(cid, name, added_at, prefix) VALUES(?, ?, ?, ?)",
                (int(cid), name, int(time.time()), prefix or ""),
            )
            conn.commit()
            return cur.rowcount > 0

    def set_root_prefix(self, cid: int, prefix: str) -> None:
        """改写某个根目录的本地输出前缀（相对 STRM_ROOT）。

        同时打上 ``prefix_locked=1``：前缀是「明确指定过的」，哪怕是空字符串
        也代表「直接放在 STRM_ROOT 下」，不能再用目录名兜底
        （115 STRM 配置里把「本地 STRM 目录」填成 /strm 就是这种情况）。
        """
        with self._lock:
            conn = self._conn()
            conn.execute(
                "UPDATE roots SET prefix=?, prefix_locked=1 WHERE cid=?",
                (prefix or "", int(cid)),
            )
            conn.commit()

    def remove_root(self, cid: int) -> bool:
        with self._lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM roots WHERE cid=?", (int(cid),))
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ 写
    def upsert_dir(self, node_id: int, parent_id: int, name: str, path: str) -> None:
        self.upsert_dirs([(node_id, parent_id, name, path)])

    def upsert_dirs(self, rows: Iterable[tuple[int, int, str, str]]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._lock:
            conn = self._conn()
            conn.executemany(
                "INSERT INTO dirs(id, parent_id, name, path) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id, "
                "name=excluded.name, path=excluded.path",
                rows,
            )
            conn.commit()

    def upsert_file(self, row: dict[str, Any]) -> None:
        self.upsert_files([row])

    def upsert_files(self, rows: Iterable[dict[str, Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        now = int(time.time())
        values = [
            (
                int(r["id"]),
                int(r.get("parent_id") or 0),
                r.get("name") or "",
                r.get("path") or "",
                r.get("pickcode") or "",
                int(r.get("size") or 0),
                r.get("sha1") or "",
                int(r.get("ctime") or 0),
                int(r.get("mtime") or 0),
                r.get("strm_path") or "",
                now,
            )
            for r in rows
        ]
        with self._lock:
            conn = self._conn()
            conn.executemany(
                "INSERT INTO files(id, parent_id, name, path, pickcode, size, sha1, "
                "ctime, mtime, strm_path, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET parent_id=excluded.parent_id, "
                "name=excluded.name, path=excluded.path, pickcode=excluded.pickcode, "
                "size=excluded.size, sha1=excluded.sha1, ctime=excluded.ctime, "
                "mtime=excluded.mtime, strm_path=excluded.strm_path, "
                "updated_at=excluded.updated_at",
                values,
            )
            conn.commit()

    def update_file_path(self, node_id: int, path: str, strm_path: str, name: str | None = None) -> None:
        with self._lock:
            conn = self._conn()
            if name is None:
                conn.execute(
                    "UPDATE files SET path=?, strm_path=?, updated_at=? WHERE id=?",
                    (path, strm_path, int(time.time()), node_id),
                )
            else:
                conn.execute(
                    "UPDATE files SET path=?, strm_path=?, name=?, updated_at=? WHERE id=?",
                    (path, strm_path, name, int(time.time()), node_id),
                )
            conn.commit()

    def rename_prefix(self, old_prefix: str, new_prefix: str) -> None:
        """目录改名/移动后，批量改写其下所有 path 前缀。"""
        with self._lock:
            conn = self._conn()
            conn.execute(
                "UPDATE dirs SET path = ? || substr(path, ?) WHERE path = ? OR path LIKE ?",
                (new_prefix, len(old_prefix) + 1, old_prefix, old_prefix + "/%"),
            )
            conn.execute(
                "UPDATE files SET path = ? || substr(path, ?) WHERE path = ? OR path LIKE ?",
                (new_prefix, len(old_prefix) + 1, old_prefix, old_prefix + "/%"),
            )
            conn.commit()

    # ------------------------------------------------------------------ 读
    def get_dir(self, node_id: int) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT * FROM dirs WHERE id=?", (int(node_id),)).fetchone()
        return dict(row) if row else None

    def get_file(self, node_id: int) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT * FROM files WHERE id=?", (int(node_id),)).fetchone()
        return dict(row) if row else None

    def file_by_pickcode(self, pickcode: str) -> dict[str, Any] | None:
        """按 pickcode 查文件（含 sha1 / size）。

        秒传要的是 **sha1 + size**（115 按这两个认文件），而播放地址里只有
        pickcode。反代拿到 strm 里的播放地址后，用它反查回索引里的这一行，
        才知道这个文件能不能秒传。

        同一个 pickcode 在本库里只会有一条（pickcode 是 115 给的临时凭证，
        同一个文件在同一个网盘里只有一个），所以取第一条即可。
        """
        pickcode = str(pickcode or "").strip()
        if not pickcode:
            return None
        row = self._conn().execute(
            "SELECT * FROM files WHERE pickcode=? AND sha1!='' LIMIT 1",
            (pickcode,),
        ).fetchone()
        return dict(row) if row else None

    def file_id_by_pickcode(self, pickcode: str) -> int:
        """pickcode → 115 文件 id（同播复制要用 file_id 去 fs_copy）。

        和 ``file_by_pickcode`` 的区别：这里不要求 sha1 —— ed2k 转存的文件
        没有 sha1，但一样可以复制。
        """
        pickcode = str(pickcode or "").strip()
        if not pickcode:
            return 0
        row = self._conn().execute(
            "SELECT id FROM files WHERE pickcode=? LIMIT 1", (pickcode,)
        ).fetchone()
        return int(row["id"]) if row else 0

    def get_node(self, node_id: int) -> tuple[str, dict[str, Any]] | None:
        d = self.get_dir(node_id)
        if d is not None:
            return "dir", d
        f = self.get_file(node_id)
        if f is not None:
            return "file", f
        return None

    def child_dirs(self, parent_id: int) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM dirs WHERE parent_id=?", (int(parent_id),)).fetchall()
        return [dict(r) for r in rows]

    def child_files(self, parent_id: int) -> list[dict[str, Any]]:
        rows = self._conn().execute("SELECT * FROM files WHERE parent_id=?", (int(parent_id),)).fetchall()
        return [dict(r) for r in rows]

    def all_strm_paths(self) -> set[str]:
        rows = self._conn().execute("SELECT strm_path FROM files WHERE strm_path != ''").fetchall()
        return {r["strm_path"] for r in rows}

    def files_with_strm(self) -> list[dict[str, Any]]:
        """联动检查用：所有「有 strm 的」文件索引行（只要必要字段）。

        一次把 4 列拉进内存，比逐行查库快得多；几万行也就几 MB。
        """
        rows = self._conn().execute(
            "SELECT id, parent_id, name, path, strm_path FROM files "
            "WHERE strm_path != '' ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def count_files_with_strm(self) -> int:
        """索引里「有 strm 的」行数（网页统计用，不拉数据）。"""
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM files WHERE strm_path != ''"
        ).fetchone()
        return int(row["n"] if row else 0)

    def file_id_by_strm_path(self, strm_path: str) -> int | None:
        """查这个 strm 路径当前属于哪个文件（用于发现同名不同后缀的冲突）。"""
        if not strm_path:
            return None
        row = self._conn().execute(
            "SELECT id FROM files WHERE strm_path = ? LIMIT 1", (strm_path,)
        ).fetchone()
        return int(row["id"]) if row else None

    def files_under(self, prefix: str) -> list[dict[str, Any]]:
        if not prefix:
            rows = self._conn().execute("SELECT * FROM files").fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM files WHERE path = ? OR path LIKE ?",
                (prefix, prefix + "/%"),
            ).fetchall()
        return [dict(r) for r in rows]

    def search_files(self, keyword: str, limit: int = 20) -> list[dict[str, Any]]:
        """按文件名/路径模糊搜索（通知指令 /find 用）。"""
        kw = str(keyword or "").strip()
        if not kw:
            return []
        like = "%" + kw.replace("%", r"\%").replace("_", r"\_") + "%"
        rows = self._conn().execute(
            "SELECT id, name, path, size, strm_path FROM files "
            "WHERE name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' "
            "ORDER BY mtime DESC LIMIT ?",
            (like, like, max(1, int(limit))),
        ).fetchall()
        return [dict(r) for r in rows]

    def update_strm_paths(self, rows: Iterable[tuple[str, int]]) -> None:
        rows = list(rows)
        if not rows:
            return
        with self._lock:
            conn = self._conn()
            conn.executemany("UPDATE files SET strm_path=? WHERE id=?", rows)
            conn.commit()

    # ------------------------------------------------------------------ 删
    def delete_dir_tree(self, node_id: int) -> list[dict[str, Any]]:
        """删除该目录及其所有子孙的索引，返回被删除的文件行（用于清理 strm）。"""
        dir_ids = self._collect_subdir_ids(int(node_id))
        if not dir_ids:
            return []
        marks = ",".join("?" * len(dir_ids))
        with self._lock:
            conn = self._conn()
            files = [
                dict(r)
                for r in conn.execute(f"SELECT * FROM files WHERE parent_id IN ({marks})", dir_ids).fetchall()
            ]
            conn.execute(f"DELETE FROM files WHERE parent_id IN ({marks})", dir_ids)
            conn.execute(f"DELETE FROM dirs WHERE id IN ({marks})", dir_ids)
            conn.commit()
        return files

    def _collect_subdir_ids(self, node_id: int) -> list[int]:
        found: list[int] = []
        queue = [int(node_id)]
        seen: set[int] = set()
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            found.append(current)
            queue.extend(r["id"] for r in self.child_dirs(current))
        return found

    def delete_file(self, node_id: int) -> dict[str, Any] | None:
        with self._lock:
            conn = self._conn()
            row = conn.execute("SELECT * FROM files WHERE id=?", (int(node_id),)).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM files WHERE id=?", (int(node_id),))
            conn.commit()
            return dict(row)

    def clear(self) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM files")
            conn.execute("DELETE FROM dirs")
            conn.execute("DELETE FROM dir_stamps")
            conn.commit()

    # ------------------------------------------------------- 目录指纹（增量）
    def get_dir_stamp(self, cid: int) -> str:
        row = self._conn().execute(
            "SELECT stamp FROM dir_stamps WHERE cid=?", (int(cid),)
        ).fetchone()
        return row["stamp"] if row else ""

    def set_dir_stamps(self, rows: Iterable[tuple[int, str]]) -> None:
        """批量记录目录指纹：目录没变时下一轮扫描可以整棵跳过。"""
        items = [(int(cid), str(stamp), int(time.time())) for cid, stamp in rows]
        if not items:
            return
        with self._lock:
            conn = self._conn()
            conn.executemany(
                "INSERT INTO dir_stamps(cid, stamp, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(cid) DO UPDATE SET stamp=excluded.stamp, "
                "updated_at=excluded.updated_at",
                items,
            )
            conn.commit()

    def set_dir_stamp(self, cid: int, stamp: str) -> None:
        self.set_dir_stamps([(cid, stamp)])

    def clear_dir_stamps(self) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM dir_stamps")
            conn.commit()

    # ------------------------------------------------------------------ ed2k
    ED2K_FIELDS = (
        "url", "name", "dir_cid", "file_id", "pickcode", "size", "status",
        "message", "strm_path", "created_at", "updated_at", "last_play_at",
    )

    def upsert_ed2k(self, row: dict[str, Any]) -> None:
        now = int(time.time())
        values = {
            "key": str(row["key"]).lower(),
            "url": row.get("url") or "",
            "name": row.get("name") or "",
            "dir_cid": int(row.get("dir_cid") or 0),
            "file_id": int(row.get("file_id") or 0),
            "pickcode": row.get("pickcode") or "",
            "size": int(row.get("size") or 0),
            "status": row.get("status") or "pending",
            "message": row.get("message") or "",
            "strm_path": row.get("strm_path") or "",
            "created_at": int(row.get("created_at") or now),
            "updated_at": now,
            "last_play_at": int(row.get("last_play_at") or 0),
        }
        cols = ", ".join(values)
        marks = ", ".join("?" * len(values))
        updates = ", ".join(
            f"{c}=excluded.{c}" for c in values if c not in ("key", "created_at")
        )
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"INSERT INTO ed2k({cols}) VALUES({marks}) "
                f"ON CONFLICT(key) DO UPDATE SET {updates}",
                tuple(values.values()),
            )
            conn.commit()

    def get_ed2k(self, key: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM ed2k WHERE key=?", (str(key).lower(),)
        ).fetchone()
        return dict(row) if row else None

    def list_ed2k(self, statuses: Iterable[str] = ()) -> list[dict[str, Any]]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            rows = self._conn().execute(
                f"SELECT * FROM ed2k WHERE status IN ({marks}) ORDER BY created_at DESC",
                tuple(statuses),
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM ed2k ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_ed2k(self, key: str, **fields: Any) -> None:
        data = {k: v for k, v in fields.items() if k in self.ED2K_FIELDS}
        if not data:
            return
        data["updated_at"] = int(time.time())
        sets = ", ".join(f"{k}=?" for k in data)
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"UPDATE ed2k SET {sets} WHERE key=?",
                (*data.values(), str(key).lower()),
            )
            conn.commit()

    def delete_ed2k(self, key: str) -> dict[str, Any] | None:
        row = self.get_ed2k(key)
        if row is None:
            return None
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM ed2k WHERE key=?", (str(key).lower(),))
            conn.commit()
        return row

    def ed2k_count(self) -> int:
        return self._conn().execute("SELECT count(*) AS c FROM ed2k").fetchone()["c"]

    def ed2k_by_strm_path(self, path: str) -> dict[str, Any] | None:
        if not path:
            return None
        row = self._conn().execute(
            "SELECT * FROM ed2k WHERE strm_path = ? LIMIT 1", (path,)
        ).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------- 同播副本
    COPY_FIELDS = (
        "key", "kind", "seq", "file_id", "pickcode", "name", "size", "dir_cid",
        "created_at", "last_used_at",
    )

    def upsert_play_copy(self, row: dict[str, Any]) -> None:
        now = int(time.time())
        values = {
            "id": str(row["id"]),
            "key": str(row.get("key") or ""),
            "kind": str(row.get("kind") or ""),
            "seq": max(0, int(row.get("seq") or 0)),
            "file_id": int(row.get("file_id") or 0),
            "pickcode": row.get("pickcode") or "",
            "name": row.get("name") or "",
            "size": int(row.get("size") or 0),
            "dir_cid": int(row.get("dir_cid") or 0),
            "created_at": int(row.get("created_at") or now),
            "last_used_at": int(row.get("last_used_at") or now),
        }
        cols = ", ".join(values)
        marks = ", ".join("?" * len(values))
        updates = ", ".join(
            f"{c}=excluded.{c}" for c in values if c not in ("id", "created_at")
        )
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"INSERT INTO play_copies({cols}) VALUES({marks}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}",
                tuple(values.values()),
            )
            conn.commit()

    def list_play_copies(self, key: str = "") -> list[dict[str, Any]]:
        if key:
            rows = self._conn().execute(
                "SELECT * FROM play_copies WHERE key=? ORDER BY seq", (str(key),)
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM play_copies ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def update_play_copy(self, copy_id: str, **fields: Any) -> None:
        data = {k: v for k, v in fields.items() if k in self.COPY_FIELDS}
        if not data or not copy_id:
            return
        sets = ", ".join(f"{k}=?" for k in data)
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"UPDATE play_copies SET {sets} WHERE id=?", (*data.values(), copy_id)
            )
            conn.commit()

    def delete_play_copy(self, copy_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM play_copies WHERE id=?", (copy_id,)
        ).fetchone()
        if row is None:
            return None
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM play_copies WHERE id=?", (copy_id,))
            conn.commit()
        return dict(row)

    def delete_play_copies_for(self, key: str) -> list[dict[str, Any]]:
        rows = self.list_play_copies(key)
        if not rows:
            return []
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM play_copies WHERE key=?", (str(key),))
            conn.commit()
        return rows

    # ------------------------------------------------------------------ 统计
    def stats(self) -> dict[str, int]:
        conn = self._conn()
        return {
            "dirs": conn.execute("SELECT count(*) AS c FROM dirs").fetchone()["c"],
            "files": conn.execute("SELECT count(*) AS c FROM files").fetchone()["c"],
            "strm": conn.execute("SELECT count(*) AS c FROM files WHERE strm_path != ''").fetchone()["c"],
        }

    # ------------------------------------------------------------------ 分享
    SHARE_FIELDS = (
        "receive_code", "url", "name", "dir_cid", "direct", "status", "message",
        "total", "done", "my_share_code", "my_receive_code", "shared_ids",
        "created_at", "updated_at",
    )

    def upsert_share(self, row: dict[str, Any]) -> None:
        now = int(time.time())
        values = {
            "share_code": str(row["share_code"]),
            "receive_code": row.get("receive_code") or "",
            "url": row.get("url") or "",
            "name": row.get("name") or "",
            "dir_cid": int(row.get("dir_cid") or 0),
            "direct": 1 if row.get("direct") else 0,
            "status": row.get("status") or "pending",
            "message": row.get("message") or "",
            "total": int(row.get("total") or 0),
            "done": int(row.get("done") or 0),
            "created_at": int(row.get("created_at") or now),
            "updated_at": now,
        }
        cols = ", ".join(values)
        marks = ", ".join("?" * len(values))
        updates = ", ".join(
            f"{c}=excluded.{c}" for c in values if c not in ("share_code", "created_at")
        )
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"INSERT INTO shares({cols}) VALUES({marks}) "
                f"ON CONFLICT(share_code) DO UPDATE SET {updates}",
                tuple(values.values()),
            )
            conn.commit()

    def get_share(self, share_code: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM shares WHERE share_code=?", (str(share_code),)
        ).fetchone()
        return dict(row) if row else None

    def get_share_by_my(self, my_share_code: str) -> dict[str, Any] | None:
        """按「自己生成的分享码」查记录（播放路由用）。"""
        row = self._conn().execute(
            "SELECT * FROM shares WHERE my_share_code=?", (str(my_share_code),)
        ).fetchone()
        return dict(row) if row else None

    def list_shares(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM shares ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_share(self, share_code: str, **fields: Any) -> None:
        data = {k: v for k, v in fields.items() if k in self.SHARE_FIELDS}
        if not data:
            return
        data["updated_at"] = int(time.time())
        sets = ", ".join(f"{k}=?" for k in data)
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"UPDATE shares SET {sets} WHERE share_code=?",
                (*data.values(), str(share_code)),
            )
            conn.commit()

    def delete_share(self, share_code: str) -> dict[str, Any] | None:
        row = self.get_share(share_code)
        if row is None:
            return None
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM shares WHERE share_code=?", (str(share_code),))
            conn.execute("DELETE FROM share_files WHERE share_code=?", (str(share_code),))
            conn.commit()
        return row

    def upsert_share_file(self, share_code: str, row: dict[str, Any]) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO share_files(share_code, rel_path, file_id, pickcode,"
                " name, size, sha1, strm_path) VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(share_code, rel_path) DO UPDATE SET"
                " file_id=excluded.file_id, pickcode=excluded.pickcode,"
                " name=excluded.name, size=excluded.size, sha1=excluded.sha1,"
                " strm_path=excluded.strm_path",
                (
                    str(share_code), row.get("rel_path") or "",
                    int(row.get("file_id") or 0), row.get("pickcode") or "",
                    row.get("name") or "", int(row.get("size") or 0),
                    row.get("sha1") or "", row.get("strm_path") or "",
                ),
            )
            conn.commit()

    def list_share_files(self, share_code: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM share_files WHERE share_code=? ORDER BY rel_path",
            (str(share_code),),
        ).fetchall()
        return [dict(r) for r in rows]

    def share_by_strm_path(self, path: str) -> dict[str, Any] | None:
        if not path:
            return None
        row = self._conn().execute(
            "SELECT f.*, s.name AS share_name FROM share_files f"
            " JOIN shares s ON s.share_code = f.share_code"
            " WHERE f.strm_path = ? LIMIT 1",
            (path,),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------ 账号
    def upsert_account(self, name: str, cookies: str, uid: str = "") -> None:
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO accounts(name, cookies, uid, created_at) VALUES(?,?,?,?)"
                " ON CONFLICT(name) DO UPDATE SET cookies=excluded.cookies,"
                " uid=excluded.uid",
                (name, cookies, uid, int(time.time())),
            )
            conn.commit()

    def list_accounts(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT name, uid, created_at FROM accounts ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_account_cookies(self, name: str) -> str:
        row = self._conn().execute(
            "SELECT cookies FROM accounts WHERE name=?", (name,)
        ).fetchone()
        return row["cookies"] if row else ""

    # ------------------------------------------------------------------ 账号显示名
    # 账号名（name）是内部标识：会变成 strm 子目录和 URL 前缀，不能随便改。
    # 网页上「115 助手 → 设置」改的是显示名，存在 meta 里，不影响内部标识。
    def get_account_display(self, name: str) -> str:
        raw = self.get_meta("account_display", "{}")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return ""
        return str(data.get(name, "") or "")

    def set_account_display(self, name: str, display: str) -> None:
        raw = self.get_meta("account_display", "{}")
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        display = (display or "").strip()
        if display:
            data[name] = display
        else:
            data.pop(name, None)
        self.set_meta("account_display", json.dumps(data, ensure_ascii=False))

    # ------------------------------------------------------------------ 媒体服务器
    def list_media_servers(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM media_servers ORDER BY created_at, rowid"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_media_server(self, sid: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM media_servers WHERE id=?", (str(sid),)
        ).fetchone()
        return dict(row) if row else None

    def upsert_media_server(self, row: dict[str, Any]) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO media_servers(id, name, host, api_key, enabled, note,"
                " created_at) VALUES(?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, host=excluded.host,"
                " api_key=excluded.api_key, enabled=excluded.enabled, note=excluded.note",
                (
                    str(row["id"]),
                    row.get("name") or "",
                    row.get("host") or "",
                    row.get("api_key") or "",
                    1 if row.get("enabled", True) else 0,
                    row.get("note") or "",
                    int(row.get("created_at") or time.time()),
                ),
            )
            conn.commit()

    def delete_media_server(self, sid: str) -> bool:
        with self._lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM media_servers WHERE id=?", (str(sid),))
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------------ 115 STRM 配置
    STRM_CONFIG_FIELDS = (
        "account", "name", "remote_cid", "remote_cids", "remote_path",
        "remote_paths", "local_dir", "path_dirs", "base_url",
        "media_exts", "meta_exts", "download_meta", "event_watch", "api_pull",
        "redirect", "auto_scan", "scan_interval",
        "status", "last_run", "files", "updated_at",
    )

    def list_strm_configs(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM strm_configs ORDER BY created_at, id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_strm_config(self, cid: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM strm_configs WHERE id=?", (str(cid),)
        ).fetchone()
        return dict(row) if row else None

    def upsert_strm_config(self, row: dict[str, Any]) -> None:
        now = int(time.time())
        values = {
            "account": row.get("account") or "",
            "name": row.get("name") or "",
            "remote_cid": str(row.get("remote_cid") or ""),
            "remote_cids": str(row.get("remote_cids") or ""),
            "remote_path": row.get("remote_path") or "",
            "remote_paths": row.get("remote_paths") or "",
            "local_dir": row.get("local_dir") or "",
            # path_dirs 是「每个网盘路径绑定哪个本地目录」的 JSON，漏了它
            # 多路径配置就会全部落回 local_dir（第一个目录）
            "path_dirs": row.get("path_dirs") or "",
            "base_url": row.get("base_url") or "",
            "media_exts": row.get("media_exts") or "",
            "meta_exts": row.get("meta_exts") or "",
            "download_meta": 1 if row.get("download_meta", True) else 0,
            "event_watch": 1 if row.get("event_watch", True) else 0,
            "api_pull": 1 if row.get("api_pull", True) else 0,
            "redirect": 1 if row.get("redirect", True) else 0,
            "auto_scan": 1 if row.get("auto_scan") else 0,
            "scan_interval": max(0, int(row.get("scan_interval") or 0)),
            "status": row.get("status") or "idle",
            "last_run": int(row.get("last_run") or 0),
            "files": int(row.get("files") or 0),
            "updated_at": now,
        }
        cols = ", ".join(values)
        marks = ", ".join("?" * len(values))
        updates = ", ".join(
            f"{c}=excluded.{c}" for c in values if c != "updated_at"
        )
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"INSERT INTO strm_configs(id, {cols}, created_at) VALUES(?, {marks}, ?)"
                f" ON CONFLICT(id) DO UPDATE SET {updates}",
                (str(row["id"]), *values.values(), int(row.get("created_at") or now)),
            )
            conn.commit()

    def update_strm_config(self, cid: str, **fields: Any) -> None:
        data = {k: v for k, v in fields.items() if k in self.STRM_CONFIG_FIELDS}
        if not data:
            return
        data["updated_at"] = int(time.time())
        sets = ", ".join(f"{k}=?" for k in data)
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"UPDATE strm_configs SET {sets} WHERE id=?",
                (*data.values(), str(cid)),
            )
            conn.commit()

    def delete_strm_config(self, cid: str) -> bool:
        with self._lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM strm_configs WHERE id=?", (str(cid),))
            conn.commit()
            return cur.rowcount > 0

    # ------------------------------------------------------------ 事件日志
    def add_strm_event(self, kind: str, message: str, *, config_id: str = "",
                       account: str = "", level: str = "info",
                       detail: str = "") -> None:
        """记一条事件（生成 / 覆盖 / 补写 / 失败 / 配置变更）。

        这是给「115 STRM」页面的**事件日志**用的：以前只有内存里的前端日志，
        刷新就没了，出问题时根本查不到「上一次全量到底做了什么」。
        """
        now = int(time.time())
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO strm_events(config_id, account, kind, level, message,"
                " detail, created_at) VALUES(?,?,?,?,?,?,?)",
                (str(config_id or ""), str(account or ""), str(kind or "info"),
                 str(level or "info"), str(message or ""), str(detail or "")[:4000], now),
            )
            # 只留最近 N 条：日志是排障用的，不需要永久保留
            conn.execute(
                "DELETE FROM strm_events WHERE id NOT IN ("
                " SELECT id FROM strm_events ORDER BY id DESC LIMIT ?)",
                (STRM_EVENT_KEEP,),
            )
            conn.commit()

    def list_strm_events(self, limit: int = 100, config_id: str = "",
                         kind: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 100), 500))
        sql = "SELECT * FROM strm_events"
        where: list[str] = []
        args: list[Any] = []
        if config_id:
            where.append("config_id=?")
            args.append(str(config_id))
        if kind:
            where.append("kind=?")
            args.append(str(kind))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn().execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def clear_strm_events(self, config_id: str = "") -> int:
        with self._lock:
            conn = self._conn()
            if config_id:
                cur = conn.execute(
                    "DELETE FROM strm_events WHERE config_id=?", (str(config_id),))
            else:
                cur = conn.execute("DELETE FROM strm_events")
            conn.commit()
            return cur.rowcount

    # --------------------------------------------------------- 天翼云盘（189）
    CLOUD189_CONFIG_FIELDS = (
        "account", "name", "remote_id", "remote_path", "local_dir", "base_url",
        "media_exts", "all_files", "status", "last_run", "files", "updated_at",
    )

    # -------------------------------------------------- 天翼云盘账号（多账号）
    CLOUD189_ACCOUNT_FIELDS = (
        "name", "login_name", "access_token", "session_key", "expires_at",
        "login_at", "source", "is_default", "auto_refresh", "status",
        "last_refresh", "refresh_count", "last_error", "updated_at",
    )

    def list_cloud189_accounts(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM cloud189_accounts ORDER BY is_default DESC, created_at, id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_cloud189_account(self, aid: str) -> dict[str, Any] | None:
        if not aid:
            return None
        row = self._conn().execute(
            "SELECT * FROM cloud189_accounts WHERE id=?", (str(aid),)).fetchone()
        return dict(row) if row else None

    def default_cloud189_account(self) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM cloud189_accounts ORDER BY is_default DESC, created_at, id"
            " LIMIT 1").fetchone()
        return dict(row) if row else None

    def upsert_cloud189_account(self, row: dict[str, Any]) -> None:
        now = int(time.time())
        values = {
            "name": row.get("name") or "",
            "login_name": row.get("login_name") or "",
            "access_token": row.get("access_token") or "",
            "session_key": row.get("session_key") or "",
            "expires_at": int(row.get("expires_at") or 0),
            "login_at": int(row.get("login_at") or 0),
            "source": row.get("source") or "",
            "is_default": 1 if row.get("is_default") else 0,
            "auto_refresh": 1 if row.get("auto_refresh", True) else 0,
            "status": row.get("status") or "ok",
            "last_refresh": int(row.get("last_refresh") or 0),
            "refresh_count": int(row.get("refresh_count") or 0),
            "last_error": row.get("last_error") or "",
            "updated_at": now,
        }
        cols = ", ".join(values)
        marks = ", ".join("?" * len(values))
        updates = ", ".join(f"{c}=excluded.{c}" for c in values if c != "updated_at")
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"INSERT INTO cloud189_accounts(id, {cols}, created_at)"
                f" VALUES(?, {marks}, ?)"
                f" ON CONFLICT(id) DO UPDATE SET {updates}",
                (str(row["id"]), *values.values(), int(row.get("created_at") or now)),
            )
            conn.commit()

    def update_cloud189_account(self, aid: str, **fields: Any) -> None:
        data = {k: v for k, v in fields.items()
                if k in self.CLOUD189_ACCOUNT_FIELDS}
        if not data:
            return
        data["updated_at"] = int(time.time())
        sets = ", ".join(f"{k}=?" for k in data)
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"UPDATE cloud189_accounts SET {sets} WHERE id=?",
                (*data.values(), str(aid)),
            )
            conn.commit()

    def set_default_cloud189_account(self, aid: str) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("UPDATE cloud189_accounts SET is_default=0")
            conn.execute("UPDATE cloud189_accounts SET is_default=1 WHERE id=?",
                         (str(aid),))
            conn.commit()

    def delete_cloud189_account(self, aid: str, *, drop_configs: bool = False) -> int:
        """删账号。``drop_configs`` 为真时把它名下的配置一起删（strm 保留）。"""
        with self._lock:
            conn = self._conn()
            removed = 0
            if drop_configs:
                ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM cloud189_configs WHERE account=?",
                    (str(aid),)).fetchall()]
                for cid in ids:
                    conn.execute("DELETE FROM cloud189_files WHERE config_id=?", (cid,))
                cur = conn.execute("DELETE FROM cloud189_configs WHERE account=?",
                                   (str(aid),))
                removed = cur.rowcount
            conn.execute("DELETE FROM cloud189_accounts WHERE id=?", (str(aid),))
            conn.commit()
        return removed

    def count_cloud189_configs(self, account: str = "") -> int:
        if account:
            row = self._conn().execute(
                "SELECT COUNT(*) AS n FROM cloud189_configs WHERE account=?",
                (str(account),)).fetchone()
        else:
            row = self._conn().execute(
                "SELECT COUNT(*) AS n FROM cloud189_configs").fetchone()
        return int(row["n"] if row else 0)

    def find_cloud189_file(self, file_id: str) -> dict[str, Any] | None:
        """按 file_id 反查「这个文件属于哪个账号 / 是不是家庭云」。

        老 strm（``/p189/play/<fileId>``）里没有账号信息，播放时靠索引补出来。
        """
        row = self._conn().execute(
            "SELECT * FROM cloud189_files WHERE file_id=? ORDER BY updated_at DESC"
            " LIMIT 1", (str(file_id),)).fetchone()
        return dict(row) if row else None

    def list_cloud189_configs(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM cloud189_configs ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]

    def get_cloud189_config(self, cid: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM cloud189_configs WHERE id=?", (str(cid),)).fetchone()
        return dict(row) if row else None

    def upsert_cloud189_config(self, row: dict[str, Any]) -> None:
        now = int(time.time())
        values = {
            "account": row.get("account") or "",
            "name": row.get("name") or "",
            "remote_id": str(row.get("remote_id") or "-11"),
            "remote_path": row.get("remote_path") or "",
            "local_dir": row.get("local_dir") or "",
            "base_url": row.get("base_url") or "",
            "media_exts": row.get("media_exts") or "",
            "all_files": 1 if row.get("all_files") else 0,
            "status": row.get("status") or "idle",
            "last_run": int(row.get("last_run") or 0),
            "files": int(row.get("files") or 0),
            "updated_at": now,
        }
        cols = ", ".join(values)
        marks = ", ".join("?" * len(values))
        updates = ", ".join(f"{c}=excluded.{c}" for c in values if c != "updated_at")
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"INSERT INTO cloud189_configs(id, {cols}, created_at)"
                f" VALUES(?, {marks}, ?)"
                f" ON CONFLICT(id) DO UPDATE SET {updates}",
                (str(row["id"]), *values.values(), int(row.get("created_at") or now)),
            )
            conn.commit()

    def update_cloud189_config(self, cid: str, **fields: Any) -> None:
        data = {k: v for k, v in fields.items() if k in self.CLOUD189_CONFIG_FIELDS}
        if not data:
            return
        data["updated_at"] = int(time.time())
        sets = ", ".join(f"{k}=?" for k in data)
        with self._lock:
            conn = self._conn()
            conn.execute(
                f"UPDATE cloud189_configs SET {sets} WHERE id=?",
                (*data.values(), str(cid)),
            )
            conn.commit()

    def delete_cloud189_config(self, cid: str) -> bool:
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                "DELETE FROM cloud189_configs WHERE id=?", (str(cid),))
            conn.execute(
                "DELETE FROM cloud189_files WHERE config_id=?", (str(cid),))
            conn.commit()
            return cur.rowcount > 0

    # -------------------------------------------------- 天翼云盘文件索引
    def upsert_cloud189_file(self, config_id: str, file_id: str, *,
                             name: str = "", rel_path: str = "",
                             strm_path: str = "", size: int = 0,
                             account: str = "", family_id: str = "") -> None:
        """记一条「这条配置生成过哪个 strm」。

        索引的作用是「全新生成」时能**精确**删掉自己生成过的文件 ——
        多条配置可能写在同一个目录里，按目录整片删会带走别人的 strm。
        ``account`` / ``family_id`` 供播放时反查（老 strm 里没有账号信息）。
        """
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO cloud189_files(config_id, file_id, account, family_id,"
                " name, rel_path, strm_path, size, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(config_id, file_id) DO UPDATE SET"
                " account=excluded.account, family_id=excluded.family_id,"
                " name=excluded.name, rel_path=excluded.rel_path,"
                " strm_path=excluded.strm_path, size=excluded.size,"
                " updated_at=excluded.updated_at",
                (str(config_id), str(file_id), str(account or ""),
                 str(family_id or ""), str(name or ""), str(rel_path or ""),
                 str(strm_path or ""), int(size or 0), int(time.time())),
            )
            conn.commit()

    def list_cloud189_files(self, config_id: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM cloud189_files"
        args: tuple[Any, ...] = ()
        if config_id:
            sql += " WHERE config_id=?"
            args = (str(config_id),)
        return [dict(r) for r in self._conn().execute(sql, args).fetchall()]

    def delete_cloud189_file(self, config_id: str, file_id: str) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute(
                "DELETE FROM cloud189_files WHERE config_id=? AND file_id=?",
                (str(config_id), str(file_id)))
            conn.commit()

    def clear_cloud189_files(self, config_id: str) -> int:
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                "DELETE FROM cloud189_files WHERE config_id=?", (str(config_id),))
            conn.commit()
            return cur.rowcount

    def cloud189_file_count(self) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS n FROM cloud189_files").fetchone()
        return int(row["n"] if row else 0)

    # ------------------------------------------------------ 天翼云盘事件
    def add_cloud189_event(self, kind: str, message: str, *, config_id: str = "",
                           level: str = "info", detail: str = "") -> None:
        now = int(time.time())
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO cloud189_events(config_id, kind, level, message,"
                " detail, created_at) VALUES(?,?,?,?,?,?)",
                (str(config_id or ""), str(kind or "info"), str(level or "info"),
                 str(message or ""), str(detail or "")[:4000], now),
            )
            conn.execute(
                "DELETE FROM cloud189_events WHERE id NOT IN ("
                " SELECT id FROM cloud189_events ORDER BY id DESC LIMIT ?)",
                (STRM_EVENT_KEEP,),
            )
            conn.commit()

    def list_cloud189_events(self, limit: int = 100,
                             config_id: str = "") -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 100), 500))
        sql = "SELECT * FROM cloud189_events"
        args: list[Any] = []
        if config_id:
            sql += " WHERE config_id=?"
            args.append(str(config_id))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = self._conn().execute(sql, tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def clear_cloud189_events(self, config_id: str = "") -> int:
        with self._lock:
            conn = self._conn()
            if config_id:
                cur = conn.execute(
                    "DELETE FROM cloud189_events WHERE config_id=?",
                    (str(config_id),))
            else:
                cur = conn.execute("DELETE FROM cloud189_events")
            conn.commit()
            return cur.rowcount

    def delete_account(self, name: str) -> bool:
        with self._lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM accounts WHERE name=?", (name,))
            conn.execute("DELETE FROM emby_users WHERE account=?", (name,))
            conn.commit()
        return cur.rowcount > 0

    def set_emby_user(self, emby_user_id: str, emby_user_name: str, account: str) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO emby_users(emby_user_id, emby_user_name, account, updated_at)"
                " VALUES(?,?,?,?) ON CONFLICT(emby_user_id) DO UPDATE SET"
                " emby_user_name=excluded.emby_user_name, account=excluded.account,"
                " updated_at=excluded.updated_at",
                (str(emby_user_id), emby_user_name or "", account, int(time.time())),
            )
            conn.commit()

    def delete_emby_user(self, emby_user_id: str) -> bool:
        """彻底删掉一条用户映射。

        之前「移除映射」只是把 account 置空（变成「未映射」），记录还在 ——
        用户反馈「移除后添加的用户没有删掉」。这里是真的删。
        """
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                "DELETE FROM emby_users WHERE emby_user_id=?", (str(emby_user_id),)
            )
            conn.commit()
            return cur.rowcount > 0

    def list_emby_users(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM emby_users ORDER BY emby_user_id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_emby_user(self, emby_user_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM emby_users WHERE emby_user_id=?", (str(emby_user_id),)
        ).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------- Emby 登录令牌
    def remember_emby_token(self, token: str, emby_user_id: str) -> None:
        """记住「Emby 登录令牌 → Emby 用户 ID」。

        播放请求不一定带 UserId（Emby 客户端只在部分接口带），但一定带
        ``X-Emby-Token``。没有这张表就认不出用户是谁，秒传账号映射也就用不上。
        参考项目 junction 用的是同一套办法。
        """
        token = str(token or "").strip()
        emby_user_id = str(emby_user_id or "").strip()
        if not token or not emby_user_id:
            return
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO emby_tokens(token, emby_user_id, updated_at)"
                " VALUES(?,?,?) ON CONFLICT(token) DO UPDATE SET"
                " emby_user_id=excluded.emby_user_id, updated_at=excluded.updated_at",
                (token, emby_user_id, int(time.time())),
            )
            conn.commit()

    def emby_user_by_token(self, token: str) -> str:
        """用登录令牌反查 Emby 用户 ID；查不到返回空串。"""
        token = str(token or "").strip()
        if not token:
            return ""
        row = self._conn().execute(
            "SELECT emby_user_id FROM emby_tokens WHERE token=?", (token,)
        ).fetchone()
        return str(row["emby_user_id"]) if row else ""

    def list_emby_tokens(self) -> dict[str, str]:
        rows = self._conn().execute(
            "SELECT token, emby_user_id FROM emby_tokens"
        ).fetchall()
        return {str(r["token"]): str(r["emby_user_id"]) for r in rows}

    # ------------------------------------------------------------------ 秒传
    def upsert_instant(self, row: dict[str, Any]) -> None:
        now = int(time.time())
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO instant(sha1, account, name, size, source_pickcode,"
                " source_account, file_id, pickcode, created_at, last_play_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(sha1, account) DO UPDATE SET"
                " name=excluded.name, size=excluded.size,"
                " source_pickcode=excluded.source_pickcode,"
                " source_account=excluded.source_account,"
                " file_id=excluded.file_id, pickcode=excluded.pickcode,"
                " last_play_at=excluded.last_play_at",
                (
                    str(row["sha1"]).upper(), str(row["account"]),
                    row.get("name") or "", int(row.get("size") or 0),
                    row.get("source_pickcode") or "",
                    row.get("source_account") or "",
                    int(row.get("file_id") or 0), row.get("pickcode") or "",
                    int(row.get("created_at") or now),
                    int(row.get("last_play_at") or 0),
                ),
            )
            conn.commit()

    def get_instant(self, sha1: str, account: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM instant WHERE sha1=? AND account=?",
            (str(sha1).upper(), str(account)),
        ).fetchone()
        return dict(row) if row else None

    def list_instant(self, account: str = "") -> list[dict[str, Any]]:
        if account:
            rows = self._conn().execute(
                "SELECT * FROM instant WHERE account=? ORDER BY last_play_at DESC",
                (str(account),),
            ).fetchall()
        else:
            rows = self._conn().execute(
                "SELECT * FROM instant ORDER BY last_play_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def instant_count(self) -> int:
        return self._conn().execute("SELECT count(*) AS c FROM instant").fetchone()["c"]

    def delete_instant(self, sha1: str, account: str) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute(
                "DELETE FROM instant WHERE sha1=? AND account=?",
                (str(sha1).upper(), str(account)),
            )
            conn.commit()

    # ------------------------------------------------------- 秒传校验值缓存
    #
    # 115 的二次校验是「它挑一段字节，客户端把这一段的 SHA1 交回去」。
    # 这个答案只跟**文件内容**有关 —— 跟哪个账号、第几次播都无关 ——
    # 所以一份内容只要被校验过一次，之后谁播都不用再去源盘取一个字节。
    # 这一层就是 nextEmby 说的「用户间秒传加速」。

    def digest_note_range(self, sha1: str, spec: str) -> bool:
        """记下 115 这次要校验哪一段，并报告这一段以前有没有出现过。

        ``spec`` 形如 ``"0-1048575"``。返回值只用于日志：115 挑的区间是会
        重复的，重复就意味着这一段的字节下次可以直接从本地拿。
        """
        sha1 = str(sha1 or "").strip().upper()
        spec = str(spec or "").strip()
        if not sha1 or not spec:
            return False
        now = int(time.time())
        row = self._conn().execute(
            "SELECT ranges FROM file_digest WHERE sha1=?", (sha1,)
        ).fetchone()
        current = str(row["ranges"]) if row else ""
        kept = [spec]
        seen = False
        for part in current.split(","):
            part = part.strip()
            if not part:
                continue
            if part == spec:
                seen = True
                continue
            if len(kept) >= 8:      # 只留最近 8 段，纯诊断用
                break
            kept.append(part)
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO file_digest(sha1,size,ranges,at,used_at)"
                " VALUES(?,0,?,?,?)"
                " ON CONFLICT(sha1) DO UPDATE SET ranges=excluded.ranges,"
                " used_at=excluded.used_at",
                (sha1, ",".join(kept), now, now),
            )
            conn.commit()
        return seen

    def digest_block_get(self, sha1: str, off: int) -> bytes | None:
        """取回记住的一段文件内容。``off`` 总是 DIGEST_BLOCK_SIZE 的倍数。"""
        sha1 = str(sha1 or "").strip().upper()
        if not sha1 or off < 0:
            return None
        row = self._conn().execute(
            "SELECT data FROM digest_block WHERE sha1=? AND off=?", (sha1, int(off))
        ).fetchone()
        if not row or not row["data"]:
            return None
        try:
            self._conn().execute(
                "UPDATE digest_block SET used_at=? WHERE sha1=? AND off=?",
                (int(time.time()), sha1, int(off)),
            )
        except sqlite3.Error:
            pass        # 记账失败不影响播放
        return bytes(row["data"])

    def digest_block_put(self, sha1: str, off: int, data: bytes) -> None:
        """记住一段文件内容。

        写失败只是让下次多读一次源盘，永远不值得为它失败一次播放。
        """
        sha1 = str(sha1 or "").strip().upper()
        if not sha1 or off < 0 or not data:
            return
        now = int(time.time())
        try:
            with self._lock:
                conn = self._conn()
                conn.execute(
                    "INSERT INTO digest_block(sha1,off,data,at,used_at)"
                    " VALUES(?,?,?,?,?)"
                    " ON CONFLICT(sha1,off) DO UPDATE SET data=excluded.data,"
                    " used_at=excluded.used_at",
                    (sha1, int(off), sqlite3.Binary(bytes(data)), now, now),
                )
                conn.commit()
        except sqlite3.Error:
            pass

    def digest_stats(self) -> dict[str, int]:
        """缓存占用情况，给网页上的「秒传」页看。"""
        conn = self._conn()
        files = conn.execute("SELECT count(*) AS c FROM file_digest").fetchone()["c"]
        blocks = conn.execute("SELECT count(*) AS c FROM digest_block").fetchone()["c"]
        total = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(data)),0) AS c FROM digest_block"
        ).fetchone()["c"]
        return {"files": int(files), "blocks": int(blocks), "bytes": int(total)}

    def digest_sweep(self, max_bytes: int) -> tuple[int, int]:
        """把缓存压在字节预算内，先丢最久没用的。

        这些行只是用来省一次重读，丢掉一块最多让以后多发一次区间请求，
        永远不会把一次播放搞坏，所以清理是安全的。
        """
        max_bytes = int(max_bytes or 0)
        if max_bytes <= 0:
            return 0, 0
        conn = self._conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(data)),0) AS c FROM digest_block"
        ).fetchone()
        total = int(row["c"]) if row else 0
        dropped = freed = 0
        while total > max_bytes and dropped < 20000:
            batch = 200
            over = total - max_bytes
            if over < batch * DIGEST_BLOCK_SIZE:
                batch = over // DIGEST_BLOCK_SIZE + 1
            got_row = conn.execute(
                "SELECT COALESCE(SUM(LENGTH(data)),0) AS c FROM ("
                " SELECT data FROM digest_block ORDER BY used_at ASC LIMIT ?)",
                (batch,),
            ).fetchone()
            got = int(got_row["c"]) if got_row else 0
            with self._lock:
                cur = conn.execute(
                    "DELETE FROM digest_block WHERE rowid IN ("
                    " SELECT rowid FROM digest_block ORDER BY used_at ASC LIMIT ?)",
                    (batch,),
                )
                conn.commit()
            n = cur.rowcount or 0
            if n <= 0:
                break
            dropped += n
            freed += got
            total -= got
        return dropped, freed

    def digest_clear(self) -> int:
        """清空校验缓存，返回清掉的块数。"""
        with self._lock:
            conn = self._conn()
            cur = conn.execute("DELETE FROM digest_block")
            conn.execute("DELETE FROM file_digest")
            conn.commit()
        return cur.rowcount or 0

    # ---------------------------------------------------------- 上传凭据缓存
    #
    # 115 的秒传要带 userkey（跨账号秒传时没它会被拒）。换凭据是整条链上
    # 最慢的一跳，能命中本地就跳过它。115 会轮换 userkey，所以凭据要缓存，
    # 但用之前得能判断它是不是过期了 —— 被拒就丢掉重换。

    def upload_cred_get(self, id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT userkey,user_id,at FROM upload_cred WHERE id=?", (str(id),)
        ).fetchone()
        if not row:
            return None
        return {"userkey": row["userkey"], "user_id": row["user_id"],
                "at": int(row["at"])}

    def upload_cred_put(self, id: str, userkey: str, user_id: str) -> None:
        now = int(time.time())
        with self._lock:
            conn = self._conn()
            conn.execute(
                "INSERT INTO upload_cred(id,userkey,user_id,at) VALUES(?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET userkey=excluded.userkey,"
                " user_id=excluded.user_id, at=excluded.at",
                (str(id), str(userkey or ""), str(user_id or ""), now),
            )
            conn.commit()

    def upload_cred_delete(self, id: str) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM upload_cred WHERE id=?", (str(id),))
            conn.commit()

