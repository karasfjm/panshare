"""多账号：每个 115 账号一个独立引擎，同时扫描生成 strm 并用各自账号播放。

- 主账号行为完全不变（strm 目录、播放路由、数据文件都保持原样）；
- 子账号在网页「115 多账号」卡片里添加（粘贴 cookies），每个子账号：
  - 数据目录 DATA_DIR/accounts/<名字>/（独立索引库、ed2k/分享设置）；
  - strm 输出到 STRM_ROOT/<名字>/ 子树；
  - 独立的 Scanner / Watcher / ed2k / 分享 线程，各自限速互不影响；
  - 播放路由带 /a/<名字> 前缀（/p115/a/<名字>/play/<pickcode>），
    用该账号自己的 cookies 解析直链；
  - 有完整的管理页（/a/<名字>/），目录管理、扫描、ed2k、分享都能独立操作。
- Emby 反代自动识别各账号 strm 树下的文件并改写成对应账号的直链。

账号登记持久化在 DATA_DIR/accounts.json；cookies 落在
DATA_DIR/accounts/<名字>/115-cookies.txt（115 刷新后自动回写）。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .config import Config
from .copies import CopyManager
from .db import Database
from .ed2k import Ed2kManager
from .emby import EmbyProxy
from .instant import InstantManager
from .p115api import P115API
from .scanner import Scanner
from .share import ShareManager
from .strm import StrmStore
from .watcher import Watcher

log = logging.getLogger(__name__)

# 账号名会变成 strm 子目录和 URL 前缀：只允许中文、字母、数字、中划线、下划线，
# 且以中文/字母/数字开头（避免 -xxx 这种以符号开头的怪目录名）
NAME_RE = re.compile(r"^[0-9A-Za-z\u4e00-\u9fff][0-9A-Za-z_\-\u4e00-\u9fff]{0,31}$")
NAME_HINT = "账号名支持中文、字母、数字、中划线、下划线，1-32 位，且不能用符号开头"
# 子账号名字会变成 strm 子目录和 URL 前缀，不能和主账号的保留目录撞车
RESERVED_NAMES = {"ed2k", "share", "instant", "accounts"}

REGISTRY_FILE = "accounts.json"


def normalize_cookies(raw: str) -> str:
    """把用户粘贴的 cookies 归一化成「k=v; k=v」单行格式。

    兼容：带「Cookie: 」前缀、从开发者工具复制的多行格式、多余空白。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    # 去掉可能的「Cookie:」前缀
    low = text.lower()
    if low.startswith("cookie:"):
        text = text[len("cookie:"):].strip()
    # 多行粘贴：换行按「; 」处理（cookie 值本身不含换行）
    text = text.replace("\r\n", "; ").replace("\r", "; ").replace("\n", "; ")
    # 压掉键值对之间多余空白
    parts = [p.strip() for p in text.split(";") if p.strip()]
    return "; ".join(parts)


class AccountError(Exception):
    pass


@dataclass
class Engine:
    """一个账号的完整运行时（主账号和子账号通用）。"""

    name: str                       # 主账号为 ""
    cfg: Config
    db: Database
    api: P115API
    store: StrmStore
    scanner: Scanner
    watcher: Watcher
    ed2k: Ed2kManager
    share: ShareManager
    instant: InstantManager
    copies: Any = None              # 同播复制（账号级，见 app/copies.py）
    state: dict[str, Any] = field(default_factory=dict)
    uid: str = ""                   # 115 用户 ID（strm 末尾标识 + 播放时定位账号）
    proxy: EmbyProxy | None = None  # Emby 反代（子账号仅用于 strm 路径解析）
    stop: threading.Event = field(default_factory=threading.Event)
    app: Any = None                 # 挂载到主服务的 FastAPI 子应用

    @property
    def display(self) -> str:
        return self.name or "主账号"


