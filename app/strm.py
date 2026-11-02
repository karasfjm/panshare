"""strm 文件的路径、命名、内容与落盘。"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import shutil
import threading
from pathlib import Path
from typing import Any
from urllib.parse import quote

log = logging.getLogger(__name__)

_ILLEGAL = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_WS = re.compile(r"\s+")

# 刮削文件后缀：网盘上删了原片，这些一起清掉，别在 Emby 里留「幽灵条目」
SCRAPE_EXTS = frozenset({
    ".nfo", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".sup", ".vtt",
    ".mp3", ".flac", ".m4a", ".aac", ".txt", ".json", ".xml",
})
# Emby / Kodi 常见的「同名前缀 + 用途后缀」：影片-poster.jpg、影片-fanart.jpg
SCRAPE_TAGS = frozenset({
    "poster", "fanart", "thumb", "banner", "logo", "clearlogo", "clearart",
    "disc", "landscape", "backdrop", "trailer", "theme", "folder", "default",
    "fanart1", "fanart2", "thumb1",
})
# 视频后缀：`a.mkv.strm` 这种保后缀命名，对应的刮削文件叫 `a.nfo`
MEDIA_EXTS = frozenset({
    ".mp4", ".mkv", ".ts", ".m2ts", ".mts", ".iso", ".avi", ".wmv", ".flv",
    ".mov", ".rmvb", ".rm", ".webm", ".mpg", ".mpeg", ".m4v", ".3gp",
    ".asf", ".vob", ".divx", ".f4v", ".ogv",
})
# 多语言字幕/音轨的中间段：影片.zh.srt / 影片.forced.ass
SCRAPE_LANGS = frozenset({
    "zh", "chs", "cht", "zh-cn", "zh-tw", "zh-hans", "zh-hant",
    "en", "eng", "ja", "jp", "jpn", "ko", "kor", "fr", "de", "es", "ru",
    "sc", "tc", "forced", "default", "sdh", "cc", "hi", "sign",
    "简体", "繁体", "中文", "英文", "双语", "中英", "chs&eng",
})


def _truncate_bytes(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


def safe_component(name: str, max_bytes: int = 200) -> str:
    """把 115 上的名字转成在各平台（含 Emby on Windows）都安全的名字。

    按 UTF-8 字节截断，避免中文名超过文件系统 255 字节限制。
    """
    name = (name or "").replace("/", "_").replace("\u0000", "")
    name = _ILLEGAL.sub("_", name)
    name = name.strip().rstrip(". ")
    if name in ("", ".", ".."):
        return "_"
    if len(name.encode("utf-8")) > max_bytes:
        stem, dot, ext = name.rpartition(".")
        if dot and ext and len(ext) <= 10:
            keep = max_bytes - len(ext.encode("utf-8")) - 1
            name = _truncate_bytes(stem, max(8, keep)) + "." + ext
        else:
            name = _truncate_bytes(name, max_bytes)
    return name or "_"


def rel_join(*parts: str) -> str:
    return "/".join(p for p in parts if p)


def strm_rel_path(rel_file: str, keep_ext: bool = False) -> str:
    """`a/b/c.mkv` -> `a/b/c.strm`；keep_ext=True 时 -> `a/b/c.mkv.strm`。"""
    directory, _, filename = rel_file.rpartition("/")
    if keep_ext:
        result = f"{filename}.strm"
    else:
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        result = f"{stem}.strm"
    return rel_join(directory, result)


def pick_strm_rel(store: Any, db: Any, rel_file: str, file_id: int,
                  claimed: dict[str, int] | None = None) -> str:
    """选一个不和别的文件撞车的 strm 相对路径。

    同目录下 `a.mkv` 和 `a.mp4`（或 `歌.lrc` / `歌.m4a`）默认都会落到 `a.strm`，
    后写的会把前者覆盖掉，播放时就串了。撞车时保留原后缀区分。

    `claimed` 是本次扫描内存里已占用的路径：同一批目录的索引是扫完才批量入库的，
    光查库发现不了刚写过的同名文件。
    """
    def taken(rel: str) -> bool:
        abs_path = str(store.abspath(rel))
        owner = (claimed or {}).get(abs_path)
        if owner is None:
            owner = db.file_id_by_strm_path(abs_path)
        return owner is not None and owner != file_id

    candidate = strm_rel_path(rel_file)
    if not taken(candidate):
        return candidate

    alt = strm_rel_path(rel_file, keep_ext=True)
    if not taken(alt):
        log.info("strm 同名冲突，改用带后缀的名字: %s", alt)
        return alt

    stem = alt.removesuffix(".strm")
    for index in range(2, 50):
        numbered = f"{stem} ({index}).strm"
        if not taken(numbered):
            log.info("strm 同名冲突，改用编号名字: %s", numbered)
            return numbered
    return candidate


def file_ext(name: str) -> str:
    _, dot, ext = name.rpartition(".")
    if dot and ext and len(ext) <= 10 and "/" not in ext:
        return "." + ext.lower()
    return ""


class StrmStore:
    def __init__(self, root: Path | str, base_url: str = "", auth_token: str = "",
                 template: str = "", play_url_ext: bool = True, ed2k_dir: str = "ed2k",
                 route_prefix: str = "", account_uid: str = ""):
        self.root = Path(root)
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.template = template
        self.play_url_ext = play_url_ext
        self.ed2k_dir = (ed2k_dir or "ed2k").strip("/")
        # 多账号：strm 末尾写 115 用户 ID 来标识归属。
        # route_prefix 只为兼容老调用方保留，**不再拼进 URL**：
        # 以前子账号的地址是 /p115/a/<账号名>/play/...，现在统一成
        # /p115/play/<pickcode>/<name>/<uid>，账号身份完全由 uid 决定。
        self.route_prefix = ("/" + route_prefix.strip("/")) if route_prefix.strip("/") else ""
        self.account_uid = str(account_uid or "")
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ 地址
    def play_url(self, pickcode: str, name: str = "", uid: str = "",
                 base_url: str = "") -> str:
        """strm 内容：``<请求头>/p115/play/<pickcode>/<name>/<115 用户 ID>``。

        请求头 = 基地址（每条 115 STRM 配置可以单独指定），后面依次是
        pickcode、URL 编码后的文件名，最后是该文件所属账号的 **115 用户 ID**。
        uid 就是「这个 strm 属于哪个账号」的标识：播放时按它找回对应账号
        去换直链（旧格式 ``/play/<pickcode>.mkv`` 依然兼容）。
        """
        base = (base_url or self.base_url).rstrip("/")
        uid = str(uid or self.account_uid or "")
        ext = file_ext(name) if self.play_url_ext else ""
        if self.template:
            values = {
                "base": base,
                "pickcode": pickcode,
                "name": quote(name),
                "ext": ext.lstrip("."),
                "token": self.auth_token,
                "id": uid,
                "uid": uid,
            }
            url = self.template
            for key, value in values.items():
                url = url.replace("{" + key + "}", str(value))
            return url
        url = f"{base}/p115/play/{pickcode}"
        if name:
            url += "/" + quote(name, safe="")
        if uid:
            url += f"/{uid}"
        elif not name:
            url += ext
        if self.auth_token:
            url += f"?token={quote(self.auth_token)}"
        return url

    # ------------------------------------------------------------- ed2k 地址
    def ed2k_url(self, key: str, name: str = "") -> str:
        """ed2k 的 strm 内容。

        指向本服务的 /p115/ed2k/<hash>：文件被清理后重新转存，pickcode 会变，
        只有这个地址是稳定的。PLAY_URL_TEMPLATE 只支持 pickcode，这里不走模板。
        """
        ext = file_ext(name) if self.play_url_ext else ""
        url = f"{self.base_url}/p115/ed2k/{key}{ext}"
        if self.account_uid:
            url += f"/{self.account_uid}"
        if self.auth_token:
            url += f"?token={quote(self.auth_token)}"
        return url

    def ed2k_rel(self, key: str, name: str, unique: bool = False) -> str:
        safe = safe_component(name or key)
        rel = strm_rel_path(safe)
        if unique:
            stem = rel.removesuffix(".strm")
            rel = f"{stem} [{str(key)[:8]}].strm"
        return rel_join(self.ed2k_dir, rel)

    # ------------------------------------------------------------- 分享地址
    def share_url(self, my_share_code: str, file_id: int, name: str = "") -> str:
        """分享转存的 strm 内容：指向自己分享里的文件，播放时再解析直链。"""
        ext = file_ext(name) if self.play_url_ext else ""
        url = f"{self.base_url}/p115/share/{my_share_code}/{file_id}{ext}"
        if self.account_uid:
            url += f"/{self.account_uid}"
        if self.auth_token:
            url += f"?token={quote(self.auth_token)}"
        return url

    # ------------------------------------------------------------------ 落盘
    def abspath(self, rel: str) -> Path:
        clean = posixpath.normpath("/" + rel).lstrip("/")
        return self.root / clean

    def _write_atomic(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp{os.getpid()}-{threading.get_ident()}")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
                fp.write(content)
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _write_content(self, target: Path, content: str,
                       force: bool = False) -> tuple[str, bool]:
        """写一个 strm；``force=True`` 时不比较内容，直接重写（刷新 mtime）。"""
        if not force:
            try:
                if target.exists():
                    old = target.read_text(encoding="utf-8", errors="ignore").strip()
                    if old == content:
                        return str(target), False
            except OSError:
                pass
        try:
            self._write_atomic(target, content + "\n")
            return str(target), True
        except OSError as exc:
            log.warning("写入 strm 失败 %s: %s", target, exc)
            return str(target), False

    def write(self, rel_file: str, pickcode: str, name: str,
              strm_rel: str | None = None, uid: str = "",
              base_url: str = "", force: bool = False) -> tuple[str, bool]:
        """写入（或跳过）一个 strm。返回 (绝对路径, 是否发生改动)。

        ``base_url`` = 这条配置自己的「STRM 请求头」，留空就用全局默认。
        ``uid`` = 文件所属账号的 115 用户 ID，留空用本 store 的账号。
        ``force`` = 不比较内容，强制重写（「全新生成 / 全量覆盖」用）。
        """
        rel_strm = strm_rel or strm_rel_path(rel_file)
        return self._write_content(
            self.abspath(rel_strm),
            self.play_url(pickcode, name, uid=uid, base_url=base_url),
            force=force,
        )

    def write_url(self, rel_file: str, url: str, name: str = "",
                  strm_rel: str | None = None,
                  force: bool = False) -> tuple[str, bool]:
        """写一个内容为任意 URL 的 strm（分享转存等自定义入口用）。"""
        rel_strm = strm_rel or strm_rel_path(rel_file)
        return self._write_content(self.abspath(rel_strm), url, force=force)

    def write_ed2k(self, key: str, name: str, db: Any = None,
                   existing_path: str = "") -> tuple[str, bool]:
        """写 ed2k 的 strm；同名不同 hash 时自动加 hash 后缀避免互相覆盖。"""
        target = self.abspath(self.ed2k_rel(key, name))
        if db is not None and str(target) != existing_path:
            owner = db.ed2k_by_strm_path(str(target))
            if owner is not None and str(owner["key"]) != str(key).lower():
                target = self.abspath(self.ed2k_rel(key, name, unique=True))
        return self._write_content(target, self.ed2k_url(key, name))

    def remove_ed2k(self, key: str, name: str) -> bool:
        return self.remove_abs(str(self.abspath(self.ed2k_rel(key, name))))

    def remove(self, rel_file: str) -> bool:
        target = self.abspath(strm_rel_path(rel_file))
        try:
            if target.exists():
                target.unlink()
                self._prune_empty(target.parent)
                return True
        except OSError as exc:
            log.warning("删除 strm 失败 %s: %s", target, exc)
        return False

    def remove_abs(self, strm_path: str) -> bool:
        """按索引里记录的绝对路径删除（处理同名不同后缀的情况）。"""
        if not strm_path:
            return False
        target = Path(strm_path)
        try:
            if target.exists() and target.is_file():
                target.unlink()
                self._prune_empty(target.parent)
                return True
        except OSError as exc:
            log.warning("删除 strm 失败 %s: %s", target, exc)
        return False

    # ------------------------------------------------------------- 刮削文件
    @staticmethod
    def _stems_for(strm_path: str) -> tuple[str, ...]:
        """strm 对应的媒体主名：``影片.mkv.strm`` → ``影片.mkv`` / ``影片``。"""
        name = Path(strm_path).name
        stem = name[:-5] if name.lower().endswith(".strm") else name
        stems = [stem]
        head, dot, ext = stem.rpartition(".")
        if dot and ("." + ext.lower()) in MEDIA_EXTS:
            stems.append(head)
        return tuple(dict.fromkeys(s for s in stems if s))

    def remove_scraped(self, strm_path: str) -> list[str]:
        """删掉 strm 对应的刮削文件（nfo / 海报 / 字幕 …）。

        只在 **strm 所在目录**里删、只删白名单后缀、只删与片名同名的文件：
        ``影片.nfo``、``影片.zh.srt``、``影片-poster.jpg`` 会被删；
        ``影片.2023.nfo``（更像另一部片）、目录级 ``poster.jpg``、
        以及别人的文件都不动。
        """
        if not strm_path:
            return []
        folder = Path(strm_path).parent
        try:
            root = Path(self.root).resolve()
            here = folder.resolve()
            if here != root and root not in here.parents:
                log.warning("跳过 STRM_ROOT 之外的刮削清理: %s", folder)
                return []
            if not here.is_dir():
                return []
            entries = list(here.iterdir())
        except OSError:
            return []

        stems = self._stems_for(strm_path)
        removed: list[str] = []
        for item in entries:
            try:
                if not item.is_file():
                    continue
            except OSError:
                continue
            suffix = item.suffix.lower()
            if suffix == ".strm" or suffix not in SCRAPE_EXTS:
                continue
            name = item.name
            hit = False
            for stem in stems:
                if not name.startswith(stem):
                    continue
                rest = name[len(stem):]
                if rest.startswith("-"):
                    # 影片-poster.jpg / 影片-fanart.jpg
                    tag = rest[1:].split(".", 1)[0].lower()
                    hit = tag in SCRAPE_TAGS
                elif rest.startswith("."):
                    # 影片.nfo / 影片.zh.srt / 影片.forced.ass
                    # 只有「一段」（同名不同后缀）或「语言段 + 后缀」才算自己人，
                    # 影片.2023.nfo 更像另一部片，不动
                    parts = rest[1:].split(".")
                    if len(parts) == 1:
                        hit = True
                    elif len(parts) == 2:
                        hit = parts[0].lower() in SCRAPE_LANGS
                if hit:
                    break
            if not hit:
                continue
            try:
                item.unlink()
                removed.append(str(item))
            except OSError as exc:
                log.debug("删除刮削文件失败 %s: %s", item, exc)
        if removed:
            log.info("已清理 %d 个刮削文件（%s）", len(removed), Path(strm_path).name)
        return removed

    def remove_rel_dir(self, rel_dir: str, delete_empty: bool = True) -> int:
        target = self.abspath(rel_dir)
        if not target.is_dir():
            return 0
        count = sum(1 for _ in target.rglob("*.strm"))
        try:
            shutil.rmtree(target)
        except OSError as exc:
            log.warning("删除目录失败 %s: %s", target, exc)
            return 0
        if delete_empty:
            self._prune_empty(target.parent)
        return count

    def remove_strm_paths(self, paths: Any) -> int:
        """按绝对路径精确删除 ``*.strm``，**不按目录整片删**。

        「全新生成」用：删哪些文件由索引决定（这条配置扫过、且真的属于它），
        因为多条配置常常共用同一个输出目录 —— 按目录 rglob 删会把别的账号
        的 strm 一起带走（`local_dir` 直接填 ``/strm`` 时 prefix 就是空串，
        等于把整个 STRM_ROOT 清空）。

        三重保险：只删 ``.strm``、必须落在本服务的 ``STRM_ROOT`` 内、文件不存在
        就当已经删掉了。
        """
        root = Path(self.root)
        try:
            root = root.resolve()
        except OSError:
            pass
        removed = 0
        for raw in paths or ():
            if not raw:
                continue
            target = Path(str(raw))
            if target.suffix.lower() != ".strm":
                continue
            try:
                if not target.resolve().is_relative_to(root):
                    log.warning("跳过 STRM_ROOT 之外的删除: %s", target)
                    continue
            except OSError:
                continue
            try:
                target.unlink()
                removed += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                log.debug("删除 strm 失败 %s: %s", target, exc)
        if removed:
            log.info("全新生成：已删除 %d 个旧 strm（按索引精确删除）", removed)
        return removed

    def remove_strm_files(self, rel_dir: str = "") -> int:
        """只删某目录（含子孙）里的 ``*.strm``，其它文件（nfo/海报/字幕）保留。

        「全新生成」用：先把这棵树的 strm 清空再重写，但用户自己放进目录
        的元数据不能一起删掉 —— 那是 ``remove_rel_dir``（整目录 rmtree）
        干的事，只适合「这条配置的目录完全由本服务托管」的场景。
        """
        target = self.abspath(rel_dir) if rel_dir else Path(self.root)
        if not target.is_dir():
            return 0
        removed = 0
        try:
            files = list(target.rglob("*.strm"))
        except OSError as exc:
            log.warning("遍历目录失败 %s: %s", target, exc)
            return 0
        for item in files:
            try:
                item.unlink()
                removed += 1
            except OSError:
                continue
        if removed:
            log.info("全新生成：已删除 %d 个旧 strm（%s）", removed, rel_dir or "/")
        return removed

    def move_abs(self, old_path: str, new_path: str) -> bool:
        """按绝对路径移动 strm（保留同名冲突时的带后缀命名）。"""
        if not old_path:
            return False
        old, new = Path(old_path), Path(new_path)
        if old == new or not old.exists():
            return False
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, new)
            self._prune_empty(old.parent)
            return True
        except OSError as exc:
            log.warning("移动 strm 失败 %s -> %s: %s", old, new, exc)
            return False

    def move(self, old_rel: str, new_rel_file: str) -> tuple[str, bool]:
        """文件级移动/改名。"""
        old_path = self.abspath(strm_rel_path(old_rel))
        new_path = self.abspath(strm_rel_path(new_rel_file))
        if old_path == new_path:
            return str(new_path), False
        if not self.move_abs(str(old_path), str(new_path)):
            return str(new_path), False
        return str(new_path), True

    def move_dir(self, old_rel_dir: str, new_rel_dir: str) -> bool:
        """目录级移动/改名，整棵子树一起搬。"""
        old_path = self.abspath(old_rel_dir)
        new_path = self.abspath(new_rel_dir)
        if old_path == new_path or not old_path.exists():
            return False
        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            if new_path.exists():
                # 目标已存在：先清掉，保持与 115 一致
                shutil.rmtree(new_path, ignore_errors=True)
            os.replace(old_path, new_path)
            self._prune_empty(old_path.parent)
            return True
        except OSError as exc:
            log.warning("移动目录失败 %s -> %s: %s", old_path, new_path, exc)
            return False

    def _prune_empty(self, start: Path) -> None:
        current = start
        root = self.root.resolve()
        while True:
            try:
                if current.resolve() == root or root not in current.resolve().parents:
                    return
                if any(current.iterdir()):
                    return
                current.rmdir()
            except OSError:
                return
            current = current.parent

    def scan_local(self) -> set[str]:
        return {str(p) for p in self.root.rglob("*.strm")}
