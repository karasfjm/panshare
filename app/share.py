"""115 分享链接 → 转存到自己的网盘 → 自己再分享 → 生成 strm。

链路：

1. 粘贴分享链接（支持 ``?password=``、``#提取码``、``share_code-receive_code`` 等
   常见格式），可选指定接收目录；
2. 用 ``share_snap`` 递归列出分享里的文件（拿到相对路径 / sha1 / 大小）；
3. 按顶层条目整体 ``share_receive`` 转存到接收目录 —— 文件从此是自己的，永久可用；
4. 把转存好的顶层条目用 ``share_send`` 创建**自己的**长期分享
   （``share_duration=-1``），记录 my_share_code / my_receive_code；
5. strm 指向 ``/p115/share/<我的分享码>/<file_id>``，播放时用
   ``share_download_url`` 从自己的分享里解析直链（302 或中转）。

为什么转存后还要走自己的分享：播放流量走分享下载通道，
不占用本账号的文件下载直链配额，也不会把主账号 cookies 暴露在播放请求里。
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
from .p115api import P115API, P115Error
from .strm import StrmStore, rel_join, safe_component, strm_rel_path

log = logging.getLogger(__name__)

# 分享快照每页条数。115 前面有阿里云 WAF，递归遍历大分享时请求很密集，
# 页越大越容易被 405 拦 —— 之前用 1150（大页）时一次遍历就能把账号打进限流，
# 遍历中断就表现为「只转存了部分内容」。改小页 + 每页之间留间隔更稳。
SNAP_PAGE = 200
# 每页之间的间隔（秒）：给 WAF 留出喘息，避免连续请求被拦
SNAP_INTERVAL = 0.35
# 单个分享最多递归多少层（防御异常深的目录树）
SNAP_MAX_DEPTH = 12

# 115 会改写文件名里的非法字符（分享快照里 ``:`` 显示成 ``_``，
# 落盘后又是另一种形态），匹配时两边都归一化后再比对
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _norm_name(name: str) -> str:
    return _ILLEGAL_CHARS.sub("_", name).strip().lower()


class ShareError(P115Error):
    """分享功能的可读错误。"""


def parse_share(text: str) -> dict[str, str]:
    """从文本中解析分享码与提取码。

    支持：

    - ``https://115.com/s/xxxxx?password=yyyy``
    - ``https://115cdn.com/s/xxxxx#yyyy``（fragment 形式）
    - ``xxxxx-yyyy``（短横线形式，p115client 的标准格式）
    - 纯 share_code（无提取码的公开分享）
    """
    raw = (text or "").strip()
    if not raw:
        raise ShareError("请粘贴 115 分享链接")
    try:
        from p115client.util import share_extract_payload

        payload = dict(share_extract_payload(raw))
    except Exception as exc:
        raise ShareError(f"无法从「{raw[:80]}…」识别分享链接: {exc}") from exc
    share_code = str(payload.get("share_code") or "").strip()
    if not share_code:
        raise ShareError(f"无法从「{raw[:80]}…」识别分享码")
    receive_code = str(payload.get("receive_code") or "").strip()
    if not receive_code:
        # p115client 的解析不认 fragment 形式（115cdn.com/s/xxx#yyyy），这里补上
        from urllib.parse import urlparse

        fragment = urlparse(raw if "//" in raw else "https://x/" + raw).fragment
        fragment = fragment.strip("/#")
        if fragment and "-" not in fragment and not fragment.isdigit():
            receive_code = fragment
    return {"share_code": share_code, "receive_code": receive_code}


class ShareManager:
    def __init__(self, cfg: Config, db: Database, api: P115API, store: StrmStore):
        self.cfg = cfg
        self.db = db
        self.api = api
        self.store = store
        self._dir_cache: tuple[int, str, float] | None = None
        # 同一个分享码的转存只允许一个线程在做
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ 配置
    @property
    def strm_dir(self) -> str:
        """分享 strm 的输出子目录（相对 STRM_ROOT，网页上可改）。"""
        raw = self.db.get_meta("share_strm_dir") or self.cfg.share_strm_dir
        return (raw or "share").strip("/") or "share"

    def settings(self) -> dict[str, Any]:
        # 读设置不查 115：WAF 限流时 get_node 失败会误显示成「未设置」。
        # 目录名在保存时就持久化到 meta，这里只读本地。
        cid = self.db.get_meta_int("share_dir_cid") or self.cfg.share_dir_cid
        name = self.db.get_meta("share_dir_name") or ""
        if not name and self._dir_cache and self._dir_cache[0] == cid:
            name = self._dir_cache[1]
        return {
            "enabled": self.cfg.share_enabled,
            "dir_cid": str(cid) if cid else "",
            "dir_name": name,
            # 目录的完整路径（保存时记下来）：网页输入框直接显示 /影音/国漫，
            # 只有目录名的话重名目录分不清
            "dir_path": self.db.get_meta("share_dir_path") or "",
            "dir_default_name": self.cfg.share_dir_name,
            "strm_dir": self.strm_dir,
            "count": self.db._conn().execute("SELECT count(*) c FROM shares").fetchone()["c"],
        }

    def save_settings(self, dir_cid: int | None = None,
                      strm_dir: str | None = None,
                      dir_path: str = "") -> dict[str, Any]:
        if dir_cid is not None:
            cid = int(dir_cid)
            if cid:
                node = self.api.get_node(cid)
                if node is None or not node["is_dir"]:
                    raise ShareError(f"115 上不存在目录 {cid}")
                self.db.set_meta("share_dir_name", node.get("name") or "")
                # 前端带了路径就存路径；没带就退回目录名
                self.db.set_meta("share_dir_path",
                                 (dir_path or "").strip() or (node.get("name") or ""))
            else:
                self.db.set_meta("share_dir_path", "")
            self.db.set_meta("share_dir_cid", cid)
            self._dir_cache = None
        if strm_dir is not None:
            # 只影响之后生成的 strm；已生成的留在老目录（不自动搬）
            self.db.set_meta("share_strm_dir", (strm_dir or "").strip("/"))
        return self.settings()

    @property
    def dir_cid(self) -> int:
        return self.db.get_meta_int("share_dir_cid") or self.cfg.share_dir_cid

    def target(self, create: bool = False) -> tuple[int, str]:
        """返回 (接收目录 cid, 名称)；create=True 时没配置就自动建一个。

        读取（create=False）不访问 115：有配置就直接返回，名字取 meta。
        """
        now = time.time()
        if self._dir_cache and self._dir_cache[2] > now:
            return self._dir_cache[0], self._dir_cache[1]
        cid = self.dir_cid
        if cid:
            name = self.db.get_meta("share_dir_name") or ""
            if not create:
                return cid, name
            node = self.api.get_node(cid)
            if node is not None and node["is_dir"]:
                name = node.get("name") or name or str(cid)
                self.db.set_meta("share_dir_name", name)
                self._dir_cache = (cid, name, now + 300)
                return cid, name
            log.warning("分享接收目录 %s 已失效，稍后重新创建", cid)
        if not create:
            return cid, self.db.get_meta("share_dir_name") or ""
        cid = self.api.find_child_dir(0, self.cfg.share_dir_name)
        if not cid:
            cid = self.api.mkdir(0, self.cfg.share_dir_name)
        self.db.set_meta("share_dir_cid", cid)
        self.db.set_meta("share_dir_name", self.cfg.share_dir_name)
        self._dir_cache = (cid, self.cfg.share_dir_name, now + 300)
        log.info("分享接收目录就绪: %s (%s)", self.cfg.share_dir_name, cid)
        return cid, self.cfg.share_dir_name

    def _share_lock(self, share_code: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(share_code, threading.Lock())

    # ------------------------------------------------------------------ 列分享内容
    def _snap_page(self, share_code: str, receive_code: str, cid: int,
                   offset: int, out: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        payload = {
            "share_code": share_code, "receive_code": receive_code,
            "cid": int(cid), "limit": SNAP_PAGE, "offset": offset,
        }
        try:
            # P115API.share_snap 带 WAF 退避重试；老对象（或测试桩）退回 client 直调
            caller = getattr(self.api, "share_snap", None)
            if callable(caller):
                resp = caller(payload)
            else:
                resp = self.api.client.share_snap(payload)
        except Exception as exc:
            raise ShareError(f"读取分享内容失败: {exc}") from exc
        if not isinstance(resp, dict) or resp.get("state") is False:
            message = str((resp or {}).get("error") or "")
            raise ShareError(f"读取分享内容失败: {message or resp}")
        if out is not None:
            info = (resp.get("data") or {}).get("shareinfo") or resp.get("shareinfo") or {}
            out["title"] = str(info.get("share_title") or out.get("title") or "")
        return list((resp.get("data") or {}).get("list") or [])

    def _iter_snap(self, share_code: str, receive_code: str, cid: int,
                   out: dict[str, Any] | None = None):
        offset = 0
        first = True
        while True:
            if not first:
                # 页与页之间留间隔，避免把账号打进 WAF 限流
                time.sleep(SNAP_INTERVAL)
            first = False
            page = self._snap_page(share_code, receive_code, cid, offset, out)
            if not page:
                return
            yield from page
            if len(page) < SNAP_PAGE:
                return
            offset += len(page)

    @staticmethod
    def _is_dir(item: dict[str, Any]) -> bool:
        # 分享接口里目录条目没有 fid / sha（目录 id 在 cid 字段）
        return not item.get("fid") and not item.get("sha")

    @staticmethod
    def _item_id(item: dict[str, Any]) -> int:
        # 文件条目的 id 在 fid，目录条目的 id 在 cid
        return int(item.get("fid") or item.get("cid") or 0)

    def list_share_tree(self, share_code: str, receive_code: str) -> dict[str, Any]:
        """递归列出分享内容。

        返回 ``{"title": ..., "items": [{"fid","name","rel_path","is_dir",
        "size","sha1"}], "top_ids": [...], "file_count": int,
        "truncated": bool, "skipped": [...]}``。

        ``truncated`` 为 True 表示遍历没走完（被 WAF 拦或目录太深）——
        这时内容是**不完整**的，调用方必须如实告诉用户，不能当成全部转存了。
        """
        title = ""
        names: dict[int, str] = {}        # fid -> name（含目录）
        items: list[dict[str, Any]] = []
        top_ids: list[int] = []
        file_count = 0
        seen: set[int] = set()
        snap_meta: dict[str, Any] = {}
        skipped: list[str] = []
        truncated = False

        def walk(cid: int, prefix: str, depth: int) -> None:
            nonlocal file_count, truncated
            if depth > SNAP_MAX_DEPTH:
                truncated = True
                skipped.append(prefix or "(根目录)")
                log.warning("分享目录层级超过 %d 层，停止递归: %s",
                            SNAP_MAX_DEPTH, prefix)
                return
            try:
                page_iter = self._iter_snap(share_code, receive_code, cid, snap_meta)
                while True:
                    try:
                        item = next(page_iter)
                    except StopIteration:
                        break
                    fid = self._item_id(item)
                    if not fid or fid in seen:
                        continue
                    seen.add(fid)
                    name = str(item.get("n") or "")
                    is_dir = self._is_dir(item)
                    if cid == 0:
                        top_ids.append(fid)
                    if is_dir:
                        names[fid] = name
                        walk(fid, rel_join(prefix, safe_component(name)), depth + 1)
                    else:
                        file_count += 1
                        items.append({
                            "fid": fid,
                            "name": name,
                            "rel_path": rel_join(prefix, safe_component(name)),
                            "size": int(item.get("s") or 0),
                            "sha1": str(item.get("sha") or "").upper(),
                        })
            except ShareError as exc:
                # 某个子目录读不出来：整个分享的内容就不完整了，必须记下来
                truncated = True
                skipped.append(prefix or "(根目录)")
                log.warning("分享子目录读取失败（内容不完整）: %s — %s", prefix, exc)

        walk(0, "", 0)
        # 标题优先用 snap 响应里的 shareinfo（share_info 对部分分享会报错）
        title = snap_meta.get("title") or title
        if not title:
            try:
                info = self.api.client.share_info(
                    {"share_code": share_code, "receive_code": receive_code}
                )
                if isinstance(info, dict):
                    data = info.get("data") or {}
                    title = str(data.get("share_title") or info.get("share_title") or "")
            except Exception as exc:  # noqa: BLE001 - 标题拿不到不致命
                log.debug("读取分享标题失败: %s", exc)
        return {
            "title": title,
            "items": items,
            "top_ids": top_ids,
            "file_count": file_count,
            "truncated": truncated,
            "skipped": skipped,
        }

    # ------------------------------------------------------------------ 添加
    def add(self, url: str, name: str = "", dir_cid: int = 0,
            force: bool = False, direct: bool = False) -> dict[str, Any]:
        """处理一条分享链接。

        ``direct=True`` =「直接生成」：只列目录、直接把 strm 指向**原分享**，
        不转存、不建自己的分享。适合只是「拿来当片源播」、不想占用自己
        网盘空间的场景；缺点是原分享失效 strm 就播不了。
        """
        if not self.cfg.share_enabled:
            raise ShareError("分享功能已关闭（SHARE_ENABLED=0）")
        payload = parse_share(url)
        share_code = payload["share_code"]
        receive_code = payload["receive_code"]

        with self._share_lock(share_code):
            rec = self.db.get_share(share_code)
            already_ready = (
                rec is not None
                and rec.get("status") in ("ready", "partial")
                and not force
                # 模式变了（转存 ↔ 直接生成）要重做，不能复用旧结果
                and bool(int(rec.get("direct") or 0)) == bool(direct)
            )

            tree = self.list_share_tree(share_code, receive_code)
            title = (name or "").strip() or tree["title"] or share_code

            self.db.upsert_share({
                "share_code": share_code,
                "receive_code": receive_code,
                "url": url.strip(),
                "name": title,
                "direct": 1 if direct else 0,
                "dir_cid": 0 if direct else (
                    dir_cid or (rec or {}).get("dir_cid") or self.target(create=True)[0]),
                "total": tree["file_count"],
                "status": "pending",
                "message": "转存中" if not already_ready else "",
            })

            if already_ready:
                # 之前已经转存过：115 的转存不做去重，再转一次会多出一份副本
                done = int(rec.get("done") or 0)
            elif direct:
                try:
                    done = self._write_direct(share_code, tree)
                except Exception as exc:
                    self.db.update_share(share_code, status="failed", message=str(exc))
                    raise
            else:
                try:
                    received = self._transfer(share_code, receive_code, tree,
                                              dir_cid or int((rec or {}).get("dir_cid") or 0))
                    done = self._locate_and_write(share_code, tree)
                    if not done and not received:
                        # 之前接收过、但文件已不在网盘（多半在回收站）：
                        # 115 拒绝重复接收同一分享，只能先还原再定位
                        target = int((rec or {}).get("dir_cid") or 0) or self.target(create=True)[0]
                        if self._revert_from_recycle(target, tree):
                            done = self._locate_and_write(share_code, tree)
                except Exception as exc:
                    self.db.update_share(share_code, status="failed", message=str(exc))
                    raise

            total = tree["file_count"]
            truncated = bool(tree.get("truncated"))
            status = "ready" if done >= total else ("partial" if done else "failed")
            if truncated:
                # 遍历没走完：total 本身就不准（少算了文件），不能报「全部完成」
                message = (
                    f"内容读取不完整（已转存 {done} 个文件，"
                    f"部分目录读取失败：{', '.join(tree.get('skipped') or ['未知'])}）。"
                    "通常是 115 限流，请稍后点「重新定位」补齐"
                )
                status = "partial" if done else "failed"
            elif done >= total:
                message = ""
            else:
                message = f"转存了 {done}/{total} 个文件，其余可能在原分享中被删除"
            self.db.update_share(share_code, status=status, message=message, done=done)
            log.info("分享处理完成: %s（%d/%d 个文件%s）", title, done, total,
                     "，遍历不完整" if truncated else "")
            return self.db.get_share(share_code) or {}

    def _transfer(self, share_code: str, receive_code: str,
                  tree: dict[str, Any], dir_cid: int) -> bool:
        """把分享里的顶层条目整体转存到接收目录。返回是否真正发起了转存。"""
        target = dir_cid or self.target(create=True)[0]
        if not target:
            raise ShareError("接收目录不可用，请先选择一个 115 目录")
        if not tree["top_ids"]:
            raise ShareError("分享里没有可转存的内容")
        try:
            resp = self.api.client.share_receive({
                "share_code": share_code,
                "receive_code": receive_code,
                "file_id": ",".join(str(i) for i in tree["top_ids"]),
                "cid": int(target),
            })
        except Exception as exc:
            raise ShareError(f"转存失败: {exc}") from exc
        if not isinstance(resp, dict) or resp.get("state") is False:
            message = str((resp or {}).get("error") or "")
            # 同一分享重复接收不算失败（幂等重试 / 之前转存过）
            if "已接收" in message or "重复接收" in message:
                log.info("分享之前已接收过，跳过转存: %s", message)
                return False
            raise ShareError(f"转存失败: {message or resp}")
        data = resp.get("data") or {}
        log.info(
            "分享转存请求成功: %s（文件 %s，目录 %s）",
            data.get("receive_title"),
            data.get("recv_file_count"),
            data.get("recv_folder_count"),
        )
        return True

    def _revert_from_recycle(self, target: int, tree: dict[str, Any]) -> int:
        """把回收站里属于本次分享的条目还原回去。

        场景：分享之前接收过、文件后来被删（在回收站里），115 会拒绝
        重复接收同一分享 —— 只能先从回收站还原再定位。返回还原条数。

        匹配规则：原父目录是接收目录，或文件名命中分享内容里的名字。
        """
        try:
            resp = self.api.client.recyclebin_list({"limit": 1150, "offset": 0})
        except Exception as exc:  # noqa: BLE001
            log.warning("读取回收站失败: %s", exc)
            return 0
        data = (resp.get("data") or {}) if isinstance(resp, dict) else {}
        entries = data.get("list") if isinstance(data, dict) else data
        names = {_norm_name(rel.rsplit("/", 1)[-1]) for rel in
                 (item["rel_path"] for item in tree["items"])}
        names.update(_norm_name(rel.split("/", 1)[0]) for rel in
                     (item["rel_path"] for item in tree["items"]))
        mine = [
            it for it in (entries or [])
            if str(it.get("cid")) == str(target)
            or _norm_name(str(it.get("file_name") or it.get("fn") or "")) in names
        ]
        if not mine:
            return 0
        payload: dict[str, Any] = {}
        for i, it in enumerate(mine):
            payload[f"rid[{i}]"] = int(it["id"])
        try:
            r = self.api.client.recyclebin_revert(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("从回收站还原失败: %s", exc)
            return 0
        ok = isinstance(r, dict) and r.get("state") is not False
        if not ok:
            log.warning("从回收站还原被拒绝: %s", r)
            return 0
        log.info("从回收站还原了 %d 个属于分享内容的条目", len(mine))
        return len(mine)

    # ------------------------------------------------------------------ 自己的分享
    def _top_received_ids(self, target: int, tree: dict[str, Any]) -> list[int]:
        """接收目录里对应本次转存的顶层条目 id（用来创建自己的分享）。

        按归一化后的顶层名字匹配，避免把接收目录里其他分享的内容也包进来。
        """
        top_names = {
            _norm_name(rel.split("/", 1)[0]) for rel in
            [item["rel_path"] for item in tree["items"]]
        }
        if not top_names:
            return []
        dirs, files = self.api.list_dir(
            target, app=self.cfg.scan_app, page_workers=0, cooldown=self.cfg.scan_cooldown
        )
        return [
            int(node["id"]) for node in list(dirs) + list(files)
            if _norm_name(node["name"]) in top_names
        ]

    def _ensure_own_share(self, share_code: str, top_ids: list[int]) -> tuple[str, str]:
        """创建（或复用）自己的长期分享，返回 (my_share_code, my_receive_code)。"""
        rec = self.db.get_share(share_code) or {}
        my_sc = str(rec.get("my_share_code") or "")
        my_rc = str(rec.get("my_receive_code") or "")
        want = ",".join(str(int(i)) for i in sorted(set(top_ids)))
        have_ids = str(rec.get("shared_ids") or "")
        if my_sc and my_rc and have_ids == want:
            return my_sc, my_rc
        if my_sc:
            # 内容变了：取消旧分享再建新的，避免分享列表里堆一堆旧条目
            try:
                self.api.client.share_update({"share_code": my_sc, "action": "delete"})
            except Exception as exc:  # noqa: BLE001 - 旧分享删不掉不致命
                log.warning("取消旧分享 %s 失败: %s", my_sc, exc)
        try:
            resp = self.api.client.share_send({
                "file_ids": want, "ignore_warn": 1,
            })
        except Exception as exc:
            raise ShareError(f"创建自己的分享失败: {exc}") from exc
        if not isinstance(resp, dict) or resp.get("state") is False:
            message = str((resp or {}).get("error") or "")
            raise ShareError(f"创建自己的分享失败: {message or resp}")
        data = resp.get("data") or {}
        my_sc = str(data.get("share_code") or data.get("share_key") or "")
        my_rc = str(data.get("receive_code") or "")
        if not my_sc:
            raise ShareError(f"创建自己的分享失败: 响应里没有分享码 {resp}")
        if my_rc:
            # 设为长期分享
            try:
                self.api.client.share_update({
                    "share_code": my_sc, "receive_code": my_rc, "share_duration": -1,
                })
            except Exception as exc:  # noqa: BLE001
                log.warning("设置分享 %s 为长期失败（仍可用，但会到期）: %s", my_sc, exc)
        self.db.update_share(
            share_code, my_share_code=my_sc, my_receive_code=my_rc, shared_ids=want,
        )
        log.info("自己的分享已就绪: %s（%d 个顶层条目）", my_sc, len(top_ids))
        return my_sc, my_rc

    # ------------------------------------------------------------------ 定位与写 strm
    def _walk_local(self, cid: int, prefix: str,
                    out: dict[str, dict[str, Any]]) -> None:
        """递归列出接收目录，得到 rel_path -> node 的映射。"""
        dirs, files = self.api.list_dir(
            cid, app=self.cfg.scan_app, page_workers=0, cooldown=self.cfg.scan_cooldown
        )
        for node in files:
            out[rel_join(prefix, node["name"])] = node
        for node in dirs:
            self._walk_local(int(node["id"]), rel_join(prefix, node["name"]), out)

    def _write_direct(self, share_code: str, tree: dict[str, Any]) -> int:
        """「直接生成」：不转存，strm 直接指向原分享里的文件。

        strm 内容是 ``/p115/share/<原分享码>/<原分享里的 fid>``，播放时用
        这个分享的提取码去解析直链 —— 文件始终留在原分享主人网盘里。
        """
        subdir = safe_component(f"{tree['title'] or share_code} [{share_code[:8]}]")
        done = 0
        for item in tree["items"]:
            fid = int(item.get("fid") or 0)
            if not fid:
                continue
            rel_strm = rel_join(self.strm_dir, subdir,
                                strm_rel_path(item["rel_path"]))
            url = self.store.share_url(share_code, fid, item["name"])
            path, _ = self.store.write_url(item["rel_path"], url, item["name"],
                                           strm_rel=rel_strm)
            self.db.upsert_share_file(share_code, {
                "rel_path": item["rel_path"],
                "file_id": fid,
                "pickcode": "",
                "name": item["name"],
                "size": int(item.get("size") or 0),
                "sha1": item.get("sha1") or "",
                "strm_path": path,
            })
            done += 1
        log.info("分享直接生成完成: %s（%d 个文件，未转存）",
                 tree.get("title") or share_code, done)
        return done

    def _locate_and_write(self, share_code: str, tree: dict[str, Any]) -> int:
        """在接收目录里按相对路径定位文件 → 建自己的分享 → 写 strm，入库。"""
        rec = self.db.get_share(share_code) or {}
        target = int(rec.get("dir_cid") or self.target(create=True)[0])
        local: dict[str, dict[str, Any]] = {}
        self._walk_local(target, "", local)
        # 归一化索引：115 落盘文件名和分享快照里的可能差非法字符（: vs _）
        norm_local: dict[str, dict[str, Any]] = {}
        for key, node in local.items():
            norm_local.setdefault(_norm_name(key), node)

        def locate(rel_path: str) -> dict[str, Any] | None:
            node = local.get(rel_path) or norm_local.get(_norm_name(rel_path))
            if node is None:
                # 转存时 115 可能合并/改名：按文件名兜底
                base = rel_path.rsplit("/", 1)[-1]
                node = local.get(base) or norm_local.get(_norm_name(base))
            return node

        located: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in tree["items"]:
            node = locate(item["rel_path"])
            if node is None or not node.get("id"):
                continue
            located.append((item, node))
        if not located:
            return 0        # 一个都没定位到：交给上层走回收站还原兜底

        # 自己再分享：优先分享接收目录里的顶层条目（包含目录树）；
        # 顶层里混进了空目录会被 115 拒绝，此时退化为直接分享定位到的文件
        file_ids = [int(node["id"]) for _, node in located]
        top_ids = self._top_received_ids(target, tree) or file_ids
        try:
            my_sc, _ = self._ensure_own_share(share_code, top_ids)
        except ShareError:
            if top_ids == file_ids:
                raise
            log.warning("按顶层条目创建分享失败，改用 %d 个文件直接分享", len(file_ids))
            my_sc, _ = self._ensure_own_share(share_code, file_ids)

        subdir = safe_component(f"{tree['title'] or share_code} [{share_code[:8]}]")
        done = 0
        for item, node in located:
            rel_strm = rel_join(self.strm_dir, subdir,
                                strm_rel_path(item["rel_path"]))
            url = self.store.share_url(my_sc, int(node["id"]), item["name"])
            path, _ = self.store.write_url(item["rel_path"], url, item["name"],
                                           strm_rel=rel_strm)
            self.db.upsert_share_file(share_code, {
                "rel_path": item["rel_path"],
                "file_id": int(node["id"]),
                "pickcode": node.get("pickcode") or "",
                "name": item["name"],
                "size": int(node.get("size") or item["size"]),
                "sha1": item["sha1"],
                "strm_path": path,
            })
            done += 1
        return done

    # ------------------------------------------------------------------ 管理
    def refresh(self, share_code: str) -> dict[str, Any]:
        """重新定位已转存的文件（比如换过接收目录 / strm 被误删后补写）。"""
        rec = self.db.get_share(share_code)
        if rec is None:
            raise ShareError(f"没有这个分享: {share_code}")
        tree = self.list_share_tree(share_code, rec.get("receive_code") or "")
        if int(rec.get("direct") or 0):
            # 直接生成模式：不需要定位，按原分享重新写一遍即可
            done = self._write_direct(share_code, tree)
        else:
            done = self._locate_and_write(share_code, tree)
        truncated = bool(tree.get("truncated"))
        total = max(int(rec.get("total") or tree["file_count"]), done)
        status = "ready" if done >= total else ("partial" if done else "failed")
        message = ""
        if truncated:
            # 遍历不完整时 total 会偏小，可能误判成「已完成」——强制标成部分完成
            status = "partial" if done else "failed"
            message = ("内容读取不完整（115 限流），已转存 " + str(done) +
                       " 个文件，请稍后再试一次补齐")
        self.db.update_share(share_code, status=status, done=done, total=total,
                             message=message)
        return self.db.get_share(share_code) or rec

    def delete(self, share_code: str, delete_files: bool = True) -> dict[str, Any]:
        rec = self.db.get_share(share_code) or self.db.get_share_by_my(share_code)
        if rec is None:
            raise ShareError(f"没有这个分享: {share_code}")
        share_code = str(rec["share_code"])
        # 记录要先读出来再删（delete_share 会连 share_files 一起清掉）
        files = self.db.list_share_files(share_code)
        # 先取消自己的分享（转存文件删除后分享就成了空壳）
        my_sc = str(rec.get("my_share_code") or "")
        if my_sc:
            try:
                self.api.client.share_update({"share_code": my_sc, "action": "delete"})
            except Exception as exc:  # noqa: BLE001 - 取消失败不阻塞删除
                log.warning("取消自己的分享 %s 失败: %s", my_sc, exc)
        rec = self.db.delete_share(share_code) or rec
        removed = 0
        for item in files:
            if item.get("strm_path"):
                self.store.remove_abs(item["strm_path"])
                removed += 1
        # strm 子目录里剩下的空壳也清掉
        subdir = safe_component(f"{rec.get('name') or share_code} [{share_code[:8]}]")
        self.store.remove_rel_dir(rel_join(self.strm_dir, subdir))
        # 「直接生成」的文件根本不在自己网盘里，那些 id 属于别人的分享，
        # 按 id 删会删到不相干的文件 —— 绝不能删
        if delete_files and not int(rec.get("direct") or 0):
            fids = [int(item["file_id"]) for item in files if item.get("file_id")]
            if fids:
                try:
                    if not self.api.delete_files(fids):
                        log.warning("删除转存文件请求被 115 拒绝: %s 个", len(fids))
                except P115Error as exc:
                    log.warning("删除分享转存文件失败（记录已删）: %s", exc)
        rec["_strm_removed"] = removed
        return rec

    def restore_strm(self) -> int:
        """补写缺失的 strm（strm 卷被清空 / 容器重建后）。"""
        count = 0
        for rec in self.db.list_shares():
            files = self.db.list_share_files(rec["share_code"])
            for item in files:
                if item.get("strm_path") and Path(item["strm_path"]).exists():
                    continue
                rel_strm = rel_join(
                    self.strm_dir,
                    safe_component(f"{rec['name']} [{rec['share_code'][:8]}]"),
                    strm_rel_path(item["rel_path"] or item["name"]),
                )
                my_sc = str(rec.get("my_share_code") or "")
                direct = int(rec.get("direct") or 0)
                if direct and item.get("file_id"):
                    # 直接生成：还指向原分享
                    url = self.store.share_url(rec["share_code"], int(item["file_id"]),
                                               item["name"])
                    self.store.write_url(item["rel_path"] or item["name"], url,
                                         item["name"], strm_rel=rel_strm)
                elif my_sc and item.get("file_id"):
                    url = self.store.share_url(my_sc, int(item["file_id"]),
                                               item["name"])
                    self.store.write_url(item["rel_path"] or item["name"], url,
                                         item["name"], strm_rel=rel_strm)
                elif item.get("pickcode"):
                    # 老记录没有自己的分享，退回 pickcode 播放地址
                    self.store.write(item["rel_path"] or item["name"],
                                     item["pickcode"], item["name"],
                                     strm_rel=rel_strm)
                else:
                    continue
                self.db.upsert_share_file(rec["share_code"], {
                    **item, "strm_path": str(self.store.abspath(rel_strm)),
                })
                count += 1
        if count:
            log.info("补齐分享 strm: %d 个", count)
        return count


__all__ = ["ShareError", "ShareManager", "parse_share"]
