"""插件：把「天翼云盘 / 通知与联动 / 企微可信IP」这类可选功能做成可安装模块。

为什么这么设计
--------------
这些功能本身只有几十 KB 代码，真正占地方的是**重依赖**：企微可信IP 要
playwright + Chromium（aarch64 上约 730MB）。之前为了让它开箱即用，把
Chromium 直接塞进镜像，镜像从 266MB 涨到 1.28GB，而且 `docker commit` 出来
的镜像没法复现。

现在改成：
* 插件源码随包附带（很小），**依赖装到 ``DATA_DIR/plugins/<id>/``** ——
  那是 /config 卷，容器重建也不用重装；
* **没装就不加载**：不启用的插件不会被 import，路由、后台线程、浏览器全部
  不存在（没装企微插件时 ``app/wxip.py`` 都不会被 import）；
* **动态挂载**：插件自己往一个独立 ``APIRouter`` 上注册路由，由本模块
  ``include_router(prefix=...)`` 挂到主应用；卸载时把这批路由从
  ``app.router.routes`` 里摘掉，不用重启容器。

插件目录结构::

    plugins/<id>/
        plugin.json     # 清单：名字 / 导航 / 路由前缀 / 依赖
        backend.py      # def setup(api: APIRouter, ctx) -> 运行时对象（可选 run/teardown）
        web.js          # 前端页面，用 /plugin-assets/<id>.js 加载
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from fastapi import APIRouter

log = logging.getLogger(__name__)

# 插件清单目录（随镜像分发）
CATALOG_DIR = Path(__file__).resolve().parent.parent / "plugins"
STATE_KEY = "plugin_state"

# pip 源：国内默认先走清华，装不上再回落官方源
PIP_INDEXES = [
    x for x in (os.environ.get("PLUGIN_PIP_INDEX", ""),
                "https://pypi.tuna.tsinghua.edu.cn/simple",
                "https://pypi.org/simple") if x
]

# Chromium 运行需要的系统库（apt 包名）。镜像里没有这些，装浏览器时要一起装。
DEFAULT_APT = [
    "libnss3", "libnspr4", "libatk1.0-0", "libatk-bridge2.0-0", "libcups2",
    "libdrm2", "libxkbcommon0", "libxcomposite1", "libxdamage1", "libxfixes3",
    "libxrandr2", "libgbm1", "libpango-1.0-0", "libcairo2", "libasound2",
    "libatspi2.0-0", "fonts-liberation",
]

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


class PluginError(Exception):
    """安装 / 卸载 / 启用失败。消息直接显示给用户，所以要说人话。"""


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def human_size(n: float) -> str:
    """字节数 → 人能看的大小（插件页上用）。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _mod_name(spec: str) -> str:
    """``playwright==1.47.0`` → ``playwright``（查依赖装没装）。"""
    return re.split(r"[<>=!~\[]", spec, maxsplit=1)[0].strip()


class PluginCtx:
    """插件能用到的核心对象 + 懒加载核心模块的入口。

    只暴露必要的东西：插件是"可选功能"，不该反手依赖核心的实现细节。
    """

    def __init__(self, manager: PluginManager):
        self._manager = manager
        self.cfg = manager.cfg
        self.db = manager.db
        self.api: Any = None
        self.store: Any = None
        self.scanner: Any = None
        self.watcher: Any = None
        self.hub: Any = None
        self.emby: Any = None
        self.state: dict[str, Any] = {}
        self.data_dir = Path(manager.cfg.data_dir)
        # 测试用：注入现成对象，插件就不自己新建（见 PluginManager.inject）
        self.override: Any = None
        # 跨账号引擎（联动 / 通知要看到全部账号）
        self.engines: Callable[[], list] | None = None

    # ------------------------------------------------------------ 拿到核心能力
    def mod(self, name: str) -> ModuleType:
        """懒加载核心模块：``ctx.mod("wxip")`` / ``"notify"`` / ``"linker"`` …

        用 ``importlib`` 而不是 ``from .. import``：插件文件不在 ``app`` 包里，
        但核心包名仍然是 ``app``，这样写最不容易错。
        """
        return importlib.import_module(f"{__package__}.{name}")

    def get(self, pid: str) -> Any:
        """拿另一个插件的运行时对象（企微插件要给通知插件发消息就是走这里）。"""
        runtime = self._manager.loaded.get(pid)
        return runtime.obj if runtime else None

    def authorize(self, request: Any, token: str = "") -> None:
        """管理接口鉴权。没有 Web（headless）时直接放行。"""
        fn = self._manager.authorize
        if fn is not None:
            fn(request, token)


