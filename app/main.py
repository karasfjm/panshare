"""入口：按 MODE 运行全量扫描 / 增量监听 / HTTP 服务。"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading

from . import __version__
from .config import Config, load_config
from .copies import CopyManager
from .db import Database
from .ed2k import Ed2kManager
from .emby import EmbyProxy
from .instant import InstantManager
from .p115api import P115API, WafBlocked
from .plugins import PluginManager
from .scanner import Scanner
from .server import create_app
from .share import ShareManager
from .strm import StrmStore
from .watcher import Watcher

log = logging.getLogger("PanShare")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def banner(cfg: Config) -> None:
    log.info("PanShare %s 启动", __version__)
    log.info("模式=%s 初始根目录=%s 输出=%s", cfg.mode, list(cfg.root_cids), cfg.strm_root)
    log.info(
        "扫描: app=%s QPS上限=%.1f 目录并发=%d 翻页并发=%d | 事件: app=%s 间隔=%.1fs 降级补扫=%.0fs",
        cfg.scan_app, cfg.api_qps, cfg.scan_workers, cfg.page_workers,
        cfg.event_app, cfg.event_interval, cfg.fallback_interval,
    )
    if cfg.play_url_template:
        log.info("播放地址模板: %s", cfg.play_url_template)
    else:
        log.info("播放地址: %s/p115/play/<pickcode>（%s）", cfg.base_url, cfg.play_mode)
    if cfg.ed2k_enabled:
        log.info(
            "ed2k: 转存目录=%s strm 目录=%s 保留=%.1f 小时",
            cfg.ed2k_dir_cid or f"自动创建「{cfg.ed2k_dir_name}」",
            cfg.strm_root / cfg.ed2k_strm_dir,
            cfg.ed2k_ttl_hours,
        )
    log.info("多账号: 配置文件账号 + 网页「115 多账号」卡片添加的账号（各账号独立扫描/播放）")
    log.info("插件: 「插件」页按需安装（天翼云盘 / 通知与联动 / 企微可信IP）")


def build(cfg: Config):
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.strm_root.mkdir(parents=True, exist_ok=True)

    # 库文件名保持不变：改了会读不到已有的索引/配置
    db = Database(cfg.data_dir / "p115-strm.db")
    if not db.get_meta("roots_seeded"):
        # 首次启动：用 ROOT_CIDS 初始化监控目录；之后以数据库为准，在网页上增删
        for cid in cfg.root_cids:
            db.add_root(cid, "")
        db.set_meta("roots_seeded", "1")
        if cfg.root_cids:
            log.info("初始化监控根目录: %s（之后可在网页「目录管理」里增删）", list(cfg.root_cids))
        else:
            log.info("未预设监控目录：打开网页在「目录管理」里添加要生成 strm 的目录")
    api = P115API(cookies=cfg.cookies, cookie_file=cfg.cookie_path, qps=cfg.api_qps)

    # 首次运行可以没有 cookies：服务照常起来，用户在网页「115 助手」里粘贴
    # cookies 添加账号后即可用。登录失败也不再直接退出 —— 否则填错一个字符
    # 就只能改 compose 重启，网页上根本进不去。
    login: dict = {"ok": False, "uid": "", "detail": ""}
    if not getattr(api, "authenticated", True):
        login["detail"] = "还没有 115 cookies"
        log.warning(
            "未配置 115 cookies：服务照常启动，打开网页 →「115 助手」粘贴 cookies 添加账号即可"
        )
    else:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                api.check_login()
                last_error = None
                break
            except WafBlocked as exc:
                # 限流不该让整个服务起不来：网页、播放、ed2k 都还能用，
                # 扫描线程会自己退避重试
                last_error = exc
                log.warning("115 暂时拒绝请求（限流/风控 405），先启动服务，稍后自动重试: %s", exc)
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                log.warning("登录校验第 %d 次失败: %s", attempt + 1, exc)
                if attempt < 2:
                    threading.Event().wait(3 * (attempt + 1))
        if last_error is None:
            login["ok"] = True
            login["uid"] = api.user_id()
        elif isinstance(last_error, WafBlocked):
            # 限流不等于 cookies 失效：标记为「暂时未知」，不要嚇用户去改 cookies
            login["ok"] = True
            login["detail"] = f"115 限流中：{last_error}"
        else:
            login["detail"] = str(last_error)
            log.error(
                "115 登录校验失败：%s\n"
                "服务继续启动，打开网页 →「115 助手」替换 cookies 即可（不用重启容器）",
                last_error,
            )

    store = StrmStore(
        cfg.strm_root,
        base_url=cfg.base_url,
        auth_token=cfg.auth_token,
        template=cfg.play_url_template,
        play_url_ext=cfg.play_url_ext,
        ed2k_dir=cfg.ed2k_strm_dir,
        # 主账号的 115 用户 ID：strm 末尾用它标识归属
        account_uid=login["uid"],
    )
    scanner = Scanner(cfg, db, api, store)
    watcher = Watcher(cfg, db, api, store, scanner)
    ed2k = Ed2kManager(cfg, db, api, store)
    share = ShareManager(cfg, db, api, store)
    instant = InstantManager(cfg, db, api)
    # 反代对象始终创建：网页上要能手动开关和改上游（需求：反代可手动修改）。
    # 没配 EMBY_URL 时 enabled=False，访问 /emby 返回 503 而不是 404。
    # 天翼云盘是插件，「/p189/play 换直链」这一步在插件装好后补进来。
    emby = EmbyProxy(cfg, db, api, store, instant, ed2k)
    # 网页上保存过的反代配置优先于环境变量
    saved_proxy = db.get_meta("proxy_settings", "")
    if saved_proxy:
        try:
            emby.apply_settings(json.loads(saved_proxy))
        except (json.JSONDecodeError, TypeError):
            log.warning("保存的 Emby 反代配置无法解析，已回落到环境变量")
    if not emby.upstream:
        log.warning("没有配置 EMBY_URL，Emby 反代可在网页「媒体与反代 → Emby 反代」里填写")
    scanner.resolve_roots()

    try:
        last_scan = json.loads(db.get_meta("last_scan_stats", "{}"))
    except json.JSONDecodeError:
        last_scan = {}
    state: dict = {"version": __version__, "scan": last_scan, "login": login}

    from .hub import AccountHub, Engine

    # 同播复制：账号级能力，主账号也要有一个（子账号在 build_engine 里各自建）
    copies = CopyManager(db, api, cfg)
    ed2k.copies = copies

    default_engine = Engine(
        name="", uid=login["uid"], cfg=cfg, db=db, api=api, store=store,
        scanner=scanner, watcher=watcher, ed2k=ed2k, share=share,
        instant=instant, copies=copies, state=state,
    )
    # shared_db：115 STRM 配置（含每条配置的请求头）都在主库里，
    # 子账号的扫描器要读它才知道自己那条配置该用哪个「STRM 请求头」
    hub = AccountHub(cfg, default_engine, shared_db=db)
    if emby is not None:
        emby.hub = hub
    hub.load()  # 恢复已登记的子账号（登录失败的只标记，不阻塞启动）

    # 双向联动 + 通知：两者都要能看到全部账号的引擎
    def _engines() -> list:
        return [default_engine, *hub.engines().values()]

    # 可选功能（天翼云盘 / 通知与联动 / 企微可信IP）都是插件：没装的不 import、
    # 不起线程、不装浏览器 —— 这也是镜像能从 1.28GB 缩回几百 MB 的原因。
    plugins = PluginManager(cfg, db)
    plugins.bind(api=api, store=store, scanner=scanner, watcher=watcher,
                 hub=hub, emby=emby, state=state, engines=_engines)

    # 秒传账号直接来自「115 助手」：不用再单独添加一遍
    instant.accounts_provider, instant.cookie_provider = _make_cookie_provider(hub)
    # 预缓存要跨账号取源文件：115 STRM 生成的文件索引在各账号自己的库里
    instant.sources_provider = _make_sources_provider(hub)

    return (db, api, store, scanner, watcher, ed2k, share, instant, emby,
            state, hub, plugins)


def boot_plugins(plugins: PluginManager, emby: EmbyProxy | None) -> None:
    """加载已安装的插件，并把天翼云盘接给 Emby 反代。

    反代要拿云盘管理器去换直链（``/p189/play`` → 天翼 CDN），所以插件装好
    之后必须把对象塞回 ``emby.cloud189``；没装就是 None，反代那条路 404。
    """
    plugins.load_all()
    if emby is not None:
        emby.cloud189 = plugins.get("cloud189")


def _make_cookie_provider(hub):
    """把「115 助手」的账号列表与 cookies 交给秒传功能用。"""

    def accounts() -> list[dict]:
        rows = hub.status()
        out = []
        for row in rows:
            name = str(row.get("name") or "")
            out.append({
                "name": name,
                "uid": "",
                "ok": bool(row.get("ok")),
                "display": hub.display_name(name),
            })
        return out

    def cookies(account: str) -> str:
        return hub.cookies_for(account)

    return accounts, cookies


def _make_sources_provider(hub):
    """把各账号的索引库交给秒传功能（预缓存要跨账号取源文件）。

    115 STRM 生成的 strm 索引写在**所属账号自己的库**里（每个账号一个
    独立索引库），只看主库的话预缓存一个文件都取不到。
    """

    def sources() -> list[tuple[str, Database]]:
        out = []
        for name, eng in hub.engines().items():
            out.append((name, eng.db))
        return out

    return sources


def need_initial_scan(cfg: Config, db: Database) -> bool:
    if cfg.full_scan_on_start == "always":
        return True
    if cfg.full_scan_on_start == "never":
        return False
    return not db.get_meta("last_full_scan") or db.stats()["files"] == 0


def run_headless(cfg: Config, db: Database, scanner: Scanner, watcher: Watcher,
                 ed2k: Ed2kManager, share: ShareManager, state: dict,
                 hub=None, instant: InstantManager | None = None,
                 plugins: PluginManager | None = None,
                 emby: EmbyProxy | None = None) -> int:
    # scan / watch 模式没有网页可以填 cookies：没登录就没什么可做的，直接说清楚
    if not getattr(getattr(scanner, "api", None), "authenticated", True):
        log.error(
            "还没有 115 cookies：scan/watch 模式需要先配置 P115_COOKIES 或 %s。"
            "想先在网页里添加账号，请用 serve/all 模式启动",
            cfg.cookie_path,
        )
        return 2

    stop = threading.Event()

    def handler(signum, _frame):
        log.info("收到信号 %s，准备退出", signum)
        stop.set()
        scanner.stop()
        if hub is not None:
            hub.stop_all()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    if hub is not None:
        hub.start_all()

    ed2k_worker = threading.Thread(
        target=ed2k.run_forever, args=(stop,), name="ed2k", daemon=True
    )
    ed2k_worker.start()

    share_worker = threading.Thread(
        target=_share_restore, args=(share,), name="share-restore", daemon=True
    )
    share_worker.start()

    # 秒传校验缓存按内容算大小、只增不减 —— 定期清一批最久没用的
    if instant is not None:
        digest_worker = threading.Thread(
            target=instant.run_forever, args=(stop,), name="instant-digest",
            daemon=True,
        )
        digest_worker.start()

    # 插件自己的后台线程（联动巡检 / TG 长轮询 / 企微 IP 检测）
    if plugins is not None:
        boot_plugins(plugins, emby)
        plugins.start_threads()

    try:
        if cfg.mode == "full" or need_initial_scan(cfg, db):
            state["scan"] = scanner.run(full=True).as_dict()
        if cfg.mode in ("all", "watch"):
            # 事件接口不可用时 Watcher 会自己降级成定时增量扫描，所以这里无条件启动
            worker = threading.Thread(target=watcher.run_forever, args=(stop,), name="watcher")
            worker.start()
            while worker.is_alive() and not stop.is_set():
                worker.join(timeout=1.0)
    except KeyboardInterrupt:
        log.info("已中断")
    return 0


def _share_restore(share: ShareManager) -> None:
    try:
        share.restore_strm()
    except Exception as exc:  # noqa: BLE001
        log.warning("补齐分享 strm 失败: %s", exc)


def run_with_server(cfg: Config, db: Database, api: P115API, store: StrmStore,
                    scanner: Scanner, watcher: Watcher, ed2k: Ed2kManager,
                    share: ShareManager, instant: InstantManager,
                    emby: EmbyProxy | None, state: dict, hub=None,
                    plugins: PluginManager | None = None) -> int:
    import uvicorn

    stop = threading.Event()
    # 主账号的 CopyManager 已经在 build() 里建好并挂到默认引擎上，这里复用同一个
    # 实例 —— 会话表在内存里，两处各建一个会让同一台设备被算成两台，重复复制。
    copies = getattr(getattr(hub, "default", None), "copies", None)
    if copies is None:
        copies = CopyManager(db, api, cfg)
        if ed2k is not None:
            ed2k.copies = copies
    app = create_app(cfg, db, api, store, scanner, watcher, state,
                     ed2k, share, instant, emby, hub, plugins=plugins,
                     copies=copies)

    # 115 STRM 配置的定时巡检（每条配置自己的开关 + 间隔）
    strm_scheduler = getattr(app.state, "strm_scheduler", None)
    if strm_scheduler is not None:
        strm_scheduler.start(stop)

    def worker() -> None:
        try:
            if cfg.mode == "serve":
                return
            if need_initial_scan(cfg, db):
                state["scan"] = scanner.run(full=True).as_dict()
            watcher.run_forever(stop)
        except Exception:
            log.exception("后台任务异常退出")

    thread = threading.Thread(target=worker, name="engine", daemon=True)
    thread.start()

    if hub is not None:
        hub.start_all()

    ed2k_worker = threading.Thread(
        target=ed2k.run_forever, args=(stop,), name="ed2k", daemon=True
    )
    ed2k_worker.start()

    share_worker = threading.Thread(
        target=_share_restore, args=(share,), name="share-restore", daemon=True
    )
    share_worker.start()

    # 秒传校验缓存按内容算大小、只增不减 —— 定期清一批最久没用的，
    # 免得看片越多把 SQLite 撑起来。
    digest_worker = threading.Thread(
        target=instant.run_forever, args=(stop,), name="instant-digest", daemon=True
    )
    digest_worker.start()

    # 插件后台线程：联动巡检 / 通知指令长轮询 / 企微 IP 检测。
    # create_app 里已经 load_all 过了，这里只是把线程拉起来（load_all 幂等）。
    if plugins is not None:
        boot_plugins(plugins, emby)
        plugins.start_threads()

    log.info("HTTP 服务监听 %s:%d", cfg.web_host, cfg.web_port)

    if emby is not None:
        # 反代独立端口：客户端服务器地址直接填 http://本机:<port>，不带任何前缀。
        # 端口以「网页上保存的值」为准（保存后立即生效，不用重启容器）；
        # 没保存过才回落到环境变量 EMBY_PROXY_PORT。
        from .server import ProxyPortRunner

        runner = ProxyPortRunner(cfg, emby)
        runner.apply(emby.settings.get("proxy_port") or cfg.emby_proxy_port)
        emby.port_runner = runner

    try:
        uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_config=None,
                    access_log=False, timeout_keep_alive=30)
    finally:
        stop.set()
        scanner.stop()
        if hub is not None:
            hub.stop_all()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("scan", "watch", "serve", "all"):
        os.environ["MODE"] = argv[0]
        argv = argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        print("用法: python -m app [scan|watch|serve|all]")
        return 0

    cfg = load_config()
    setup_logging(cfg.log_level)
    banner(cfg)

    (db, api, store, scanner, watcher, ed2k, share, instant, emby, state,
     hub, plugins) = build(cfg)

    if cfg.mode in ("serve", "all") and cfg.web_enabled:
        return run_with_server(cfg, db, api, store, scanner, watcher, ed2k,
                               share, instant, emby, state, hub, plugins=plugins)
    return run_headless(cfg, db, scanner, watcher, ed2k, share, state, hub,
                        instant, plugins=plugins, emby=emby)


if __name__ == "__main__":
    raise SystemExit(main())