def build_engine(cfg: Config, name: str = "",
                 shared_db: Database | None = None) -> Engine:
    """按 cfg 构造一个账号引擎（含登录校验，失败抛 P115Error）。

    ``shared_db`` 是主账号的库：115 STRM 配置（含每条配置的「STRM 请求头」）
    都存在主库里，子账号的扫描器只读它来找自己那条配置。
    """
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.strm_root.mkdir(parents=True, exist_ok=True)

    db = Database(cfg.data_dir / "p115-strm.db")
    if not db.get_meta("roots_seeded"):
        for cid in cfg.root_cids:
            db.add_root(cid, "")
        db.set_meta("roots_seeded", "1")

    api = P115API(cookies=cfg.cookies, cookie_file=cfg.cookie_path, qps=cfg.api_qps)
    api.check_login()  # 登录失败直接抛错，调用方决定怎么处理

    # 115 用户 ID：strm 末尾用它标识归属，播放时按它找回这个账号的引擎
    uid = api.user_id()
    store = StrmStore(
        cfg.strm_root,
        base_url=cfg.base_url,
        auth_token=cfg.auth_token,
        template=cfg.play_url_template,
        play_url_ext=cfg.play_url_ext,
        ed2k_dir=cfg.ed2k_strm_dir,
        # 不再按账号名加 /a/<名字> 前缀：strm 末尾的 uid 已经能定位账号，
        # 所有账号的播放地址因此长得完全一样，Emby 那边不用关心是谁的盘
        route_prefix="",
        account_uid=uid,
    )
    scanner = Scanner(cfg, db, api, store,
                      configs_db=shared_db, account=name)
    watcher = Watcher(cfg, db, api, store, scanner, shared_db=shared_db)
    ed2k = Ed2kManager(cfg, db, api, store)
    share = ShareManager(cfg, db, api, store)
    instant = InstantManager(cfg, db, api)
    # 同播复制是账号级的：strm 播放、ed2k 播放都走同一个实例、同一份配置
    copies = CopyManager(db, api, cfg)
    ed2k.copies = copies
    try:
        last_scan = json.loads(db.get_meta("last_scan_stats", "{}"))
    except json.JSONDecodeError:
        last_scan = {}
    return Engine(name=name, uid=uid, cfg=cfg, db=db, api=api, store=store,
                  scanner=scanner, watcher=watcher, ed2k=ed2k, share=share,
                  instant=instant, copies=copies,
                  state={"version": "", "scan": last_scan})


def need_initial_scan(cfg: Config, db: Database) -> bool:
    if cfg.full_scan_on_start == "always":
        return True
    if cfg.full_scan_on_start == "never":
        return False
    return not db.get_meta("last_full_scan") or db.stats()["files"] == 0


def _replace(api: Any, cookies: str) -> None:
    """给 api 换一份 cookies（老对象没有这个方法就跳过）。"""
    fn = getattr(api, "replace_cookies", None)
    if callable(fn):
        fn(cookies)


