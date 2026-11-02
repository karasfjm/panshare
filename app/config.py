"""配置：全部来自环境变量，方便在 Docker / compose 中覆盖。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 默认为常见的视频后缀；STRM_ALL_FILES=1 时忽略此列表
DEFAULT_EXT = (
    ".mp4,.mkv,.ts,.m2ts,.mts,.iso,.avi,.wmv,.flv,.mov,.rmvb,.rm,"
    ".webm,.mpg,.mpeg,.m4v,.3gp,.asf,.vob,.divx,.f4v,.ogv"
)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def _bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    value = _env(name)
    if not value:
        return default
    try:
        return int(float(value))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    value = _env(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    value = _env(name, default)
    if not value:
        return ()
    return tuple(part.strip() for part in value.replace(";", ",").split(",") if part.strip())


def _normalize_ext(items: tuple[str, ...]) -> tuple[str, ...]:
    out = []
    for item in items:
        item = item.strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = "." + item
        out.append(item)
    return tuple(dict.fromkeys(out))


@dataclass
class Config:
    # ---------- 115 账号 ----------
    cookies: str = ""
    cookie_file: Path = Path("/config/115-cookies.txt")

    # ---------- 路径 ----------
    data_dir: Path = Path("/config")
    strm_root: Path = Path("/strm")

    # ---------- 扫描范围 ----------
    # 默认不预设任何监控目录：第一次启动是空的，在网页「目录管理」里添加。
    # （老版本默认 0 = 整个网盘，会把所有子目录都算作「已在监控范围内」，导致加不进去）
    root_cids: tuple[int, ...] = ()
    include_root_name: bool = True
    include_ext: tuple[str, ...] = ()
    strm_all_files: bool = False
    include_regex: str = ""
    exclude_regex: str = ""
    exclude_dirs: tuple[str, ...] = ()

    # ---------- 全量扫描 ----------
    # 115 的接口前面有阿里云 WAF：QPS 一高就按账号返回 405 拦截页（不是接口下线），
    # 所以默认很保守（2 QPS、单目录串行），扫大网盘会慢一些，但不会被封。
    scan_app: str = "web"
    api_qps: float = 2.0           # 全局请求上限（次/秒），所有接口共用
    scan_workers: int = 2          # 同时遍历的目录数
    page_workers: int = 0          # 单个目录内并发翻页数（0=串行）
    scan_cooldown: float = 0.0     # 额外的每次请求冷却（秒），限速已由 api_qps 负责
    scan_batch_log: int = 500

    # ---------- 增量（生活事件） ----------
    # 事件接口被限流 / 探测失败时，自动降级为 fallback_interval 秒一次的指纹增量扫描。
    event_app: str = "web"
    event_interval: float = 30.0
    event_cooldown: float = 0.6
    event_batch: int = 1000
    event_enabled: bool = True
    fallback_interval: float = 600.0   # 事件不可用时的补扫间隔（秒）
    smart_scan: bool = True            # 增量扫描按目录 mtime 跳过未变化子树

    # ---------- 启动 / 维护 ----------
    mode: str = "all"              # all | full | watch | serve
    full_scan_on_start: str = "auto"   # auto | always | never
    reconcile_hours: float = 0.0       # >0 时定时全量对账
    delete_orphan: bool = True         # 全量扫描后清理本地不再对应的 .strm
    delete_empty_dirs: bool = True

    # ---------- HTTP ----------
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 9527
    public_base_url: str = ""
    auth_token: str = ""
    # ---------- 网页登录（账号密码由 compose 变量生成） ----------
    web_username: str = "admin"          # UI_USERNAME
    web_password: str = "password"       # UI_PASSWORD（留空 = 不启用登录）
    web_session_ttl: int = 604800        # UI_SESSION_TTL（秒，默认 7 天）
    web_show_hint: bool = True           # UI_SHOW_HINT（登录页提示默认账号）
    web_site_name: str = "PanShare"      # UI_SITE_NAME
    play_mode: str = "302"          # 302 | proxy
    play_url_template: str = ""     # 自定义播放地址模板
    play_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    play_cache_seconds: int = 900
    play_url_ext: bool = True

    # ---------- ed2k：离线转存后生成 strm ----------
    ed2k_enabled: bool = True
    ed2k_dir_cid: int = 0                 # 115 上的转存目录（0 = 首次使用时自动创建）
    ed2k_dir_name: str = "p115-strm-ed2k" # 自动创建时用的目录名（建在网盘根目录）
    ed2k_strm_dir: str = "ed2k"           # strm 输出子目录（相对 STRM_ROOT）
    ed2k_ttl_hours: float = 24.0          # 转存文件保留时长（0 = 不自动清理）
    ed2k_clean_interval: float = 600.0    # 每隔多少秒检查一次是否该清理
    ed2k_poll_interval: float = 20.0      # 转存中的任务多久刷新一次
    ed2k_play_wait: float = 45.0          # 播放时最多等多久让文件转存到位
    # 添加后立刻提交离线转存（True）还是只写 strm、等播放时再转存（False）
    ed2k_auto_transfer: bool = False

    # ---------- 115 分享转存：分享链接 → 转存到网盘 → 生成 strm ----------
    share_enabled: bool = True
    share_dir_cid: int = 0                # 接收目录（0 = 首次使用时自动创建）
    share_dir_name: str = "p115-strm-share"
    share_strm_dir: str = "share"         # strm 输出子目录（相对 STRM_ROOT）

    # ---------- Emby 302 反代 ----------
    emby_enabled: bool = False
    emby_url: str = ""                    # 上游 Emby 地址，如 http://emby:8096
    emby_strm_prefix: str = ""            # Emby 里 strm 挂载路径前缀（留空自动识别）
    emby_path_rules: tuple[str, ...] = () # 自定义路径替换规则 regex=>template
    emby_proxy_port: int = 0              # 反代独立端口（0 = 关闭）；客户端直填这个端口，不带 /emby 前缀

    # ---------- 秒传播放（多账号） ----------
    instant_dir_name: str = "p115-strm-instant"  # 秒传暂存目录（建在目标账号根目录）
    instant_cache_seconds: int = 1800     # 秒传结果缓存时长
    # 校验缓存（115 二次校验要的那段字节）的字节预算与清理间隔。
    # 这一层按内容算大小，只增不减，所以必须有个上限 + 定期清理，
    # 否则看片越多缓存越大，SQLite 会被撑起来。
    instant_digest_max_mb: int = 512      # 缓存字节上限（MB），0 = 不限制
    instant_digest_clean_interval: float = 3600.0   # 每隔多少秒检查一次是否超限
    instant_digest_poll_interval: float = 300.0      # 后台线程的检查间隔
    # Emby 播放改写时就把还没秒传的文件先秒传掉，直接把 115 直链发给播放器
    # （关掉则只发本服务地址，等播放器来请求时再秒传）。
    instant_preresolve: bool = True
    # 预解析最多等多久（秒）：秒传卡住时不至于把 Emby 的 PlaybackInfo 拖死，
    # 超过就先回退，秒传在后台继续跑。0 = 不限制。
    instant_preresolve_timeout: float = 25.0

    log_level: str = "INFO"
    tz: str = "Asia/Shanghai"

    # 运行时派生
    roots: dict[int, str] = field(default_factory=dict)

    @property
    def cookie_path(self) -> Path:
        return self.cookie_file

    @property
    def base_url(self) -> str:
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        host = self.web_host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        return f"http://{host}:{self.web_port}"


def load_config() -> Config:
    cfg = Config()

    cfg.cookies = _env("P115_COOKIES")
    cfg.cookie_file = Path(_env("P115_COOKIE_FILE", "/config/115-cookies.txt")).expanduser()
    cfg.data_dir = Path(_env("DATA_DIR", "/config")).expanduser()
    cfg.strm_root = Path(_env("STRM_ROOT", "/strm")).expanduser()

    raw_cids = _csv("ROOT_CIDS", "")
    cids: list[int] = []
    for item in raw_cids:
        try:
            cids.append(int(item))
        except ValueError:
            raise SystemExit(
                f"ROOT_CIDS 中含有非法目录 id: {item!r}（只接受数字 id）"
            ) from None
    # 允许为空：空表示「先不监控任何目录」，在网页上添加后才开始生成
    cfg.root_cids = tuple(dict.fromkeys(cids))
    cfg.include_root_name = _bool("INCLUDE_ROOT_NAME", True)

    cfg.strm_all_files = _bool("STRM_ALL_FILES", False)
    cfg.include_ext = _normalize_ext(_csv("STRM_EXT", DEFAULT_EXT))
    cfg.include_regex = _env("INCLUDE_REGEX")
    cfg.exclude_regex = _env("EXCLUDE_REGEX")
    cfg.exclude_dirs = _csv("EXCLUDE_DIRS")

    cfg.scan_app = _env("SCAN_APP", "web") or "web"
    cfg.api_qps = max(0.1, _float("API_QPS", 2.0))
    cfg.scan_workers = max(1, _int("SCAN_WORKERS", 2))
    cfg.page_workers = max(0, _int("PAGE_WORKERS", 0))
    cfg.scan_cooldown = max(0.0, _float("SCAN_COOLDOWN", 0.0))
    cfg.scan_batch_log = max(1, _int("SCAN_BATCH_LOG", 500))

    cfg.event_enabled = _bool("EVENT_ENABLED", True)
    cfg.event_app = _env("EVENT_APP", "web") or "web"
    cfg.event_interval = max(3.0, _float("EVENT_INTERVAL", 30.0))
    cfg.event_cooldown = max(0.0, _float("EVENT_COOLDOWN", 0.6))
    cfg.event_batch = max(1, _int("EVENT_BATCH", 1000))
    cfg.fallback_interval = max(30.0, _float("FALLBACK_INTERVAL", 600.0))
    cfg.smart_scan = _bool("SMART_SCAN", True)

    cfg.mode = (_env("MODE", "all") or "all").lower()
    cfg.full_scan_on_start = (_env("FULL_SCAN_ON_START", "auto") or "auto").lower()
    cfg.reconcile_hours = max(0.0, _float("RECONCILE_HOURS", 0.0))
    cfg.delete_orphan = _bool("DELETE_ORPHAN", True)
    cfg.delete_empty_dirs = _bool("DELETE_EMPTY_DIRS", True)

    cfg.web_enabled = _bool("WEB_ENABLED", True)
    cfg.web_host = _env("WEB_HOST", "0.0.0.0") or "0.0.0.0"
    cfg.web_port = _int("WEB_PORT", 9527)
    cfg.public_base_url = _env("PUBLIC_BASE_URL")
    cfg.auth_token = _env("AUTH_TOKEN")
    cfg.web_username = _env("UI_USERNAME", "admin") or "admin"
    cfg.web_password = _env("UI_PASSWORD", "password")
    cfg.web_session_ttl = max(60, _int("UI_SESSION_TTL", 604800))
    cfg.web_show_hint = _bool("UI_SHOW_HINT", True)
    cfg.web_site_name = _env("UI_SITE_NAME", "PanShare") or "PanShare"
    cfg.play_mode = (_env("PLAY_MODE", "302") or "302").lower()
    cfg.play_url_template = _env("PLAY_URL_TEMPLATE")
    cfg.play_user_agent = _env("PLAY_USER_AGENT", cfg.play_user_agent)
    cfg.play_cache_seconds = max(0, _int("PLAY_CACHE_SECONDS", 900))
    cfg.play_url_ext = _bool("PLAY_URL_EXT", True)

    cfg.ed2k_enabled = _bool("ED2K_ENABLED", True)
    cfg.ed2k_dir_cid = max(0, _int("ED2K_DIR_CID", 0))
    cfg.ed2k_dir_name = _env("ED2K_DIR_NAME", "p115-strm-ed2k") or "p115-strm-ed2k"
    cfg.ed2k_strm_dir = _env("ED2K_STRM_DIR", "ed2k").strip("/") or "ed2k"
    cfg.ed2k_ttl_hours = max(0.0, _float("ED2K_TTL_HOURS", 24.0))
    cfg.ed2k_clean_interval = max(60.0, _float("ED2K_CLEAN_INTERVAL", 600.0))
    cfg.ed2k_poll_interval = max(5.0, _float("ED2K_POLL_INTERVAL", 20.0))
    cfg.ed2k_play_wait = max(0.0, _float("ED2K_PLAY_WAIT", 45.0))
    cfg.ed2k_auto_transfer = _bool("ED2K_AUTO_TRANSFER", False)

    cfg.share_enabled = _bool("SHARE_ENABLED", True)
    cfg.share_dir_cid = max(0, _int("SHARE_DIR_CID", 0))
    cfg.share_dir_name = _env("SHARE_DIR_NAME", "p115-strm-share") or "p115-strm-share"
    cfg.share_strm_dir = _env("SHARE_STRM_DIR", "share").strip("/") or "share"

    cfg.emby_enabled = _bool("EMBY_ENABLED", False)
    cfg.emby_url = _env("EMBY_URL").rstrip("/")
    cfg.emby_strm_prefix = _env("EMBY_STRM_PREFIX")
    cfg.emby_path_rules = tuple(
        line.strip() for line in _env("EMBY_PATH_RULES").splitlines() if "=>" in line
    )
    cfg.emby_proxy_port = _int("EMBY_PROXY_PORT", 0)

    cfg.instant_dir_name = _env("INSTANT_DIR_NAME", "p115-strm-instant") or "p115-strm-instant"
    cfg.instant_cache_seconds = max(60, _int("INSTANT_CACHE_SECONDS", 1800))
    cfg.instant_digest_max_mb = max(0, _int("INSTANT_DIGEST_MAX_MB", 512))
    cfg.instant_digest_clean_interval = max(
        60.0, _float("INSTANT_DIGEST_CLEAN_INTERVAL", 3600.0)
    )
    cfg.instant_digest_poll_interval = max(
        30.0, _float("INSTANT_DIGEST_POLL_INTERVAL", 300.0)
    )
    cfg.instant_preresolve = _bool("INSTANT_PRERESOLVE", True)
    cfg.instant_preresolve_timeout = max(
        0.0, _float("INSTANT_PRERESOLVE_TIMEOUT", 25.0)
    )

    cfg.log_level = (_env("LOG_LEVEL", "INFO") or "INFO").upper()
    cfg.tz = _env("TZ", "Asia/Shanghai") or "Asia/Shanghai"

    if cfg.mode not in ("all", "full", "watch", "serve"):
        raise SystemExit(f"MODE 只能是 all/full/watch/serve，当前为 {cfg.mode!r}")
    if cfg.play_mode not in ("302", "proxy"):
        raise SystemExit(f"PLAY_MODE 只能是 302/proxy，当前为 {cfg.play_mode!r}")
    if not cfg.play_url_template and not cfg.web_enabled:
        raise SystemExit("PLAY_URL_TEMPLATE 为空时必须开启 WEB_ENABLED，否则 strm 里没有可用地址")
    return cfg