class PluginRuntime:
    """一个已加载的插件：模块、运行时对象、路由、后台线程。"""

    def __init__(self, pid: str, manifest: dict, module: ModuleType,
                 obj: Any, router: APIRouter, prefix: str):
        self.id = pid
        self.manifest = manifest
        self.module = module
        self.obj = obj
        self.router = router
        self.prefix = prefix
        self.routes: list[Any] = []
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()

    def start(self) -> None:
        """启动插件的后台线程（插件模块有 ``run(obj, stop)`` 才会启）。"""
        run = getattr(self.module, "run", None)
        if run is None or self.thread is not None:
            return
        self.thread = threading.Thread(
            target=run, args=(self.obj, self.stop),
            name=f"plugin-{self.id}", daemon=True,
        )
        self.thread.start()
        log.info("插件 %s 后台线程已启动", self.id)

    def stop_thread(self) -> None:
        self.stop.set()


class PluginManager:
    """插件清单 / 安装状态 / 运行时（路由 + 线程）的唯一管家。"""

    def __init__(self, cfg: Any, db: Any):
        self.cfg = cfg
        self.db = db
        self.work_dir = Path(cfg.data_dir) / "plugins"
        self._state = self._load_state()
        self._lock = threading.RLock()
        self.loaded: dict[str, PluginRuntime] = {}
        self.tasks: dict[str, dict] = {}
        self.app: Any = None
        self.authorize: Callable | None = None
        self.ctx = PluginCtx(self)
        self._stop_all = threading.Event()
        self._running = False   # 启动阶段跑完之后，新装的插件要立刻起线程
        self._injected: dict[str, Any] = {}   # 测试/嵌入式注入的对象
        self._size_cache: dict[str, tuple[float, int]] = {}   # 依赖目录体积，60 秒内复用

    # ================================================================ 状态读写
    def _load_state(self) -> dict:
        try:
            raw = json.loads(self.db.get_meta(STATE_KEY, "{}") or "{}")
        except json.JSONDecodeError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("installed", {})
        raw.setdefault("enabled", {})
        raw.setdefault("removed", {})   # 用户主动卸载过的：别再自动装回来
        return raw

    def _save(self) -> None:
        self.db.set_meta(STATE_KEY, json.dumps(self._state, ensure_ascii=False))

    # ================================================================ 清单
    def manifests(self) -> list[dict]:
        """读 ``plugins/*/plugin.json``。读不出来的只记日志，不影响别的插件。"""
        out: list[dict] = []
        if not CATALOG_DIR.exists():
            return out
        for path in sorted(CATALOG_DIR.iterdir()):
            manifest_file = path / "plugin.json"
            if not path.is_dir() or not manifest_file.exists():
                continue
            try:
                manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("插件清单读不出来 %s: %s", manifest_file, exc)
                continue
            if not isinstance(manifest, dict) or not manifest.get("id"):
                continue
            manifest["_dir"] = str(path)
            out.append(manifest)
        return out

    def manifest(self, pid: str) -> dict | None:
        return next((m for m in self.manifests() if m["id"] == pid), None)

    def is_installed(self, pid: str) -> bool:
        return pid in self._state["installed"]

    def is_enabled(self, pid: str) -> bool:
        return self.is_installed(pid) and bool(self._state["enabled"].get(pid))

    def get(self, pid: str) -> Any:
        runtime = self.loaded.get(pid)
        return runtime.obj if runtime else None

    # ================================================================ 依赖
    def site_dir(self, pid: str) -> Path:
        return self.work_dir / pid / "site-packages"

    def browsers_dir(self, pid: str) -> Path:
        return self.work_dir / pid / "browsers"

    @staticmethod
    def _has_browser(path: Path, name: str) -> bool:
        if not path.exists():
            return False
        return any(child.is_dir() and child.name.startswith(name)
                   for child in path.iterdir())

    def browsers_path(self, pid: str, manifests: list[str] | None = None) -> Path:
        """浏览器实际所在目录。

        优先用插件自己装的那份；没有就看看镜像里有没有旧的
        ``/root/.cache/ms-playwright``（v1.18 及以前的镜像把 Chromium 打进去了，
        能复用就别让用户重下 600MB）。
        """
        own = self.browsers_dir(pid)
        wanted = manifests or []
        if wanted and any(self._has_browser(own, n) for n in wanted):
            return own
        system = Path.home() / ".cache" / "ms-playwright"
        if wanted and any(self._has_browser(system, n) for n in wanted):
            return system
        return own

    def _module_ready(self, pid: str, name: str) -> bool:
        """依赖装没装：先看插件自己的 site-packages，再看系统里有没有。

        检查系统那份是为了兼容 v1.18 及以前把 playwright 直接装在容器里的
        老镜像 —— 那时候依赖不在插件目录下，但确实能用，不该谎报"缺依赖"。
        """
        site = self.site_dir(pid)
        if (site / name).exists() or (site / f"{name}.py").exists():
            return True
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    def _dep_size(self, pid: str) -> int:
        """插件依赖目录占多少。Chromium 有上万个文件，别每次刷新都去 rglob。"""
        path = self.work_dir / pid
        if not path.exists():
            return 0
        cached = self._size_cache.get(pid)
        now = time.time()
        if cached and now - cached[0] < 60:
            return cached[1]
        size = _dir_size(path)
        self._size_cache[pid] = (now, size)
        return size

    def deps_status(self, pid: str, manifest: dict) -> dict:
        """依赖齐不齐。前端据此决定显示「安装」还是「已就绪」。"""
        py_specs = [str(x) for x in (manifest.get("python") or [])]
        missing = [n for n in (_mod_name(s) for s in py_specs) if not self._module_ready(pid, n)]
        browsers = [str(x) for x in (manifest.get("browser") or [])]
        browser_ok = True
        browser_path = ""
        if browsers:
            path = self.browsers_path(pid, browsers)
            browser_ok = any(self._has_browser(path, n) for n in browsers)
            browser_path = str(path)
        return {
            "python": py_specs,
            "browser": browsers,
            "missing": missing,
            "ready": not missing and browser_ok,
            "browser_path": browser_path,
            "size": human_size(self._dep_size(pid)),
        }

    # ================================================================ 状态汇总
    def status(self) -> dict:
        rows = []
        for manifest in self.manifests():
            pid = str(manifest["id"])
            installed = self.is_installed(pid)
            task = self.tasks.get(pid) or {}
            deps = self.deps_status(pid, manifest)
            rows.append({
                "id": pid,
                "name": manifest.get("name") or pid,
                "version": str(manifest.get("version") or ""),
                "summary": manifest.get("summary") or "",
                "desc": manifest.get("desc") or "",
                "icon": manifest.get("icon") or "box",
                "section": manifest.get("section") or "",
                "nav": manifest.get("nav") or "",
                "hash": manifest.get("hash") or "",
                "prefix": manifest.get("prefix") or "",
                "installed": installed,
                "enabled": self.is_enabled(pid),
                "loaded": pid in self.loaded,
                "has_web": bool(manifest.get("web")),
                "deps": deps,
                "size": human_size(_dir_size(Path(str(manifest["_dir"])))),
                "download_size": str(manifest.get("download_size") or ""),
                "builtin": bool(manifest.get("builtin")),
                "busy": task.get("status") == "running",
                "task": {"status": task.get("status", ""), "error": task.get("error", ""),
                         "log": (task.get("log") or [])[-12:]},
            })
        return {
            "plugins": rows,
            "work_dir": str(self.work_dir),
            "pip_index": PIP_INDEXES[0] if PIP_INDEXES else "",
        }

    def task_log(self, pid: str) -> dict:
        task = self.tasks.get(pid) or {}
        return {"status": task.get("status", ""), "error": task.get("error", ""),
                "log": (task.get("log") or [])[-100:], "done": bool(task.get("done"))}

    # ================================================================ 安装
    def _say(self, pid: str, message: str) -> None:
        """往安装日志里追加一行（前端轮询显示，看得见进度才不会以为卡死）。"""
        task = self.tasks.setdefault(pid, {"status": "idle", "log": []})
        task.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {message}")
        task["log"] = task["log"][-300:]
        log.info("[插件 %s] %s", pid, message)

    def install(self, pid: str) -> dict:
        """开始安装（后台跑，前端轮询 /api/plugins/<id>/log 看进度）。"""
        manifest = self.manifest(pid)
        if manifest is None:
            raise PluginError(f"没有这个插件：{pid}")
        with self._lock:
            task = self.tasks.get(pid)
            if task and task.get("status") == "running":
                raise PluginError("这个插件正在安装中，完成后再试")
            self.tasks[pid] = {"status": "running", "log": [], "started": time.time()}
        threading.Thread(target=self._install_worker, args=(pid, manifest),
                         name=f"plugin-install-{pid}", daemon=True).start()
        return {"ok": True, "accepted": True}

    def _install_worker(self, pid: str, manifest: dict) -> None:
        task = self.tasks[pid]
        try:
            self._say(pid, f"开始安装「{manifest.get('name') or pid}」")
            self.work_dir.mkdir(parents=True, exist_ok=True)
            self.site_dir(pid).mkdir(parents=True, exist_ok=True)

            py_specs = [str(x) for x in (manifest.get("python") or [])]
            if py_specs:
                self._pip_install(pid, py_specs)

            browsers = [str(x) for x in (manifest.get("browser") or [])]
            if browsers:
                self._apt_install(pid, [str(x) for x in (manifest.get("apt") or DEFAULT_APT)])
                self._browser_install(pid, browsers)

            with self._lock:
                self._state["installed"][pid] = {
                    "version": str(manifest.get("version") or ""),
                    "at": int(time.time()),
                }
                self._state["enabled"][pid] = True
                self._state["removed"].pop(pid, None)
                self._save()
            self._say(pid, "依赖就绪，正在加载插件")
            self.load(pid)
            self._say(pid, "安装完成，刷新页面即可使用")
            task["status"] = "done"
        except Exception as exc:  # noqa: BLE001 - 任何失败都要回显给用户
            task["status"] = "error"
            task["error"] = str(exc)
            self._say(pid, f"安装失败：{exc}")
            log.warning("插件 %s 安装失败: %s", pid, exc)
        finally:
            task["done"] = time.time()
            self._size_cache.pop(pid, None)   # 体积变了，下次重新算

    def _run(self, pid: str, cmd: list[str], timeout: int = 1800,
             env: dict | None = None) -> tuple[int, str]:
        """跑外部命令，边跑边把关键行写进安装日志。"""
        shown = " ".join(cmd[:5]) + (" …" if len(cmd) > 5 else "")
        self._say(pid, f"$ {shown}")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env or os.environ.copy(),
            )
        except OSError as exc:
            raise PluginError(f"命令启动失败：{exc}") from exc
        tail: list[str] = []
        deadline = time.time() + timeout
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                tail = [*tail[-79:], line]
                if any(k in line for k in ("Downloading", "Downloaded", "Installing",
                                           "Collecting", "Successfully", "error",
                                           "Error", "E: ", "Get:")):
                    self._say(pid, line[:300])
            if time.time() > deadline:
                proc.kill()
                raise PluginError("安装超时，请检查网络后重试")
        proc.wait(timeout=120)
        return proc.returncode, "\n".join(tail)

    def _pip_install(self, pid: str, specs: list[str]) -> None:
        self._say(pid, f"安装 Python 依赖：{' '.join(specs)}")
        target = self.site_dir(pid)
        last = ""
        for index in PIP_INDEXES:
            code, out = self._run(pid, [
                sys.executable, "-m", "pip", "install", "--no-cache-dir",
                "--disable-pip-version-check", "--target", str(target),
                "--index-url", index, *specs,
            ])
            if code == 0:
                self._say(pid, "Python 依赖已就绪")
                return
            last = out[-400:]
            self._say(pid, f"源 {index} 没装上，换下一个")
        raise PluginError(f"pip 安装失败（换个源或用代理再试）：{last}")

    def _apt_install(self, pid: str, packages: list[str]) -> None:
        """装 Chromium 需要的系统库。

        先直接 install（已装过就是秒过），不行才 update —— apt update 在国内
        常常慢到几分钟，能省就省。
        """
        if not packages:
            return
        if self._libs_present():
            self._say(pid, "系统库已存在，跳过 apt")
            return
        base = ["apt-get", "install", "-y", "--no-install-recommends"]
        code, _ = self._run(pid, [*base, *packages], timeout=600)
        if code == 0:
            return
        self._say(pid, "需要先更新软件源（这一步可能要好几分钟）")
        self._run(pid, ["apt-get", "update"], timeout=1800)
        code, out = self._run(pid, [*base, *packages], timeout=1800)
        if code != 0:
            raise PluginError(f"系统库安装失败：{out[-400:]}")
        # 软件包索引留着一百多 MB，装完就删（镜像里那份也没用了）
        for path in Path("/var/lib/apt/lists").glob("*"):
            if path.name in ("partial", "lock"):
                continue
            try:
                path.unlink() if path.is_file() else shutil.rmtree(path, ignore_errors=True)
            except OSError:
                log.debug("清理 apt 索引失败: %s", path)

    @staticmethod
    def _libs_present() -> bool:
        """粗查 NSS 在不在（Chromium 起不来的第一原因）。"""
        for base in ("/usr/lib", "/lib"):
            root = Path(base)
            if not root.exists():
                continue
            for path in root.glob("*/libnss3.so*"):
                return True
        return False

    def _browser_install(self, pid: str, browsers: list[str]) -> None:
        env = os.environ.copy()
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(self.browsers_dir(pid))
        env["PYTHONPATH"] = str(self.site_dir(pid)) + os.pathsep + env.get("PYTHONPATH", "")
        for name in browsers:
            reuse = self.browsers_path(pid, [name])
            if self._has_browser(reuse, name):
                if reuse != self.browsers_dir(pid):
                    self._say(pid, f"复用镜像里已有的浏览器：{reuse}")
                else:
                    self._say(pid, f"{name} 已下载过，跳过")
                continue
            self._say(pid, f"下载 {name}（aarch64 约 600MB，慢的话十分钟）")
            code, out = self._run(pid, [
                sys.executable, "-m", "playwright", "install", name,
            ], timeout=3600, env=env)
            if code != 0:
                raise PluginError(f"{name} 下载失败：{out[-400:]}")

    # ================================================================ 卸载 / 启停
    def uninstall(self, pid: str, purge: bool = False) -> dict:
        self.detach(pid)
        with self._lock:
            self._state["installed"].pop(pid, None)
            self._state["enabled"].pop(pid, None)
            # 记住是用户主动卸的：下次启动别因为"数据库里还有配置"又装回来
            self._state["removed"][pid] = int(time.time())
            self._save()
        self._size_cache.pop(pid, None)
        if purge:
            removed = _dir_size(self.work_dir / pid) if (self.work_dir / pid).exists() else 0
            shutil.rmtree(self.work_dir / pid, ignore_errors=True)
            log.info("插件 %s 已卸载并删掉依赖（%s）", pid, human_size(removed))
        else:
            log.info("插件 %s 已卸载（依赖保留，重装不用再下载）", pid)
        return {"ok": True}

    def set_enabled(self, pid: str, enabled: bool) -> dict:
        if enabled:
            if not self.is_installed(pid):
                raise PluginError("先安装再启用")
            with self._lock:
                self._state["enabled"][pid] = True
                self._save()
            self.load(pid)
        else:
            with self._lock:
                self._state["enabled"][pid] = False
                self._save()
            self.detach(pid)
        return {"ok": True, "enabled": enabled}

    # ================================================================ 加载
    def bind(self, **core: Any) -> None:
        """把核心对象塞给插件上下文（build 阶段调用）。"""
        for key, value in core.items():
            setattr(self.ctx, key, value)

    def attach(self, app: Any, authorize: Callable) -> None:
        """接到 Web 应用上：记下 app 与鉴权函数，之后加载的插件会自己挂路由。

        已经加载过的插件（headless 模式下先建好了对象）在这里补挂一次路由。
        """
        self.app = app
        self.authorize = authorize
        for runtime in self.loaded.values():
            if not runtime.routes:
                self._mount(runtime)

    def _module(self, pid: str, manifest: dict) -> ModuleType:
        backend = Path(str(manifest["_dir"])) / str(manifest.get("backend") or "backend.py")
        if not backend.exists():
            raise PluginError(f"插件缺少后端文件：{backend.name}")
        name = f"panshare_plugin_{pid}"
        spec = importlib.util.spec_from_file_location(name, backend)
        if spec is None or spec.loader is None:
            raise PluginError("插件模块无法加载")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def load(self, pid: str) -> PluginRuntime:
        """加载一个插件：加依赖路径 → import backend → 注册路由 → 挂到应用。"""
        with self._lock:
            existed = self.loaded.get(pid)
        if existed is not None:
            return existed
        manifest = self.manifest(pid)
        if manifest is None:
            raise PluginError(f"插件源码缺失：{pid}")

        # 依赖装在 /config 卷上，import 之前要先进 sys.path
        site = self.site_dir(pid)
        if site.exists() and str(site) not in sys.path:
            sys.path.insert(0, str(site))
        browsers = [str(x) for x in (manifest.get("browser") or [])]
        if browsers:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(self.browsers_path(pid, browsers))

        module = self._module(pid, manifest)
        router = APIRouter()
        # 注入的对象优先（测试里用假管理器顶替真管理器）
        self.ctx.override = self._injected.get(pid)
        try:
            obj = module.setup(router, self.ctx) if hasattr(module, "setup") else None
        finally:
            self.ctx.override = None
        runtime = PluginRuntime(pid, manifest, module, obj, router,
                                str(manifest.get("prefix") or f"/api/{pid}"))
        with self._lock:
            self.loaded[pid] = runtime
        if self.app is not None:
            self._mount(runtime)
        if self._running:
            # 运行期才装上的插件：不用重启，线程也一起起来
            runtime.start()
        log.info("插件 %s 已加载（%s）", pid, runtime.prefix)
        return runtime

    def _mount(self, runtime: PluginRuntime) -> None:
        """把插件路由挂到主应用，并记下加进来的 route（卸载时要摘掉）。"""
        before = {id(r) for r in self.app.router.routes}
        self.app.include_router(runtime.router, prefix=runtime.prefix)
        runtime.routes = [r for r in self.app.router.routes if id(r) not in before]

    def detach(self, pid: str) -> bool:
        """停线程 + 摘路由。进程不重启也能把功能收干净。"""
        runtime = self.loaded.pop(pid, None)
        if runtime is None:
            return False
        runtime.stop_thread()
        if self.app is not None and runtime.routes:
            dead = {id(r) for r in runtime.routes}
            self.app.router.routes[:] = [
                r for r in self.app.router.routes if id(r) not in dead
            ]
            runtime.routes = []
        teardown = getattr(runtime.module, "teardown", None)
        if teardown is not None:
            try:
                teardown(runtime.obj)
            except Exception:
                log.debug("插件 %s teardown 失败", pid, exc_info=True)
        log.info("插件 %s 已停用", pid)
        return True

    # ================================================================ 批量
    def seed(self) -> None:
        """升级迁移：以前就在用的功能，装到新版不能凭空消失。

        判据是「数据库里有它的配置」（``seed_if``），不是"版本号变了就全装"：
        新部署的实例什么都查不到，三个插件就都是未安装，眼前干净。
        用户显式卸载过的会记在 ``removed`` 里，之后不再自动装回来 ——
        否则每次重启都冒出来，像卸载没生效。
        """
        installed: list[str] = []
        for manifest in self.manifests():
            pid = str(manifest["id"])
            if pid in self._state["installed"] or pid in self._state["removed"]:
                continue
            markers = [x for x in str(manifest.get("seed_if") or "").split("|") if x]
            if not any(self._marker_hit(x) for x in markers):
                continue
            self._state["installed"][pid] = {
                "version": str(manifest.get("version") or ""), "at": int(time.time())}
            self._state["enabled"][pid] = True
            installed.append(pid)
        if installed:
            self._save()
            log.info("插件：检测到这些功能以前就在用，已自动标记为已安装：%s",
                     "、".join(installed))

    def _marker_hit(self, marker: str) -> bool:
        kind, _, key = marker.partition(":")
        if kind == "meta":
            return bool(self.db.get_meta(key, ""))
        if kind == "rows":
            checker = getattr(self.db, "table_has_rows", None)
            return bool(checker and checker(key))
        return False

    def load_all(self) -> None:
        """加载所有「已安装 + 已启用」的插件（启动时调用一次）。"""
        self.seed()
        for manifest in self.manifests():
            pid = str(manifest["id"])
            if not self.is_enabled(pid):
                continue
            try:
                self.load(pid)
            except Exception as exc:  # noqa: BLE001 - 单个插件坏了不能拖垮整个服务
                log.warning("插件 %s 加载失败: %s", pid, exc)

    def start_threads(self) -> None:
        self._running = True
        for runtime in self.loaded.values():
            runtime.start()

    def stop_all(self) -> None:
        self._stop_all.set()
        for runtime in list(self.loaded.values()):
            runtime.stop_thread()

    # ================================================================ 资源
    def web_asset(self, pid: str) -> Path | None:
        """插件前端 JS 路径（只给已安装的插件）。"""
        if not self.is_installed(pid):
            return None
        manifest = self.manifest(pid)
        if manifest is None:
            return None
        name = manifest.get("web")
        if not name or not _SAFE_NAME.match(str(name)):
            return None
        path = Path(str(manifest["_dir"])) / str(name)
        return path if path.exists() else None

    # ================================================================ 测试注入
    def inject(self, pid: str, obj: Any) -> PluginRuntime:
        """测试 / 嵌入式：加载插件路由但复用传入的对象（不新建、不装依赖）。

        注入的对象会记住：后面被停用再启用时仍用它，不会突然去连真网盘。
        """
        self._injected[pid] = obj
        return self.load(pid)
