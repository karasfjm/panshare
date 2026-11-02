"""端到端验证：真实账号 + 假 Emby 上游。

1. 起一个假 Emby 上游（返回带 MediaSources 的 PlaybackInfo）；
2. 起本服务（EMBY_ENABLED=1，监控一个小目录）；
3. 依次验证：首页按钮/卡片、全量扫描、分享转存、Emby 反代改写、秒传接口。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WORK = Path(tempfile.mkdtemp(prefix="p115e2e-"))
CID = "3497526408832353671"  # 家里家外 (2025)：小目录，2 个文件

os.environ.update(
    DATA_DIR=str(WORK / "config"),
    STRM_ROOT=str(WORK / "strm"),
    ROOT_CIDS=CID,
    P115_COOKIE_FILE=str(ROOT / "config" / "115-cookies.txt"),
    WEB_PORT="9536",
    PUBLIC_BASE_URL="http://127.0.0.1:9536",
    API_QPS="4",
    EMBY_ENABLED="1",
    EMBY_URL="http://127.0.0.1:9601",
    MODE="serve",
    LOG_LEVEL="INFO",
    # 端到端要跑管理接口，这里关掉登录（登录本身在冒烟测试里覆盖）
    UI_PASSWORD="",
)

import httpx
import uvicorn

from app.config import load_config
from app.main import build
from app.server import create_app

FAKE_EMBY = {
    "MediaSources": [
        # 会在改写时命中「本服务 strm」规则
        {"Id": "ms1", "Path": "", "Name": "movie.mp4", "Protocol": "File"},
        {"Id": "ms2", "Path": "/mnt/whatever/unknown.mkv", "Name": "unknown.mkv"},
    ],
}


class FakeEmby(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        body = json.dumps(FAKE_EMBY).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        body = json.dumps({"ServerName": "fake-emby"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    cfg = load_config()
    (db, api, store, scanner, watcher, ed2k, share, instant, emby, state,
     _hub) = build(cfg)

    upstream = HTTPServer(("127.0.0.1", 9601), FakeEmby)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    app = create_app(cfg, db, api, store, scanner, watcher, state,
                     ed2k, share, instant, emby)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=9536, log_level="warning", access_log=False,
    ))
    threading.Thread(target=server.run, daemon=True).start()
    base = "http://127.0.0.1:9536"
    for _ in range(50):
        try:
            if httpx.get(base + "/healthz", timeout=2).status_code == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)

    print("== 1. 全量扫描 ==")
    state["scan"] = scanner.run(full=True).as_dict()
    print("   ", state["scan"])

    print("== 2. 首页（SPA 外壳） ==")
    page = httpx.get(base + "/", timeout=10).text
    for needle in ("loginView", "appView", "/static/js/main.js"):
        assert needle in page, needle
    # 经典管理面已移除
    assert "ed2k 转存播放" not in page
    # 静态资源可访问
    assert httpx.get(base + "/static/styles.css", timeout=10).status_code == 200
    print("    OK：SPA 外壳与静态资源")

    print("== 3. 手动触发接口 ==")
    assert httpx.post(base + "/api/scan", params={"full": 0}, timeout=15).status_code == 200
    r = httpx.post(base + "/api/poll", timeout=60).json()
    assert r["accepted"] is True, r
    print("    OK:", r)

    print("== 4. 分享转存（真实账号，自建分享自收） ==")
    # 先建一个测试分享
    rows = db._conn().execute(
        "SELECT id FROM files LIMIT 1"
    ).fetchall()
    assert rows, "应已有索引文件"
    sent = api.client.share_send({"file_ids": str(rows[0]["id"])})
    assert sent.get("state"), sent
    lst = api.client.share_list({"limit": 1, "offset": 0, "share_state": 1})
    item = lst["list"][0]
    share_url = f"https://115.com/s/{item['share_code']}?password={item['receive_code']}"
    print("    测试分享:", share_url)
    r = httpx.post(base + "/api/share", params={"url": share_url, "force": "true"},
                   timeout=180).json()
    rec = r["share"]
    assert rec["status"] == "ready", rec
    print("    OK: 转存", rec["done"], "/", rec["total"], "strm 已生成")
    strms = list((cfg.strm_root / "share").rglob("*.strm"))
    assert strms, "share strm 未生成"
    print("    strm:", strms[0].relative_to(cfg.strm_root))
    # 清理测试分享与文件
    httpx.delete(base + f"/api/share/{rec['share_code']}", timeout=120)
    api.client.share_update({"share_code": item["share_code"], "action": "delete"})
    print("    已清理")

    print("== 5. Emby 反代：PlaybackInfo 改写 ==")
    file_rows = db._conn().execute(
        "SELECT * FROM files WHERE strm_path != '' LIMIT 1"
    ).fetchall()
    assert file_rows
    row = dict(file_rows[0])
    FAKE_EMBY["MediaSources"][0]["Path"] = row["strm_path"]
    r = httpx.post(base + "/emby/Items/123/PlaybackInfo?UserId=uX", json={},
                   timeout=30).json()
    ms1, ms2 = r["MediaSources"]
    assert ms1["SupportsDirectPlay"] is True and ms1["DirectStreamUrl"] == ms1["Path"]
    assert ms2["Path"] == "/mnt/whatever/unknown.mkv"  # 未命中的保持原样
    if ms1["Path"].startswith(base + "/p115/"):
        print("    （直链预解析回退为本服务地址）", ms1["Path"])
    else:
        # v1.5.5：反代当场用请求方 UA 向 115 取好直链，直接发给播放设备
        assert "115" in ms1["Path"] or ms1["Path"].startswith("http"), ms1
        print("    OK: 115 直链:", ms1["Path"][:110])

    print("== 6. Emby 反代透传（非 PlaybackInfo） ==")
    r = httpx.get(base + "/emby/system/info", timeout=15).json()
    assert r["ServerName"] == "fake-emby", r
    print("    OK: GET /emby/system/info ->", r)

    print("== 7. 秒传接口 ==")
    st = httpx.get(base + "/api/instant", timeout=15).json()
    assert st["accounts"] == [] and "instant_dir" in st, st
    # 无效账号添加应 400
    r = httpx.post(base + "/api/instant/accounts",
                   params={"name": "bad", "cookies": "invalid"}, timeout=60)
    assert r.status_code == 400, r.text
    print("    OK: /api/instant 正常，非法 cookies 被拒绝")

    server.should_exit = True
    upstream.shutdown()
    db.close()
    print("\n端到端全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
