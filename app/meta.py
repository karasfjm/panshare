"""元数据文件下载：把 115 上同目录的 nfo / 海报 / 字幕 拉到本地 strm 旁边。

115 STRM 配置里一直有「下载元数据文件」和「元数据文件扩展名」两个字段，
但镜像 1.23.0 里这两个字段只存不用（README 里也写着「当前版本不会下载元
数据文件」），MetaDownloader 在仓库历史里存在过却没接线。这里补全：

* 扫描器在遍历目录时顺带收集 ``meta_exts`` 命中的文件节点；
* 通过 ``download_url``（pickcode 直链）下载，写到对应 strm 目录旁；
* 本地已存在同名文件就跳过（绝不覆盖用户自己刮的元数据）；
* 先写 ``.part`` 再 ``os.replace``，中断不会留下半个文件；
* 单文件超过 ``max_bytes`` 放弃；失败只记日志，不影响 strm 主流程。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx

from .strm import rel_join, safe_component

log = logging.getLogger(__name__)

# 默认上限 64MB：nfo / jpg / 字幕 都远小于这个值
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
# 直链只用来下载元数据，给个宽松但有限的超时
DEFAULT_TIMEOUT = 30.0
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class MetaStats:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    size: int = 0

    def merge(self, other: "MetaStats") -> None:
        self.downloaded += other.downloaded
        self.skipped += other.skipped
        self.failed += other.failed
        self.size += other.size

    @property
    def touched(self) -> bool:
        return bool(self.downloaded or self.failed)


def split_meta_exts(raw: Any) -> frozenset[str]:
    """把配置里的 ``meta_exts`` 逗号串转成带点小写后缀集合。"""
    out: set[str] = set()
    for part in str(raw or "").split(","):
        part = part.strip().lower().lstrip(".")
        if part:
            out.add("." + part)
    return frozenset(out)


class MetaDownloader:
    """按目录批量下载元数据文件（串行 + 重试，避免把 115 直链打成限流）。"""

    def __init__(self, api: Any, store: Any, max_bytes: int = DEFAULT_MAX_BYTES,
                 timeout: float = DEFAULT_TIMEOUT, retries: int = 2,
                 retry_wait: float = 3.0):
        self.api = api
        self.store = store
        self.max_bytes = max(0, int(max_bytes))
        self.timeout = max(5.0, float(timeout))
        self.retries = max(0, int(retries))
        self.retry_wait = max(0.0, float(retry_wait))

    # ------------------------------------------------------------------ 对外
    def fetch(self, rel_dir: str, nodes: Iterable[dict[str, Any]],
              force: bool = False) -> MetaStats:
        """把一个目录里需要下载的元数据节点全部拉下来。"""
        stats = MetaStats()
        if self.max_bytes <= 0:
            return stats
        for node in nodes:
            try:
                stats.merge(self.fetch_one(rel_dir, node, force=force))
            except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响其它
                stats.failed += 1
                log.debug("下载元数据 %s 失败: %s", node.get("name"), exc)
        if stats.downloaded:
            log.info(
                "元数据已下载 %d 个（失败 %d，跳过 %d）→ %s",
                stats.downloaded, stats.failed, stats.skipped, rel_dir or "/",
            )
        return stats

    def fetch_one(self, rel_dir: str, node: dict[str, Any],
                  force: bool = False) -> MetaStats:
        """下载一个元数据文件。返回本次的统计（成功/跳过/失败各一个）。"""
        stats = MetaStats()
        name = str(node.get("name") or "")
        pickcode = str(node.get("pickcode") or "")
        if not name:
            return stats
        target = self._target(rel_dir, name)
        if target is None:
            stats.failed += 1
            return stats
        if target.exists() and not force:
            # 本地已经有了：可能是用户自己刮的，一律不覆盖
            stats.skipped += 1
            return stats
        if not pickcode:
            ensure = getattr(self.api, "ensure_pickcode", None)
            if callable(ensure):
                pickcode = str(ensure(node) or "")
        if not pickcode:
            stats.failed += 1
            log.debug("元数据 %s 没有 pickcode，跳过下载", name)
            return stats

        for attempt in range(self.retries + 1):
            try:
                url, headers = self.api.download_url(pickcode, user_agent=_USER_AGENT)
                break
            except Exception as exc:  # noqa: BLE001 - 拿不到直链先重试
                if attempt >= self.retries:
                    stats.failed += 1
                    log.warning("取元数据直链失败 %s: %s", name, exc)
                    return stats
                time.sleep(self.retry_wait)

        try:
            written = self._download(url, headers, target)
        except _TooLarge:
            stats.failed += 1
            log.warning("元数据超过 %d 字节上限，已跳过: %s", self.max_bytes, name)
            return stats
        except Exception as exc:  # noqa: BLE001 - 网络问题按失败计数
            stats.failed += 1
            log.warning("下载元数据失败 %s: %s", name, exc)
            self._cleanup(target)
            return stats
        stats.downloaded += 1
        stats.size += written
        return stats

    # ------------------------------------------------------------------ 内部
    def _target(self, rel_dir: str, name: str) -> Path | None:
        safe = safe_component(name)
        if not safe or safe == "_":
            return None
        return Path(self.store.abspath(rel_join(rel_dir, safe)))

    def _download(self, url: str, headers: dict[str, str], target: Path) -> int:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.part")
        send_headers = {k: v for k, v in (headers or {}).items() if k.lower() != "host"}
        send_headers.setdefault("User-Agent", _USER_AGENT)
        written = 0
        try:
            with httpx.Client(follow_redirects=True, timeout=self.timeout) as client:
                with client.stream("GET", url, headers=send_headers) as resp:
                    resp.raise_for_status()
                    declared = int(resp.headers.get("content-length") or 0)
                    if declared and declared > self.max_bytes:
                        raise _TooLarge()
                    with open(tmp, "wb") as fp:
                        for chunk in resp.iter_bytes(64 * 1024):
                            written += len(chunk)
                            if written > self.max_bytes:
                                raise _TooLarge()
                            fp.write(chunk)
            os.replace(tmp, target)
            return written
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    @staticmethod
    def _cleanup(target: Path) -> None:
        for path in (target.with_name(f".{target.name}.part"),):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass


class _TooLarge(Exception):
    """下载内容超过上限（内部用，不往外抛）。"""


__all__ = ["DEFAULT_MAX_BYTES", "MetaDownloader", "MetaStats", "split_meta_exts"]
