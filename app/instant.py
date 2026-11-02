"""秒传播放：把文件从源账号秒传到目标账号，用目标账号的直链播放。

原理：115 的文件由 sha1 + size 唯一确定。目标账号调用 upload_init 时，
若 115 服务器上已有相同 sha1 的文件（任何人传过都算），就能「秒传」——
不用上传数据，目标账号立刻拥有这个文件，然后用它自己的直链播放。

流程（播放时）：

1. 从本地索引拿到文件的 sha1 / size / 源 pickcode；
2. 目标账号创建「暂存目录」（每个账号一个，名字 INSTANT_DIR_NAME）；
3. ``upload_file_init`` 秒传；大于 1MB 的文件 115 会要求二次校验：
   返回一个字节区间（sign_check），用**源账号**的直链按 Range 拉那段字节、
   算 sha1 回传 —— 整个过程只传约 1MB 的校验数据；
4. 拿到目标账号的 pickcode，再走正常的 download_url → 302/中转。

配合 Emby：在「秒传账号」里给 Emby 用户分配账号后，Emby 反代在改写
PlaybackInfo 时会把 MediaSource 指到 ``/p115/instant/<sha1>``，
播放时按上面流程用分配的账号解析直链。结果按 (账号, sha1) 缓存，
同一个文件多个用户同时播放只会秒传一次（同播复制）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any

import httpx

from .config import Config
from .db import DIGEST_BLOCK_SIZE, Database
from .p115api import P115API, P115Error

log = logging.getLogger(__name__)

# 校验缓存里每一块的大小。读源盘时按这个大小对齐取数，缓存也按同样的大小
# 存，两边对不上就等于没缓存。
BLOCK_SIZE = DIGEST_BLOCK_SIZE

# 115 要求校验的区间通常落在文件开头。这里先按整个块的粒度把数据拉下来，
# 同一块里之后再被要求校验别的区间时就能直接命中。
PRECHECK_BYTES = 1024 * 1024


class InstantError(P115Error):
    """秒传功能的可读错误。"""


class InstantManager:
    def __init__(self, cfg: Config, db: Database, api: P115API,
                 accounts_provider: Any = None, cookie_provider: Any = None,
                 sources_provider: Any = None):
        self.cfg = cfg
        self.db = db
        self.api = api          # 主账号（源）
        self._clients: dict[str, P115API] = {}
        self._clients_lock = threading.Lock()
        # (account, sha1) -> Lock：同一文件的秒传只允许一个线程在做（同播单飞）
        self._resolve_locks: dict[tuple[str, str], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # 账号 -> 暂存目录 cid 的缓存。**必须在 __init__ 里就建好**：
        # ``_staging()`` 走「网页上手动设置过暂存目录」那条分支时会直接往里写，
        # 晚建（以前是在函数末尾才补）就是 AttributeError →
        # 表现为播放 500 / 「秒传不生效」。
        self._staging_cache: dict[str, int] = {}
        # 秒传账号直接取自「115 助手」：由 hub 提供账号列表与 cookies，
        # 这样不用再单独添加一遍秒传账号（多个账号都能选）
        self.accounts_provider = accounts_provider
        self.cookie_provider = cookie_provider
        # 其它账号的索引库（预缓存要跨账号取源文件）
        self.sources_provider = sources_provider

    # ------------------------------------------------------------------ 账号
    def add_account(self, name: str, cookies: str) -> dict[str, Any]:
        name = (name or "").strip()
        cookies = (cookies or "").strip().strip('"')
        if not name or not cookies:
            raise InstantError("账号名和 cookies 都不能为空")
        try:
            client = P115API(cookies=cookies, qps=self.cfg.api_qps)
            client.check_login()
        except P115Error as exc:
            raise InstantError(f"账号「{name}」的 cookies 校验失败: {exc}") from exc
        except Exception as exc:
            raise InstantError(f"账号「{name}」的 cookies 无法使用: {exc}") from exc
        uid = str(getattr(client.client, "user_id", "") or "")
        self.db.upsert_account(name, cookies, uid)
        client._instant_account = name
        with self._clients_lock:
            self._clients[name] = client
        log.info("已添加秒传账号「%s」(uid=%s)", name, uid)
        return {"name": name, "uid": uid}

    def remove_account(self, name: str) -> bool:
        ok = self.db.delete_account(name)
        with self._clients_lock:
            self._clients.pop(name, None)
        return ok

    def list_accounts(self) -> list[dict[str, Any]]:
        """秒传账号列表：来自「115 助手」（可多选）。

        网页上「秒传账号」多选框保存的名单在 ``ui_settings.instant_accounts``：
        选了就只有这些账号能用来秒传（没选过则全部可用）。
        """
        accounts: list[dict[str, Any]] = []
        if self.accounts_provider is not None:
            try:
                accounts = self.accounts_provider()
            except Exception as exc:  # noqa: BLE001 - 取不到就退回本地表
                log.debug("取 115 助手账号失败: %s", exc)
                accounts = []
        if not accounts:
            accounts = self.db.list_accounts()
        allowed = self.allowed_accounts()
        if allowed is not None:
            accounts = [a for a in accounts if str(a.get("name") or "") in allowed]
        return accounts

    def allowed_accounts(self) -> set[str] | None:
        """网页上勾选的秒传账号名单；没勾选过（None）表示不限制。"""
        raw = self.db.get_meta("ui_settings", "")
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        picked = data.get("instant_accounts")
        if not isinstance(picked, list):
            return None
        # 空数组 = 用户没勾过任何账号，此时不限制（保持旧行为）
        if not picked:
            return None
        return {str(x) for x in picked}

    def cookies_for(self, account: str) -> str:
        """取某个账号的 cookies：先问「115 助手」，再问本地账号表。"""
        if self.cookie_provider is not None:
            try:
                cookies = self.cookie_provider(account)
            except Exception as exc:  # noqa: BLE001
                log.debug("取账号 %s 的 cookies 失败: %s", account, exc)
                cookies = ""
            if cookies:
                return cookies
        return self.db.get_account_cookies(account)

    def client(self, account: str, enforce_allowance: bool = True) -> P115API:
        """拿到目标账号的 P115API（带缓存，失败重读 cookies）。

        ``enforce_allowance=False`` 用于**源**账号与清理操作：
        网页上勾选的「秒传账号」名单只该限制「秒传到哪个账号」，
        不该限制「从哪个账号取文件」（源账号没被勾选也照样能读它的直链），
        也不该让「清空暂存目录」这种清理动作做不了。
        """
        allowed = self.allowed_accounts()
        if (enforce_allowance and allowed is not None
                and str(account or "") not in allowed):
            raise InstantError(f"账号「{account or '默认账号'}」没有被选为秒传账号")
        with self._clients_lock:
            client = self._clients.get(account)
        if client is not None:
            return client
        cookies = self.cookies_for(account)
        if not cookies:
            raise InstantError(f"没有名为「{account}」的秒传账号")
        client = P115API(cookies=cookies, qps=self.cfg.api_qps)
        # 记下这个客户端属于哪个账号：上传凭据按账号缓存，命名空间不能混。
        client._instant_account = str(account or "")
        with self._clients_lock:
            self._clients[account] = client
        return client

    def has_accounts(self) -> bool:
        return bool(self.list_accounts())

    # ------------------------------------------------------------------ 用户映射
    def set_user(self, emby_user_id: str, emby_user_name: str, account: str) -> None:
        emby_user_id = str(emby_user_id or "").strip()
        account = str(account or "").strip()
        if not emby_user_id:
            raise InstantError("Emby 用户 ID 不能为空")
        if account and not self.cookies_for(account):
            raise InstantError(f"没有名为「{account}」的秒传账号")
        self.db.set_emby_user(emby_user_id, emby_user_name, account)

    def list_users(self) -> list[dict[str, Any]]:
        return self.db.list_emby_users()

    def account_for_user(self, emby_user_id: str) -> str:
        rec = self.db.get_emby_user(str(emby_user_id or ""))
        return str(rec.get("account") or "") if rec else ""

    # ------------------------------------------------------------------ 暂存目录
    def _staging(self, account: str, create: bool = True) -> int:
        """秒传暂存目录的 cid。

        目录可以**手动设置**（网页上填路径或选目录），按账号分别记在
        ``instant_staging`` 这个 meta 里；没设置就用默认目录名
        （``INSTANT_DIR_NAME``）建在网盘根下。
        """
        client = self.client(account)
        cid = self._staging_cache.get(account)
        if cid:
            node = client.get_node(int(cid))
            if node is not None and node["is_dir"]:
                return int(cid)
            # 缓存的目录被删了/不是目录了：丢掉这条缓存，往下重新找
            self._staging_cache.pop(account, None)

        # 手动设置的目录：优先按路径找，找不到再按 cid 直接用
        custom = self.staging_dir(account)
        cid = 0
        if custom.get("cid"):
            try:
                cid = int(custom["cid"])
            except (TypeError, ValueError):
                cid = 0
        if cid:
            node = client.get_node(cid)
            if node is not None and node["is_dir"]:
                self._staging_cache[account] = cid
                return cid
        if custom.get("path"):
            found = client.find_child_dir(0, str(custom["path"]).strip("/"))
            if found:
                self._staging_cache[account] = found
                return found

        cid = client.find_child_dir(0, self.cfg.instant_dir_name)
        if not cid:
            if not create:
                raise InstantError(f"账号「{account}」还没有秒传暂存目录")
            cid = client.mkdir(0, self.cfg.instant_dir_name)
        self._staging_cache[account] = cid
        return cid

    # ------------------------------------------------------------ 暂存目录设置
    def staging_dir(self, account: str) -> dict[str, Any]:
        """某个账号的暂存目录设置：``{"path": "...", "cid": 123}``。

        存在 db 的 ``instant_staging`` meta 里（JSON，键是账号名）。
        """
        raw = self.db.get_meta("instant_staging", "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        item = data.get(account or "")
        return item if isinstance(item, dict) else {}

    def set_staging_dir(self, account: str, path: str = "",
                        cid: int = 0) -> dict[str, Any]:
        """设置某个账号的秒传暂存目录（路径或目录 id）。

        路径与 cid 给一个就行；都给空表示恢复默认目录。
        """
        account = (account or "").strip()
        raw = self.db.get_meta("instant_staging", "")
        try:
            data = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        path = (path or "").strip()
        cid = int(cid or 0)
        if path or cid:
            data[account] = {"path": path, "cid": cid}
        else:
            data.pop(account, None)
        self.db.set_meta("instant_staging", json.dumps(data, ensure_ascii=False))
        # 改了设置要把缓存清掉，否则还按旧的 cid 走
        self._staging_cache.pop(account, None)
        return self.staging_dir(account)

    # ------------------------------------------------------------------ 秒传
    def _rapid_upload(self, target: P115API, filename: str, sha1: str,
                      size: int, pid: int, source_pickcode: str,
                      source_account: str = "") -> dict[str, Any]:
        """发起一次秒传（带上传凭据 + 凭据轮换重试）。

        115 拒绝一次秒传有两种形状：一种是回错误码，另一种是照样回 200、
        再把 status 写成 1（statuscode != 0）。**两种都要当成「凭据可能过期」**
        来处理 —— 参考项目 junction 的注释写得很清楚：
        「115 对一份轮换过的 userkey 常常不报错，而是回一个 status=1 说
        秒传不可用。漏掉这一种，过期的凭据就会让这个网盘的每一次秒传都失败
        到进程重启为止」。

        所以：第一次用拿到的凭据试；不成功就**重新换一份凭据再试一次**，
        第二次还不成才是真的不行。

        另外，115 拒绝的原因有两种，处置方式不同：

        - **凭据被轮换** → 丢掉旧凭据、重换一份、再试一次；
        - **SHA1 被拒**（源盘把文件换了，记住的校验值过时了）→ 换凭据没有
          意义，要重新拿源盘当前的 SHA1 再来。参考项目 junction 对这一种
          是「丢掉记住的文件信息，重新问 115 拿新的 SHA1，当场再秒传一次」，
          这样源盘换了文件时观众这一集照样看得上，而不是白等一轮。
        """
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                cred = self._upload_cred(target)
            except InstantError as exc:
                if attempt == 2:
                    raise
                last_error = exc
                log.warning("取上传凭据失败，重试一次: %s", exc)
                continue
            try:
                resp = target.client.upload_file_init(
                    filename,
                    sha1,
                    size,
                    pid=pid,
                    read_range_bytes_or_hash=self._read_range_from_source(
                        source_pickcode, source_account, sha1=sha1
                    ),
                    # 上传凭据：跨账号秒传必须带上，115 才认这次上传
                    payload={
                        "userkey": cred["userkey"],
                        "user_id": cred["user_id"],
                    },
                )
            except Exception as exc:
                if attempt == 2:
                    raise InstantError(f"秒传请求失败: {exc}") from exc
                last_error = exc
                log.warning("秒传请求失败，换一份凭据重试: %s", exc)
                continue
            # statuscode 才是 115 的秒传结论（resp['state'] 只看 status 字段）
            if int(resp.get("statuscode") or 0) == 0:
                return resp
            if attempt == 2:
                return resp
            # 凭据被 115 轮换了：这一份不能再用，丢掉重换。
            self._forget_upload_cred(target)
            last_error = InstantError(
                f"秒传被拒（statuscode={resp.get('statuscode')} "
                f"{resp.get('statusmsg') or ''}）"
            )
            log.warning(
                "秒传被拒（statuscode=%s %s），换一份上传凭据再试一次",
                resp.get("statuscode"), resp.get("statusmsg") or "",
            )
        if last_error:
            raise last_error
        raise InstantError("秒传失败")

    def _read_range_from_source(self, source_pickcode: str,
                                source_account: str = "",
                                sha1: str = ""):
        """生成「按字节区间取文件内容」的回调（用**源账号**的直链）。

        源账号 = 文件原本所在的账号。115 STRM 生成的文件可能在子账号下
        （strm 带 /a/<账号名>/ 前缀），这时要用那个账号的凭据取直链 ——
        用主账号去取会拿不到（文件不在主账号的网盘里）。

        **缓存**：115 二次校验要的那一段，答案只跟文件内容有关，跟哪个账号、
        第几次播都无关。所以这里先查本地缓存：

        - 整个请求区间都在缓存里 → 直接用内存里的字节算 SHA1，一个字节都
          不用从源盘取；
        - 只有一部分在 → 缺的那部分才回源盘取，补齐后整段存进缓存。

        这就是 nextEmby 说的「用户间秒传加速」：第一个人播某部片子时答案就
        学到了，之后不管换哪个用户、还是进程重启过，同一份内容都不用再从
        源盘取一个字节。
        """
        api = self.api
        if source_account:
            try:
                # 源账号不受「秒传账号」名单限制：名单管的是秒传到哪，
                # 不是从哪读文件。
                api = self.client(source_account, enforce_allowance=False)
            except InstantError:
                api = self.api     # 拿不到就退回主账号

        sha1 = str(sha1 or "").upper()
        # 已经从源盘取过的块，按块起点记住，避免同一个区间被读两次。
        blocks: dict[int, bytes] = {}
        # 这次传输一共从源盘取了多少字节、命中了多少缓存 —— 日志要用。
        stats = {"fetches": 0, "bytes": 0, "cached": 0}

        def _fetch_range(url: str, headers: dict[str, str], spec: str) -> bytes:
            """从源盘取一段字节。"""
            with httpx.Client(follow_redirects=True, timeout=30) as http:
                resp = http.get(
                    url,
                    headers={
                        "User-Agent": self.cfg.play_user_agent,
                        "Range": f"bytes={spec}",
                        **{k: v for k, v in (headers or {}).items()
                           if k.lower() not in ("host", "user-agent")},
                    },
                )
            if resp.status_code not in (200, 206) or not resp.content:
                raise InstantError(
                    f"二次校验取数据失败（HTTP {resp.status_code}），请稍后重试"
                )
            stats["fetches"] += 1
            stats["bytes"] += len(resp.content)
            return resp.content

        def _cached_block(off: int) -> bytes | None:
            """从本地缓存取一块（内存 → 库）。"""
            if off in blocks:
                stats["cached"] += 1
                return blocks[off]
            if not sha1:
                return None
            data = self.db.digest_block_get(sha1, off)
            if data:
                stats["cached"] += 1
                blocks[off] = data
            return data

        def _remember_block(off: int, data: bytes) -> None:
            blocks[off] = data
            if sha1:
                self.db.digest_block_put(sha1, off, data)

        def read_range(sign_check: str) -> bytes:
            # sign_check 形如 "0-1048575"，也可能是 "0-"（读到文件尾）。
            try:
                start_s, _, end_s = str(sign_check).partition("-")
                start = int(start_s or 0)
                end = int(end_s) if end_s.strip() else start + PRECHECK_BYTES - 1
            except (TypeError, ValueError):
                # 区间规格看不懂就别自作主张，直接回源盘取。
                url, headers = api.download_url(
                    source_pickcode, user_agent=self.cfg.play_user_agent,
                    cache_seconds=120,
                )
                return _fetch_range(url, headers, str(sign_check))

            if end < start:
                end = start

            # 先看这一段是不是已经在本地了。
            have = bytearray()
            missing: list[int] = []
            pos = start
            while pos <= end:
                off = (pos // BLOCK_SIZE) * BLOCK_SIZE
                block_end = off + BLOCK_SIZE - 1
                data = _cached_block(off)
                if data is None:
                    if off not in missing:
                        missing.append(off)
                else:
                    # 这一块在缓存里：只取我们需要的那一段。
                    lo = max(pos, off) - off
                    hi = min(end, block_end) - off
                    have.extend(data[lo:hi + 1])
                pos = min(end, block_end) + 1

            if not missing:
                # 整段都在缓存里：源盘一个字节都不用碰。
                return bytes(have)

            # 缺的才回源盘取。**按块对齐整块取**：只取需要的那几十字节是省了
            # 这一次，但存不进缓存（凑不满一块），下次同一块还要再读一遍。
            # 整块取回来存住，之后 115 再挑这一块里的任何区间都不用碰源盘。
            url, headers = api.download_url(
                source_pickcode, user_agent=self.cfg.play_user_agent,
                cache_seconds=120,
            )
            for off in missing:
                block_end = off + BLOCK_SIZE - 1
                chunk = _fetch_range(url, headers, f"{off}-{block_end}")
                if len(chunk) < BLOCK_SIZE:
                    # 文件尾那半块是特例：存了也用不满，但存着无害。
                    pass
                _remember_block(off, chunk)
                # 把刚才这一段拼进结果。
                lo = max(start, off) - off
                hi = min(end, block_end) - off
                if lo < len(chunk):
                    have.extend(chunk[lo:hi + 1])

            out = bytes(have[:end - start + 1])
            if sha1:
                # 记下 115 这次挑的是哪一段：重复挑同一段是缓存能命中的前提。
                self.db.digest_note_range(sha1, f"{start}-{end}")
            return out

        read_range.stats = stats          # type: ignore[attr-defined]
        return read_range


    def _cred_id(self, target: P115API) -> str:
        """凭据缓存的键。

        源账号和目标账号是两个不同的 115 账号，各有各的 userkey，所以
        命名空间要分开 —— 否则一个账号的凭据会被另一个账号用掉。

        账号名优先取客户端上的标记；没有标记（外部直接塞进 ``_clients``
        的客户端）就从字典里反查一次，保证不同客户端不会共用同一份凭据。
        """
        name = str(getattr(target, "_instant_account", "") or "")
        if not name:
            with self._clients_lock:
                for key, value in self._clients.items():
                    if value is target:
                        name = str(key or "")
                        break
        return f"usr:{name}" if name else "usr:main"

    def _upload_cred(self, target: P115API) -> dict[str, str]:
        """取目标账号的上传凭据（userkey / user_id）。

        115 的秒传要带上传凭据：``upload_init`` 不带 userkey 时，跨账号秒传
        会被拒（同账号能过，因为凭据可以由 cookie 推断）。参考项目 junction
        把它叫 PrimeUpload —— 「换上传凭据是整条链上最慢的一跳」。

        115 会轮换 userkey，所以凭据要缓存：能命中本地就跳过这一跳，
        115 说这份不行时再丢掉重换一次（``_forget_upload_cred``）。
        """
        cred_id = self._cred_id(target)
        cached = self.db.upload_cred_get(cred_id)
        if cached and cached.get("userkey"):
            return {"userkey": cached["userkey"],
                    "user_id": cached.get("user_id") or ""}

        info = target.client.upload_info()
        if not isinstance(info, dict) or info.get("state") is False:
            raise InstantError("取上传凭据失败：115 没给回 userkey")
        userkey = str(info.get("userkey") or "").strip()
        user_id = str(info.get("user_id") or "").strip()
        if not userkey:
            raise InstantError("取上传凭据失败：响应里没有 userkey")
        self.db.upload_cred_put(cred_id, userkey, user_id)
        return {"userkey": userkey, "user_id": user_id}

    def _forget_upload_cred(self, target: P115API) -> None:
        """丢掉记住的上传凭据（115 拒绝了它，说明这份已经轮换掉了）。"""
        try:
            self.db.upload_cred_delete(self._cred_id(target))
        except Exception as exc:  # noqa: BLE001 - 记账失败不影响播放
            log.debug("丢弃上传凭据失败: %s", exc)

    def _re_resolve_source(self, source_pickcode: str,
                           source_account: str = "") -> dict[str, Any] | None:
        """重新问源盘：这个文件现在的 SHA1 / 大小是多少。

        115 拒绝一次秒传，几乎都是因为源盘把文件换了 —— 记住的那份 SHA1
        已经过时。这时候重新问一次 115 拿当前的校验值，这一集就能救回来。

        问不到就返回 None（照旧按失败处理）。
        """
        api = self.api
        if source_account:
            try:
                api = self.client(source_account, enforce_allowance=False)
            except InstantError:
                api = self.api
        getter = getattr(api, "file_by_pickcode", None)
        if not callable(getter):
            return None
        try:
            info = getter(source_pickcode)
        except Exception as exc:  # noqa: BLE001 - 查不到就按失败处理
            log.debug("重新解析源盘文件失败（%s）: %s", source_pickcode, exc)
            return None
        if not info:
            return None
        return info

    def resolve(self, sha1: str, size: int, name: str, account: str,
                source_pickcode: str = "",
                source_account: str = "") -> dict[str, Any]:
        """确保文件在目标账号存在，返回 instant 记录（含目标 pickcode）。

        ``account`` = 秒传到哪个账号；``source_account`` = 文件原本在哪个账号
        （取二次校验的数据要用它的直链）。
        """
        sha1 = str(sha1 or "").upper()
        if not sha1 or not size:
            raise InstantError("缺少文件的 sha1 / size，无法秒传")
        if not account:
            raise InstantError("未指定秒传账号")

        rec = self.db.get_instant(sha1, account)
        if rec and rec.get("pickcode"):
            client = self.client(account)
            node = client.get_node(int(rec["file_id"])) if rec.get("file_id") else None
            if node is not None and not node["is_dir"]:
                return rec

        key = (account, sha1)
        with self._locks_guard:
            lock = self._resolve_locks.setdefault(key, threading.Lock())
        with lock:
            rec = self.db.get_instant(sha1, account)
            if rec and rec.get("pickcode"):
                client = self.client(account)
                node = (client.get_node(int(rec["file_id"]))
                        if rec.get("file_id") else None)
                if node is not None and not node["is_dir"]:
                    return rec

            source_pickcode = source_pickcode or (rec or {}).get("source_pickcode") or ""
            if not source_pickcode:
                raise InstantError("缺少源文件的 pickcode，无法做二次校验")

            target = self.client(account)
            pid = self._staging(account)
            log.info("秒传开始: %s → 账号「%s」", name or sha1[:12], account)
            resp = self._rapid_upload(target, name or f"{sha1}.bin", sha1,
                                      int(size), pid, source_pickcode,
                                      source_account)
            if int(resp.get("statuscode") or 0) != 0:
                # 秒传是按 SHA1 校验的，被拒往往是源盘把文件换了 —— 记住的
                # SHA1 过时了。重新问一次源盘拿当前的 SHA1/大小，当场再秒传
                # 一次：源盘换了文件时，观众这一集照样看得上，而不是白等一轮。
                fresh = self._re_resolve_source(source_pickcode, source_account)
                if fresh and fresh.get("sha1") and fresh["sha1"] != sha1:
                    log.info(
                        "115 不认上一次的校验值（%s），按重新取到的 SHA1 再秒传一次",
                        source_pickcode,
                    )
                    sha1 = fresh["sha1"]
                    size = int(fresh.get("size") or size)
                    name = fresh.get("name") or name
                    resp = self._rapid_upload(target, name or f"{sha1}.bin",
                                              sha1, int(size), pid,
                                              source_pickcode, source_account)
            if resp.get("statuscode") != 0:
                raise InstantError(
                    f"秒传失败（statuscode={resp.get('statuscode')} "
                    f"{resp.get('statusmsg') or ''}）"
                    "——文件可能不在 115 的资源库里，或源文件取不到校验数据"
                )
            pickcode = str(resp.get("pickcode") or "")
            if not pickcode:
                raise InstantError("秒传返回里没有 pickcode")
            # 用目标账号按 pickcode 反查 file_id（115 可能已存在同文件，id 不一定新建）
            file_id = 0
            try:
                detail = target.client.fs_supervision({"pickcode": pickcode})
                payload = (detail or {}).get("data") or {}
                file_id = int(payload.get("file_id") or 0)
            except Exception as exc:  # noqa: BLE001 - 反查失败不致命
                log.debug("秒传后反查 file_id 失败: %s", exc)

            row = {
                "sha1": sha1,
                "account": account,
                "name": name,
                "size": int(size),
                "source_pickcode": source_pickcode,
                # 源账号要一起存：upsert 会覆盖同名字段，
                # 不写就把它冲成空了（下次播放取直链会用错账号）
                "source_account": source_account,
                "file_id": file_id,
                "pickcode": pickcode,
                "last_play_at": int(time.time()),
            }
            self.db.upsert_instant(row)
            log.info("秒传完成: %s → 账号「%s」（pickcode=%s）",
                     name or sha1[:12], account, pickcode)
            return self.db.get_instant(sha1, account) or row

    # ------------------------------------------------------------------ 播放
    def lookup(self, sha1: str, account: str) -> dict[str, Any] | None:
        return self.db.get_instant(sha1, account)

    def register(self, sha1: str, size: int, name: str, account: str,
                 source_pickcode: str, source_account: str = "") -> None:
        """登记一条「这个文件要用这个账号播」的映射（不秒传，播放时再做）。

        ``source_account`` = 文件原本所在的账号，播放时取二次校验数据要用它。
        """
        sha1 = str(sha1 or "").upper()
        if not sha1 or not account:
            return
        if self.db.get_instant(sha1, account) is None:
            self.db.upsert_instant({
                "sha1": sha1, "account": account, "name": name,
                "size": int(size), "source_pickcode": source_pickcode,
                "source_account": source_account,
            })

    def direct_url(self, sha1: str, account: str,
                   user_agent: str = "") -> tuple[str, dict[str, str]]:
        """拿目标账号的播放直链（结果进缓存）。"""
        rec = self.db.get_instant(sha1, account)
        if not rec or not rec.get("pickcode"):
            raise InstantError(f"文件还没有秒传到账号「{account}」")
        target = self.client(account)
        url, headers = target.download_url(
            rec["pickcode"],
            user_agent=user_agent or self.cfg.play_user_agent,
            cache_seconds=self.cfg.instant_cache_seconds,
        )
        self.db.upsert_instant({**rec, "last_play_at": int(time.time())})
        return url, headers

    def ensure_direct(self, sha1: str, account: str,
                      user_agent: str = "") -> tuple[str, dict[str, str]]:
        """确保文件已秒传，然后返回它的直链。

        ``direct_url`` 只负责「已经秒传过的」文件；预解析（Emby 播放改写）
        发生在播放器拿到地址**之前**，这时文件往往只登记过、还没秒传，
        直接用 ``direct_url`` 必然失败并回退成「本服务地址」。回退地址是
        容器内网/宿主机地址，播放器多半访问不到，用户看到的就是秒传不生效、
        一直转圈。所以这里补上「没有 pickcode 就先秒传一次」这一步。
        """
        sha1 = str(sha1 or "").upper()
        rec = self.db.get_instant(sha1, account)
        if rec is None:
            raise InstantError(f"还没有这个文件的秒传记录（账号「{account}」）")
        if not rec.get("pickcode"):
            rec = self.resolve(
                rec["sha1"], int(rec.get("size") or 0), str(rec.get("name") or ""),
                account, str(rec.get("source_pickcode") or ""),
                str(rec.get("source_account") or ""),
            )
            # 源盘换过文件时 resolve 会按新 SHA1 再秒传，记录主键也跟着变了
            sha1 = str(rec.get("sha1") or sha1).upper()
        return self.direct_url(sha1, account, user_agent=user_agent)

    # ------------------------------------------------------------------ 预缓存 / 清理
    def source_files(self, limit: int = 50) -> list[tuple[str, dict[str, Any]]]:
        """待预缓存的源文件：``[(源账号, 文件行), ...]``。

        **跨账号取**：115 STRM 生成的文件索引在**所属账号自己的库**里
        （每个账号一个独立索引库），只看本实例的库会一个文件都取不到。
        源账号要一起带上 —— 取二次校验数据时要用它的直链。

        源账号是空串表示「主服务这一套」（配置文件里的那个账号）。
        """
        limit = max(1, min(int(limit), 500))
        conn_rows = self.db._conn().execute(
            "SELECT id, name, sha1, size, pickcode FROM files"
            " WHERE sha1 != '' AND size > 0 ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = [("", dict(r)) for r in conn_rows]
        # 115 助手登记的其他账号：各自的索引库
        extra = self.other_sources()
        for name, sdb in extra:
            try:
                got = sdb._conn().execute(
                    "SELECT id, name, sha1, size, pickcode FROM files"
                    " WHERE sha1 != '' AND size > 0"
                    " ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            except Exception as exc:  # noqa: BLE001 - 单个库读不了不影响别的
                log.debug("读账号「%s」的索引失败: %s", name, exc)
                continue
            out.extend((name, dict(r)) for r in got)
        # 按更新时间倒序（取最近的文件），同时间按名字稳定排序
        out.sort(key=lambda item: (-int(item[1].get("updated_at") or 0),
                                   str(item[1].get("name") or "")))
        return out[:limit]

    def other_sources(self) -> list[tuple[str, Database]]:
        """其它账号的索引库：``[(账号名, db), ...]``。

        由「115 助手」（hub）通过 ``sources_provider`` 提供。
        """
        if self.sources_provider is None:
            return []
        try:
            got = self.sources_provider()
        except Exception as exc:  # noqa: BLE001 - 取不到就只用本库
            log.debug("取其它账号的索引库失败: %s", exc)
            return []
        out: list[tuple[str, Database]] = []
        for item in got or []:
            try:
                name, sdb = item
            except (TypeError, ValueError):
                continue
            if sdb is not None and sdb is not self.db:
                out.append((str(name or ""), sdb))
        return out

    def prewarm(self, account: str, limit: int = 50) -> dict[str, Any]:
        """预缓存：把索引里的文件提前秒传到目标账号，播放时零等待。

        按文件在索引里的更新时间倒序取前 N 个（有 sha1 的才参与）。
        """

        items = self.source_files(limit)
        ok = fail = 0
        errors: list[str] = []
        for source_account, row in items:
            try:
                self.resolve(row["sha1"], int(row["size"]), row["name"],
                             account, row["pickcode"], source_account)
                ok += 1
            except Exception as exc:  # noqa: BLE001 - 单个失败继续
                fail += 1
                if len(errors) < 3:
                    errors.append(f"{row['name']}: {exc}")
            time.sleep(0.3)
        # 预缓存是最容易把校验缓存撑大的动作（一批文件各塞一块进来），
        # 做完顺手压回预算内。后台线程也会做，这里只是让效果立刻可见。
        try:
            self.digest_sweep()
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响预缓存结果
            log.debug("预缓存后清理校验缓存失败: %s", exc)
        return {"ok": ok, "failed": fail, "total": len(items), "errors": errors}

    def clean(self, account: str) -> dict[str, Any]:
        """清空某个账号的秒传暂存目录（记录一并删除，下次播放会重新秒传）。"""
        # 清理是维护动作，不该被「秒传账号」名单拦住
        client = self.client(account, enforce_allowance=False)
        pid = self._staging(account, create=False)
        removed = 0
        stack = [pid]
        while stack:
            cid = stack.pop()
            dirs, files = client.list_dir(cid, page_workers=0, cooldown=0.0)
            if files:
                client.delete_files([int(f["id"]) for f in files])
                removed += len(files)
            stack.extend(int(d["id"]) for d in dirs)
        for rec in self.db.list_instant(account):
            self.db.delete_instant(rec["sha1"], account)
        return {"removed": removed, "account": account}

    def status(self) -> dict[str, Any]:
        return {
            # 用 list_accounts()（115 助手的账号），不是 db.list_accounts()：
            # 后者只查本地 accounts 表，子账号没写进去时就是空列表，
            # 页面上的「秒传账号」会一个都显示不出来。
            # 「主账号」这个虚拟项已随主账号设置一起移除，这里只列真实账号。
            "accounts": list(self.list_accounts()),
            "users": self.db.list_emby_users(),
            "instant_dir": self.cfg.instant_dir_name,
            "staging": {
                a.get("name", ""): self.staging_dir(a.get("name", ""))
                for a in list(self.list_accounts())
            },
            "cache_seconds": self.cfg.instant_cache_seconds,
            "records": self.db.instant_count(),
            # 校验缓存：第二个人播同一部片子时靠它省掉整个源盘读取
            "digest": self.db.digest_stats(),
            # 缓存上限（MB），0 = 不限制。页面上要让用户看得见这个数
            "digest_max_mb": int(getattr(self.cfg, "instant_digest_max_mb", 0) or 0),
        }

    # ------------------------------------------------------------------ 校验缓存
    def digest_stats(self) -> dict[str, int]:
        """校验缓存的占用情况。"""
        return self.db.digest_stats()

    def digest_clear(self) -> dict[str, int]:
        """清空校验缓存。

        这些缓存只是用来省一次源盘读取，清掉最坏结果是下次多读一次，
        不会影响任何已经能播的文件。
        """
        blocks = self.db.digest_clear()
        log.info("已清空秒传校验缓存（%d 块）", blocks)
        return {"blocks": blocks}

    def digest_sweep(self, max_bytes: int = 0) -> dict[str, int]:
        """把校验缓存压在字节预算内（先丢最久没用的）。

        ``max_bytes`` 不给就用配置里的上限（``INSTANT_DIGEST_MAX_MB``）；
        配置成 0 表示不限制，那就什么也不做。
        """
        if max_bytes:
            limit = int(max_bytes)
        else:
            limit_mb = int(getattr(self.cfg, "instant_digest_max_mb", 0) or 0)
            if limit_mb <= 0:
                return {"dropped": 0, "freed": 0}
            limit = limit_mb * (1 << 20)
        dropped, freed = self.db.digest_sweep(limit)
        if dropped:
            log.info("秒传校验缓存超出上限，丢掉 %d 块（释放 %.1f MB）",
                     dropped, freed / (1 << 20))
        return {"dropped": dropped, "freed": freed}

    # ------------------------------------------------------------------ 后台清理
    def run_forever(self, stop: threading.Event) -> None:
        """后台定期把校验缓存压在字节预算内。

        这一层缓存的是文件内容，按内容算大小、只增不减 —— 看片越多它越大。
        不设上限的话 SQLite 会被撑起来，所以这里定期清一批最久没用的。

        丢掉的块只是「省一次源盘读取」这一层没了：下一次播放那一个区间
        重新从源盘取一次就好，绝不会把一次播放搞坏。所以这个清理可以放心做。
        """
        limit_mb = int(getattr(self.cfg, "instant_digest_max_mb", 0) or 0)
        if limit_mb <= 0:
            # 0 = 不限制：那就没什么可清的，直接退出，不占一个线程。
            log.info("秒传校验缓存不设上限（INSTANT_DIGEST_MAX_MB=0），跳过定期清理")
            return
        limit = limit_mb * (1 << 20)
        interval = float(getattr(self.cfg, "instant_digest_clean_interval", 3600.0))
        poll = float(getattr(self.cfg, "instant_digest_poll_interval", 300.0))
        if interval <= 0:
            interval = 3600.0
        if poll <= 0:
            poll = 300.0
        log.info(
            "秒传校验缓存清理任务启动：上限 %d MB，每 %.0f 分钟检查一次",
            limit_mb, interval / 60,
        )
        last_clean = 0.0
        while not stop.is_set():
            now = time.time()
            if now - last_clean >= interval:
                last_clean = now
                try:
                    stats = self.digest_stats()
                    if stats.get("bytes", 0) > limit:
                        self.digest_sweep(limit)
                except Exception as exc:  # noqa: BLE001 - 清理失败不影响播放
                    log.warning("清理秒传校验缓存失败: %s", exc)
            stop.wait(poll)


__all__ = ["InstantError", "InstantManager"]