class AccountHub:
    """子账号登记表 + 引擎生命周期管理。"""

    def __init__(self, cfg: Config, default: Engine,
                 engine_builder: Callable[[Config, str], Engine] | None = None,
                 auto_start: bool = True,
                 shared_db: Database | None = None):
        self.cfg = cfg
        self.default = default
        self._builder = engine_builder or build_engine
        # 主账号的库：115 STRM 配置都在这里，子账号扫描器只读它
        self.shared_db = shared_db
        self.auto_start = auto_start
        self._engines: dict[str, Engine] = {}
        self._registry: list[dict[str, str]] = []
        self._lock = threading.RLock()
        self._app: Any = None
        self.registry_path = cfg.data_dir / REGISTRY_FILE
        # 原始凭据（配置文件里那个）：取消主账号时要恢复成它
        self._origin_cookies = cfg.cookies or ""

    # ---------------------------------------------------------------- 登记
    def load(self) -> None:
        """启动时按登记表恢复子账号（登录失败的标记 error，不阻塞启动）。"""
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
            self._registry = list(data.get("accounts") or [])
        except (OSError, ValueError):
            self._registry = []
        for item in self._registry:
            name = str(item.get("name") or "")
            if not name:
                continue
            try:
                eng = self._build_and_register(name)
                item["uid"] = eng.uid
                item.pop("error", None)
                log.info("子账号「%s」已加载（uid=%s）", name, eng.uid or "?")
            except Exception as exc:  # noqa: BLE001 - 单个账号失败不影响其他
                item["status"] = "error"
                item["error"] = str(exc)
                log.warning("子账号「%s」加载失败（网页里可删除后重加）: %s", name, exc)
        self._save()
        self._drop_main_marker()

    def _drop_main_marker(self) -> None:
        """清掉旧版本留下的「主账号」标记。

        「设为主账号」这个设置已经移除：默认引擎永远用配置文件里那个账号，
        各工具页自己选要用哪个号。老库里的标记如果不清理，界面还会显示
        「主账号」徽标，让人以为有东西没生效。
        """
        try:
            if self.default.db.get_meta("main_account", ""):
                self.default.db.set_meta("main_account", "")
                log.info("已清除旧版本遗留的主账号标记（该设置已移除）")
        except Exception as exc:  # noqa: BLE001 - 清不掉不影响启动
            log.warning("清除主账号标记失败: %s", exc)

    def _save(self) -> None:
        try:
            self.registry_path.parent.mkdir(parents=True, exist_ok=True)
            self.registry_path.write_text(
                json.dumps({"accounts": self._registry}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("保存账号登记表失败: %s", exc)

    def _sub_cfg(self, name: str, cookie_file: Path) -> Config:
        return replace(
            self.cfg,
            cookies="",
            cookie_file=cookie_file,
            data_dir=self.cfg.data_dir / "accounts" / name,
            # strm 输出目录不再按账号名加一层前缀：每条 115 STRM 配置里的
            # 「本地 STRM 目录（映射目录）」说什么就是什么，所有账号共用同一个
            # strm 根 —— 否则用户在配置里填 /115-原档/影音，实际却落到
            # <STRM_ROOT>/<账号名>/115-原档/影音，Emby 要挂两个目录才看得到。
            strm_root=self.cfg.strm_root,
            root_cids=(),                 # 子账号目录在管理页里自己添加
            roots={},                     # 不能共享主账号的可变字典
            emby_enabled=False,           # Emby 由主引擎的反代统一处理
        )

    def _build_and_register(self, name: str) -> Engine:
        cookie_file = self.cfg.data_dir / "accounts" / name / "115-cookies.txt"
        sub_cfg = self._sub_cfg(name, cookie_file)
        if self._builder is build_engine and self.shared_db is not None:
            eng = self._builder(sub_cfg, name, shared_db=self.shared_db)
        else:
            eng = self._builder(sub_cfg, name)
        eng.state["version"] = self.default.state.get("version", "")
        # 子账号的 proxy 要用**主服务的 instant**：Emby 用户 → 秒传账号的
        # 映射存在主库的 emby_users 表里，用子账号自己的 instant 就查不到，
        # 映射形同失效（子账号的 strm 播放时不会走秒传）。
        # instant 只用它的 db（查映射）与 provider（取 cookies），
        # 秒传目标仍然是映射里指定的那个账号，不受这里影响。
        eng.proxy = EmbyProxy(
            self.cfg, eng.db, eng.api, eng.store, self.default.instant, eng.ed2k,
            account=name,
        ) if self.cfg.emby_enabled and self.cfg.emby_url else None
        with self._lock:
            self._engines[name] = eng
        if self._app is not None:
            self._mount(eng)
        return eng

    # ---------------------------------------------------------------- 增删
    def _auto_name(self) -> str:
        """给没填名字的账号取一个不冲突的名字（115 / 115-2 / 115-3 …）。"""
        taken = {str(item.get("name") or "") for item in self._registry}
        taken |= set(self._engines)
        if "115" not in taken:
            return "115"
        for i in range(2, 100):
            cand = f"115-{i}"
            if cand not in taken:
                return cand
        raise AccountError("账号数量过多，请手动填写账号名")

    def add(self, name: str, cookies: str) -> Engine:
        name = (name or "").strip()
        cookies = normalize_cookies(cookies)
        if not cookies:
            raise AccountError("cookies 不能为空")
        if not name:
            # 首次运行最常见的情况：用户只粘贴 cookies，不想给账号起名字。
            # 自动取一个不冲突的名字（115 / 115-2 / 115-3 …）
            name = self._auto_name()
        if not NAME_RE.match(name):
            raise AccountError(NAME_HINT)
        if name.lower() in RESERVED_NAMES:
            raise AccountError(f"账号名不能使用保留字: {name}")
        with self._lock:
            if self.get(name) is not None or any(
                item.get("name") == name for item in self._registry
            ):
                raise AccountError(f"账号「{name}」已存在")
            cookie_file = self.cfg.data_dir / "accounts" / name / "115-cookies.txt"
            cookie_file.parent.mkdir(parents=True, exist_ok=True)
            cookie_file.write_text(cookies + "\n", encoding="utf-8")
            try:
                eng = self._build_and_register(name)
            except Exception:
                shutil.rmtree(cookie_file.parent, ignore_errors=True)
                raise
            self._registry = [
                item for item in self._registry if item.get("name") != name
            ]
            self._registry.append({"name": name, "status": "ok", "uid": eng.uid})
            self._save()
        if self.auto_start:
            self.start_workers(eng)
        log.info("子账号「%s」添加成功，strm 输出: %s", name, eng.cfg.strm_root)
        return eng

    def remove(self, name: str) -> None:
        with self._lock:
            eng = self._engines.pop(name, None)
            self._registry = [
                item for item in self._registry if item.get("name") != name
            ]
            self._save()
        if eng is not None:
            eng.stop.set()
            eng.scanner.stop()
            try:
                eng.db.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("关闭账号「%s」数据库失败: %s", name, exc)
            if self._app is not None:
                self._unmount(name)
            # strm 根现在和主账号共用，绝不能整个删掉（会清空所有人的 strm）
            if str(eng.cfg.strm_root) != str(self.default.cfg.strm_root):
                shutil.rmtree(eng.cfg.strm_root, ignore_errors=True)
            shutil.rmtree(eng.cfg.data_dir, ignore_errors=True)
        log.info("子账号「%s」已删除（本地 strm 与索引一并清理）", name)

    # ---------------------------------------------------------------- 查询
    def get(self, name: str) -> Engine | None:
        """按名字取引擎。

        主账号也可能有名字（用户把它设为主账号之后），所以先看子账号表，
        再看主账号 —— 两者都不会重名。
        """
        eng = self._engines.get(name)
        if eng is not None:
            return eng
        if self.default is not None and self.default.name == name:
            return self.default
        return None

    def by_uid(self, uid: str) -> Engine | None:
        """按 115 用户 ID 找账号（strm 末尾那串数字就是它）。

        strm 里不再写账号名，所以播放时只能靠 uid 反查；
        查不到（老 strm / 账号被删）时返回 None，由调用方回落默认账号。
        """
        uid = str(uid or "").strip()
        if not uid:
            return None
        for eng in self._engines.values():
            if str(eng.uid or "") == uid:
                return eng
        if self.default is not None and str(self.default.uid or "") == uid:
            return self.default
        return None

    def uid_of(self, name: str) -> str:
        eng = self.get(name or "")
        if eng is not None:
            return str(eng.uid or "")
        for item in self._registry:
            if str(item.get("name") or "") == str(name or ""):
                return str(item.get("uid") or "")
        return ""

    def refresh_uid(self, eng: Engine) -> str:
        """cookies 换过之后重新取一次 uid，并同步到 strm 写出器。"""
        uid = ""
        try:
            uid = eng.api.user_id()
        except Exception as exc:  # noqa: BLE001 - 取不到不影响服务
            log.warning("账号「%s」获取 115 用户 ID 失败: %s", eng.display, exc)
        eng.uid = uid
        if getattr(eng, "store", None) is not None:
            eng.store.account_uid = uid
        for item in self._registry:
            if str(item.get("name") or "") == str(eng.name or ""):
                item["uid"] = uid
        return uid

    def main_account_name(self) -> str:
        """兼容老调用方：主账号设置已移除，永远返回空串。

        默认引擎固定使用配置文件里那个账号（主账号位），界面不再有
        「谁是主账号」这个概念。保留这个方法只是为了让还没更新的
        调用方不炸。
        """
        return ""

    def engines(self) -> dict[str, Engine]:
        return dict(self._engines)

    def count(self) -> int:
        return len(self._engines)

    def resolve_strm_rel(self, rel: str, user_id: str) -> str | None:
        """Emby 反代用：rel 相对**共用**的 STRM_ROOT，问出它属于哪个账号。

        strm 输出目录现在完全由每条 115 STRM 配置里的「本地 STRM 目录」决定，
        不再按账号名分一层，所以没有「看到前缀就知道是谁」这回事了：
        先按老布局（`<账号名>/...`）试一次，再逐个账号在自己的索引里查。
        """
        rel = rel.strip("/")

        def ask(eng: Engine, sub_rel: str) -> str | None:
            if eng.proxy is None:
                return None
            try:
                return eng.proxy._from_strm_rel(sub_rel, user_id)
            except Exception as exc:  # noqa: BLE001 - 单个账号失败不影响其余
                log.warning("账号「%s」strm 解析失败 %s: %s", eng.name, sub_rel, exc)
                return None

        for name, eng in self._engines.items():
            if rel == name or rel.startswith(name + "/"):
                got = ask(eng, rel[len(name):].lstrip("/"))
                if got:
                    return got
        for _name, eng in self._engines.items():
            got = ask(eng, rel)
            if got:
                return got
        return None

    def status(self) -> list[dict[str, Any]]:
        out = []
        for item in self._registry:
            name = str(item.get("name") or "")
            eng = self._engines.get(name)
            row: dict[str, Any] = {
                "name": name, "ok": eng is not None,
                "uid": str((eng.uid if eng is not None else item.get("uid")) or ""),
            }
            if item.get("error"):
                row["error"] = item["error"]
            if eng is not None:
                scan = eng.state.get("scan") or {}
                row.update({
                    "files": eng.db.stats()["files"],
                    "strm_root": str(eng.cfg.strm_root),
                    "last_scan": scan.get("finished_at") or "",
                })
            out.append(row)
        return out

    # ------------------------------------------------------------------ 显示名
    def display_name(self, name: str) -> str:
        """账号的显示名（网页上可以改，不影响引擎内部的账号名）。"""
        return self.default.db.get_account_display(name)

    def set_display_name(self, name: str, display: str) -> None:
        self.default.db.set_account_display(name, display)

    # ------------------------------------------------------------------ cookies
    def cookies_for(self, name: str) -> str:
        """取某个账号的 cookies（秒传等「账号来自 115 助手」的功能要用）。

        name 为空 = 配置文件里那个账号（内部名是空串的主账号位）。
        主账号设置已移除，这里不再有「标记指向别的账号」的情况。
        """
        if not name:
            # 读 cookie 文件而不是 cfg.cookies：环境变量优先时二者可能不同步，
            # 网页上改过 cookies 后文件才是最新的。
            return self._read_cookie_file(self.cfg.cookie_path) or self._origin_cookies
        eng = self._engines.get(name)
        if eng is None:
            return ""
        return self._read_cookie_file(eng.cfg.cookie_path)

    @staticmethod
    def _read_cookie_file(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def main_cookie_path(self) -> Path:
        """主账号 cookies 的落盘位置。

        主账号的 cookies 来自 P115_COOKIES 环境变量或 115-cookies.txt 文件。
        环境变量优先时改文件没用，所以切主账号会先把环境变量的值写进文件，
        再把文件当作唯一来源。
        """
        return self.cfg.cookie_path

    def set_main_cookies(self, cookies: str) -> None:
        """更新主账号的 cookies（立即生效，不用重启容器）。"""
        cookies = normalize_cookies(cookies)
        if not cookies:
            raise AccountError("cookies 不能为空")
        path = self.main_cookie_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cookies + "\n", encoding="utf-8")
        # 环境变量优先的话，内存里的 cfg.cookies 还是旧的 —— 一并更新
        self.cfg.cookies = cookies
        _replace(self.default.api, cookies)
        uid = self.refresh_uid(self.default)
        self._mark_login(True, uid)
        log.info("主账号 cookies 已更新（%d 字符）", len(cookies))

    def clear_main_cookies(self) -> None:
        """清掉主账号的 cookies（等于删除这个账号）。

        之后扫描/转存都会跳过，直到重新填上 cookies；服务本身照常运行。
        """
        path = self.main_cookie_path()
        try:
            path.write_text("", encoding="utf-8")
        except OSError as exc:
            raise AccountError(f"清空主账号 cookies 失败: {exc}") from exc
        self.cfg.cookies = ""
        # 内存里的客户端也要退回未登录，否则「删了还能用」会让人以为没删掉
        _replace(self.default.api, "")
        self.default.uid = ""
        if getattr(self.default, "store", None) is not None:
            self.default.store.account_uid = ""
        self._mark_login(False, "")
        log.info("主账号 cookies 已清空")

    def _mark_login(self, ok: bool, uid: str, detail: str = "") -> None:
        """把登录状态写回 state（网页顶部的提示条读它）。"""
        state = getattr(self.default, "state", None)
        if isinstance(state, dict):
            state["login"] = {"ok": ok, "uid": uid, "detail": detail}

    def promote_to_main(self, name: str) -> str:
        """已移除：「设为主账号」不再支持。

        这个设置的作用是让默认引擎改用某个子账号的凭据（默认引擎跑
        Emby 反代、默认扫描、转存兜底）。实际使用中它没有必要：

        * 每个工具页（分享 / ed2k / 秒传 / 115 STRM）本来就能单独选账号；
        * 想换默认身份，改 ``P115_COOKIES`` / ``115-cookies.txt`` 即可。

        留着这个方法只为了老版本前端或脚本调用时报一个清楚的错，
        而不是 AttributeError。
        """
        raise AccountError("「设为主账号」功能已移除：请在各工具页里直接选择账号")

    def test_cookies(self, name: str, probe: str | None = None) -> dict[str, Any]:
        """测试 cookies 是否还能用（不改任何状态）。

        ``probe`` 给了就测这串（网页上「先改再测」用），否则测账号当前保存的。
        """
        cookies = (probe or "").strip() or self.cookies_for(name)
        if not cookies:
            return {"ok": False, "detail": "没有 cookies"}
        api = P115API(cookies=cookies, qps=self.cfg.api_qps)
        try:
            api.check_login()
        except Exception as exc:  # noqa: BLE001 - 失败原因直接回给网页
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": "cookies 可用"}

    # ------------------------------------------------------------ 挂载/线程
    def bind_app(self, app: Any) -> None:
        self._app = app
        for eng in self._engines.values():
            self._mount(eng)

    def _mount(self, eng: Engine) -> None:
        if self._app is None or eng.app is not None:
            return
        from .server import create_app

        # 子账号只提供 API（前端统一在主页面里按账号切换），
        # spa_root=False：子账号根路径跳回主页面，且不重复挂载 /static
        sub_app = create_app(
            eng.cfg, eng.db, eng.api, eng.store, eng.scanner, eng.watcher,
            eng.state, eng.ed2k, eng.share, eng.instant, None, None,
            spa_root=False,
        )
        self._app.mount(f"/a/{eng.name}", sub_app)
        eng.app = sub_app
        log.info("子账号「%s」API: /a/%s/api/…", eng.name, eng.name)

    def _unmount(self, name: str) -> None:
        if self._app is None:
            return
        prefix = f"/a/{name}"
        self._app.router.routes = [
            r for r in self._app.router.routes
            if getattr(r, "path", "") != prefix
        ]

    def start_workers(self, eng: Engine) -> None:
        """启动一个引擎的后台线程（初始扫描 + 增量监听 + ed2k + 分享补齐）。"""
        eng.stop.clear()

        def engine_worker() -> None:
            try:
                if eng.cfg.mode == "serve":
                    return
                if need_initial_scan(eng.cfg, eng.db):
                    eng.state["scan"] = eng.scanner.run(full=True).as_dict()
                eng.watcher.run_forever(eng.stop)
            except Exception:
                log.exception("账号「%s」后台任务异常退出", eng.display)

        threading.Thread(target=engine_worker, name=f"engine-{eng.name or 'main'}",
                         daemon=True).start()
        threading.Thread(target=eng.ed2k.run_forever, args=(eng.stop,),
                         name=f"ed2k-{eng.name or 'main'}", daemon=True).start()
        threading.Thread(target=_share_restore, args=(eng,),
                         name=f"share-restore-{eng.name or 'main'}",
                         daemon=True).start()

    def start_all(self) -> None:
        for eng in self._engines.values():
            self.start_workers(eng)

    def stop_all(self) -> None:
        for eng in self._engines.values():
            eng.stop.set()
            eng.scanner.stop()


def _share_restore(eng: Engine) -> None:
    try:
        eng.share.restore_strm()
    except Exception as exc:  # noqa: BLE001
        log.warning("账号「%s」补齐分享 strm 失败: %s", eng.display, exc)


__all__ = [
    "NAME_RE",
    "RESERVED_NAMES",
    "AccountError",
    "AccountHub",
    "Engine",
    "build_engine",
    "need_initial_scan",
]
