"""本地冒烟测试：不连 115，用假 API 跑通 扫描 / 增量 / 播放 三条链路。

运行：python tests/smoke_test.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORK = Path(tempfile.mkdtemp(prefix="p115strm-test-"))
os.environ.update(
    {
        "DATA_DIR": str(WORK / "config"),
        "STRM_ROOT": str(WORK / "strm"),
        # 主账号的 cookies 文件：默认是容器里的 /config/115-cookies.txt，
        # 测试里要指到临时目录，否则「切换主账号」会写到不该碰的路径
        "P115_COOKIE_FILE": str(WORK / "config" / "115-cookies.txt"),
        "ROOT_CIDS": "0",
        "PUBLIC_BASE_URL": "http://127.0.0.1:9527",
        "WEB_PORT": "9527",
        "LOG_LEVEL": "WARNING",
    }
)

from app.config import load_config
from app.db import Database
from app.scanner import Scanner
from app.strm import StrmStore
from app.watcher import Watcher


class FakeAPI:
    def __init__(self) -> None:
        self.uid = "1001"        # 假的 115 用户 ID（strm 末尾用它标识账号）
        self.nodes = {
            1000: {"id": 1000, "parent_id": 0, "name": "test.mkv", "is_dir": False},
            1001: {"id": 1001, "parent_id": 100, "name": "movie.mp4", "is_dir": False},
            1002: {"id": 1002, "parent_id": 101, "name": "a.mkv", "is_dir": False},
            1003: {"id": 1003, "parent_id": 101, "name": "b.txt", "is_dir": False},
            100: {"id": 100, "parent_id": 0, "name": "电影", "is_dir": True},
            101: {"id": 101, "parent_id": 100, "name": "2024", "is_dir": True},
            102: {"id": 102, "parent_id": 0, "name": "新剧", "is_dir": True},
            1004: {"id": 1004, "parent_id": 102, "name": "ep01.mkv", "is_dir": False},
        }
        self.tree = {
            0: [self.nodes[100], self.nodes[1000]],
            100: [self.nodes[101], self.nodes[1001]],
            101: [self.nodes[1002], self.nodes[1003]],
            102: [self.nodes[1004]],
        }
        self.client = None
        # ed2k / 离线下载相关
        self.cloud: dict[str, dict] = {}
        self.cloud_added: list[tuple[str, int, str]] = []
        self.cloud_deleted: list[str] = []
        self.deleted_files: list[int] = []
        self._next_id = 9000

    def user_id(self) -> str:
        return self.uid

    def _full(self, node):
        return {
            **node,
            "pickcode": f"pc{node['id']}",
            "size": 123,
            "sha1": "abc",
            "ctime": 1,
            "mtime": node.get("mtime", 2),
        }

    def iter_dir(self, cid, **kwargs):
        for node in self.tree.get(cid, []):
            yield self._full(node)

    def list_dir(self, cid, **kwargs):
        nodes = [self._full(n) for n in self.tree.get(cid, [])]
        return [n for n in nodes if n["is_dir"]], [n for n in nodes if not n["is_dir"]]

    def get_node(self, nid):
        node = self.nodes.get(int(nid))
        return self._full(node) if node else None

    def ensure_pickcode(self, node):
        if node.get("is_dir"):
            return ""
        if node.get("pickcode"):
            return node["pickcode"]
        detail = self.get_node(node["id"])
        return (detail or {}).get("pickcode", "")

    def mkdir(self, parent_id, name):
        self._next_id += 1
        node = {"id": self._next_id, "parent_id": int(parent_id), "name": name, "is_dir": True}
        self.nodes[node["id"]] = node
        self.tree.setdefault(int(parent_id), []).append(node)
        return node["id"]

    def find_child_dir(self, parent_id, name):
        for node in self.tree.get(int(parent_id), []):
            if node["is_dir"] and node["name"] == name:
                return node["id"]
        return None

    def cloud_add(self, url, dir_cid, info_hash=""):
        self.cloud_added.append((url, int(dir_cid), info_hash))
        return {"ok": True, "message": "", "errcode": 0}

    def cloud_task(self, info_hash):
        return self.cloud.get(info_hash, {"state": False, "error_msg": "用户任务不存在"})

    def cloud_del(self, info_hash):
        self.cloud_deleted.append(info_hash)
        self.cloud.pop(info_hash, None)
        return True

    def delete_files(self, fids):
        for fid in fids:
            fid = int(fid)
            self.deleted_files.append(fid)
            node = self.nodes.pop(fid, None)
            if node is not None:
                self.tree[node["parent_id"]] = [
                    n for n in self.tree.get(node["parent_id"], []) if n["id"] != fid
                ]
        return True


class FakeShareClient:
    """模拟 115 分享接口：share_info / share_snap / share_receive。

    按真实接口的形态模拟两个坑：

    - 分享快照里目录条目只有 ``cid`` 没有 ``fid``；
    - 转存落盘的文件名会把非法字符改写（快照 ``第一集:序章`` ↔ 落盘
      ``第一集_序章``），定位必须做归一化匹配。
    """

    def __init__(self, api: FakeAPI) -> None:
        self.api = api
        self.snap = {
            ("s123abc", 0): [
                # 真实目录条目：id 在 cid，没有 fid / sha / ico
                {"cid": 2001, "pid": 0, "n": "分享电影", "fc": 1, "s": 756,
                 "t": "1781435997", "e": "", "fl": []},
            ],
            ("s123abc", 2001): [
                {"fid": 2002, "cid": 2001, "n": "第一集:序章.mkv", "fc": 0,
                 "s": 456, "sha": "FF" * 20, "ico": "mkv"},
            ],
        }
        self.received: list[dict] = []
        self.already_received = False
        self.own_shares: dict[str, dict] = {}
        self.downloaded: list[dict] = []
        self._own_seq = 0
        self._next_fid = 5000

    def share_info(self, payload):
        return {"state": True, "data": {"share_title": "分享电影", "share_state": 1}}

    def share_snap(self, payload):
        key = (payload["share_code"], int(payload["cid"]))
        items = self.snap.get(key, [])
        offset = int(payload.get("offset") or 0)
        limit = int(payload.get("limit") or 1150)
        return {"state": True,
                "data": {"list": items[offset:offset + limit], "count": len(items),
                         "shareinfo": {"share_title": "分享电影快照标题"}}}

    def share_receive(self, payload):
        self.received.append(dict(payload))
        if self.already_received:
            return {"state": False, "error": "文件已接收，无需重复接收！", "errno": 4101203}
        target = int(payload["cid"])
        # 按分享树把内容复制进目标目录（目录结构与转存一致），
        # 落盘文件名模拟 115 对非法字符的改写
        renamed = str.maketrans({":": "_", "/": "_", "\\": "_"})
        for item in self.snap.get((payload["share_code"], 0), []):
            self._next_fid += 1
            if not item.get("fid"):
                node = {"id": self._next_fid, "parent_id": target,
                        "name": item["n"].translate(renamed), "is_dir": True}
                self.api.nodes[node["id"]] = node
                self.api.tree.setdefault(target, []).append(node)
                # 子文件
                for sub in self.snap.get((payload["share_code"], item["cid"]), []):
                    self._next_fid += 1
                    child = {"id": self._next_fid, "parent_id": node["id"],
                             "name": sub["n"].translate(renamed), "is_dir": False}
                    self.api.nodes[child["id"]] = child
                    self.api.tree.setdefault(node["id"], []).append(child)
        return {"state": True, "data": {"receive_title": "分享电影",
                                        "recv_file_count": 1, "recv_folder_count": 1}}

    def share_send(self, payload):
        ids = [int(x) for x in str(payload.get("file_ids")).split(",") if x.strip().isdigit()]
        self._own_seq += 1
        sc = f"myshare{self._own_seq:02d}"
        self.own_shares[sc] = {"file_ids": ids, "deleted": False}
        return {"state": True, "data": {"share_code": sc, "receive_code": "r999",
                                        "share_url": f"https://115.com/s/{sc}?password=r999"}}

    def share_update(self, payload):
        sc = str(payload.get("share_code") or "")
        if payload.get("action") in ("delete", "cancel"):
            if sc in self.own_shares:
                self.own_shares[sc]["deleted"] = True
            return {"state": True, "data": {}}
        return {"state": True, "data": {sc: {"receive_code": "r999",
                                             "share_ex_time": -1}}}

    def share_download_url(self, payload):
        self.downloaded.append(dict(payload))
        url = ("https://cdn.example.com/share-dl/" + str(payload["file_id"]) + ".mkv")
        return SimpleNamespace(url=url, headers={"User-Agent": "115Player"})

    def fs_delete(self, fids):
        return self.api.delete_files(fids)


class FakeTargetAPI:
    """模拟第二个 115 账号：秒传 / 暂存目录 / 直链。"""

    def __init__(self) -> None:
        self.nodes: dict[int, dict] = {}
        self.tree: dict[int, list[dict]] = {}
        self.client = self
        self.uploaded: list[dict] = []
        self._next_id = 7000
        self._staging_id = 8000
        self.nodes[8000] = {"id": 8000, "parent_id": 0, "name": "p115-strm-instant",
                            "is_dir": True}
        self.tree[0] = [self.nodes[8000]]
        self.tree[8000] = []

    def upload_info(self):
        """上传凭据：115 的秒传要带 userkey（跨账号时没它会被拒）。"""
        self.cred_calls = getattr(self, "cred_calls", 0) + 1
        return {"state": True, "user_id": "222", "userkey": "FAKEUSERKEY" * 4}

    def upload_file_init(self, filename, filesha1, filesize, pid=0,
                         read_range_bytes_or_hash=None, payload=None, **kwargs):
        # 模拟二次校验：真的调用一次回调
        assert read_range_bytes_or_hash is not None
        data = read_range_bytes_or_hash("0-63")
        assert isinstance(data, (bytes, bytearray)) and len(data) == 64
        # 上传凭据必须带上
        assert payload and payload.get("userkey"), "秒传必须带 userkey"
        self.uploaded.append({"filename": filename, "sha1": filesha1,
                              "size": filesize, "pid": pid,
                              "userkey": payload.get("userkey")})
        self._next_id += 1
        node = {"id": self._next_id, "parent_id": int(pid), "name": filename,
                "is_dir": False}
        self.nodes[node["id"]] = node
        self.tree.setdefault(int(pid), []).append(node)
        self.pick_map = getattr(self, "pick_map", {})
        self.pick_map[f"tp{self._next_id}"] = node["id"]
        return {"statuscode": 0, "status": 2, "reuse": True,
                "pickcode": f"tp{self._next_id}"}

    def fs_supervision(self, payload):
        pickcode = payload["pickcode"]
        fid = self.pick_map.get(pickcode)
        return {"state": True, "data": {"file_id": fid, "file_name": "x",
                                        "file_sha1": "", "file_size": 1}}

    def get_node(self, nid):
        node = self.nodes.get(int(nid))
        return {**node, "pickcode": f"tpc{node['id']}"} if node else None

    def find_child_dir(self, parent_id, name):
        for node in self.tree.get(int(parent_id), []):
            if node["is_dir"] and node["name"] == name:
                return node["id"]
        return None

    def mkdir(self, parent_id, name):
        self._next_id += 1
        node = {"id": self._next_id, "parent_id": int(parent_id), "name": name,
                "is_dir": True}
        self.nodes[node["id"]] = node
        self.tree.setdefault(int(parent_id), []).append(node)
        return node["id"]

    def list_dir(self, cid, **kwargs):
        nodes = [dict(n, pickcode=f"tpc{n['id']}")
                 for n in self.tree.get(int(cid), [])]
        return ([n for n in nodes if n["is_dir"]],
                [n for n in nodes if not n["is_dir"]])

    def delete_files(self, fids):
        for fid in fids:
            node = self.nodes.pop(int(fid), None)
            if node is not None:
                self.tree[node["parent_id"]] = [
                    n for n in self.tree.get(node["parent_id"], []) if n["id"] != fid
                ]
        return True

    def download_url(self, pickcode, user_agent="", cache_seconds=600):
        return f"https://target.example.com/{pickcode}", {}


def _free_port() -> int:
    """找一个当前没被占用的本地端口（用来测独立端口监听）。"""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> int:
    from app.p115api import normalize_node
    from app.strm import safe_component

    # ---------------- 字段解析（115 的几种原始形状） ----------------
    web_raw = {"cid": "100", "fid": "1001", "n": "沙丘2.mkv", "pc": "abc",
               "s": "123", "sha": "dead", "fc": "1", "tp": "1", "te": "2"}
    node = normalize_node(web_raw)
    assert node["is_dir"] is False and node["id"] == 1001
    assert node["parent_id"] == 100 and node["pickcode"] == "abc" and node["name"] == "沙丘2.mkv"

    d = normalize_node({"cid": "100", "pid": "0", "n": "电影", "fc": "0"})
    assert d["is_dir"] and d["id"] == 100 and d["parent_id"] == 0

    i = normalize_node({"file_id": 1002, "file_name": "a.mkv", "parent_id": 101,
                        "pick_code": "pc2", "file_size": 9, "sha1": "ab"})
    assert i["id"] == 1002 and i["parent_id"] == 101 and i["pickcode"] == "pc2"

    cleaned = safe_component('a/b:c*?"<>|.mkv')
    assert cleaned.endswith(".mkv") and not any(ch in cleaned for ch in '/:*?"<>|')
    assert len(safe_component("中文" * 200).encode()) <= 200

    # ---------------- 单节点查询：真实返回是 {"data": [ {...} ]} ----------------
    from app.p115api import P115API, first_dict

    assert first_dict({"state": True, "data": [{"fid": "1001", "pc": "abc"}]})["fid"] == "1001"
    assert first_dict({"data": {"list": [{"fid": 7}]}}) == {"fid": 7}
    assert first_dict({"data": []}) is None

    class StubClient:
        def fs_file(self, payload, **kwargs):
            assert payload == {"file_id": 1001}, payload
            return {"state": True, "data": [
                {"fid": "1001", "pid": "100", "n": "movie.mp4", "pc": "pc1001", "fc": "1"}
            ]}

    api0 = P115API.__new__(P115API)  # 不走 __init__，只测解析逻辑
    api0.client = StubClient()
    got = api0.get_node(1001)
    assert got and got["id"] == 1001 and got["pickcode"] == "pc1001", got
    # 列表接口没给 pc 时，靠 details 补齐（本地算法推导的 pickcode 与 115 不符，不能用）
    assert api0.ensure_pickcode({"id": 1001, "name": "movie.mp4",
                                 "is_dir": False, "pickcode": ""}) == "pc1001"
    print("字段解析/命名 OK")

    # ---------------- 接口回退 & WAF 限流（2026-10：webapi /files 返回 405） ----------------
    import app.p115api as api_mod
    from app.p115api import APP_CHAIN, WafBlocked

    assert APP_CHAIN[0] == "web" and "aps" in APP_CHAIN, APP_CHAIN
    calls: list[str] = []

    def fake_iterdir(client, cid, **kwargs):
        calls.append(kwargs["app"])
        if kwargs["app"] != "web":
            raise ConnectionError("connection reset by peer")
        yield {"id": 1, "parent_id": 0, "name": "a.mkv", "is_dir": False,
               "pickcode": "p1", "size": 1, "sha1": "x", "ctime": 0, "mtime": 0}

    orig_iterdir = api_mod.iterdir
    api_mod.iterdir = fake_iterdir
    try:
        api_fb = P115API.__new__(P115API)
        api_fb.client = None
        api_fb._app_hint = ""
        api_fb._blocked_until = 0.0
        nodes = list(api_fb.iter_dir(0, app="chrome"))
        assert [n["id"] for n in nodes] == [1], nodes
        assert calls == ["chrome", "web"], calls
        # 记住成功过的接口，下一次直接用它
        assert api_fb._app_hint == "web", api_fb._app_hint
        calls.clear()
        list(api_fb.iter_dir(0, app="chrome"))
        assert calls == ["web"], calls
    finally:
        api_mod.iterdir = orig_iterdir
    print("接口失败自动回退 OK:", APP_CHAIN)

    # 405 是 WAF 限流：先换入口（WAF 按域名封），全都被拦才冷却并让后续请求快速失败
    def fake_405(client, cid, **kwargs):
        calls.append(kwargs["app"])
        raise OSError("HTTP Error 405: Method Not Allowed")

    calls.clear()
    api_mod.iterdir = fake_405
    try:
        api_waf = P115API.__new__(P115API)
        api_waf.client = None
        api_waf._app_hint = ""
        api_waf._blocked_until = 0.0
        try:
            list(api_waf.iter_dir(0))
            raise AssertionError("405 应该抛 WafBlocked")
        except WafBlocked as exc:
            assert exc.retry_after > 0
        assert calls == ["web", "aps", "chrome"], calls  # 每个入口都试一次
        calls.clear()
        try:
            list(api_waf.iter_dir(0))
            raise AssertionError("冷却期内不应再发请求")
        except WafBlocked:
            pass
        assert calls == [], calls
    finally:
        api_mod.iterdir = orig_iterdir
    print("405 识别为 WAF 限流并退避 OK")

    # ---------------- 全局限速：多线程共享一个令牌桶 ----------------
    from app.p115api import RateLimiter

    lim = RateLimiter(0.05)
    t0 = time.monotonic()
    for _ in range(6):
        lim.wait()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.24, elapsed  # 5 个间隔 × 0.05s
    print(f"全局限速 OK: 6 次请求用了 {elapsed:.2f}s")

    # ---------------- 生活事件接口下线 → 降级判断 ----------------
    assert Watcher._endpoint_gone(OSError("HTTP Error 405: Method Not Allowed"))
    assert not Watcher._endpoint_gone(OSError("HTTP Error 500: Internal Server Error"))
    print("事件接口下线识别 OK")

    cfg = load_config()
    db = Database(Path(cfg.data_dir) / "p115-strm.db")
    # 模拟 main.build() 的首次种子：ROOT_CIDS 写入数据库
    assert db.add_root(0, "") is True
    api = FakeAPI()
    store = StrmStore(cfg.strm_root, base_url=cfg.base_url, play_url_ext=cfg.play_url_ext,
                      account_uid="1001")
    scanner = Scanner(cfg, db, api, store)
    watcher = Watcher(cfg, db, api, store, scanner)
    from app.ed2k import Ed2kManager

    ed2k = Ed2kManager(cfg, db, api, store)

    # ---------------- 全量 ----------------
    stats = scanner.run(full=True)
    assert stats.files == 3, stats.as_dict()  # b.txt 被后缀过滤
    assert stats.written == 3, stats.as_dict()
    # strm 内容：请求头 + pickcode + name（URL 编码）+ 115 用户 ID
    assert (cfg.strm_root / "test.strm").read_text().strip() == (
        "http://127.0.0.1:9527/p115/play/pc1000/test.mkv/1001"
    ), (cfg.strm_root / "test.strm").read_text()
    assert (cfg.strm_root / "电影" / "movie.strm").exists()
    assert (cfg.strm_root / "电影" / "2024" / "a.strm").exists()
    assert not (cfg.strm_root / "电影" / "2024" / "b.strm").exists()
    print("全量扫描 OK:", stats.as_dict())

    # 幂等 + 指纹跳过：目录 mtime 没变时整棵子树不用再列一遍
    stats2 = scanner.run(full=False)
    assert stats2.written == 0 and stats2.skipped >= 1, stats2.as_dict()
    # 「电影」这一支被跳过，所以这轮只重扫了根目录
    assert stats2.dirs == 1, stats2.as_dict()
    print("增量扫描按目录指纹跳过 OK:", stats2.as_dict())

    # 目录 mtime 变了就要重扫这一支（模拟 115 上新增/删除后的 mtime 更新）
    api.nodes[100]["mtime"] = 999
    stats3 = scanner.run(full=False)
    assert stats3.written == 0, stats3.as_dict()
    assert stats3.dirs == 2 and stats3.skipped == 1, stats3.as_dict()
    print("目录变更后重新扫描 OK")

    # ---------------- 增量：新增目录 + 文件 ----------------
    assert watcher.resolve_dir_path(0) == ""
    assert watcher.resolve_dir_path(101) == "电影/2024"
    assert db.get_file(1000)["strm_path"].endswith("test.strm")

    watcher.on_dir(api.get_node(102), scan_subtree=True)
    assert (cfg.strm_root / "新剧" / "ep01.strm").exists()
    print("新增目录增量 OK")

    # ---------------- 事件兜底解析：事件里的 pick_code 与真实 pickcode 不同，必须丢弃 ----------------
    ev_dir = {"type": 17, "file_id": "102", "parent_id": "0", "file_name": "新剧",
              "file_category": "0", "pick_code": "eventonly"}
    n_dir = Watcher._node_from_event(ev_dir)
    assert n_dir and n_dir["is_dir"] and n_dir["pickcode"] == "", n_dir

    ev_file = {"type": 2, "file_id": "1004", "parent_id": "102", "file_name": "ep01.mkv",
               "file_category": "1", "pick_code": "eventonly", "file_size": "9"}
    n_file = Watcher._node_from_event(ev_file)
    assert n_file and not n_file["is_dir"] and n_file["pickcode"] == "", n_file
    assert n_file["size"] == 9
    # 目录改名/文件改名的类型覆盖 file_category
    assert Watcher._node_from_event({**ev_dir, "type": 20})["is_dir"] is True
    assert Watcher._node_from_event({**ev_file, "type": 24, "file_category": "0"})["is_dir"] is False
    print("事件兜底解析 OK")

    # ---------------- 增量：文件改名 ----------------
    api.nodes[1002]["name"] = "a-renamed.mkv"
    watcher.on_file(api.get_node(1002))
    assert (cfg.strm_root / "电影" / "2024" / "a-renamed.strm").exists()
    assert not (cfg.strm_root / "电影" / "2024" / "a.strm").exists()
    assert db.get_file(1002)["path"] == "电影/2024/a-renamed.mkv"
    print("文件改名增量 OK")

    # ---------------- 增量：目录改名（整棵子树搬迁） ----------------
    api.nodes[100]["name"] = "电影2"
    watcher.on_dir(api.get_node(100))
    assert (cfg.strm_root / "电影2" / "2024" / "a-renamed.strm").exists()
    assert not (cfg.strm_root / "电影").exists()
    assert db.get_file(1001)["path"] == "电影2/movie.mp4"
    assert db.get_file(1001)["strm_path"].endswith(str(Path("电影2") / "movie.strm"))
    print("目录改名增量 OK")

    # ---------------- 增量：删除 ----------------
    watcher.on_delete(1002)
    assert not (cfg.strm_root / "电影2" / "2024" / "a-renamed.strm").exists()
    assert db.get_file(1002) is None

    watcher.on_delete(100)
    assert not (cfg.strm_root / "电影2").exists()
    assert db.get_file(1001) is None and db.get_dir(101) is None
    print("删除增量 OK")

    # ---------------- 播放接口 ----------------
    from fastapi.testclient import TestClient

    from app.server import create_app

    seen_ua = []

    def fake_dl(pc, user_agent="", cache_seconds=600):
        seen_ua.append(user_agent)
        return f"https://cdn.example.com/{pc}", {}

    api.download_url = fake_dl
    # 登录账号密码由 compose 变量（UI_USERNAME / UI_PASSWORD）生成
    cfg.web_username = "tester"
    cfg.web_password = "s3cret"
    app = create_app(cfg, db, api, store, scanner, watcher, {"version": "test", "scan": {}}, ed2k)
    client = TestClient(app)
    assert client.get("/healthz").json() == {"ok": True}
    resp = client.get("/p115/play/pc1000.mkv", follow_redirects=False)
    assert resp.status_code == 302, resp.status_code
    assert resp.headers["location"] == "https://cdn.example.com/pc1000"
    # 302 模式必须用「请求方的 UA」取直链（f=1 链接校验 UA 一致）
    assert seen_ua and "testclient" in seen_ua[0].lower(), seen_ua

    # ---------------- 登录鉴权（compose 变量生成的账号密码） ----------------
    sess = client.get("/api/session").json()
    assert sess["authenticated"] is False and sess["enabled"] is True, sess
    # 未登录时管理接口一律 401
    assert client.get("/api/status").status_code == 401
    # 播放路由不受登录影响（播放器没有 Cookie）
    assert client.get("/p115/play/pc1000.mkv", follow_redirects=False).status_code == 302
    # 密码不对 → 401
    assert client.post("/api/login", json={"username": "tester", "password": "bad"}).status_code == 401
    # 密码对 → 发会话 Cookie，之后管理接口放行
    r = client.post("/api/login", json={"username": "tester", "password": "s3cret"})
    assert r.status_code == 200, r.text
    assert "p115_session" in r.cookies, r.cookies
    assert client.get("/api/session").json()["authenticated"] is True
    assert client.get("/api/status").json()["db"]["files"] >= 0
    # 退出登录后重新变成未登录
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/status").status_code == 401
    client.post("/api/login", json={"username": "tester", "password": "s3cret"})

    # ---------------- 首页：SPA 外壳（经典管理面已移除） ----------------
    page = client.get("/").text
    assert "loginView" in page and "appView" in page, page[:200]
    # 导航与页面由前端 JS 渲染，这里确认脚本都已挂上
    for name in ("core.js", "api.js", "data.js", "shell.js", "views.js",
                 "tool-115.js", "tool-share.js", "tool-ed2k.js",
                 "tool-instant.js", "tool-strm115.js", "tool-cloud189.js",
                 "main.js"):
        assert f"/static/js/{name}" in page, name
    # 经典管理面（旧的卡片页）不该再出现
    assert "ed2k 转存播放" not in page
    assert "loadDirs()" not in page
    # 静态资源可访问
    assert client.get("/static/js/main.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    # 导航结构：工具箱已取消，选项都放到「工具」下面
    shell = (Path(ROOT) / "web" / "js" / "shell.js").read_text(encoding="utf-8")
    nav_block = shell[shell.index("const NAV"):shell.index("const ROUTES")]
    assert "工具箱" not in nav_block
    assert "section: '工具'" in nav_block
    for name in ("115 助手", "115 分享转存", "ed2k STRM", "115 秒传", "115 STRM",
                 "天翼云盘"):
        assert name in nav_block, name
    assert "section: '媒体与反代'" in nav_block
    assert "媒体服务器" in nav_block and "Emby 反代" in nav_block
    # 「115 目录」不在导航里
    assert "115 目录" not in nav_block
    print("播放/状态接口 OK")

    # ---------------- 同名不同后缀：不能互相覆盖 ----------------
    api.nodes[1007] = {"id": 1007, "parent_id": 100, "name": "same.mkv", "is_dir": False}
    api.nodes[1008] = {"id": 1008, "parent_id": 100, "name": "same.mp4", "is_dir": False}
    api.tree[100] = [*api.tree[100], api.nodes[1007], api.nodes[1008]]
    scanner.scan_subtree(100, "电影")
    first = cfg.strm_root / "电影" / "same.strm"
    second = cfg.strm_root / "电影" / "same.mp4.strm"
    assert first.exists() and second.exists(), sorted(p.name for p in (cfg.strm_root / "电影").iterdir())
    assert "pc1007" in first.read_text(encoding="utf-8")
    assert "pc1008" in second.read_text(encoding="utf-8")
    print("同名不同后缀 OK")

    # 删除其中一个，不能误删另一个
    watcher.on_delete(1007)
    assert not first.exists() and second.exists()
    print("冲突文件删除 OK")

    # ---------------- 目录管理：只对添加的目录生成 ----------------
    # 初始只有种子根 0
    # cid 必须是字符串：19 位 id 用 JSON number 传给浏览器会丢精度
    assert [x["cid"] for x in client.get("/api/roots").json()["roots"]] == ["0"]

    # 浏览接口：能列出 115 的子目录，且尚未添加
    api.tree[0] = [api.nodes[100], api.nodes[102]]
    data = client.get("/api/115/dirs", params={"cid": 0}).json()
    assert {d["id"] for d in data["dirs"]} == {"100", "102"}, data
    assert all(not d["managed"] for d in data["dirs"])

    # 移除根 0：已生成的 strm 与索引全部清掉
    r = client.delete("/api/roots/0").json()
    assert r["removed"] and r["prefix"] == "", r
    assert db.stats()["files"] == 0 and db.stats()["dirs"] == 0
    assert not list(cfg.strm_root.rglob("*.strm"))

    # 添加「电影2」并同步扫描：只有它生成 strm
    resp = client.post("/api/roots", params={"cid": 100, "sync": 1})
    assert resp.status_code == 200, resp.text
    assert (cfg.strm_root / "电影2" / "movie.strm").exists()
    roots = client.get("/api/roots").json()["roots"]
    assert [x["cid"] for x in roots] == ["100"] and roots[0]["name"] == "电影2"
    assert roots[0]["files"] == 4, roots  # movie / a-renamed / same.mkv / same.mp4

    # 嵌套拒绝：101 在「电影2」内部
    assert client.post("/api/roots", params={"cid": 101}).status_code == 400
    # 再添加「新剧」
    assert client.post("/api/roots", params={"cid": 102, "sync": 1}).status_code == 200
    assert (cfg.strm_root / "新剧" / "ep01.strm").exists()
    # 整个网盘(0) 是已添加目录的祖先 → 拒绝；重复添加 → 409
    assert client.post("/api/roots", params={"cid": 0}).status_code == 400
    assert client.post("/api/roots", params={"cid": 102}).status_code == 409

    # 移除「电影2」：「新剧」不受影响
    assert client.delete("/api/roots/100").json()["removed"]
    assert not (cfg.strm_root / "电影2").exists()
    assert (cfg.strm_root / "新剧" / "ep01.strm").exists()
    assert [x["cid"] for x in client.get("/api/roots").json()["roots"]] == ["102"]
    # 不存在的目录
    assert client.post("/api/roots", params={"cid": 999}).status_code == 404
    print("目录管理 OK")

    # ---------------- 转存/接收目录设置持久化（刷新后不丢） ----------------
    # 模拟 WAF 限流：get_node 失败也不能把已保存的目录显示成「未设置」
    ed2k.save_settings(dir_cid=102)
    assert ed2k.settings()["dir_cid"] == "102", ed2k.settings()
    real_get_node = api.get_node
    api.get_node = lambda nid: None  # 页面加载时 115 请求全失败
    try:
        st = ed2k.settings()
        assert st["dir_cid"] == "102" and st["dir_name"], st
        ed2k._dir_cache = None
        st2 = ed2k.settings()
        assert st2["dir_cid"] == "102" and st2["dir_name"], st2
    finally:
        api.get_node = real_get_node
    # 模拟刷新：全新实例读同一个 db
    from app.ed2k import Ed2kManager as _EM
    ed2k_fresh = _EM(cfg, db, api, store)
    assert ed2k_fresh.settings()["dir_cid"] == "102", ed2k_fresh.settings()
    # 分享接收目录同样
    from app.share import ShareManager as _SM
    share_tmp = _SM(cfg, db, api, store)
    share_tmp.save_settings(102)
    share_fresh = _SM(cfg, db, api, store)
    st3 = share_fresh.settings()
    assert st3["dir_cid"] == "102" and st3["dir_name"], st3
    print("转存/接收目录设置持久化 OK")

    # ---------------- ed2k：转存 → strm → 播放 → 清理 → 重新转存 ----------------
    from app.ed2k import Ed2kError, Ed2kNotReady, parse_ed2k

    ed2k_url = "ed2k://|file|测试视频.mkv|1024|ABCDEF0123456789ABCDEF0123456789|/"
    ekey = "abcdef0123456789abcdef0123456789"
    parsed = parse_ed2k(ed2k_url)
    assert parsed and parsed["key"] == ekey and parsed["name"] == "测试视频.mkv", parsed
    assert parse_ed2k("magnet:?xt=urn:btih:abc") is None

    rec = ed2k.add(ed2k_url)
    assert rec["status"] == "pending", rec
    strm = cfg.strm_root / "ed2k" / "测试视频.strm"
    assert strm.exists(), sorted(p.name for p in cfg.strm_root.rglob("*.strm"))
    assert strm.read_text(encoding="utf-8").strip() == (
        f"http://127.0.0.1:9527/p115/ed2k/{ekey}.mkv/1001")
    dir_cid = int(ed2k.settings()["dir_cid"])   # 对外是字符串，内部用 int
    assert dir_cid and api.cloud_added[-1][1] == dir_cid, api.cloud_added

    # 文件落盘 + 任务完成 → ready，并记下真实 pickcode
    api.nodes[1500] = {"id": 1500, "parent_id": dir_cid, "name": "测试视频.mkv", "is_dir": False}
    api.tree.setdefault(dir_cid, []).append(api.nodes[1500])
    api.cloud[ekey] = {"state": True, "display_status": "finished",
                       "percentDone": 100, "status_text": "下载成功"}
    assert ed2k.refresh(ekey) == 1
    rec = db.get_ed2k(ekey)
    assert rec["status"] == "ready" and rec["pickcode"] == "pc1500" and rec["file_id"] == 1500, rec

    # strm 里带后缀也能播（播放器有时会补扩展名）
    resp = client.get(f"/p115/ed2k/{ekey}.mkv", follow_redirects=False)
    assert resp.status_code == 302, resp.status_code
    assert resp.headers["location"] == "https://cdn.example.com/pc1500"
    assert db.get_ed2k(ekey)["last_play_at"] > 0

    # 全量对账后的 prune 不能把 ed2k 的 strm 当孤儿删掉（它不在 files 索引里）
    scanner.run(full=True)
    assert strm.exists(), "全量扫描后的 prune 误删了 ed2k strm"

    # 定时清理：保留 1 小时，把最后播放时间拨到 2 小时前
    db.update_ed2k(ekey, last_play_at=int(time.time()) - 7200)
    ed2k.save_settings(ttl_hours=1)
    result = ed2k.clean()
    assert result["removed"] == 1 and api.deleted_files == [1500], result
    assert db.get_ed2k(ekey)["status"] == "cleared"
    assert strm.exists()  # strm 不跟着删，下次播放还能重新转存

    # 文件已被清理 → 播放时重新提交转存；等不到就提示稍后重试
    api.cloud_added.clear()
    try:
        ed2k.playable(ekey, wait=0)
    except Ed2kNotReady:
        pass
    else:
        raise AssertionError("清理后应该报 Ed2kNotReady")
    assert api.cloud_added and api.cloud_added[-1][0] == ed2k_url, api.cloud_added

    # 重新转存完成 → 又能播，pickcode 换成新的
    api.nodes[1501] = {"id": 1501, "parent_id": dir_cid, "name": "测试视频.mkv", "is_dir": False}
    api.tree.setdefault(dir_cid, []).append(api.nodes[1501])
    api.cloud[ekey] = {"state": True, "display_status": "finished",
                       "percentDone": 100, "status_text": "下载成功"}
    assert ed2k.playable(ekey, wait=0) == "pc1501"
    assert db.get_ed2k(ekey)["file_id"] == 1501

    # 非法链接拒绝 + 删除记录连带 strm
    try:
        ed2k.add("http://example.com/a.mkv")
    except Ed2kError:
        pass
    else:
        raise AssertionError("非 ed2k 链接应该报错")
    assert client.post("/api/ed2k", params={"url": "bad"}).status_code == 400
    assert ed2k.delete(ekey)["key"] == ekey
    assert not strm.exists() and db.get_ed2k(ekey) is None
    print("ed2k 转存/播放/清理 OK")

    # ---------------- 自定义转存目录 + 批量 ed2k ----------------
    r = client.post("/api/ed2k/settings", params={"dir_cid": "102"}).json()
    assert r["settings"]["dir_cid"] == "102" and r["settings"]["dir_name"] == "新剧", r

    batch = "\n".join(
        [
            "ed2k://|file|批量A.mkv|2048|" + "a" * 32 + "|/",
            "前面还有一句说明文字，忽略掉",
            "ed2k://|file|批量B.mkv|4096|" + "b" * 32 + "|/",
            "ed2k://|file|批量A重复.mkv|2048|" + "a" * 32 + "|/",  # 同 hash 去重
        ]
    )
    api.cloud_added.clear()
    res = client.post("/api/ed2k/batch", params={"urls": batch}).json()
    assert res["added"] == 2 and res["failed"] == 0, res
    assert (cfg.strm_root / "ed2k" / "批量A.strm").exists()
    assert (cfg.strm_root / "ed2k" / "批量B.strm").exists()
    assert len(api.cloud_added) == 2, api.cloud_added
    assert all(item[1] == 102 for item in api.cloud_added), api.cloud_added

    # 非 ed2k 的批量输入要报 400，而不是静默成功
    assert client.post("/api/ed2k/batch", params={"urls": "hello"}).status_code == 400
    # 目录 id 支持从 115 链接里解析
    r = client.post("/api/ed2k/settings",
                    params={"dir_cid": "https://115.com/?cid=102&offset=0"}).json()
    assert r["settings"]["dir_cid"] == "102", r
    print("批量 ed2k / 自定义转存目录 OK")

    # ---------------- 手动触发：全量/增量/同步 ----------------
    r = client.post("/api/scan", params={"full": 0})
    assert r.status_code == 200 and r.json()["accepted"] is True, r.text
    deadline = time.time() + 10
    while time.time() < deadline:
        state_scan = client.get("/api/status").json().get("scan") or {}
        if state_scan.get("finished_at"):
            break
        time.sleep(0.2)
    assert state_scan and state_scan.get("finished_at"), state_scan

    watcher.events_ok = False  # 强制走增量扫描路径
    r = client.post("/api/poll")
    assert r.status_code == 200 and r.json()["mode"] == "interval", r.text
    print("手动触发接口 OK")

    # ---------------- 分享转存 ----------------
    from app.share import ShareManager, parse_share

    parsed = parse_share("https://115.com/s/s123abc?password=rcode")
    assert parsed == {"share_code": "s123abc", "receive_code": "rcode"}, parsed
    parsed = parse_share("https://115cdn.com/s/s123abc#rcode")
    assert parsed["receive_code"] == "rcode", parsed

    api.client = FakeShareClient(api)
    share = ShareManager(cfg, db, api, store)
    api2 = create_app(cfg, db, api, store, scanner, watcher,
                      {"version": "test", "scan": {}}, ed2k, share, None, None)
    client2 = TestClient(api2)
    # 新建的 app 也要先登录（管理接口要求会话）
    assert client2.post("/api/login", json={
        "username": cfg.web_username, "password": cfg.web_password}).status_code == 200

    r = client2.post("/api/share", params={
        "url": "https://115.com/s/s123abc?password=rcode", "force": "true"})
    assert r.status_code == 200, r.text
    rec = r.json()["share"]
    assert rec["status"] == "ready" and rec["done"] == 1 and rec["total"] == 1, rec
    assert len(api.client.received) == 1, api.client.received

    items = client2.get("/api/share").json()["items"]
    # 标题优先取 snap 响应里的 shareinfo（share_info 对部分分享会报错）
    assert items and items[0]["name"] == "分享电影快照标题", items
    # strm 落在 share/<标题 [sharecode]>/ 分享树结构下，
    # 内容指向「自己的分享」播放路由（转存 → 自己再分享 → strm）
    strms = list((cfg.strm_root / "share").rglob("*.strm"))
    assert len(strms) == 1, [str(p) for p in strms]
    content = strms[0].read_text(encoding="utf-8").strip()
    assert "/p115/share/myshare01/" in content, content
    assert api.client.own_shares, api.client.own_shares
    assert not any(v["deleted"] for v in api.client.own_shares.values())
    print("分享转存生成 strm OK")

    # 播放路由：从自己的分享里解析直链
    from urllib.parse import parse_qs, urlparse
    u = urlparse(content)
    r = client2.get(u.path, params={k: v[0] for k, v in parse_qs(u.query).items()},
                    follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text)
    assert r.headers["location"].startswith("https://cdn.example.com/share-dl/"), \
        r.headers.get("location")
    assert api.client.downloaded and api.client.downloaded[-1]["share_code"] == "myshare01"
    print("分享播放路由 OK")

    # 重复添加：转存接口返回「已接收」应幂等跳过，自己的分享也复用不重建
    api.client.already_received = True
    r = client2.post("/api/share", params={
        "url": "https://115.com/s/s123abc?password=rcode", "force": "true"})
    assert r.status_code == 200, r.text
    rec = r.json()["share"]
    assert rec["status"] == "ready" and rec["done"] == 1 and rec["total"] == 1, rec
    assert len(api.client.own_shares) == 1, api.client.own_shares
    print("重复接收幂等 OK")

    # 删除：strm 清掉、115 转存文件删掉、自己的分享取消（回归：先删记录导致文件删不掉）
    r = client2.delete("/api/share/s123abc")
    assert r.status_code == 200, r.text
    assert not list((cfg.strm_root / "share").rglob("*.strm")), "strm 未清理"
    assert api.deleted_files, "115 转存文件没有删除"
    assert all(v["deleted"] for v in api.client.own_shares.values()), api.client.own_shares
    assert not db.list_shares() or all(
        s["share_code"] != "s123abc" for s in db.list_shares())
    print("分享删除（文件+自己的分享）OK")

    # ---------------- 秒传 ----------------
    from app.instant import InstantManager

    instant = InstantManager(cfg, db, api)
    target = FakeTargetAPI()
    instant._clients["acc2"] = target
    db.upsert_account("acc2", "fake-cookies", "20002")
    # 回调打真网络，替换成本地假数据
    # 第二个参数是源账号（文件原本所在的账号）—— v1.8.9 起会传
    # 第三个参数是文件 sha1 —— 校验缓存按它索引（2026-10 移植自 junction）
    instant._read_range_from_source = lambda pc, acc="", sha1="": (
        lambda sign_check: b"\x00" * 64)

    instant.register("A" * 40, 4096, "movie.mp4", "acc2", "pc1000")
    rec = instant.resolve("A" * 40, 4096, "movie.mp4", "acc2")
    assert rec["pickcode"].startswith("tp"), rec
    assert len(target.uploaded) == 1 and target.uploaded[0]["sha1"] == "A" * 40
    # 幂等：文件已在目标账号，不重复秒传
    instant.resolve("A" * 40, 4096, "movie.mp4", "acc2")
    assert len(target.uploaded) == 1, target.uploaded

    app3 = create_app(cfg, db, api, store, scanner, watcher,
                      {"version": "test", "scan": {}}, ed2k, share, instant, None)
    client3 = TestClient(app3)
    assert client3.post("/api/login", json={
        "username": cfg.web_username, "password": cfg.web_password}).status_code == 200
    r = client3.get("/p115/instant/" + "A" * 40 + ".mp4", params={"account": "acc2"},
                    follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text)
    assert r.headers["location"] == "https://target.example.com/tp9002" or \
        r.headers["location"].startswith("https://target.example.com/tp"), r.headers["location"]

    # 用户映射 + 状态接口
    assert client3.post("/api/instant/user", params={
        "user_id": "u1", "account": "acc2", "name": "小明"}).status_code == 200
    st = client3.get("/api/instant").json()
    assert any(u["account"] == "acc2" for u in st["users"]), st
    # 映射到主账号（account 留空）也要能存
    assert client3.post("/api/instant/user", params={
        "user_id": "u2", "name": "小红"}).status_code == 200
    st = client3.get("/api/instant").json()
    assert any(u["account"] == "" for u in st["users"]), st
    # 映射到不存在的账号 → 400，错误信息是字符串（不能是对象）
    r = client3.post("/api/instant/user", params={
        "user_id": "u3", "account": "没有这个号"})
    assert r.status_code == 400, r.text
    assert isinstance(r.json().get("detail"), str), r.json()
    print("秒传解析/播放路由 OK")

    # ---------------- 秒传：校验值缓存（移植自 junction） ----------------
    #
    # 115 的二次校验答案只跟文件内容有关。所以同一份内容被校验过一次之后，
    # 换一个账号再播（或者进程重启过）都不用再从源盘取一个字节 ——
    # 这就是 nextEmby 说的「用户间秒传加速」。
    from app.instant import InstantManager as _IM2

    inst2 = _IM2(cfg, db, api)
    # 记录源盘被读了多少次、读了多少字节
    src_reads = {"n": 0, "bytes": 0}
    # 源盘上的文件内容：按块对齐读，所以要给足一块
    src_body = b"\x11" * (512 * 1024)

    def _fake_download_url(pc, user_agent="", cache_seconds=600):
        return "https://src.example.com/dl", {"User-Agent": "ua"}

    class _FakeResp:
        status_code = 206
        content = src_body

    def _fake_http_get(url, headers=None, **kw):
        src_reads["n"] += 1
        rng = str((headers or {}).get("Range") or "")
        spec = rng.split("=", 1)[-1]
        try:
            lo_s, _, hi_s = spec.partition("-")
            lo = int(lo_s or 0)
            hi = int(hi_s) if hi_s.strip() else lo + len(src_body) - 1
        except ValueError:
            lo, hi = 0, len(src_body) - 1
        src_reads["bytes"] += hi - lo + 1
        _FakeResp.content = src_body[lo:hi + 1]
        return _FakeResp()

    src_api = api
    _orig_download_url = api.download_url
    src_api.download_url = _fake_download_url
    import app.instant as _inst_mod
    real_client_cls = _inst_mod.httpx.Client

    class _FakeHttpClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, headers=None, **kw):
            return _fake_http_get(url, headers, **kw)

    _inst_mod.httpx.Client = _FakeHttpClient
    try:
        sha1_c = "B" * 40
        # 第一次读：必须真的去源盘取（按块对齐，整块取回）
        cb1 = inst2._read_range_from_source("pcX", "", sha1_c)
        got1 = cb1("0-63")
        assert got1 == src_body[0:64], got1[:16]
        assert src_reads["n"] == 1, src_reads
        assert src_reads["bytes"] == 512 * 1024, src_reads

        # 第二次读同一段：应当命中缓存，一个字节都不从源盘取
        cb2 = inst2._read_range_from_source("pcX", "", sha1_c)
        got2 = cb2("0-63")
        assert got2 == got1, (got1[:16], got2[:16])
        assert src_reads["n"] == 1, f"缓存没命中，又读了源盘: {src_reads}"
        assert src_reads["bytes"] == 512 * 1024, src_reads

        # 同一块里的别的区间也命中（这正是「整块取回」换来的好处）
        got3 = cb2("100-163")
        assert got3 == src_body[100:164], got3[:16]
        assert src_reads["n"] == 1, f"同块其它区间应命中: {src_reads}"

        # 换一个内容（不同 sha1）→ 又要回源盘
        cb4 = inst2._read_range_from_source("pcX", "", "C" * 40)
        cb4("0-63")
        assert src_reads["n"] == 2, f"换内容应当重新读源盘: {src_reads}"

        # 缓存确实落库了
        stats = db.digest_stats()
        assert stats["blocks"] >= 1, stats
        assert stats["bytes"] > 0, stats
        # 记下了 115 要校验的区间
        row = db._conn().execute(
            "SELECT ranges FROM file_digest WHERE sha1=?", (sha1_c,)
        ).fetchone()
        assert row and "0-63" in row["ranges"], row["ranges"] if row else None

        # 清空缓存
        n = db.digest_clear()
        assert n >= 1, n
        assert db.digest_stats()["blocks"] == 0, db.digest_stats()
        print("秒传：校验值缓存命中/落库/清理 OK")
    finally:
        _inst_mod.httpx.Client = real_client_cls
        src_api.download_url = _orig_download_url

    # ---------------- 秒传：上传凭据缓存（移植自 junction） ----------------
    #
    # 换上传凭据是整条链上最慢的一跳，能命中本地就跳过它。
    inst3 = _IM2(cfg, db, api)
    tgt = FakeTargetAPI()
    inst3._clients["acc3"] = tgt
    db.upsert_account("acc3", "fake-cookies", "30003")
    cred_calls = {"n": 0}
    _orig_upload_info = tgt.upload_info

    def _counting_upload_info():
        cred_calls["n"] += 1
        return _orig_upload_info()

    tgt.upload_info = _counting_upload_info
    c1 = inst3._upload_cred(tgt)
    assert c1["userkey"], c1
    assert cred_calls["n"] == 1, cred_calls
    c2 = inst3._upload_cred(tgt)
    assert c2["userkey"] == c1["userkey"], (c1, c2)
    assert cred_calls["n"] == 1, f"凭据没缓存，又换了一次: {cred_calls}"
    # 115 轮换后丢弃重取
    inst3._forget_upload_cred(tgt)
    c3 = inst3._upload_cred(tgt)
    assert c3["userkey"] == c1["userkey"], (c1, c3)
    assert cred_calls["n"] == 2, cred_calls
    # 不同账号的凭据不能共用
    tgt2 = FakeTargetAPI()
    inst3._clients["acc4"] = tgt2
    db.upsert_account("acc4", "fake-cookies", "40004")
    assert inst3._cred_id(tgt) != inst3._cred_id(tgt2), "两个账号共用了凭据键"
    print("秒传：上传凭据缓存/轮换/账号隔离 OK")

    # ---------------- 秒传：校验缓存的管理接口 ----------------
    db.digest_block_put("D" * 40, 0, b"\x22" * 1024)
    assert db.digest_stats()["blocks"] == 1, db.digest_stats()
    r = client3.get("/api/instant/digest")
    assert r.status_code == 200, r.text
    assert r.json()["blocks"] == 1, r.json()
    r = client3.delete("/api/instant/digest")
    assert r.status_code == 200, r.text
    assert db.digest_stats()["blocks"] == 0, db.digest_stats()
    # 状态接口里也要能看到缓存占用
    st = client3.get("/api/instant").json()
    assert "digest" in st, st
    print("秒传：校验缓存管理接口 OK")

    # ---------------- 秒传：校验缓存的定期清理 ----------------
    #
    # 这一层缓存的是文件内容，按内容算大小、只增不减 —— 不设上限的话
    # 看片越多 SQLite 越大。所以要有个预算 + 定期把最久没用的丢掉。
    import threading as _th
    import time as _time

    # 上限设成 1MB，塞三块 512KB 的进去（1.5MB > 1MB）—— 超了，应该被清掉一块
    cfg.instant_digest_max_mb = 1
    inst4 = _IM2(cfg, db, api)
    db.digest_block_put("E" * 40, 0, b"\x33" * (512 * 1024))
    db.digest_block_put("E" * 40, 512 * 1024, b"\x44" * (512 * 1024))
    db.digest_block_put("E" * 40, 1024 * 1024, b"\x55" * (512 * 1024))
    assert db.digest_stats()["bytes"] > 1024 * 1024, db.digest_stats()
    # 手动 sweep：压回上限内
    res = inst4.digest_sweep()
    assert res["dropped"] >= 1, res
    assert db.digest_stats()["bytes"] <= 1024 * 1024, db.digest_stats()

    # 上限设成 0 = 不限制：sweep 不做任何事，也不该把缓存清空
    cfg.instant_digest_max_mb = 0
    db.digest_block_put("F" * 40, 0, b"\x55" * (512 * 1024))
    before = db.digest_stats()["blocks"]
    res = inst4.digest_sweep()
    assert res["dropped"] == 0, res
    assert db.digest_stats()["blocks"] == before, "不限制时不该清缓存"

    # run_forever：上限为 0 时直接返回，不占线程
    stop_evt = _th.Event()
    t = _th.Thread(target=inst4.run_forever, args=(stop_evt,), daemon=True)
    t.start()
    t.join(timeout=2)
    assert not t.is_alive(), "上限为 0 时后台线程应该直接退出"

    # 上限 > 0 时线程会跑起来；塞一堆超限的块，等它清一轮
    cfg.instant_digest_max_mb = 1
    cfg.instant_digest_clean_interval = 0.0   # 立刻触发一次检查
    cfg.instant_digest_poll_interval = 0.05
    db.digest_block_put("G" * 40, 0, b"\x66" * (512 * 1024))
    db.digest_block_put("G" * 40, 512 * 1024, b"\x77" * (512 * 1024))
    db.digest_block_put("G" * 40, 1024 * 1024, b"\x88" * (512 * 1024))
    assert db.digest_stats()["bytes"] > 1024 * 1024, db.digest_stats()
    t2 = _th.Thread(target=inst4.run_forever, args=(stop_evt,), daemon=True)
    t2.start()
    deadline = _time.time() + 8
    while _time.time() < deadline:
        if db.digest_stats()["bytes"] <= 1024 * 1024:
            break
        _time.sleep(0.1)
    stop_evt.set()
    t2.join(timeout=3)
    assert db.digest_stats()["bytes"] <= 1024 * 1024, \
        f"后台线程没有把缓存压回预算内: {db.digest_stats()}"
    cfg.instant_digest_max_mb = 512
    print("秒传：校验缓存定期清理（预算/sweep/后台线程）OK")


    # 首页：SPA 外壳 + 手动触发接口都在
    page = client3.get("/").text
    assert "loginView" in page and "appView" in page
    assert "triggerScan" not in page, "经典管理面已移除"
    # 手动触发的动作改由 API 提供
    assert client3.post("/api/scan", params={"full": 1}).status_code == 200
    assert client3.post("/api/poll").status_code == 200
    _check_page_scripts(page)
    print("首页 SPA 外壳与手动触发接口 OK")

    # ---------------- Emby 反代：PlaybackInfo 改写 ----------------
    from app.emby import EmbyProxy

    cfg.emby_url = "http://emby.example:8096"
    proxy = EmbyProxy(cfg, db, api, store, instant)
    # 找一条已扫描入库的 strm（前面添加过「电影」目录）
    rows = db._conn().execute(
        "SELECT * FROM files WHERE strm_path != '' AND pickcode != '' LIMIT 1"
    ).fetchall()
    assert rows, "索引里应有文件"
    row = dict(rows[0])

    stub_request = type("R", (), {"query_params": {}})()
    payload = {"MediaSources": [
        {"Path": row["strm_path"], "Name": row["name"], "Protocol": "File"},
        {"Path": "/not/matching/unknown.mkv"},
    ]}
    out = proxy.rewrite_playback_info(payload, stub_request, b"")
    first = out["MediaSources"][0]
    assert first["Path"].startswith(store.base_url + "/p115/play/"), first
    assert first["SupportsDirectPlay"] is True
    assert out["MediaSources"][1]["Path"] == "/not/matching/unknown.mkv"
    print("Emby strm 路径改写 OK")

    # pickcode 规则 / dbpath 自定义规则 / ed2k strm
    payload = {"MediaSources": [{"Path": "/mnt/115/pickcode=" + row["pickcode"]}]}
    out = proxy.rewrite_playback_info(payload, stub_request, b"")
    assert out["MediaSources"][0]["Path"].startswith(store.base_url + "/p115/play/")

    # 本服务自己的直链（strm 内容就是 /p115/... URL）：URL 不变，但强制直连标记，
    # 防止客户端选 /videos/{id}/original.* 让 Emby 中转
    direct_url = store.base_url + "/p115/share/abcd1234/1234567890123456789.mkv"
    payload = {"MediaSources": [{
        "Path": direct_url, "Protocol": "Http",
        "SupportsDirectPlay": False, "SupportsDirectStream": True,
        "DirectStreamUrl": "/videos/9573/original.mkv?api_key=x",
        "TranscodingUrl": "/videos/9573/master.m3u8",
    }]}
    out = proxy.rewrite_playback_info(payload, stub_request, b"")
    m0 = out["MediaSources"][0]
    assert m0["Path"] == direct_url, m0
    assert m0["SupportsDirectPlay"] is True and m0["SupportsDirectStream"] is True, m0
    assert m0["DirectStreamUrl"] == direct_url, m0
    assert "TranscodingUrl" not in m0, m0
    print("Emby 本服务直链强制直连 OK")

    # ---------------- 回归：strm 内容是播放地址时也要走秒传 ----------------
    #
    # 这是线上踩到的坑：strm 里存的是**源账号**的播放地址
    # （/p115[/a/<账号>]/play/<pickcode>），反代认得这个形状就直接原样返回，
    # 结果秒传被整个跳过 —— 用户看到的现象就是「秒传不生效」，
    # 文件字节直接从源账号出去（源盘直出）。
    #
    # 正确做法：反查这个 pickcode 对应的文件（要 sha1 + size），
    # 用户映射了秒传账号就改写成秒传路由。
    inst_r = InstantManager(cfg, db, api)
    # 造一个带 sha1/size 的索引行
    db._conn().execute(
        "INSERT INTO files(id,parent_id,name,path,pickcode,size,sha1,strm_path,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(id) DO UPDATE SET pickcode=excluded.pickcode,"
        " size=excluded.size, sha1=excluded.sha1",
        (900001, 0, "回归测试.mp4", "/回归测试.mp4", "pcREGRESS01",
         4096, "R" * 40, "/回归测试.mp4", 1),
    )
    db._conn().commit()
    # 用户 u1 映射到秒传账号 acc2（前面 set_user 过）
    inst_r.account_for_user = lambda uid: "acc2" if uid == "u1" else ""
    proxy_r = EmbyProxy(cfg, db, api, store, inst_r)
    req_u1 = type("R", (), {"query_params": {}, "headers": {}})()

    # strm 内容 = 源账号的播放地址
    play_url = store.base_url + "/p115/play/pcREGRESS01.mkv"
    payload = {"UserId": "u1",
               "MediaSources": [{"Path": play_url, "Name": "回归测试.mp4"}]}
    out = proxy_r.rewrite_playback_info(payload, req_u1, b"")
    got = out["MediaSources"][0]["Path"]
    assert "/p115/instant/" in got, f"没走秒传路由: {got}"
    assert "R" * 40 in got, got
    assert "account=acc2" in got, got
    # 并且要登记了秒传映射（播放时才有源 pickcode 可用）
    rec = db.get_instant("R" * 40, "acc2")
    assert rec and rec["source_pickcode"] == "pcREGRESS01", rec
    print("回归：strm 是播放地址时改写成秒传路由 OK")

    # 没映射秒传账号的用户 → 保持原样（源盘直出），不能硬改
    req_u2 = type("R", (), {"query_params": {}, "headers": {}})()
    payload = {"UserId": "u9",
               "MediaSources": [{"Path": play_url, "Name": "回归测试.mp4"}]}
    out = proxy_r.rewrite_playback_info(payload, req_u2, b"")
    assert out["MediaSources"][0]["Path"] == play_url, out["MediaSources"][0]
    print("回归：未映射秒传账号时保持原地址 OK")

    # pickcode 不在索引里 → 也保持原样，不能把播放搞坏
    unknown_url = store.base_url + "/p115/play/pcNOTININDEX.mkv"
    payload = {"UserId": "u1",
               "MediaSources": [{"Path": unknown_url, "Name": "x.mkv"}]}
    out = proxy_r.rewrite_playback_info(payload, req_u1, b"")
    assert out["MediaSources"][0]["Path"] == unknown_url, out["MediaSources"][0]
    print("回归：索引里没有的文件保持原地址 OK")

    # ed2k / share / instant 路由不是播放路由，不能被改成秒传
    for keep in ("/p115/ed2k/abcdef", "/p115/share/xyz/123", "/p115/instant/" + "R" * 40):
        u = store.base_url + keep
        payload = {"UserId": "u1", "MediaSources": [{"Path": u, "Name": "x.mkv"}]}
        out = proxy_r.rewrite_playback_info(payload, req_u1, b"")
        assert out["MediaSources"][0]["Path"] == u, (keep, out["MediaSources"][0])
    print("回归：非播放路由不被改写 OK")

    # 天翼云盘（/p189/play/...）不是 115 的路由：反代必须原样放过去，
    # 否则天翼的 strm 在 Emby 里会被改成一个解析不了的地址
    c189_url = store.base_url + "/p189/play/2001/%E5%A4%A9%E7%BF%BC.mkv"
    payload = {"UserId": "u1", "MediaSources": [{"Path": c189_url, "Name": "天翼.mkv"}]}
    out = proxy_r.rewrite_playback_info(payload, req_u1, b"")
    assert out["MediaSources"][0]["Path"] == c189_url, out["MediaSources"][0]
    print("回归：天翼云盘播放地址不被改写 OK")

    # 旧扫描生成的 strm 用了 docker 内网地址：PlaybackInfo 里 origin 规范化成
    # PUBLIC_BASE_URL，客户端才访问得到
    bad_url = "http://172.17.0.1:9527/p115/share/abcd1234/1234567890123456789.mkv"
    payload = {"MediaSources": [{"Path": bad_url, "Protocol": "Http"}]}
    out = proxy.rewrite_playback_info(payload, stub_request, b"")
    m0 = out["MediaSources"][0]
    assert m0["Path"].startswith(store.base_url + "/p115/share/"), m0
    assert m0["SupportsDirectPlay"] is True, m0
    print("Emby 直链 origin 规范化 OK")

    # ★ v1.5.5：播放时直接解析 115 最终直链发给播放设备
    # PlaybackInfo 请求带 UA → 反代当场用该 UA 向 115 取直链，MediaSource 就是 CDN 地址
    ua_req = type("R", (), {
        "query_params": {},
        "headers": {"user-agent": "Infuse/8.0"},
    })()
    dl_calls = len(seen_ua)
    payload = {"MediaSources": [{"Path": row["strm_path"], "Name": row["name"]}]}
    out = proxy.rewrite_playback_info(payload, ua_req, b"")
    m0 = out["MediaSources"][0]
    assert m0["Path"] == "https://cdn.example.com/" + row["pickcode"], m0
    assert m0["DirectStreamUrl"] == m0["Path"] and m0["SupportsDirectPlay"] is True, m0
    # 直链按 UA 绑定并缓存：同 UA 的第二次 PlaybackInfo 不再打 115
    out = proxy.rewrite_playback_info(
        {"MediaSources": [{"Path": row["strm_path"]}]}, ua_req, b"")
    assert len(seen_ua) == dl_calls + 1, seen_ua
    # 预解析失败（分享记录不在库里）→ 回退本服务地址，302/中转兜底
    out = proxy.rewrite_playback_info(
        {"MediaSources": [{"Path": direct_url, "Protocol": "Http"}]}, ua_req, b"")
    assert out["MediaSources"][0]["Path"].startswith(store.base_url + "/p115/share/"), \
        out["MediaSources"][0]
    print("Emby 反代直链预解析（UA 绑定 + 回退）OK")

    # 直链缓存按 UA 区分（f=1 链接要求取链接的 UA == 请求 CDN 的 UA）
    print("播放路由按请求方 UA 取直链 OK（见 302 断言处）")

    cfg.emby_path_rules = (r"/123/(.+)\.mkv=>dbpath:$1.mkv",)
    proxy2 = EmbyProxy(cfg, db, api, store, instant)
    payload = {"MediaSources": [{"Path": "/123/电影/2024/movie.mkv"}]}
    out = proxy2.rewrite_playback_info(payload, stub_request, b"")
    # row 的 path 与 /123/ 前缀不一定对上：能解析成 play url 就算命中，否则保留原样
    assert out["MediaSources"][0]["Path"].startswith(
        (store.base_url + "/p115/play/", "/123/")
    )
    cfg.emby_path_rules = ()

    ed2k_rows = [x for x in db.list_ed2k() if x.get("strm_path")]
    if ed2k_rows:
        payload = {"MediaSources": [{"Path": ed2k_rows[0]["strm_path"]}]}
        out = proxy.rewrite_playback_info(payload, stub_request, b"")
        assert "/p115/ed2k/" in out["MediaSources"][0]["Path"], out["MediaSources"][0]

    # ed2k 的直链预解析：带 UA 的 PlaybackInfo 直接拿到 CDN 直链
    proxy_ed = EmbyProxy(cfg, db, api, store, instant, ed2k)
    rec3 = ed2k.add("ed2k://|file|直链预解析.mkv|2048|" + "d" * 32 + "|/")
    api.nodes[1600] = {"id": 1600, "parent_id": dir_cid,
                       "name": "直链预解析.mkv", "is_dir": False}
    api.tree.setdefault(dir_cid, []).append(api.nodes[1600])
    api.cloud[rec3["key"]] = {"state": True, "display_status": "finished",
                              "percentDone": 100, "status_text": "下载成功"}
    assert ed2k.refresh(rec3["key"]) == 1
    payload = {"MediaSources": [
        {"Path": f"http://127.0.0.1:9527/p115/ed2k/{rec3['key']}.mkv"}]}
    out = proxy_ed.rewrite_playback_info(payload, ua_req, b"")
    assert out["MediaSources"][0]["Path"] == "https://cdn.example.com/pc1600", \
        out["MediaSources"][0]
    print("Emby 反代 ed2k 直链预解析 OK")

    # 用户映射了秒传账号：MediaSource 指向秒传路由；带 UA 时预解析成目标账号的直链
    payload = {"MediaSources": [{"Path": row["strm_path"], "Name": row["name"]}]}
    out = proxy.rewrite_playback_info(payload, stub_request,
                                      b'{"UserId": "u1"}')
    assert "/p115/instant/" in out["MediaSources"][0]["Path"], out["MediaSources"][0]
    instant.resolve(row["sha1"].upper(), int(row["size"]), row["name"], "acc2")
    out = proxy.rewrite_playback_info(payload, ua_req, b'{"UserId": "u1"}')
    assert out["MediaSources"][0]["Path"].startswith("https://target.example.com/"), \
        out["MediaSources"][0]
    print("Emby 反代规则改写 OK")

    # 同播复制：同一 key 只解析一次（缓存命中；key = 用户|客户端地址|路径，
    # 这个桩没有 Host 头，所以客户端地址是空的 —— 天翼直链改写要靠它）
    assert proxy.cache.get(
        f"u1||{row['strm_path'].replace(chr(92), '/')}") is not None
    print("Emby 反代缓存 OK")

    # gzip 透传：Emby 登录响应是压缩 JSON，content-encoding 头必须原样带回，
    # 否则客户端把压缩字节当明文解析 →「处理请求时出错」
    import gzip as _gzip
    import threading as _threading
    from http.server import BaseHTTPRequestHandler as _H
    from http.server import HTTPServer as _HS

    class _GzEmby(_H):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            self.rfile.read(n)
            payload = _gzip.compress(b'{"User": {"Id": "u9"}, "AccessToken": "t"}')
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-encoding", "gzip")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    gz_srv = _HS(("127.0.0.1", 0), _GzEmby)
    gz_port = gz_srv.server_address[1]
    _threading.Thread(target=gz_srv.serve_forever, daemon=True).start()
    cfg.emby_url = f"http://127.0.0.1:{gz_port}"
    proxy_gz = EmbyProxy(cfg, db, api, store, instant)
    req = type("R", (), {
        "method": "POST",
        "url": type("U", (), {"query": ""})(),
        "headers": {"content-type": "application/json",
                    "accept-encoding": "gzip"},
        "body": None,
    })()

    async def _body():
        return b"{}"

    req.body = _body
    import asyncio as _aio

    async def _run():
        resp = await proxy_gz.handle(req, "emby/Users/AuthenticateByName")
        assert resp.status_code == 200
        raw = b"".join([chunk async for chunk in resp.body_iterator])
        return resp, raw

    resp, raw = _aio.new_event_loop().run_until_complete(_run())
    assert _gzip.decompress(raw) == b'{"User": {"Id": "u9"}, "AccessToken": "t"}'
    # 登录时要记住 token → 用户 ID：播放请求不一定带 UserId，但一定带 token
    assert db.emby_user_by_token("t") == "u9", "登录后应记住 token 对应的用户"
    assert ("content-encoding", "gzip") in [
        (k.decode() if isinstance(k, bytes) else k,
         v.decode() if isinstance(v, bytes) else v)
        for k, v in resp.raw_headers
    ]
    gz_srv.shutdown()
    print("Emby 反代 gzip 透传 OK")

    # ---------------- 多账号：独立引擎 + uid 路由 + 跨账号 Emby 改写 ----------------
    from app.ed2k import Ed2kManager as _AccEd2k
    from app.hub import AccountHub, Engine
    from app.instant import InstantManager as _AccInstant
    from app.share import ShareManager as _AccShare

    # 每个子账号一个固定的 115 用户 ID（strm 末尾就是它，播放时按它找回账号）
    uid_of = {"acc1": "88000001", "acc2": "88000002"}

    def fake_builder(sub_cfg, name):
        acc_uid = uid_of.get(name, "88000009")
        sub_cfg.data_dir.mkdir(parents=True, exist_ok=True)
        sub_cfg.strm_root.mkdir(parents=True, exist_ok=True)
        acc_db = Database(sub_cfg.data_dir / "p115-strm.db")
        acc_db.set_meta("roots_seeded", "1")
        acc_api = FakeAPI()
        # 用独立域名区分子账号的直链（主账号走 target.example.com）
        acc_api.download_url = (
            lambda pc, user_agent="", cache_seconds=600:
                (f"https://acc1.example.com/{pc}", {})
        )
        acc_store = StrmStore(sub_cfg.strm_root, base_url=sub_cfg.base_url,
                              account_uid=acc_uid)
        acc_scanner = Scanner(sub_cfg, acc_db, acc_api, acc_store)
        acc_watcher = Watcher(sub_cfg, acc_db, acc_api, acc_store, acc_scanner)
        return Engine(name=name, uid=acc_uid, cfg=sub_cfg, db=acc_db,
                      api=acc_api, store=acc_store, scanner=acc_scanner,
                      watcher=acc_watcher,
                      ed2k=_AccEd2k(sub_cfg, acc_db, acc_api, acc_store),
                      share=_AccShare(sub_cfg, acc_db, acc_api, acc_store),
                      instant=_AccInstant(sub_cfg, acc_db, acc_api))

    main_engine = Engine(name="", cfg=cfg, db=db, api=api, store=store,
                         scanner=scanner, watcher=watcher, ed2k=ed2k,
                         share=share, instant=instant)
    hub = AccountHub(cfg, main_engine, engine_builder=fake_builder, auto_start=False)
    # 和 main.py 一致：秒传账号直接来自「115 助手」（hub），
    # 不挂 provider 的话 list_accounts() 会回落到本地 accounts 表（子账号不在里面）
    instant.accounts_provider = lambda: hub.status()
    instant.cookie_provider = lambda name: hub.cookies_for(name)
    emby_proxy = EmbyProxy(cfg, db, api, store, instant, ed2k, hub=hub)

    # ---------------- Emby 登录令牌 → 用户 ID（播放请求不带 UserId 时用） ----------------
    # 播放请求不一定带 UserId（Emby 客户端只在部分接口带），但一定带
    # X-Emby-Token。认不出用户是谁，秒传账号映射就用不上。
    db.remember_emby_token("tok-abc", "emby-user-1")
    assert db.emby_user_by_token("tok-abc") == "emby-user-1"
    assert db.emby_user_by_token("tok-nope") == ""
    # 空值不写
    db.remember_emby_token("", "x")
    db.remember_emby_token("t", "")
    assert db.emby_user_by_token("") == ""
    # 反代能从请求头里认出用户
    class _Req:
        headers = {"X-Emby-Token": "tok-abc"}
        query_params = {}
    assert emby_proxy._detect_user(_Req(), b"", {}) == "emby-user-1", "应用令牌反查用户"
    class _Req2:
        headers = {"X-Emby-Token": "tok-abc"}
        query_params = {"UserId": "direct-uid"}
    assert emby_proxy._detect_user(_Req2(), b"", {}) == "direct-uid", "UserId 优先"
    print("秒传：Emby 令牌反查用户 OK")

    app_acc = create_app(cfg, db, api, store, scanner, watcher,
                         {"version": "test", "scan": {}}, ed2k, share, instant,
                         emby_proxy, hub)
    cacc = TestClient(app_acc)
    assert cacc.post("/api/login", json={
        "username": cfg.web_username, "password": cfg.web_password}).status_code == 200

    # 添加子账号 + 校验非法输入
    r = cacc.post("/api/accounts",
                  params={"name": "acc1", "cookies": "UID=1; CID=2; SEID=3"})
    assert r.status_code == 200, r.text
    assert hub.get("acc1") is not None
    assert cacc.post("/api/accounts",
                     params={"name": "ed2k", "cookies": "x"}).status_code == 400
    assert cacc.post("/api/accounts",
                     params={"name": "bad name!", "cookies": "x"}).status_code == 400
    assert cacc.post("/api/accounts",
                     params={"name": "acc1", "cookies": "x"}).status_code == 400
    # 中文账号名可用（v1.6.1：此前被 NAME_RE 误拒）
    assert cacc.post("/api/accounts",
                     params={"name": "备用号", "cookies": "UID=9"}).status_code == 200, r.text
    assert hub.get("备用号") is not None
    # cookies 粘贴容错：多行 / Cookie: 前缀都能归一化（v1.6.1）
    from app.hub import normalize_cookies
    assert normalize_cookies("Cookie: UID=1;\nCID=2\nSEID=3") == "UID=1; CID=2; SEID=3"
    assert cacc.post("/api/accounts",
                     params={"name": "acc2", "cookies": "Cookie: UID=1;\nCID=2\nSEID=3"}
                     ).status_code == 200
    rows = {a["name"]: a for a in cacc.get("/api/accounts").json()["accounts"]}
    assert rows["acc1"]["ok"] is True, rows
    assert "备用号" in rows and "acc2" in rows, rows
    # v1.8.8：账号列表只列添加过的账号（不再有独立的 name="" 主账号行）
    ordered = cacc.get("/api/accounts").json()["accounts"]
    assert all(a["name"] for a in ordered), ordered
    assert "acc1" in {a["name"] for a in ordered}, ordered
    assert "" not in {a["name"] for a in ordered}, ordered

    # 「设为主账号」已移除：接口保留但明确返回 410，且不再有任何 main 标记
    acc1_cookie_before = hub.cookies_for("acc1")
    acc2_cookie_before = hub.cookies_for("acc2")
    r = cacc.post("/api/accounts/acc1/promote")
    assert r.status_code == 410, r.text
    assert hub.main_account_name() == "", hub.main_account_name()
    # 各账号的 cookies 没被动过
    assert hub.cookies_for("acc1") == acc1_cookie_before, "acc1 的 cookies 被改了"
    assert hub.cookies_for("acc2") == acc2_cookie_before, "acc2 的 cookies 被改了"
    rows = cacc.get("/api/accounts").json()["accounts"]
    assert all("main" not in a for a in rows), rows
    # 主账号位（name=""）固定是配置文件里那个账号，不再跟着谁走
    assert hub.cookies_for("") == cfg.cookies, (hub.cookies_for(""), cfg.cookies)

    # 子账号扫描：strm 输出目录**不再按账号名加前缀**，直接落在共用 STRM_ROOT
    # 的「本地 STRM 目录」（这里 root 前缀为空 → 就是 STRM_ROOT 本身），
    # 地址里也没有 /a/<账号名>，账号身份只在末尾的 115 用户 ID 上
    eng = hub.get("acc1")
    eng.db.add_root(100, "")
    before = {str(p) for p in cfg.strm_root.rglob("*.strm")}
    stats = eng.scanner.run(full=True)
    assert stats.files >= 1 and stats.written >= 1, stats.as_dict()
    sub_strms = [p for p in cfg.strm_root.rglob("*.strm") if str(p) not in before] or \
        list(cfg.strm_root.rglob("*.strm"))
    assert sub_strms, "子账号 strm 未生成"
    assert not (cfg.strm_root / "acc1").exists(), "strm 不该再按账号名分目录"
    sub_url = sub_strms[0].read_text(encoding="utf-8").strip()
    assert "/p115/a/" not in sub_url, sub_url
    assert sub_url.endswith("/88000001"), sub_url
    assert "/p115/play/" in sub_url, sub_url

    # 子账号播放路由：只靠末尾的 uid 就能用该账号自己的 API 取直链
    acc_row = dict(eng.db._conn().execute(
        "SELECT pickcode FROM files WHERE pickcode != '' LIMIT 1").fetchone())
    r = cacc.get(f"/p115/play/{acc_row['pickcode']}/movie.mp4/88000001",
                 follow_redirects=False)
    assert r.status_code == 302, r.status_code
    assert r.headers["location"].startswith("https://acc1.example.com/"), r.headers
    # 未知 uid 回落主账号（老 strm 也能播）
    r = cacc.get(f"/p115/play/{acc_row['pickcode']}/movie.mp4/00000000",
                 follow_redirects=False)
    assert r.status_code in (302, 404), r.status_code
    # 老路由（/a/<名字>/）继续可用，老 strm 不用立刻重写
    r = cacc.get(f"/p115/a/acc1/play/{acc_row['pickcode']}.mkv",
                 follow_redirects=False)
    assert r.status_code == 302, r.status_code
    # 不存在的账号 → 404
    assert cacc.get("/p115/a/nope/play/pc1000",
                    follow_redirects=False).status_code == 404

    # Emby 反代跨账号：子账号 strm 树下的文件改写成子账号的直链
    eng.proxy = EmbyProxy(cfg, eng.db, eng.api, eng.store, eng.instant,
                          eng.ed2k, hub=hub)
    proxy_hub = EmbyProxy(cfg, db, api, store, instant, ed2k, hub=hub)
    payload = {"MediaSources": [{"Path": str(sub_strms[0]), "Name": "acc-movie"}]}
    out = proxy_hub.rewrite_playback_info(payload, ua_req, b"")
    m0 = out["MediaSources"][0]
    assert m0["Path"].startswith("https://acc1.example.com/"), m0
    assert m0["SupportsDirectPlay"] is True, m0
    print("多账号：独立扫描/播放路由/Emby 跨账号改写 OK")

    # 删除子账号：strm 与索引一并清理
    assert cacc.delete("/api/accounts/acc1").status_code == 200
    assert hub.get("acc1") is None
    assert not (cfg.strm_root / "acc1").exists()
    assert not (cfg.data_dir / "accounts" / "acc1").exists()
    assert all(a["name"] != "acc1"
               for a in cacc.get("/api/accounts").json()["accounts"])
    print("多账号：删除清理 OK")

    # ---------------- 115 助手：显示名（改名按钮） ----------------
    cacc.post("/api/accounts", params={"name": "acc9", "cookies": "UID=9; CID=9"})
    rows = {a["name"]: a for a in cacc.get("/api/accounts").json()["accounts"]}
    assert rows["acc9"]["display"] == "", rows["acc9"]
    r = cacc.put("/api/accounts/acc9/display", params={"display": "客厅大号"})
    assert r.status_code == 200, r.text
    rows = {a["name"]: a for a in cacc.get("/api/accounts").json()["accounts"]}
    assert rows["acc9"]["display"] == "客厅大号", rows["acc9"]
    # 内部账号名（strm 目录 / URL 前缀）不变
    assert hub.get("acc9") is not None
    # 不存在的账号 → 404
    assert cacc.put("/api/accounts/nope/display",
                    params={"display": "x"}).status_code == 404
    print("115 助手：账号显示名 OK")

    # ---------------- 115 助手：cookie 读取 / 替换 / 测试 ----------------
    r = cacc.get("/api/accounts/acc9/cookie")
    assert r.status_code == 200, r.text
    assert "UID=9" in r.json()["cookies"], r.json()
    # 主账号用 main 表示（内部名是空串，URL 里不能是空的 —— 
    # 直接拼空串会变成 /api/accounts//cookie，路由匹配不到 404、
    # DELETE 更会 405「method not allowed」）
    r = cacc.get("/api/accounts/main/cookie")
    assert r.status_code == 200, r.text
    # 主账号改显示名（走 main 路径，不能 404）
    r = cacc.put("/api/accounts/main/display", params={"display": "主号"})
    assert r.status_code == 200, r.text
    assert db.get_account_display("") == "主号"
    # 主账号删除（清空 cookies，走 main 路径，不能 405）
    r = cacc.delete("/api/accounts/main")
    assert r.status_code == 200, r.text
    # 测试 cookie（传要测的那串）
    r = cacc.post("/api/accounts/acc9/test-cookie", json={"cookies": "UID=9; CID=9"})
    assert r.status_code == 200, r.text
    assert "ok" in r.json(), r.json()
    # 不存在的账号 → 404
    assert cacc.get("/api/accounts/nope/cookie").status_code == 404
    assert cacc.post("/api/accounts/nope/test-cookie").status_code == 404
    print("115 助手：cookie 读取/测试 OK")

    # ---------------- v1.8.9：源账号贯穿 / 删除映射 / 暂存目录 ----------------
    # 1) 移除映射要**真删**记录（之前只是把 account 置空，记录还在）
    db.set_emby_user("tmpuser", "临时", "acc9")
    assert db.get_emby_user("tmpuser") is not None
    assert cacc.delete("/api/instant/user/tmpuser").status_code == 200
    assert db.get_emby_user("tmpuser") is None, "移除映射后记录还在"
    assert cacc.delete("/api/instant/user/tmpuser").status_code == 404

    # 2) 暂存目录可以手动设置（路径或 cid），按账号分别记
    r = cacc.post("/api/instant/staging", params={
        "account": "acc9", "path": "/我的秒传暂存"})
    assert r.status_code == 200, r.text
    assert instant.staging_dir("acc9").get("path") == "/我的秒传暂存", instant.staging_dir("acc9")
    # 别的账号不受影响
    assert instant.staging_dir("acc2") == {}, instant.staging_dir("acc2")
    # 目录 id 也要能设
    r = cacc.post("/api/instant/staging", params={"account": "acc9", "cid": 12345})
    assert r.status_code == 200, r.text
    assert instant.staging_dir("acc9").get("cid") == 12345, instant.staging_dir("acc9")
    # 恢复默认
    r = cacc.post("/api/instant/staging", params={"account": "acc9"})
    assert r.status_code == 200, r.text
    assert instant.staging_dir("acc9") == {}, instant.staging_dir("acc9")

    # 3) 秒传要记住**源账号**（文件原本在哪个账号）——
    #    115 STRM 的文件可能在子账号下，取二次校验数据要用源账号的直链
    instant.register("B" * 40, 4096, "movie.mp4", "acc9", "pc-src", "srcAcc")
    rec = db.get_instant("B" * 40, "acc9")
    assert rec is not None and rec.get("source_account") == "srcAcc", rec
    # 反代的秒传路由要带上源账号
    assert hasattr(emby_proxy, "source_account"), "反代要有 source_account 方法"
    assert emby_proxy.source_account() == "", emby_proxy.source_account()
    url = emby_proxy._instant_route("C" * 40, 4096, "x.mkv", "acc9", "srcAcc")
    assert "source_account=srcAcc" in url, url
    print("秒传：源账号 / 删除映射 / 暂存目录 OK")

    # ---------------- 115 助手：主账号设置已移除 ----------------
    # 主账号的 cookies 文件要先存在（线上是 /config/115-cookies.txt）
    main_cookie_path = Path(cfg.cookie_path)
    main_cookie_path.parent.mkdir(parents=True, exist_ok=True)
    main_cookie_path.write_text("UID=main; CID=main\n", encoding="utf-8")
    main_before = hub.cookies_for("")
    acc9_before = hub.cookies_for("acc9")
    r = cacc.post("/api/accounts/acc9/promote")
    assert r.status_code == 410, r.text
    # 各账号 cookies 都不动；主账号位固定是配置文件里那个账号
    assert hub.cookies_for("acc9") == acc9_before, "acc9 的 cookies 被改了"
    assert hub.cookies_for("") == main_before, "主账号位不该再跟着谁走"
    assert hub.main_account_name() == "", hub.main_account_name()
    rows = cacc.get("/api/accounts").json()["accounts"]
    assert all("main" not in a for a in rows), rows
    print("115 助手：主账号设置已移除 OK")

    # ---------------- 秒传：预缓存 / 清空暂存（account 必填） ----------------
    # 前端原来只把账号拼进路径（/a/<名字>/api/instant/prewarm），
    # 后端签名是 account: str（必填**查询参数**）→ 422
    # 「account: Field required」。这里确认接口接受 account 查询参数。
    r = cacc.post("/api/instant/prewarm", params={"account": "acc9"})
    assert r.status_code == 200, r.text
    assert r.json().get("account") == "acc9", r.text
    # 账号不存在 → 404（不是 422）
    r = cacc.post("/api/instant/prewarm", params={"account": "没有这个号"})
    assert r.status_code == 404, r.text
    # 不给账号 → 400（可读提示，不是 FastAPI 的 Field required）
    r = cacc.post("/api/instant/prewarm")
    assert r.status_code == 400, r.text
    assert isinstance(r.json().get("detail"), str), r.text
    # 清空暂存目录：同样接受 account 查询参数
    r = cacc.post("/api/instant/clean", params={"account": "acc9"})
    assert r.status_code in (200, 502), r.text   # 502 = 网盘侧失败（没目录等）
    r = cacc.post("/api/instant/clean", params={"account": "没有这个号"})
    assert r.status_code in (404, 502), r.text
    r = cacc.post("/api/instant/clean")
    assert r.status_code == 400, r.text
    # 秒传账号名单只该限制「秒传到哪」：
    # 把名单设成别的账号后，取源直链与清空暂存仍要能用
    cacc.put("/api/settings", json={"settings": {"instant_accounts": ["acc2"]}})
    assert instant.allowed_accounts() == {"acc2"}, instant.allowed_accounts()
    # 源账号不在名单里也要能拿到它的直链（否则跨账号秒传直接失败）
    src_client = instant.client("acc9", enforce_allowance=False)
    assert src_client is not None
    # 但「秒传到」不在名单里的账号仍然要被拒
    from app.instant import InstantError
    try:
        instant.client("acc9")
        raise AssertionError("不在名单里的账号应该被拒绝秒传")
    except InstantError:
        pass
    cacc.put("/api/settings", json={"settings": {"instant_accounts": ["acc9"]}})
    # 预缓存的源文件要跨账号取（115 STRM 的索引在各账号自己的库里）
    src_items = instant.source_files(10)
    assert isinstance(src_items, list), src_items
    print("秒传：预缓存 / 清空暂存 OK")

    # ---------------- 界面设置（各工具的默认账号） ----------------
    r = cacc.put("/api/settings", json={"settings": {
        "default_account_share": "acc9", "default_account_ed2k": "acc9"}})
    assert r.status_code == 200, r.text
    st = cacc.get("/api/settings").json()["settings"]
    assert st["default_account_share"] == "acc9" and st["default_account_ed2k"] == "acc9", st
    # 合并保存，不会覆盖别的键
    cacc.put("/api/settings", json={"settings": {"instant_accounts": ["acc9"]}})
    st = cacc.get("/api/settings").json()["settings"]
    assert st["default_account_share"] == "acc9" and st["instant_accounts"] == ["acc9"], st
    print("界面设置：默认账号持久化 OK")

    # ---------------- 媒体服务器（多个 Emby） ----------------
    assert cacc.get("/api/media-servers").json()["servers"] == []
    r = cacc.post("/api/media-servers", json={
        "name": "客厅 Emby", "host": "http://192.168.1.10:8096",
        "api_key": "abcdef123456", "enabled": True, "note": "4K"})
    assert r.status_code == 200, r.text
    r = cacc.post("/api/media-servers", json={
        "name": "卧室 Emby", "host": "http://192.168.1.11:8096/"})
    assert r.status_code == 200, r.text
    sid2 = r.json()["server"]["id"]
    servers = cacc.get("/api/media-servers").json()["servers"]
    assert len(servers) == 2, servers
    assert {s["name"] for s in servers} == {"客厅 Emby", "卧室 Emby"}
    # 尾斜杠被去掉
    assert servers[1]["host"] == "http://192.168.2.125:8096", servers[1]
    # 地址非法 → 400
    assert cacc.post("/api/media-servers", json={
        "host": "192.168.2.124:8096"}).status_code == 400
    assert cacc.post("/api/media-servers", json={"host": ""}).status_code == 400
    # 编辑已有配置：应该更新原记录，不能新增一条（前端传 sid，后端也认 id）
    sid1 = servers[0]["id"]
    r = cacc.post("/api/media-servers", json={
        "sid": sid1, "name": "客厅 Emby 改", "host": "http://192.168.2.124:8097"})
    assert r.status_code == 200, r.text
    assert r.json()["server"]["id"] == sid1, r.json()
    servers2 = cacc.get("/api/media-servers").json()["servers"]
    assert len(servers2) == 2, servers2
    assert {s["name"] for s in servers2} == {"客厅 Emby 改", "卧室 Emby"}, servers2
    # 用 id 字段也一样能更新
    r = cacc.post("/api/media-servers", json={
        "id": sid1, "name": "客厅 Emby", "host": "http://192.168.2.124:8096"})
    assert r.json()["server"]["id"] == sid1, r.json()
    assert len(cacc.get("/api/media-servers").json()["servers"]) == 2
    # 删除
    assert cacc.delete(f"/api/media-servers/{sid2}").status_code == 200
    assert cacc.delete(f"/api/media-servers/{sid2}").status_code == 404
    assert len(cacc.get("/api/media-servers").json()["servers"]) == 1
    print("媒体服务器：多 Emby CRUD + 编辑不新增 OK")

    # ---------------- 秒传：勾选的账号才可用 ----------------
    from app.instant import InstantError
    # 未勾选（空数组）→ 不限制
    cacc.put("/api/settings", json={"settings": {"instant_accounts": []}})
    assert instant.allowed_accounts() is None, instant.allowed_accounts()
    # 勾选了 → 只有勾选的能用
    cacc.put("/api/settings", json={"settings": {"instant_accounts": ["acc9"]}})
    allowed = instant.allowed_accounts()
    assert allowed == {"acc9"}, allowed
    # 未勾选的账号拿不到客户端
    try:
        instant.client("acc-not-picked")
        raise AssertionError("未勾选的账号不该拿到客户端")
    except InstantError:
        pass
    # /api/instant 的 accounts 必须列出可用账号（以前用 db.list_accounts()
    # 只能查到本地 accounts 表，子账号没写进去就是空的）
    # 「主账号」那个虚拟项（name == ""）已随主账号设置一起移除，这里顺带锁住
    inst = cacc.get("/api/instant").json()
    names = [a.get("name") for a in inst["accounts"]]
    assert "" not in names, names
    assert "acc9" in names, names
    # 勾选后，只剩勾选的账号
    cacc.put("/api/settings", json={"settings": {"instant_accounts": ["acc9"]}})
    inst = cacc.get("/api/instant").json()
    names = [a.get("name") for a in inst["accounts"]]
    assert "acc9" in names and "acc-not-picked" not in names, names
    print("秒传：勾选账号限定 OK")
    cacc.put("/api/settings", json={"settings": {"instant_accounts": []}})

    # ---------------- Emby 反代：手动修改 ----------------
    px = cacc.get("/api/proxy").json()
    assert "settings" in px and px["servers"], px
    r = cacc.put("/api/proxy", json={
        "enabled": True, "upstream": "http://emby.local:8096",
        "strm_prefix": "/emby", "cache_seconds": 1800,
        "path_rules": "abc=>def", "log_level": "debug"})
    assert r.status_code == 200, r.text
    got = cacc.get("/api/proxy").json()["settings"]
    assert got["upstream"] == "http://emby.local:8096", got
    assert got["cache_seconds"] == 1800 and got["log_level"] == "debug", got
    assert got["path_rules"] == ["abc=>def"], got
    # 立即生效：反代对象的上游跟着变
    assert emby_proxy.upstream == "http://emby.local:8096", emby_proxy.upstream
    # 关闭反代 → /emby 返回 503
    cacc.put("/api/proxy", json={"enabled": False})
    assert cacc.get("/emby/system/info").status_code == 503
    cacc.put("/api/proxy", json={"enabled": True})
    print("Emby 反代：手动修改 OK")

    # ---------------- Emby 反代：路径完整透传（不剥前缀） ----------------
    # 上游 Emby 的 BaseUrl 可能是 / 也可能是 /emby；客户端请求什么前缀，
    # 上游就要收到同样的前缀，剥掉反而会 404。
    from http.server import BaseHTTPRequestHandler as _H2
    from http.server import HTTPServer as _HS2

    seen_paths: list[str] = []

    class _PathEmby(_H2):
        def do_GET(self):
            seen_paths.append(self.path)
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *_a):
            pass

    p_srv = _HS2(("127.0.0.1", 0), _PathEmby)
    p_port = p_srv.server_address[1]
    _threading.Thread(target=p_srv.serve_forever, daemon=True).start()
    cacc.put("/api/proxy", json={"upstream": f"http://127.0.0.1:{p_port}"})
    resp = cacc.get("/emby/System/Info/Public")
    assert resp.status_code == 200, resp.status_code
    assert seen_paths and seen_paths[-1].startswith("/emby/System/Info/Public"), seen_paths
    p_srv.shutdown()
    print("Emby 反代：路径完整透传 OK")

    # ---------------- Emby 反代：独立端口动态启停 ----------------
    from app.server import ProxyPortRunner

    runner = ProxyPortRunner(cfg, emby_proxy)
    emby_proxy.port_runner = runner
    assert runner.port == 0
    # 起一个独立端口：请求应该被代理到上游
    cacc.put("/api/proxy", json={"proxy_port": 0})
    assert runner.port == 0
    # 保存后立即生效（不用重启容器）
    free_port = _free_port()
    cacc.put("/api/proxy", json={"proxy_port": free_port})
    assert runner.port == free_port, runner.port
    assert cacc.get("/api/proxy/check").json()["port"]["listening"] == free_port
    # 换端口：旧的关掉，新的起来
    free_port2 = _free_port()
    cacc.put("/api/proxy", json={"proxy_port": free_port2})
    assert runner.port == free_port2, runner.port
    # 关掉独立端口
    cacc.put("/api/proxy", json={"proxy_port": 0})
    assert runner.port == 0
    emby_proxy.port_runner = None
    print("Emby 反代：独立端口动态启停 OK")

    # ---------------- Emby 反代：连通性检测 ----------------
    chk = cacc.get("/api/proxy/check").json()
    assert "upstream" in chk and "port" in chk, chk
    assert chk["upstream"].get("url"), chk
    print("Emby 反代：连通性检测 OK")

    # ---------------- 115 STRM 配置 ----------------
    assert cacc.get("/api/strm/configs").json()["configs"] == []
    r = cacc.post("/api/strm/configs", json={
        "account": "", "name": "电影", "remote_cid": "100",
        "remote_path": "/电影", "local_dir": "115-原档/电影",
        "base_url": "http://172.17.0.1:9527", "event_watch": True})
    assert r.status_code == 200, r.text
    cfg_id = r.json()["config"]["id"]
    configs = cacc.get("/api/strm/configs").json()["configs"]
    assert len(configs) == 1 and configs[0]["name"] == "电影", configs
    assert configs[0]["remote_cid"] == "100" and configs[0]["local_dir"] == "115-原档/电影"
    # 扩展名默认值：没填就用默认那两组
    assert configs[0]["media_exts"] == "mp4,mkv,ts,iso,rmvb,avi,mov,wmv,m2ts", configs[0]
    assert configs[0]["meta_exts"] == "nfo,jpg,jpeg,png,srt,ass,sub,idx", configs[0]
    # 显式传空 → 仍然用默认值
    r = cacc.post("/api/strm/configs", json={
        "id": cfg_id, "name": "电影", "remote_cid": "100",
        "media_exts": "", "meta_exts": ""})
    assert r.json()["config"]["media_exts"] == "mp4,mkv,ts,iso,rmvb,avi,mov,wmv,m2ts"
    # 显式传值 → 尊重用户输入
    r = cacc.post("/api/strm/configs", json={
        "id": cfg_id, "name": "电影", "remote_cid": "100",
        "media_exts": "mp4,mkv", "meta_exts": "nfo"})
    assert r.json()["config"]["media_exts"] == "mp4,mkv"
    assert r.json()["config"]["meta_exts"] == "nfo"
    # 恢复默认，方便后面断言
    r = cacc.post("/api/strm/configs", json={
        "id": cfg_id, "name": "电影", "remote_cid": "100",
        "remote_path": "/电影", "local_dir": "115-原档/电影",
        "media_exts": "", "meta_exts": ""})
    # 缺少网盘路径 → 400；非法 cid → 400
    assert cacc.post("/api/strm/configs", json={"name": "x"}).status_code == 400
    assert cacc.post("/api/strm/configs", json={
        "name": "x", "remote_cid": "abc"}).status_code == 400
    # 修改配置
    r = cacc.post("/api/strm/configs", json={
        "id": cfg_id, "name": "电影2", "remote_cid": "100",
        "remote_path": "/电影", "local_dir": "115-原档/电影2"})
    assert r.status_code == 200, r.text
    configs = cacc.get("/api/strm/configs").json()["configs"]
    assert configs[0]["name"] == "电影2" and configs[0]["local_dir"] == "115-原档/电影2", configs
    # 只改一个字段时，其它字段不能被清空（account / local_dir / base_url 等）
    r = cacc.post("/api/strm/configs", json={"id": cfg_id, "name": "电影3"})
    assert r.status_code == 200, r.text
    cfg3 = r.json()["config"]
    assert cfg3["name"] == "电影3", cfg3
    assert cfg3["local_dir"] == "115-原档/电影2", cfg3
    assert cfg3["remote_path"] == "/电影", cfg3
    assert cfg3["account"] == "", cfg3
    # 账号也不能被清空：改其它字段时保留原账号
    r = cacc.post("/api/strm/configs", json={
        "id": cfg_id, "name": "电影4", "remote_cid": "100", "account": "33t"})
    assert r.json()["config"]["account"] == "33t", r.json()
    r = cacc.post("/api/strm/configs", json={
        "id": cfg_id, "name": "电影5", "remote_cid": "100"})
    assert r.json()["config"]["account"] == "33t", r.json()
    # 恢复成主账号
    cacc.post("/api/strm/configs", json={
        "id": cfg_id, "name": "电影2", "remote_cid": "100", "account": ""})
    # 运行一次（后台扫描）
    assert cacc.post(f"/api/strm/configs/{cfg_id}/run", json={"mode": "inc"}).status_code == 200
    time.sleep(0.5)
    configs = cacc.get("/api/strm/configs").json()["configs"]
    assert configs[0]["status"] in ("idle", "running"), configs
    # 全量：清掉该目录的旧索引再重扫
    assert cacc.post(f"/api/strm/configs/{cfg_id}/run",
                     json={"mode": "full"}).status_code == 200
    time.sleep(0.5)
    configs = cacc.get("/api/strm/configs").json()["configs"]
    assert configs[0]["status"] in ("idle", "running"), configs
    # 删除
    assert cacc.delete(f"/api/strm/configs/{cfg_id}").status_code == 200
    assert cacc.delete(f"/api/strm/configs/{cfg_id}").status_code == 404
    assert cacc.get("/api/strm/configs").json()["configs"] == []
    print("115 STRM：配置 CRUD 与运行 OK")

    # ---------------- 本地映射目录浏览（本地目录选择器用） ----------------
    sub_local = cfg.strm_root / "测试目录" / "子目录"
    sub_local.mkdir(parents=True, exist_ok=True)
    (sub_local / "a.strm").write_text("http://x/1\n", encoding="utf-8")
    d = cacc.get("/api/local/dirs").json()
    assert str(Path(d["root"])) == str(Path(cfg.strm_root).resolve()), d
    assert "测试目录" in {x["name"] for x in d["dirs"]}, d
    d2 = cacc.get("/api/local/dirs", params={"path": "测试目录"}).json()
    # 相对路径按 STRM_ROOT 解析（前端传的是绝对路径，这里验证兜底）
    assert d2["dirs"] and d2["dirs"][0]["name"] == "子目录", d2
    d3 = cacc.get("/api/local/dirs", params={"path": str(sub_local.parent)}).json()
    hit = [x for x in d3["dirs"] if x["name"] == "子目录"]
    assert hit and hit[0]["strm"] == 1, d3     # 目录里已有 1 个 strm
    # 越界：只能浏览 STRM_ROOT 之内
    assert cacc.get("/api/local/dirs", params={"path": "/"}).status_code == 400
    assert cacc.get("/api/local/dirs", params={"path": "/config"}).status_code == 400
    assert cacc.get("/api/local/dirs",
                    params={"path": str(sub_local / "不存在")}).status_code == 404
    print("本地映射目录浏览 OK")

    # ---------------- 分享 / ed2k 的本地 strm 目录可配置 ----------------
    r = cacc.post("/api/share/settings", params={"strm_dir": "我的分享"})
    assert r.status_code == 200, r.text
    assert r.json()["strm_dir"] == "我的分享", r.json()
    r = cacc.post("/api/ed2k/settings", params={"strm_dir": "我的ed2k"})
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["strm_dir"] == "我的ed2k", r.json()
    print("分享 / ed2k 本地输出目录可配置 OK")

    # ---------------- 事件日志（落库，刷新不丢） ----------------
    ev = cacc.get("/api/strm/events", params={"limit": 50}).json()["events"]
    kinds = {e["kind"] for e in ev}
    assert "config_save" in kinds, kinds
    assert "run_done" in kinds, kinds
    assert any(e["message"] for e in ev), ev
    before_cnt = len(ev)
    # 清空后归零
    assert cacc.delete("/api/strm/events").json()["removed"] >= 1
    assert cacc.get("/api/strm/events").json()["events"] == []
    print("事件日志 OK")

    # ---------------- 全新生成：先删旧 strm 再从头生成 ----------------
    r = cacc.post("/api/strm/configs", json={
        "name": "全新", "remote_cid": "100", "remote_path": "/电影",
        "local_dir": "全新目录"})
    assert r.status_code == 200, r.text
    rid = r.json()["config"]["id"]
    target = cfg.strm_root / "全新目录"
    target.mkdir(parents=True, exist_ok=True)
    # 别人的 strm（别的配置 / 别的账号写进同一个目录）：全新生成绝对不能碰它。
    # 曾经是「按目录 rglob 整片删」，线上两条配置都填 /strm 时会删掉几万个别人的文件。
    stranger = target / "别人的.strm"
    stranger.write_text("http://other/1\n", encoding="utf-8")
    assert cacc.post(f"/api/strm/configs/{rid}/run",
                     json={"mode": "full"}).status_code == 200
    time.sleep(1.5)
    mine = [p for p in target.rglob("*.strm") if p.name != stranger.name]
    assert mine, "全量生成后应该有 strm"
    before_mtime = max(p.stat().st_mtime_ns for p in mine)
    # 全新：先删这条配置生成过的 strm，再从头生成一遍
    assert cacc.post(f"/api/strm/configs/{rid}/run",
                     json={"mode": "rebuild"}).status_code == 200
    time.sleep(1.5)
    again = [p for p in target.rglob("*.strm") if p.name != stranger.name]
    assert again, "全新生成后应该有新写的 strm"
    assert max(p.stat().st_mtime_ns for p in again) > before_mtime, \
        "全新生成应该重写自己的 strm"
    assert stranger.exists(), "全新生成不许删别的配置 / 别的账号的 strm"
    ev = cacc.get("/api/strm/events").json()["events"]
    msg = " ".join(e.get("message") or "" for e in ev)
    assert "全新生成完成" in msg and "清理旧文件" in msg, ev[:3]
    assert cacc.delete(f"/api/strm/configs/{rid}").status_code == 200
    print("115 STRM：全新生成覆盖 OK")

    # ---------------- 天翼云盘（189）：多账号 / 家庭云 / 续期 / 生成 / 播放 ----
    from app.cloud189 import Cloud189Client, Cloud189Manager

    tree189 = {
        "-11": [{"id": "200", "name": "天翼电影", "size": 0, "is_dir": True}],
        "200": [
            {"id": "2001", "name": "天翼测试.mkv", "size": 100, "is_dir": False},
            {"id": "2002", "name": "封面.jpg", "size": 10, "is_dir": False},
        ],
        # 家庭云（folderId 留空的那一层）
        "": [{"id": "900", "name": "家庭片库", "size": 0, "is_dir": True}],
        "900": [{"id": "9001", "name": "家庭测试.mkv", "size": 100, "is_dir": False}],
    }

    class _Fake189:
        def __init__(self, tree):
            self.tree = tree

        def list_files(self, folder_id, family_id="", page_size=100):
            return self.tree.get(str(folder_id), [])

        def list_families(self):
            return [{"family_id": "579", "name": "18000000000", "count": 1}]

        def download_url(self, file_id, family_id=""):
            tail = f"f{family_id}/" if family_id else ""
            return f"https://cdn189.example.com/{tail}{file_id}"

    cloud = Cloud189Manager(cfg, db, store)
    cloud.client = lambda *a, **k: _Fake189(tree189)
    # 天翼云盘是插件：这里手工把它标成已安装，并把管理器注入进去
    from app.plugins import PluginManager
    plugins = PluginManager(cfg, db)
    plugins.bind(cfg=cfg, db=db, api=api, store=store, scanner=scanner,
                 watcher=watcher, hub=None, emby=None, state={}, engines=lambda: [])
    plugins._state["installed"]["cloud189"] = {"version": "test", "at": 0}
    plugins._state["enabled"]["cloud189"] = True
    plugins.inject("cloud189", cloud)
    # 天翼云盘：接口都挂在主服务上，账号用 ?account=<id> 指定，没有 /a/<账号> 变体
    app189 = create_app(cfg, db, api, store, scanner, watcher,
                        {"version": "test", "scan": {}}, ed2k, plugins=plugins)
    c189 = TestClient(app189)
    c189.post("/api/login", json={"username": "tester", "password": cfg.web_password})
    st = c189.get("/api/cloud189").json()
    assert st["logged_in"] is False and st["configs"] == [], st
    assert st["accounts"] == [] and st["renew_ahead_hours"] == 24, st
    # 乱码 cookie 必须 400（不能悄悄写进库）
    bad = c189.post("/api/cloud189/cookie", json={"cookie": "hello world"})
    assert bad.status_code == 400, bad.text
    assert c189.get("/api/cloud189").json()["has_token"] is False
    # 扫码排查接口存在（扫码没反应时靠它看原始响应）
    assert "log" in c189.get("/api/cloud189/login/debug").json()

    # 造两个账号：A 有会话密钥（能续期），B 是手动 token（不能续期）
    now_ms = int(time.time() * 1000)
    db.upsert_cloud189_account({
        "id": "c189a", "name": "账号A", "login_name": "a@189.cn",
        "access_token": "tok-a", "session_key": "SK-A",
        "expires_at": now_ms + 86400 * 1000, "is_default": 1, "source": "cookie"})
    db.upsert_cloud189_account({
        "id": "c189b", "name": "账号B", "login_name": "b@189.cn",
        "access_token": "tok-b", "expires_at": now_ms + 86400 * 1000,
        "is_default": 0, "source": "token"})
    accs = c189.get("/api/cloud189/accounts").json()
    assert [a["id"] for a in accs["accounts"]] == ["c189a", "c189b"], accs
    assert accs["accounts"][0]["can_refresh"] is True
    assert accs["accounts"][1]["can_refresh"] is False
    # 手动续期：会话密钥换成新 token（接口层不联网，用假的换票函数）
    _real_exchange = Cloud189Client.token_by_sessions_key
    Cloud189Client.token_by_sessions_key = staticmethod(
        lambda session_key, trace=None: ("tok-a-new", 7200))
    try:
        rr = c189.post("/api/cloud189/accounts/c189a/refresh")
        assert rr.status_code == 200 and rr.json()["account"]["refresh_count"] == 1, rr.text
        assert rr.json()["account"]["can_refresh"] is True
        # 没有会话密钥的账号续期必须 400（说清楚为什么要重新登录）
        rr2 = c189.post("/api/cloud189/accounts/c189b/refresh")
        assert rr2.status_code == 400 and "会话密钥" in rr2.text, rr2.text
    finally:
        Cloud189Client.token_by_sessions_key = _real_exchange

    # 本地目录必填：不填直接 400（避免 strm 落到根目录把别人的库搅乱）
    assert c189.post("/api/cloud189/configs",
                     json={"name": "天翼", "remote_id": "200"}).status_code == 400
    # 配置必须绑到存在的账号
    assert c189.post("/api/cloud189/configs", json={
        "name": "x", "remote_id": "200", "local_dir": "天翼",
        "account": "c189nope"}).status_code == 400

    r = c189.post("/api/cloud189/configs", json={
        "name": "天翼测试", "remote_id": "200", "remote_path": "/天翼电影",
        "local_dir": "天翼", "media_exts": "mkv", "account": "c189a",
    })
    assert r.status_code == 200, r.text
    cid189 = r.json()["config"]["id"]
    assert r.json()["config"]["account"] == "c189a"
    assert c189.get("/api/cloud189/configs").json()["configs"][0]["id"] == cid189

    # 列目录：个人云根最上面是「家庭云」，后面才是自己建的目录
    dirs = c189.get("/api/cloud189/dirs?folder_id=-11").json()
    assert [d["id"] for d in dirs["dirs"]] == ["fam", "200"], dirs
    fams = c189.get("/api/cloud189/dirs?folder_id=fam").json()
    assert [d["id"] for d in fams["dirs"]] == ["fam:579"], fams
    fam_root = c189.get("/api/cloud189/dirs?folder_id=fam:579").json()
    assert [d["id"] for d in fam_root["dirs"]] == ["fam:579:900"], fam_root

    # 生成（个人云）：strm 里要带账号段
    assert c189.post(f"/api/cloud189/configs/{cid189}/run",
                     json={"mode": "inc"}).status_code == 200
    time.sleep(1.5)
    # remote_id=200 就是「天翼电影」那一层，所以文件直接落在 local_dir 下
    mine189 = cfg.strm_root / "天翼" / "天翼测试.strm"
    assert mine189.exists(), sorted(p.name for p in (cfg.strm_root / "天翼").rglob("*"))
    content189 = mine189.read_text(encoding="utf-8").strip()
    assert "/p189/play/c189a/2001" in content189 and "cdn189" not in content189, content189
    assert not (cfg.strm_root / "天翼" / "封面.strm").exists()

    # 生成（家庭云）：配置的 remote_id = fam:<家庭id>，strm 里多一段 /fam/579/
    r2 = c189.post("/api/cloud189/configs", json={
        "name": "家庭云测试", "remote_id": "fam:579", "remote_path": "/个人云/家庭云/18000000000",
        "local_dir": "天翼家庭", "media_exts": "mkv", "account": "c189b",
    })
    assert r2.status_code == 200, r2.text
    cid_fam = r2.json()["config"]["id"]
    assert c189.post(f"/api/cloud189/configs/{cid_fam}/run",
                     json={"mode": "inc"}).status_code == 200
    time.sleep(1.5)
    # 家庭云根下只有「家庭片库」这一层目录，文件在它里面
    fam_strm = cfg.strm_root / "天翼家庭" / "家庭片库" / "家庭测试.strm"
    assert fam_strm.exists(), sorted(p.name for p in (cfg.strm_root / "天翼家庭").rglob("*"))
    fam_content = fam_strm.read_text(encoding="utf-8").strip()
    assert "/p189/play/c189b/fam/579/9001" in fam_content, fam_content

    # 生成（整个家庭云）：remote_id = fam，每个家庭落到同名子目录
    r3 = c189.post("/api/cloud189/configs", json={
        "name": "整个家庭云", "remote_id": "fam", "remote_path": "/个人云/家庭云",
        "local_dir": "天翼全部家庭", "media_exts": "mkv", "account": "c189b",
    })
    assert r3.status_code == 200, r3.text
    cid_all = r3.json()["config"]["id"]
    assert c189.post(f"/api/cloud189/configs/{cid_all}/run",
                     json={"mode": "inc"}).status_code == 200
    time.sleep(1.5)
    all_strm = (cfg.strm_root / "天翼全部家庭" / "18000000000"
                / "家庭片库" / "家庭测试.strm")
    assert all_strm.exists(), sorted(
        p.name for p in (cfg.strm_root / "天翼全部家庭").rglob("*"))
    assert "/p189/play/c189b/fam/579/9001" in all_strm.read_text(encoding="utf-8")

    ev189 = c189.get("/api/cloud189/events").json()["events"]
    kinds189 = [e["kind"] for e in ev189]
    assert "run_done" in kinds189 and "refresh" in kinds189, kinds189[:5]

    # 播放：/p189/play/<账号>/<fileId> 换直链后 302
    resp = c189.get("/p189/play/c189a/2001", follow_redirects=False)
    assert resp.status_code == 302, resp.status_code
    assert resp.headers["location"] == "https://cdn189.example.com/2001"
    # 家庭云播放：直链按 familyId 取（假客户端会把 family 编进 URL）
    resp_f = c189.get("/p189/play/c189b/fam/579/9001", follow_redirects=False)
    assert resp_f.status_code == 302, resp_f.status_code
    assert resp_f.headers["location"] == "https://cdn189.example.com/f579/9001"
    # 老格式（没有账号段）继续可用：按索引反查归属
    resp_old = c189.get("/p189/play/2001", follow_redirects=False)
    assert resp_old.status_code == 302, resp_old.status_code

    # 反代端口也要能播天翼：以前 /p189 会被原样转给 Emby 然后 404，
    # 反代改写出来的地址就成了死链。这里锁住「反代端口能换发天翼直链」。
    from app.emby import EmbyProxy
    from app.server import create_proxy_app

    emby189 = EmbyProxy(cfg, db, api, store, instant, ed2k, cloud189=cloud)
    proxy_app = create_proxy_app(emby189)
    cp189 = TestClient(proxy_app)
    pr = cp189.get("/p189/play/c189a/2001", follow_redirects=False)
    assert pr.status_code == 302, (pr.status_code, pr.text)
    assert pr.headers["location"] == "https://cdn189.example.com/2001", pr.headers
    pr_f = cp189.get("/p189/play/c189b/fam/579/9001", follow_redirects=False)
    assert pr_f.headers["location"] == "https://cdn189.example.com/f579/9001"

    # 反代改写 PlaybackInfo：天翼 strm → 「客户端正在用的地址 + /p189/play」
    class _Req189:
        headers = {"host": "127.0.0.1:7259", "user-agent": "Infuse/7"}
        query_params = {}
        url = SimpleNamespace(scheme="http")

    pay189 = {"MediaSources": [{"Path": str(mine189), "Protocol": "File"}]}
    got189 = emby189.rewrite_playback_info(pay189, _Req189(), b"")
    direct189 = got189["MediaSources"][0]["DirectStreamUrl"]
    assert direct189.startswith("http://127.0.0.1:7259/p189/play/c189a/2001"), direct189
    print("天翼云盘：反代端口直链播放 OK")

    # 删账号：名下还有配置 → 400；带 force 才能连配置一起删
    assert c189.delete("/api/cloud189/accounts/c189a").status_code == 400
    assert c189.post("/api/cloud189/accounts/c189a/default").status_code == 200
    # 删除配置：默认保留 strm
    assert c189.delete(f"/api/cloud189/configs/{cid189}").status_code == 200
    assert mine189.exists(), "默认只删配置，不动已生成的 strm"
    assert c189.delete("/api/cloud189/configs/" + cid_fam).status_code == 200
    assert c189.delete("/api/cloud189/configs/" + cid_all).status_code == 200
    out = c189.delete("/api/cloud189/accounts/c189a?force=1").json()
    assert out["removed_configs"] == 0 and c189.get(
        "/api/cloud189").json()["accounts"][0]["id"] == "c189b"
    print("天翼云盘：多账号 / 家庭云 / 续期 / 配置 / 生成 / 播放 / 事件 OK")

    # ---------------- 清理 ----------------
    db.close()
    shutil.rmtree(WORK, ignore_errors=True)
    print("\n全部通过 ✅")
    return 0


def _check_page_scripts(page: str) -> None:
    """校验前端 JS 语法：SPA 用外链脚本，逐个用 node --check 过一遍。"""
    import re as _re
    import subprocess

    node = r"C:/Users/koala/.workbuddy/binaries/node/versions/22.22.2-6/node.exe"
    scripts = _re.findall(r'<script[^>]+src="([^"]+)"', page)
    assert scripts, "页面里没有脚本引用"
    for src in scripts:
        if not src.startswith("/static/"):
            continue
        # 带 ?v=版本号 的资源路径要去掉查询串
        rel = src[len("/static/"):].split("?")[0]
        path = ROOT / "web" / rel
        assert path.exists(), f"脚本不存在：{src}"
        try:
            proc = subprocess.run(
                [node, "--check", str(path)],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except FileNotFoundError:
            return  # 没有 node 就跳过语法检查
        if proc.returncode != 0:
            raise AssertionError(f"{src} 语法错误:\n{proc.stderr[:2000]}")
    # 秒传状态必须走主服务：用户映射 / 暂存目录设置 / 校验缓存都只存在主库里，
    # 打到子账号路径 /api/a/<名字>/instant 会 404，页面就一直是初始空状态
    # （表现为「映射保存了列表还是空」「暂存目录改了顶部还显示默认目录」）。
    api_js = (ROOT / "web" / "js" / "api.js").read_text(encoding="utf-8")
    assert "accBase(account) + '/api/instant'" not in api_js, \
        "秒传状态接口不能走子账号路径（会 404）"
    assert "'/api/instant' + qs" in api_js, "秒传状态接口应走主服务"
    # 弹窗必须支持嵌套：「配置表单里开选目录」曾经把外层表单 DOM 冲掉，
    # 选完目录回来 querySelector 全 null（报 cannot set properties of null）。
    core_js = (ROOT / "web" / "js" / "core.js").read_text(encoding="utf-8")
    assert "modalStack" in core_js and "closeAll" in core_js, \
        "弹窗需要栈式管理（嵌套弹窗不能冲掉下层 DOM）"
    assert "layerAt(" in core_js, "嵌套弹窗要新建独立层，不能复用同一个 #modalBox"


if __name__ == "__main__":
    raise SystemExit(main())
