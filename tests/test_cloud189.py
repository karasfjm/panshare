"""天翼云盘（189）集成测试：登录态存取 / strm 生成 / 全新模式的精确删除。

全部用假客户端跑，不联网 —— 天翼云盘的接口要登录态才能用，而 CI 里没有账号。
这里锁住的是集成逻辑本身：

1. strm 内容必须是 ``<请求头>/p189/play/<fileId>``（不能直接写云盘直链：
   直链带时效和凭据，落盘就等于把账号凭据写进媒体库）；
2. 后缀过滤 / 「不过滤」两种模式的行为；
3. 云端删掉（或改名）文件后，全量能把本地残留的 strm 收掉；
4. 「全新生成」只删**这条配置索引里记录过**的 strm —— 同一目录下别的配置、
   别的账号（比如 115 的）生成的文件一个都不能动。
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cloud189 import (  # noqa: E402
    Cloud189Client, Cloud189Error, Cloud189Manager, _expires_to_ms, _unhtml,
    build_remote, parse_remote, parse_session_key, qr_matrix, split_exts,
)
from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.strm import StrmStore  # noqa: E402

BASE = "http://192.168.2.124:9527"


# --------------------------------------------------------------------- 假客户端
class FakeClient:
    """按 folder_id 返回固定目录树；``tree`` 可在用例里改（模拟改名 / 删除）。"""

    def __init__(self, tree: dict[str, list[dict]],
                 families: list[dict] | None = None):
        self.tree = tree
        self.calls: list[str] = []
        self.families = families or []

    def list_files(self, folder_id: str, family_id: str = "",
                   page_size: int = 100) -> list[dict]:
        self.calls.append(str(folder_id))
        return self.tree.get(str(folder_id), [])

    def list_families(self) -> list[dict]:
        return self.families

    def download_url(self, file_id: str, family_id: str = "") -> str:
        tail = f"f{family_id}/" if family_id else ""
        return f"https://cdn.example.com/{tail}{file_id}?token=abc"


def _dir(fid: str, name: str) -> dict:
    return {"id": fid, "name": name, "size": 0, "is_dir": True}


def _file(fid: str, name: str, size: int = 1024) -> dict:
    return {"id": fid, "name": name, "size": size, "is_dir": False}


def _manager(tree: dict[str, list[dict]], families: list[dict] | None = None,
             patch: bool = True):
    root = Path(tempfile.mkdtemp(prefix="panshare-189-"))
    strm_root = root / "strm"
    strm_root.mkdir(parents=True, exist_ok=True)
    cfg = Config(strm_root=strm_root, data_dir=root, auth_token="")
    db = Database(str(root / "t.db"))
    store = StrmStore(strm_root, base_url=BASE, play_url_ext=True)
    mg = Cloud189Manager(cfg, db, store)
    fake = FakeClient(tree, families)
    if patch:
        mg.client = lambda *a, **k: fake   # 跳过 token 校验，只测生成逻辑
    return mg, db, store, fake


def _make_config(mg, db, *, local: str = "天翼", exts: str = "", all_files: bool = False,
                 base_url: str = BASE) -> dict:
    row = {
        "id": "c189_test",
        "account": "c189test",
        "name": "天翼电影",
        "remote_id": "-11",
        "remote_path": "/个人云/电影",
        "local_dir": local,
        "base_url": base_url,
        "media_exts": exts,
        "all_files": all_files,
        "status": "idle",
    }
    db.upsert_cloud189_config(row)
    return db.get_cloud189_config("c189_test")


# ------------------------------------------------------------------ 纯函数单测
def test_expires_to_ms_handles_three_formats():
    """expiresIn 有三种口径：时长秒数 / 秒级时间戳 / 毫秒时间戳。"""
    assert _expires_to_ms(0) == 0
    assert _expires_to_ms("") == 0
    # 时长：应该落在未来
    future = _expires_to_ms(3600)
    assert future > 10**12
    # 秒级时间戳 / 毫秒时间戳都要落到同一时刻附近
    assert _expires_to_ms(1750000000) == 1750000000000
    assert _expires_to_ms(1750000000000) == 1750000000000


def test_split_exts_accepts_both_styles():
    assert split_exts("mp4,.MKV; avi") == {".mp4", ".mkv", ".avi"}
    assert split_exts("mp4，mkv") == {".mp4", ".mkv"}
    assert split_exts("") == set()


def test_qr_matrix_is_square_and_large_enough():
    rows = qr_matrix("https://cloud.189.cn/api/portal/loginUrl.action?uuid=abc")
    assert rows, "二维码矩阵不能为空"
    size = len(rows)
    assert size == len(rows[0])
    assert (size - 17) % 4 == 0, "尺寸必须是 4v+17"
    assert any(any(row) for row in rows), "至少要有深色模块"


def test_qr_matrix_matches_reference_encoder():
    """和 python-qrcode 逐模块对照。

    自带的编码器是本项目唯一的「手写协议实现」，画歪一个模块就是扫不动的
    二维码 —— 装上 ``qrcode`` 的环境里必须完全一致（没装就跳过）。
    """
    qrcode = __import__("pytest").importorskip("qrcode")
    from qrcode.constants import ERROR_CORRECT_M

    content = ("https://open.e.189.cn/api/account/qrClinentLogin.do"
               "?paras=new_uuid%3Dz8tyiopwtldsttx9%7C8013418323")
    mine = qr_matrix(content, "M")
    ref_qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=0, mask_pattern=0)
    ref_qr.add_data(content, optimize=0)
    ref_qr.make(fit=True)
    ref = ref_qr.get_matrix()
    assert len(mine) == len(ref), (len(mine), len(ref))
    diff = sum(1 for y, row in enumerate(ref)
               for x, cell in enumerate(row) if bool(cell) != mine[y][x])
    assert diff == 0, f"二维码矩阵与参考实现有 {diff} 个模块不一致"


# ---------------------------------------------------------------- strm 生成
def test_incremental_writes_play_url_and_index():
    tree = {
        "-11": [_dir("100", "电影")],
        "100": [_file("1001", "阿凡达.mkv"), _file("1002", "封面.jpg")],
    }
    mg, db, store, _fake = _manager(tree)
    row = _make_config(mg, db, exts="mp4,mkv")
    mg._run(row, "inc")

    target = store.root / "天翼" / "电影" / "阿凡达.strm"
    assert target.exists(), "应该按「本地目录 + 云端结构」落盘"
    content = target.read_text(encoding="utf-8").strip()
    assert content == (f"{BASE}/p189/play/c189test/1001/"
                       "%E9%98%BF%E5%87%A1%E8%BE%BE.mkv"), content
    assert "cdn.example.com" not in content, "绝不能把云盘直链写进 strm"
    # jpg 被后缀过滤掉
    assert not (store.root / "天翼" / "电影" / "封面.strm").exists()
    rows = db.list_cloud189_files("c189_test")
    assert [r["file_id"] for r in rows] == ["1001"]
    assert db.get_cloud189_config("c189_test")["status"] == "idle"


def test_all_files_mode_ignores_suffix():
    tree = {"-11": [_file("1", "a.jpg"), _file("2", "b.mkv")]}
    mg, db, store, _fake = _manager(tree)
    row = _make_config(mg, db, local="", all_files=True)
    mg._run(row, "inc")
    assert (store.root / "a.strm").exists(), "不过滤时应该直接落在 strm 根下"
    assert (store.root / "b.strm").exists()


def test_full_run_prunes_files_deleted_in_cloud():
    tree = {"-11": [_file("1", "a.mkv"), _file("2", "b.mkv")]}
    mg, db, store, fake = _manager(tree)
    row = _make_config(mg, db)
    mg._run(row, "inc")
    assert (store.root / "天翼" / "a.strm").exists()
    assert (store.root / "天翼" / "b.strm").exists()

    # 云端把 b.mkv 删了：全量应该把本地残留一起收掉
    fake.tree["-11"] = [_file("1", "a.mkv")]
    mg._run(row, "full")
    assert (store.root / "天翼" / "a.strm").exists()
    assert not (store.root / "天翼" / "b.strm").exists(), "全量要清理云端已删除的残留"
    assert [r["file_id"] for r in db.list_cloud189_files("c189_test")] == ["1"]


def test_rebuild_only_deletes_own_strm():
    """「全新」只删这条配置索引里记录过的文件，别人的 strm 不许动。"""
    tree = {"-11": [_file("1", "a.mkv")]}
    mg, db, store, _fake = _manager(tree)
    row = _make_config(mg, db)
    mg._run(row, "inc")

    mine = store.root / "天翼" / "a.strm"
    # 同目录下别人（比如 115 STRM）生成的文件
    other = store.root / "天翼" / "别人的.mkv.strm"
    other.write_text("http://x/p115/play/abc\n", encoding="utf-8")
    mine.write_text("stale", encoding="utf-8")

    before = mine.stat().st_mtime_ns
    mg._run(row, "rebuild")
    assert other.exists(), "别的账号 / 别的配置的 strm 不能被「全新」删掉"
    assert mine.read_text(encoding="utf-8").strip().startswith(
        f"{BASE}/p189/play/c189test/1")
    assert mine.stat().st_mtime_ns >= before, "全新生成必须重写（mtime 会变）"
    events = [e["kind"] for e in db.list_cloud189_events(50)]
    assert "run_done" in events and "run_start" in events
    done = [e for e in db.list_cloud189_events(50) if e["kind"] == "run_done"][0]
    assert "全新生成完成" in done["message"]


def test_same_stem_files_do_not_overwrite_each_other():
    """同目录 a.mkv / a.mp4 都会落到 a.strm，必须自动加后缀区分。"""
    tree = {"-11": [_file("1", "a.mkv"), _file("2", "a.mp4")]}
    mg, db, store, _fake = _manager(tree)
    row = _make_config(mg, db, exts="mp4,mkv")
    mg._run(row, "inc")
    files = sorted(p.name for p in (store.root / "天翼").rglob("*.strm"))
    assert len(files) == 2, files
    assert "a.strm" in files


def test_local_prefix_accepts_absolute_and_relative():
    mg, _db, store, _fake = _manager({})
    assert mg._local_prefix("天翼") == "天翼"
    assert mg._local_prefix("/strm/天翼") == "天翼"
    assert mg._local_prefix("strm/天翼") == "天翼"
    assert mg._local_prefix("strm") == ""
    assert mg._local_prefix("") == ""


# ---------------------------------------------------------------- 账号 / 状态
def _add_account(db, aid: str = "c189test", *, token: str = "tok-123",
                 ttl: int = 86400, session_key: str = "",
                 name: str = "测试账号", is_default: int = 1) -> dict:
    """直接往库里塞一个账号（有效期从现在起 ttl 秒）。"""
    db.upsert_cloud189_account({
        "id": aid, "name": name, "login_name": name + "@189.cn",
        "access_token": token,
        "expires_at": int(time.time() * 1000) + ttl * 1000,
        "session_key": session_key,
        "is_default": is_default,
        "source": "cookie" if session_key else "token",
    })
    return db.get_cloud189_account(aid)


def test_account_status_and_logout():
    mg, db, _store, _fake = _manager({})
    assert mg.status()["logged_in"] is False
    _add_account(db, "c189a", ttl=3600)
    st = mg.status()
    assert st["logged_in"] is True and st["has_token"] is True
    assert st["expires_text"], "应该能算出可读的过期时间"
    assert st["accounts"][0]["id"] == "c189a"
    assert st["accounts"][0]["logged_in"] is True
    # 退出登录：清 token 与会话，账号记录还在（配置继续挂着）
    mg.logout("c189a")
    st = mg.status()
    assert st["logged_in"] is False
    assert st["accounts"][0]["status"] == "logged_out"
    assert st["accounts"][0]["can_refresh"] is False


def test_legacy_meta_token_migrates_to_account():
    """老版本把 token 放 meta 里，升级后要自动变成一条账号记录。"""
    root = Path(tempfile.mkdtemp(prefix="panshare-189-"))
    cfg = Config(strm_root=root / "strm", data_dir=root, auth_token="")
    db = Database(str(root / "t.db"))
    db.set_meta("cloud189_token", json.dumps({
        "accessToken": "legacy-token", "expiresAt": 1795779901451,
        "loginAt": 1793187901517, "source": "qrcode"}))
    db.set_meta("cloud189_account", "18009666628@189.cn")
    db.upsert_cloud189_config({"id": "c_old", "name": "老配置", "remote_id": "-11",
                               "local_dir": "天翼"})
    mg = Cloud189Manager(cfg, db, StrmStore(root / "strm"))
    st = mg.status()
    assert st["accounts"][0]["id"] == "c189main"
    assert st["accounts"][0]["expires_at"] == 1795779901451
    # 老配置自动归到这条账号
    assert db.get_cloud189_config("c_old")["account"] == "c189main"
    # meta 里的老 token 已清空（避免下次启动又迁一遍）
    assert db.get_meta("cloud189_token", "") == ""


def test_play_url_has_account_and_family_segments():
    tree = {"-11": [_file("1", "a.mkv")]}
    mg, db, _store, fake = _manager(tree)
    url = mg.play_url("123", "影片.mkv", base_url=BASE, account_id="c189a")
    assert url == f"{BASE}/p189/play/c189a/123/%E5%BD%B1%E7%89%87.mkv", url
    # 老格式（不带账号）继续可用：没给请求头时回落到这台服务的对外地址
    assert mg.play_url("123").startswith(mg.cfg.base_url + "/p189/play/123")
    # 家庭云：多一段 fam/<家庭id>
    fam_url = mg.play_url("9", "", base_url=BASE, account_id="c189a", family_id="579")
    assert fam_url == f"{BASE}/p189/play/c189a/fam/579/9", fam_url
    # 直链缓存按「账号 + 家庭 + 文件」分开，且 120 秒内不重复打接口
    calls = {"n": 0}

    class _C(FakeClient):
        def download_url(self, file_id: str, family_id: str = "") -> str:
            calls["n"] += 1
            return super().download_url(file_id, family_id)

    mg.client = lambda *a, **k: _C(tree)
    assert mg.resolve_link("1").endswith("/1?token=abc")
    assert mg.resolve_link("1").endswith("/1?token=abc")
    assert calls["n"] == 1, "120 秒内重复取链应该命中缓存"
    # 老 strm 没有账号段：靠索引反查补出账号与家庭
    db.upsert_cloud189_file("c189_x", "77", strm_path="x.strm",
                            account="c189f", family_id="579")
    assert mg.resolve_link("77").endswith("/f579/77?token=abc")


def test_token_auto_refresh_uses_session_key(monkeypatch):
    """token 快到期时用 sessionKey 静默换新（天翼唯一的续期方式）。"""
    mg, db, _store, _fake = _manager({}, patch=False)
    _add_account(db, "c189a", ttl=60, session_key="SK-1")   # 剩 1 分钟 → 该续了
    calls = {"n": 0}

    def fake_exchange(session_key, trace=None):
        calls["n"] += 1
        assert session_key == "SK-1"
        return "fresh-token", 7200

    monkeypatch.setattr(Cloud189Client, "token_by_sessions_key",
                        staticmethod(fake_exchange))
    row = mg.client("c189a")
    assert row.access_token == "fresh-token"
    assert calls["n"] == 1
    fresh = db.get_cloud189_account("c189a")
    assert fresh["refresh_count"] == 1
    assert fresh["last_refresh"] > 0
    assert "refresh" in [e["kind"] for e in db.list_cloud189_events(20)]


def test_expired_token_without_session_key_says_relogin():
    mg, db, _store, _fake = _manager({}, patch=False)
    _add_account(db, "c189a", ttl=-60)          # 已经过期，且没有 sessionKey
    try:
        mg.client("c189a")
    except Cloud189Error as exc:
        assert "重新登录" in str(exc)
    else:
        raise AssertionError("过期的 token 必须报错")


def test_refresh_failure_marks_account_expired(monkeypatch):
    mg, db, _store, _fake = _manager({})
    _add_account(db, "c189a", ttl=60, session_key="SK-BAD")

    def boom(session_key, trace=None):
        raise Cloud189Error("InvalidSessionKey")

    monkeypatch.setattr(Cloud189Client, "token_by_sessions_key",
                        staticmethod(boom))
    try:
        mg.refresh_account("c189a", reason="manual")
    except Cloud189Error:
        pass
    else:
        raise AssertionError("续期失败要抛错")
    row = db.get_cloud189_account("c189a")
    assert row["status"] == "expired" and "InvalidSessionKey" in row["last_error"]
    kinds = [e["kind"] for e in db.list_cloud189_events(20)]
    assert "refresh_fail" in kinds


def test_auto_refresh_tick_only_touches_due_accounts(monkeypatch):
    mg, db, _store, _fake = _manager({})
    _add_account(db, "c189far", ttl=7 * 86400, session_key="SK-FAR")
    _add_account(db, "c189soon", ttl=600, session_key="SK-SOON", is_default=0)
    _add_account(db, "c189no", ttl=600, session_key="", is_default=0)
    seen = []

    def fake_exchange(session_key, trace=None):
        seen.append(session_key)
        return "tok-" + session_key, 7200

    monkeypatch.setattr(Cloud189Client, "token_by_sessions_key",
                        staticmethod(fake_exchange))
    out = mg.auto_refresh_tick()
    assert seen == ["SK-SOON"], seen        # 远期的、没有会话密钥的都不动
    assert {x["id"]: x["ok"] for x in out}["c189soon"] is True
    # 关掉自动续期的账号直接跳过
    seen.clear()
    mg.set_auto_refresh("c189soon", False)
    mg.auto_refresh_tick()
    assert seen == []


# ---------------------------------------------------------------------------
# Cookie 登录：从一段 cookie 里抠 SESSION_KEY
# ---------------------------------------------------------------------------

import urllib.parse  # noqa: E402

import pytest  # noqa: E402

_SK = "0123456789ABCDEF0123456789ABCDEF01234567"


def test_parse_session_key_from_whole_cookie():
    raw = f"GUID=abc123; COOKIE_LOGIN_USER={_SK}%26SESSION_KEY%3Dxyz789; other=1"
    assert parse_session_key(raw) == "xyz789"


def test_parse_session_key_from_value_only():
    # 浏览器复制出来的值本身就是 urlencode 过的 query
    value = urllib.parse.quote(f"LOGIN_NAME=18900000000&SESSION_KEY={_SK}", safe="")
    assert parse_session_key(value) == _SK


def test_parse_session_key_accepts_plain_key():
    assert parse_session_key(_SK) == _SK
    assert parse_session_key("  Cookie: " + _SK + "  ") == _SK


def test_parse_session_key_rejects_garbage():
    with pytest.raises(Cloud189Error):
        parse_session_key("")
    with pytest.raises(Cloud189Error):
        parse_session_key("hello world")


# ---------------------------------------------------------------------------
# 家庭云：目录引用编解码 / 目录树 / 生成
# ---------------------------------------------------------------------------

def test_parse_remote_roundtrip():
    assert parse_remote("") == ("personal", "", "-11")
    assert parse_remote("-11") == ("personal", "", "-11")
    assert parse_remote("313079017") == ("personal", "", "313079017")
    assert parse_remote("fam") == ("family_list", "", "")
    assert parse_remote("fam:579739066") == ("family", "579739066", "")
    assert parse_remote("fam:579:123") == ("family", "579", "123")
    assert build_remote("family", "579") == "fam:579"
    assert build_remote("family", "579", "123") == "fam:579:123"
    assert build_remote("personal", "", "99") == "99"


def test_list_dirs_puts_family_cloud_on_personal_root():
    """个人云根目录要能看到「家庭云」这一项，点进去列出家庭。"""
    tree = {"-11": [_dir("100", "电影")], "": [_dir("900", "家庭片")]}
    fams = [{"family_id": "579", "name": "18009666628", "count": 4}]
    mg, db, _store, _fake = _manager(tree, fams)
    _add_account(db, "c189a")

    root = mg.list_dirs("-11", "c189a")
    ids = [d["id"] for d in root["dirs"]]
    assert ids == ["fam", "100"], ids
    assert root["dirs"][0]["name"] == "家庭云"

    # 家庭列表
    fam_node = mg.list_dirs("fam", "c189a")
    assert [d["id"] for d in fam_node["dirs"]] == ["fam:579"]
    assert fam_node["dirs"][0]["name"] == "18009666628"

    # 家庭云根目录（不带 folderId 的那种列举）
    fam_root = mg.list_dirs("fam:579", "c189a")
    assert [d["id"] for d in fam_root["dirs"]] == ["fam:579:900"]
    assert fam_root["scope"] == "family"


def test_family_generation_writes_family_play_url():
    tree = {
        "": [_file("9001", "家庭片.mkv")],
        "900": [_file("9002", "子目录片.mkv")],
    }
    mg, db, store, _fake = _manager(tree)
    _add_account(db, "c189a")
    row = {
        "id": "c189_fam", "account": "c189a", "name": "家庭云电影",
        "remote_id": "fam:579", "remote_path": "/个人云/家庭云/18009666628",
        "local_dir": "天翼家庭", "base_url": BASE, "media_exts": "mkv",
        "all_files": 0, "status": "idle",
    }
    db.upsert_cloud189_config(row)
    mg._run(db.get_cloud189_config("c189_fam"), "inc")

    target = store.root / "天翼家庭" / "家庭片.strm"
    assert target.exists(), sorted(p.name for p in store.root.rglob("*.strm"))
    content = target.read_text(encoding="utf-8").strip()
    assert content == (f"{BASE}/p189/play/c189a/fam/579/9001/"
                       "%E5%AE%B6%E5%BA%AD%E7%89%87.mkv"), content
    # 索引里记着账号和家庭，老 strm 播放时能反查
    rec = db.list_cloud189_files("c189_fam")[0]
    assert rec["account"] == "c189a" and rec["family_id"] == "579"


def test_family_download_url_is_unescaped():
    """家庭云直链里带 &amp;，不还原就是 403。"""
    assert _unhtml("http://d/x?a=1&amp;b=2") == "http://d/x?a=1&b=2"


# ------------------------------------------------------- 反代端口直链播放
def test_strm_play_url_swaps_origin_to_client_address():
    """天翼 strm 里的请求头（docker 网关）→ 换成客户端真正能访问的地址。

    strm 内容是 ``<请求头>/p189/play/...``，生成时用的是 ``BASE_URL``
    （容器里常常是 172.17.0.1），局域网播放器连不上 —— 这也是「天翼在
    Emby 里点开就转圈」的根因。
    """
    mg, _db, store, _fake = _manager({})
    target = store.root / "天翼" / "影片.strm"
    target.parent.mkdir(parents=True, exist_ok=True)
    content = "http://172.17.0.1:9527/p189/play/c189a/2001/%E5%BD%B1%E7%89%87.mkv"
    target.write_text(content + "\n", encoding="utf-8")

    url = mg.strm_play_url(str(target), "http://192.168.2.124:7259")
    assert url == (
        "http://192.168.2.124:7259/p189/play/c189a/2001/%E5%BD%B1%E7%89%87.mkv"
    ), url
    # 没有客户端地址就原样返回（不瞎猜）
    assert mg.strm_play_url(str(target), "") == content
    # origin 已经一致时不动
    assert mg.strm_play_url(str(target), "http://172.17.0.1:9527") == content
    # 不是天翼的 strm（115 的）→ 空串，交给 115 那套解析
    other = store.root / "115.strm"
    other.write_text("http://172.17.0.1:9527/p115/play/pc123/a.mkv",
                     encoding="utf-8")
    assert mg.strm_play_url(str(other), "http://192.168.2.124:7259") == ""
    # 文件不存在不能炸
    assert mg.strm_play_url(str(store.root / "nope.strm"), "http://x:1") == ""


def test_emby_proxy_rewrites_cloud189_to_proxy_port():
    """Emby 反代要把天翼 strm 改写成「反代端口 + /p189/play」，播放器才连得上。

    115 的 strm 会被解析成 115 最终直链；天翼这边的直链带签名、13 分钟就过期，
    缓存下来给播放器会在拖动进度条时 403，所以交给反代端口**每次现取**。
    """
    from types import SimpleNamespace

    from app.emby import EmbyProxy

    mg, _db, store, _fake = _manager({})
    target = store.root / "天翼" / "影片.strm"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{BASE}/p189/play/c189a/2001/%E5%BD%B1%E7%89%87.mkv",
                      encoding="utf-8")

    proxy = EmbyProxy(mg.cfg, _db, api=None, store=store, cloud189=mg)

    class _Req:
        headers = {"host": "192.168.2.124:7259", "user-agent": "Infuse/7"}
        query_params = {}
        url = SimpleNamespace(scheme="http")

    payload = {"MediaSources": [{"Path": str(target), "Protocol": "File"}]}
    out = proxy.rewrite_playback_info(payload, _Req(), b"")
    source = out["MediaSources"][0]
    assert source["DirectStreamUrl"] == (
        "http://192.168.2.124:7259/p189/play/c189a/2001/%E5%BD%B1%E7%89%87.mkv"
    ), source
    assert source["Protocol"] == "http" and source["SupportsDirectPlay"] is True
    # 没有映射到天翼（比如普通 115 strm）时不能乱返回
    plain = store.root / "普通.strm"
    plain.write_text("http://172.17.0.1:9527/p115/play/pc1/a.mkv",
                     encoding="utf-8")
    assert mg.strm_play_url(str(plain), "http://192.168.2.124:7259") == ""


def _family_tree(**dirs):
    """按 ``家庭id = {folder_id: [条目]}`` 造一棵「每个家庭各不相同」的树。"""
    class _FamClient(FakeClient):
        def list_files(self, folder_id, family_id="", page_size=100):
            self.calls.append(f"{family_id}:{folder_id}")
            bucket = self.tree.get(family_id) or self.tree.get("*") or {}
            return bucket.get(str(folder_id), [])

    client = _FamClient({fid: {k: v for k, v in tree.items()}
                         for fid, tree in dirs.items()})
    return client


def test_run_config_accepts_whole_family_cloud():
    """「家庭云」整选不该再被拦下（以前会提示「请点进具体家庭」）。"""
    fams = [{"family_id": "579", "name": "18009666628", "count": 1}]
    mg, db, _store, _fake = _manager({}, fams)
    _add_account(db, "c189a")
    db.upsert_cloud189_config({
        "id": "c189_all", "account": "c189a", "name": "整个家庭云",
        "remote_id": "fam", "remote_path": "/个人云/家庭云",
        "local_dir": "天翼全部家庭", "base_url": BASE, "media_exts": "mkv",
        "all_files": 0, "status": "idle",
    })
    mg._run = lambda *a, **k: None          # 只看入口校验，不真跑
    assert mg.run_config("c189_all", "inc")["accepted"] is True


def test_whole_family_cloud_generation_uses_one_dir_per_family():
    """整选「家庭云」：每个家庭一个同名子目录，strm 里的家庭段各是各的。"""
    mg, db, store, _fake = _manager({})
    fam_client = _family_tree(**{
        "579": {"": [_file("9001", "家庭片.mkv")]},
        "300": {"": [_file("9501", "另一个家庭.mkv")]},
    })
    fam_client.families = [
        {"family_id": "579", "name": "18009666628", "count": 1},
        {"family_id": "300", "name": "17738728676", "count": 1},
    ]
    mg.client = lambda *a, **k: fam_client
    _add_account(db, "c189a")
    db.upsert_cloud189_config({
        "id": "c189_all", "account": "c189a", "name": "整个家庭云",
        "remote_id": "fam", "remote_path": "/个人云/家庭云",
        "local_dir": "天翼全部家庭", "base_url": BASE, "media_exts": "mkv",
        "all_files": 0, "status": "idle",
    })
    mg._run(db.get_cloud189_config("c189_all"), "inc")

    a = store.root / "天翼全部家庭" / "18009666628" / "家庭片.strm"
    b = store.root / "天翼全部家庭" / "17738728676" / "另一个家庭.strm"
    assert a.exists(), sorted(p.as_posix() for p in store.root.rglob("*.strm"))
    assert b.exists(), sorted(p.as_posix() for p in store.root.rglob("*.strm"))
    assert "/p189/play/c189a/fam/579/9001/" in a.read_text(encoding="utf-8")
    assert "/p189/play/c189a/fam/300/9501/" in b.read_text(encoding="utf-8")
    recs = {r["file_id"]: r for r in db.list_cloud189_files("c189_all")}
    assert recs["9001"]["family_id"] == "579" and recs["9501"]["family_id"] == "300"


def test_multi_account_configs_use_their_own_account():
    """两个账号各一条配置：strm 里的账号段必须各是各的。"""
    tree = {"-11": [_file("1", "a.mkv")]}
    mg, db, store, _fake = _manager(tree)
    _add_account(db, "c189a", name="A")
    _add_account(db, "c189b", name="B", is_default=0)
    for aid, local in (("c189a", "账号A"), ("c189b", "账号B")):
        db.upsert_cloud189_config({
            "id": "cfg_" + aid, "account": aid, "name": aid, "remote_id": "-11",
            "local_dir": local, "base_url": BASE, "media_exts": "mkv",
            "all_files": 0, "status": "idle",
        })
        mg._run(db.get_cloud189_config("cfg_" + aid), "inc")
    a = (store.root / "账号A" / "a.strm").read_text(encoding="utf-8").strip()
    b = (store.root / "账号B" / "a.strm").read_text(encoding="utf-8").strip()
    assert "/p189/play/c189a/" in a and "/p189/play/c189b/" in b, (a, b)


def test_delete_account_with_configs_needs_force():
    mg, db, _store, _fake = _manager({})
    _add_account(db, "c189a")
    db.upsert_cloud189_config({"id": "c1", "account": "c189a", "name": "x",
                               "remote_id": "-11", "local_dir": "天翼"})
    try:
        mg.delete_account("c189a")
    except Cloud189Error as exc:
        assert "还有 1 条生成配置" in str(exc)
    else:
        raise AssertionError("有配置时删账号必须拒绝")
    out = mg.delete_account("c189a", force=True)
    assert out["removed_configs"] == 1
    assert db.get_cloud189_account("c189a") is None
    assert db.get_cloud189_config("c1") is None


def test_adding_second_account_does_not_touch_default(monkeypatch):
    """再登录一个账号（account 传空）必须是新建，不能覆盖默认账号。"""
    mg, db, _store, _fake = _manager({})
    _add_account(db, "c189a", token="tok-a", name="A")
    monkeypatch.setattr(mg, "_fill_profile", lambda aid: "新账号")
    row = mg._save_login("", "tok-new", 3600, "SK-NEW", "cookie")
    assert row["id"] != "c189a"
    assert row["access_token"] == "tok-new"
    assert db.get_cloud189_account("c189a")["access_token"] == "tok-a"
    accs = mg.accounts()
    assert [a["id"] for a in accs][0] == "c189a", "默认账号不能变"
    assert len(accs) == 2
    # 再登一次同一个账号（带 id）→ 更新而不是新建
    row2 = mg._save_login("c189a", "tok-a2", 3600, "SK-A", "cookie")
    assert row2["id"] == "c189a" and row2["access_token"] == "tok-a2"
    assert len(mg.accounts()) == 2


def test_delete_default_promotes_another_account():
    """删掉默认账号后，剩下的第一个要自动接上默认标记。"""
    mg, db, _store, _fake = _manager({})
    _add_account(db, "c189a", name="A")                 # 默认
    _add_account(db, "c189b", name="B", is_default=0)
    mg.delete_account("c189a")
    accs = mg.accounts()
    assert len(accs) == 1 and accs[0]["is_default"] == 1, accs
    assert mg.status()["default"] == "c189b"


def test_missing_default_heals_on_status():
    """历史数据里一条默认都没有时，status() 会顺手补一个。"""
    mg, db, _store, _fake = _manager({})
    _add_account(db, "c189a", name="A", is_default=0)
    _add_account(db, "c189b", name="B", is_default=0)
    st = mg.status()
    assert st["default"] == "c189a", st["default"]
