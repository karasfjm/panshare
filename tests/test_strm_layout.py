"""strm 布局 / 多网盘路径 / 每条配置的请求头 / 分享「直接生成」的回归测试。

这几个点都是线上反馈改出来的，单独锁住：

1. strm 内容改成 ``请求头 + pickcode + name（URL 编码） + 115 用户 ID``，
   末尾的 uid 用来标识「这个 strm 属于哪个账号」，前面不再有 /a/<账号名>；
2. 每条 115 STRM 配置里「一个网盘路径绑定一个本地目录」，生成前建好目录，
   手工删掉的 strm 再跑一次能补回来；
3. 配置里的「STRM 请求头」必须真的写进 strm（以前只存不用）；
4. 分享可以「直接生成」，strm 指向原分享，播放时用原分享的提取码解析。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app.db import (  # noqa: E402
    Database, strm_config_cids, strm_config_pairs, strm_config_paths,
)
from app.db import strm_config_cids as _cids  # noqa: F401,E402
from app.scanner import Scanner  # noqa: E402
from app.server import (  # noqa: E402
    _pickcode_from_tail, _play_share, _play_tail_uid, _strm_config_jobs,
)
from app.share import ShareManager  # noqa: E402
from app.strm import StrmStore  # noqa: E402


# --------------------------------------------------------------------- 基础件
def _store(base: str = "http://192.168.2.124:9527", root: str | None = None) -> StrmStore:
    root = root or tempfile.mkdtemp(prefix="panshare-strm-")
    return StrmStore(Path(root), base_url=base, play_url_ext=True)


def _db() -> Database:
    tmp = tempfile.mkdtemp(prefix="panshare-db-")
    return Database(str(Path(tmp) / "t.db"))


# ------------------------------------------------------------------ strm 内容
def test_play_url_new_layout():
    """请求头 + pickcode + name（URL 编码）+ 115 用户 ID。"""
    store = _store()
    url = store.play_url("ak5qy5s306coxh4qb", "黑袍纠察队 第一集.mkv", uid="1987654321")
    assert url == (
        "http://192.168.2.124:9527/p115/play/ak5qy5s306coxh4qb/"
        "%E9%BB%91%E8%A2%8D%E7%BA%A0%E5%AF%9F%E9%98%9F%20%E7%AC%AC%E4%B8%80%E9%9B%86.mkv/"
        "1987654321"
    ), url


def test_play_url_uses_account_uid_by_default():
    """没显式传 uid 时用这个 store 所属账号的 115 用户 ID。"""
    root = tempfile.mkdtemp(prefix="panshare-strm-")
    store = StrmStore(Path(root), base_url="http://h:9527", account_uid="555")
    assert store.play_url("pc1", "a.mkv") == "http://h:9527/p115/play/pc1/a.mkv/555"


def test_play_url_has_no_account_prefix():
    """strm 里不再出现 /a/<账号名>，账号身份只在末尾的 uid 上。"""
    root = tempfile.mkdtemp(prefix="panshare-strm-")
    store = StrmStore(Path(root), base_url="http://h:9527",
                      route_prefix="/a/33t", account_uid="555")
    url = store.play_url("pc1", "a b.mkv")
    assert "/a/33t" not in url, url
    assert url.endswith("/p115/play/pc1/a%20b.mkv/555"), url


def test_play_url_legacy_without_name():
    """没有文件名时保持老形状（只多不带 uid），兼容老调用方。"""
    store = _store()
    assert store.play_url("pc1") == "http://192.168.2.124:9527/p115/play/pc1"


def test_play_url_custom_base_url():
    """每条配置的请求头要能覆盖全局。"""
    store = _store()
    url = store.play_url("pc1", "a.mkv", uid="1", base_url="http://172.17.0.1:9527/")
    assert url.startswith("http://172.17.0.1:9527/p115/play/pc1/"), url


@pytest.mark.parametrize("tail,expect", [
    ("pc1000/test.mkv/1000", "pc1000"),
    ("pc1000.mkv", "pc1000"),
    ("pc1000", "pc1000"),
    ("/pc1000/test.mkv/1000", "pc1000"),
])
def test_pickcode_from_tail(tail, expect):
    assert _pickcode_from_tail(tail) == expect


@pytest.mark.parametrize("tail,expect", [
    ("pc1000/test.mkv/1987654321", "1987654321"),   # 新格式：末段是 uid
    ("pc1000.mkv", ""),                             # 旧格式：没有 uid
    ("pc1000", ""),
    ("ed2kkey/1987654321", "1987654321"),
])
def test_play_tail_uid(tail, expect):
    assert _play_tail_uid(tail) == expect


def test_repair_missing_rewrites_deleted_strm():
    """全量生成过、后来被手工删掉的 strm：再跑一次要能补回来。"""
    db = _db()
    root = Path(tempfile.mkdtemp(prefix="panshare-strm-"))
    store = StrmStore(root, base_url="http://h:9527", account_uid="777")
    cfg = SimpleNamespace(strm_all_files=True, include_ext=(), include_regex="",
                          exclude_regex="", exclude_dirs=(), include_root_name=True)
    scanner = Scanner(cfg, db, api=None, store=store)
    db.add_root(100, "影音", "")
    target = store.abspath("影音/a.mkv.strm")
    db.upsert_file({
        "id": 1, "parent_id": 100, "name": "a.mkv", "path": "影音/a.mkv",
        "pickcode": "pc9", "size": 1, "sha1": "", "ctime": 0, "mtime": 0,
        "strm_path": str(target),
    })
    assert not target.exists()
    assert scanner.repair_missing("影音") == 1
    assert target.exists()
    assert target.read_text(encoding="utf-8").strip() == (
        "http://h:9527/p115/play/pc9/a.mkv/777")


# ------------------------------------------------- 一路径一目录（path_dirs）
def test_pairs_from_path_dirs():
    row = {"path_dirs": json.dumps([
        {"cid": "100", "path": "/电影", "dir": "/strm/电影"},
        {"cid": "200", "path": "/剧集", "dir": "/strm/剧集"},
    ], ensure_ascii=False)}
    assert strm_config_pairs(row) == [
        {"cid": "100", "path": "/电影", "dir": "/strm/电影"},
        {"cid": "200", "path": "/剧集", "dir": "/strm/剧集"},
    ]
    # 一个路径一个目录：不再自动加子目录
    assert _strm_config_jobs(row, "strm") == [
        (100, "/电影", "电影"),
        (200, "/剧集", "剧集"),
    ]


def test_pairs_fall_back_to_legacy_fields():
    row = {"remote_cids": "100,200", "remote_paths": "/电影,/剧集",
           "local_dir": "/strm/115-原档"}
    pairs = strm_config_pairs(row)
    assert [p["cid"] for p in pairs] == ["100", "200"]
    assert {p["dir"] for p in pairs} == {"/strm/115-原档"}



# ------------------------------------------------------- 多网盘路径 / 本地前缀
def test_config_cids_and_paths_parse():
    row = {"remote_cids": "100,200,300", "remote_paths": "/电影,/剧集,/动漫"}
    assert strm_config_cids(row) == [100, 200, 300]
    assert strm_config_paths(row) == ["/电影", "/剧集", "/动漫"]
    # 老配置只有单个字段
    old = {"remote_cid": "100", "remote_path": "/电影"}
    assert strm_config_cids(old) == [100]
    assert strm_config_paths(old) == ["/电影"]


def test_jobs_single_path_keeps_prefix():
    """只有一个网盘路径：本地前缀就是「本地 STRM 目录」，老行为不变。"""
    row = {"remote_cids": "100", "remote_paths": "/影音", "local_dir": "115-原档/影音"}
    assert _strm_config_jobs(row) == [(100, "/影音", "115-原档/影音")]


def test_jobs_multi_path_uses_per_path_subdir():
    """多个网盘路径：每条落到自己的子目录，避免同名互相覆盖。"""
    row = {
        "remote_cids": "100,200",
        "remote_paths": "/电影,/剧集",
        "local_dir": "115-原档",
    }
    assert _strm_config_jobs(row) == [
        (100, "/电影", "115-原档/电影"),
        (200, "/剧集", "115-原档/剧集"),
    ]


def test_jobs_strips_mount_root_segment():
    """「本地 STRM 目录」填的是容器挂载路径（/strm/...）时不能多一层 strm。

    STRM_ROOT 本身就是 /strm，用户填 /strm 或 /strm/1080p 时，
    生成的文件必须落在 /strm 和 /strm/1080p，而不是 /strm/strm/...
    """
    row = {"remote_cids": "100", "remote_paths": "/1080p", "local_dir": "/strm"}
    assert _strm_config_jobs(row, "strm") == [(100, "/1080p", "")]

    row = {"remote_cids": "100", "remote_paths": "/1080p", "local_dir": "/strm/1080p"}
    assert _strm_config_jobs(row, "strm") == [(100, "/1080p", "1080p")]

    # STRM_ROOT 改成别的名字时，按那个名字剥
    row = {"remote_cids": "100", "remote_paths": "/1080p", "local_dir": "/media"}
    assert _strm_config_jobs(row, "media") == [(100, "/1080p", "")]


def test_scanner_base_url_per_config():
    """扫描时按目录前缀取那条配置的请求头（这就是「填了却没用」的修复）。"""
    db = _db()
    db.add_root(100, "影音", "115-原档/影音")
    db.add_root(200, "剧集", "115-原档/剧集")
    db.upsert_strm_config({
        "id": "cfg1", "account": "", "name": "影音", "remote_cids": "100",
        "remote_paths": "/影音", "local_dir": "115-原档/影音",
        "base_url": "http://172.17.0.1:9527",
    })
    cfg = SimpleNamespace(strm_all_files=True, include_ext=(), include_regex="",
                          exclude_regex="", exclude_dirs=(), include_root_name=True)
    store = _store()
    scanner = Scanner(cfg, db, api=None, store=store)
    assert scanner.base_url_for("115-原档/影音/电影/a.mkv") == "http://172.17.0.1:9527"
    # 没配请求头的目录：返回空（用全局 base_url）
    assert scanner.base_url_for("115-原档/剧集/a.mkv") == ""


def test_scanner_reads_shared_configs_for_sub_account():
    """子账号的索引库是独立的，但 115 STRM 配置只存在主库。

    子账号扫描器必须读主库、且只看自己名下的配置，否则子账号那条
    配置的「STRM 请求头」永远取不到（写出来还是全局地址）。
    """
    main_db = _db()      # 主库：存 strm_configs
    sub_db = _db()       # 子账号库：只存自己的根目录与文件索引
    sub_db.add_root(300, "LTX", "LTX")
    sub_db.add_root(400, "其它", "其它")
    main_db.upsert_strm_config({
        "id": "cfg_sub", "account": "不吃香菜", "name": "LTX",
        "remote_cids": "300", "remote_paths": "/LTX",
        "local_dir": "LTX", "base_url": "http://172.17.0.1:9527",
    })
    # 另一个账号在同名前缀上配了别的请求头：不能串到子账号这边
    main_db.upsert_strm_config({
        "id": "cfg_other", "account": "33t", "name": "LTX",
        "remote_cids": "400", "remote_paths": "/其它",
        "local_dir": "其它", "base_url": "http://10.0.0.9:9527",
    })
    cfg = SimpleNamespace(strm_all_files=True, include_ext=(), include_regex="",
                          exclude_regex="", exclude_dirs=(), include_root_name=True)
    scanner = Scanner(cfg, sub_db, api=None, store=_store(),
                      configs_db=main_db, account="不吃香菜")
    assert scanner.base_url_for("LTX/a.mkv") == "http://172.17.0.1:9527"
    # 别的账号的配置对当前账号不可见 → 回落全局地址
    assert scanner.base_url_for("其它/a.mkv") == ""


def _scan_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        strm_all_files=True, include_ext=(), include_regex="", exclude_regex="",
        exclude_dirs=(), include_root_name=True, scan_workers=1, smart_scan=True,
        delete_orphan=True, ed2k_dir_cid=0, scan_app="web", page_workers=0,
        scan_cooldown=0.0, roots={},
    )


def test_locked_empty_prefix_survives_root_resolve():
    """配置把「本地 STRM 目录」填成挂载根（如 /strm）时前缀是空串。

    空串不能用「目录名」兜底，否则下一次全量扫描会把文件从 /strm 挪到
    /strm/1080p，和上一次生成的位置对不上。
    """
    db = _db()
    db.add_root(100, "1080p")           # 手动加的根：没锁，用目录名兜底
    db.add_root(200, "LTX")
    db.set_root_prefix(200, "")         # 配置明确指定「就放根上」
    scanner = Scanner(_scan_cfg(), db, api=None, store=_store())
    roots = scanner.resolve_roots()
    assert roots[100] == "1080p"
    assert roots[200] == ""


def test_prune_never_deletes_other_accounts_files():
    """多个账号共用 STRM_ROOT：清理孤儿只能删「自己上一轮写过的」。

    线上事故：子账号库是空的 → 全量扫描 0 个目录 → 按「不在我的索引里」
    把共用的 /strm 下另一个账号的 8000+ 个 strm 全删了。
    """
    root = Path(tempfile.mkdtemp(prefix="panshare-prune-"))
    store = StrmStore(root, base_url="http://h:9527", play_url_ext=True)
    mine = root / "mine" / "a.strm"
    mine.parent.mkdir(parents=True)
    mine.write_text("http://h:9527/p115/play/pc1", encoding="utf-8")
    other = root / "other" / "b.strm"
    other.parent.mkdir(parents=True)
    other.write_text("http://h:9527/p115/play/pc2", encoding="utf-8")

    db = _db()
    db.upsert_files([{
        "id": 1, "parent_id": 0, "name": "a.mkv", "path": "mine/a.mkv",
        "pickcode": "pc1", "strm_path": str(mine),
    }])
    scanner = Scanner(_scan_cfg(), db, api=None, store=store)
    # 上一轮索引里只有 mine/a.strm；这次索引里它也在 → 谁都不该被删
    scanner._prev_paths = {str(mine.resolve())}
    assert scanner.prune() == 0
    assert mine.exists() and other.exists()

    # mine 从 115 上没了：只删它，别人的文件必须留着
    db.clear()
    assert scanner.prune() == 1
    assert not mine.exists()
    assert other.exists()


def test_scan_without_roots_touches_nothing():
    """没登记监控目录时不能「扫 0 个目录 → 认定全失效」。"""
    root = Path(tempfile.mkdtemp(prefix="panshare-noroots-"))
    store = StrmStore(root, base_url="http://h:9527", play_url_ext=True)
    keep = root / "影音" / "a.strm"
    keep.parent.mkdir(parents=True)
    keep.write_text("http://h:9527/p115/play/pc1", encoding="utf-8")

    db = _db()
    db.upsert_files([{
        "id": 1, "parent_id": 0, "name": "a.mkv", "path": "影音/a.mkv",
        "pickcode": "pc1", "strm_path": str(keep),
    }])
    scanner = Scanner(_scan_cfg(), db, api=None, store=store)
    stats = scanner.run(full=True)
    assert stats.removed == 0
    assert keep.exists()
    # 索引也不能被清空（清了下次就真按空索引开始扫）
    assert len(db.all_strm_paths()) == 1


# --------------------------------------------------------------- 分享直接生成
class _FakeDL:
    url = "https://cdn.example/x.mkv"
    headers = {"User-Agent": "ua"}


class _FakeClient:
    def __init__(self):
        self.calls = []

    def share_download_url(self, payload):
        self.calls.append(payload)
        return _FakeDL()


def _share_mgr() -> tuple[ShareManager, Database, StrmStore]:
    db = _db()
    store = _store()
    cfg = SimpleNamespace(share_enabled=True, share_strm_dir="115-分享",
                          share_dir_cid=0, share_dir_name="分享接收", scan_app="web",
                          scan_cooldown=0.0)
    mgr = ShareManager(cfg, db, api=SimpleNamespace(client=_FakeClient()), store=store)
    return mgr, db, store


def test_share_direct_writes_original_share_url():
    mgr, db, store = _share_mgr()
    tree = {
        "title": "某部电影",
        "items": [
            {"fid": 777, "name": "movie.mkv", "rel_path": "movie.mkv", "size": 10},
            {"fid": 778, "name": "sub.srt", "rel_path": "sub.srt", "size": 1},
        ],
    }
    done = mgr._write_direct("swabc", tree)
    assert done == 2
    rows = db.list_share_files("swabc")
    by_name = {r["name"]: r for r in rows}
    content = Path(by_name["movie.mkv"]["strm_path"]).read_text(encoding="utf-8").strip()
    # 指向**原分享**：/p115/share/<原分享码>/<原分享里的 fid>
    assert content.endswith("/p115/share/swabc/777.mkv"), content


def test_play_share_direct_uses_original_receive_code():
    """直接生成的分享：播放时用原分享的提取码解析直链。"""
    mgr, db, store = _share_mgr()
    db.upsert_share({
        "share_code": "swabc", "receive_code": "abcd", "url": "u",
        "name": "某部电影", "direct": 1, "status": "ready", "total": 1, "done": 1,
    })
    api = SimpleNamespace(client=_FakeClient())
    cfg = SimpleNamespace(share_enabled=True, play_mode="302", play_user_agent="ua")
    request = SimpleNamespace(headers={"user-agent": "ua"}, method="GET")
    resp = _play_share(cfg, mgr, db, api, "swabc", "777.mkv", request)
    assert getattr(resp, "status_code", None) == 302, resp
    call = api.client.calls[-1]
    assert call == {"file_id": 777, "share_code": "swabc", "receive_code": "abcd"}, call


def test_play_share_unknown_and_non_direct():
    mgr, db, store = _share_mgr()
    api = SimpleNamespace(client=_FakeClient())
    cfg = SimpleNamespace(share_enabled=True, play_mode="302", play_user_agent="ua")
    request = SimpleNamespace(headers={}, method="GET")
    with pytest.raises(Exception):
        _play_share(cfg, mgr, db, api, "nope", "1", request)
    db.upsert_share({"share_code": "sx", "receive_code": "r", "direct": 0,
                     "status": "ready", "name": "x"})
    with pytest.raises(Exception):
        # 转存模式但还没有自己的分享码：老逻辑应该报 404，不能拿原分享去解析
        _play_share(cfg, mgr, db, api, "sx", "1", request)


# --------------------------------------------------- 秒传：按 strm 末尾 uid 找源账号
# 线上问题：strm 去掉 /a/<账号名>、末尾改成 uid 之后，秒传整段失效。
# 原因是「这个 strm 属于哪个账号」还从 URL 前缀里取（永远是空 → 只会查主库），
# 子账号的文件在主库里查不到，于是静默回退成源盘直出。
class _FakeEngine:
    def __init__(self, name: str, db: Database, uid: str = ""):
        self.name = name
        self.db = db
        self.uid = uid


class _FakeHub:
    def __init__(self, engines: list[_FakeEngine], default: _FakeEngine):
        self._engines = {e.name: e for e in engines}
        self.default = default

    def get(self, name: str):
        if name == self.default.name:
            return self.default
        return self._engines.get(name)

    def engines(self) -> dict[str, _FakeEngine]:
        return dict(self._engines)

    def by_uid(self, uid: str):
        for eng in list(self._engines.values()) + [self.default]:
            if eng.uid and eng.uid == uid:
                return eng
        return None


class _FakeInstant:
    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.registered: list[tuple] = []

    def account_for_user(self, user_id: str) -> str:
        return self.mapping.get(user_id, "")

    def register(self, *args) -> None:
        self.registered.append(args)


def _instant_proxy(main_db: Database, hub: _FakeHub, instant: _FakeInstant):
    from app.emby import EmbyProxy

    proxy = object.__new__(EmbyProxy)
    proxy.db = main_db
    proxy.hub = hub
    proxy.instant = instant
    proxy.store = _store()
    proxy.account = ""
    proxy.settings = {}
    return proxy


def _file_row(pickcode: str, name: str = "The.Boys.S02E02.1080p.mkv") -> dict:
    return {
        "id": 9, "parent_id": 1, "name": name, "path": "黑袍纠察队/Season 2/" + name,
        "pickcode": pickcode, "size": 1024, "sha1": "A" * 40,
        "ctime": 0, "mtime": 0, "strm_path": "/strm/x.strm",
    }


def _layout_fixture():
    main_db = _db()          # 主账号索引
    sub_db = _db()           # 33t 的索引
    sub_db.upsert_file(_file_row("pc33t"))
    hub = _FakeHub([_FakeEngine("33t", sub_db, uid="341885548")],
                   default=_FakeEngine("", main_db, uid="331466418"))
    instant = _FakeInstant({"emby-user": "不吃香菜"})
    return _instant_proxy(main_db, hub, instant), instant, sub_db


def test_instant_route_resolves_source_account_from_uid_tail():
    """新 strm：/p115/play/<pickcode>/<name>/<uid> 要能找回源账号再来秒传。"""
    proxy, instant, _sub = _layout_fixture()
    url = ("http://172.17.0.1:9527/p115/play/pc33t/"
           "The.Boys.S02E02.1080p.mkv/341885548")
    got = proxy._instant_url_for(url, "emby-user", {"Name": "x"})
    assert got is not None, "子账号的文件必须能命中秒传路由"
    assert "/p115/instant/" + "A" * 40 + ".mkv" in got, got
    assert "account=%E4%B8%8D%E5%90%83%E9%A6%99%E8%8F%9C" in got, got
    assert "source_account=33t" in got, got
    # 登记时也要带对源账号 —— 二次校验要用源账号的直链读数据
    assert instant.registered
    assert instant.registered[-1][-1] == "33t"


def test_instant_route_falls_back_when_strm_has_no_uid():
    """老 strm（没有 uid 尾段）也要靠「翻各账号索引」找回源账号。"""
    proxy, instant, _sub = _layout_fixture()
    url = "http://h:9527/p115/play/pc33t/The.Boys.S02E02.1080p.mkv"
    got = proxy._instant_url_for(url, "emby-user", {"Name": "x"})
    assert got is not None, got
    assert "source_account=33t" in got, got
    assert instant.registered[-1][-1] == "33t"


def test_instant_route_main_account_has_no_source_account():
    """主账号的文件：不带 source_account（它就是「主服务那一套」）。"""
    proxy, instant, _sub = _layout_fixture()
    proxy.db.upsert_file(_file_row("pcmain", "main.mkv"))
    got = proxy._instant_url_for(
        "http://h:9527/p115/play/pcmain/main.mkv/331466418", "emby-user", {})
    assert got is not None, got
    assert "source_account=" not in got, got
    assert instant.registered[-1][-1] == ""


def test_instant_route_skips_without_user_mapping():
    """没映射秒传账号的用户：不做秒传，交给原地址播。"""
    proxy, _instant, _sub = _layout_fixture()
    url = ("http://h:9527/p115/play/pc33t/"
           "The.Boys.S02E02.1080p.mkv/341885548")
    assert proxy._instant_url_for(url, "someone-else", {}) is None


# --------------------------------------------------- 全量覆盖 / 全新生成 / 事件日志
def test_write_force_rewrites_same_content():
    """``force=True`` 时内容相同也要重写（全量 / 全新生成靠它刷新 mtime）。"""
    store = _store()
    path, changed = store.write("a/b.mkv", "pc1", "b.mkv", uid="100")
    assert changed is True
    assert Path(path).exists()
    # 内容一样 → 不 force 就不写
    _, again = store.write("a/b.mkv", "pc1", "b.mkv", uid="100")
    assert again is False
    # force → 一定重写
    _, forced = store.write("a/b.mkv", "pc1", "b.mkv", uid="100", force=True)
    assert forced is True


def test_remove_strm_files_keeps_metadata():
    """「全新生成」只删 *.strm，nfo / 字幕这些元数据必须留着。"""
    store = _store()
    store.write("d/电影.mkv", "pc1", "电影.mkv", uid="100")
    (store.root / "d" / "电影.nfo").write_text("meta", encoding="utf-8")
    (store.root / "d" / "海报.jpg").write_text("img", encoding="utf-8")
    removed = store.remove_strm_files("d")
    assert removed == 1, removed
    assert not (store.root / "d" / "电影.strm").exists()
    assert (store.root / "d" / "电影.nfo").exists()
    assert (store.root / "d" / "海报.jpg").exists()


def test_remove_strm_paths_only_touches_given_files():
    """「全新生成」必须按索引里的路径精确删，不能按目录整片删。

    线上真实场景：两条 115 STRM 配置的「本地 STRM 目录」都填 ``/strm``
    （前缀为空串），按目录 rglob 删会把另一条配置、另一个账号的 strm
    一起带走，几万个文件就没了。
    """
    store = _store()
    mine, _ = store.write("a/我的.mkv", "pc1", "我的.mkv", uid="100")
    other, _ = store.write("b/别人的.mkv", "pc2", "别人的.mkv", uid="200")
    (store.root / "a" / "我的.nfo").write_text("meta", encoding="utf-8")
    # 只删「我这条配置索引里记着的」那一个
    removed = store.remove_strm_paths([mine])
    assert removed == 1, removed
    assert not (store.root / "a" / "我的.strm").exists()
    assert (store.root / "a" / "我的.nfo").exists(), "元数据不能删"
    assert Path(other).exists(), "别人目录里的 strm 不能动"


def test_remove_strm_paths_rejects_outside_root_and_non_strm():
    """安全边界：只删 STRM_ROOT 内的 *.strm，越界和别的后缀一律跳过。"""
    store = _store()
    outside = Path(tempfile.mkdtemp(prefix="panshare-outside-")) / "x.strm"
    outside.write_text("http://x", encoding="utf-8")
    inside_nfo, _ = store.write("a/片子.mkv", "pc1", "片子.mkv", uid="100")
    nfo = Path(inside_nfo).with_suffix(".nfo")
    nfo.write_text("meta", encoding="utf-8")
    removed = store.remove_strm_paths([str(outside), str(nfo), "", None])
    assert removed == 0, removed
    assert outside.exists(), "STRM_ROOT 之外的文件不许删"
    assert nfo.exists(), "非 .strm 不许删"
    assert Path(inside_nfo).exists()


def test_scanner_full_scan_forces_rewrite():
    """监控目录点「全量」= 全部重新生成覆盖（内容没变也重写）。"""
    db = _db()
    store = _store()
    cfg = SimpleNamespace(
        strm_all_files=True, include_ext=(), include_regex="", exclude_regex="",
        exclude_dirs=(), include_root_name=False, scan_workers=1, page_workers=1,
        scan_app="", scan_cooldown=0, smart_scan=True, delete_orphan=False,
        roots={}, ed2k_dir_cid=0,
    )
    nodes = {
        1: [
            {"id": 11, "parent_id": 1, "name": "电影.mkv", "is_dir": False,
             "size": 100, "sha1": "a" * 40, "ctime": 0, "mtime": 0,
             "pickcode": "pc11"},
        ],
    }

    class _API:
        def iter_dir(self, cid, **kw):
            for node in nodes.get(cid, []):
                yield node

        def get_node(self, cid):
            return {"id": cid, "parent_id": 0, "name": "root", "is_dir": True}

        def ensure_pickcode(self, node):
            return node["pickcode"]

    db.add_root(1, "")
    db.set_root_prefix(1, "")
    scanner = Scanner(cfg, db, _API(), store)
    first = scanner.run(full=True)
    assert first.written == 1, first.as_dict()
    assert first.overwritten == 1, first.as_dict()     # 全量就是强制重写
    target = store.root / "电影.strm"
    mtime1 = target.stat().st_mtime_ns
    # 增量：内容没变 → 跳过
    inc = scanner.run(full=False)
    assert inc.written == 0, inc.as_dict()
    assert inc.overwritten == 0, inc.as_dict()
    # 再全量 → 又重写一遍（mtime 必须变）
    again = scanner.run(full=True)
    assert again.overwritten == 1, again.as_dict()
    assert target.stat().st_mtime_ns >= mtime1


def test_strm_event_log_roundtrip():
    db = _db()
    db.add_strm_event("run_done", "完成：测试", config_id="cfg1", account="acc1",
                      detail=json.dumps({"files": 3}, ensure_ascii=False))
    db.add_strm_event("config_save", "新增配置：x", config_id="cfg1")
    rows = db.list_strm_events(limit=10)
    assert len(rows) == 2, rows
    assert rows[0]["kind"] == "config_save", rows      # 按时间倒序
    assert rows[1]["detail"] == '{"files": 3}', rows
    assert len(db.list_strm_events(limit=10, config_id="cfg1")) == 2
    assert db.list_strm_events(limit=10, config_id="nope") == []
    assert db.clear_strm_events("cfg1") == 2
    assert db.list_strm_events(limit=10) == []
