"""元数据下载的大小校验回归测试。

115 的 CDN 偶尔会回一个 HTTP 200 的 HTML 错误页，如果照单全收，
Emby 会把这个「nfo」当成已刮削，比没有 nfo 更糟。这里锁住：下载完必须
和 115 上报的 size 一致，不一致就丢弃，且不留 .part 残骸。
"""

from __future__ import annotations

import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.meta import MetaDownloader
from app.strm import StrmStore

CONTENT = b"<movie><title>x</title></movie>"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server 的接口名
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(CONTENT)))
        self.end_headers()
        self.wfile.write(CONTENT)

    def log_message(self, *args):
        pass


class _API:
    def __init__(self, url: str) -> None:
        self.url = url

    def download_url(self, pickcode: str, user_agent: str = ""):
        return self.url, {"user-agent": user_agent}


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/x.nfo"


def _downloader(url: str):
    tmp = Path(tempfile.mkdtemp(prefix="panshare-meta-size-"))
    return MetaDownloader(_API(url), StrmStore(tmp / "strm"), retries=0), tmp


def test_size_matches_downloads():
    server, url = _serve()
    try:
        dl, tmp = _downloader(url)
        stats = dl.fetch_one("电影/2024", {"name": "x.nfo", "pickcode": "pc", "size": len(CONTENT)})
        assert (stats.downloaded, stats.failed, stats.skipped) == (1, 0, 0)
        assert (tmp / "strm" / "电影" / "2024" / "x.nfo").read_bytes() == CONTENT
    finally:
        server.shutdown()


def test_size_mismatch_discards_without_part_file():
    server, url = _serve()
    try:
        dl, tmp = _downloader(url)
        # 115 说这个文件有 999 字节，实际只回来一个 HTML 错误页
        stats = dl.fetch_one("电影/2024", {"name": "x.nfo", "pickcode": "pc", "size": 999})
        assert (stats.downloaded, stats.failed) == (0, 1)
        folder = tmp / "strm" / "电影" / "2024"
        assert not (folder / "x.nfo").exists()
        assert not (folder / ".x.nfo.part").exists()
    finally:
        server.shutdown()


def test_no_reported_size_still_downloads():
    """老节点没带 size 时照旧下载，不能因为加了校验就全军覆没。"""
    server, url = _serve()
    try:
        dl, _tmp = _downloader(url)
        stats = dl.fetch_one("/", {"name": "x.nfo", "pickcode": "pc"})
        assert (stats.downloaded, stats.failed) == (1, 0)
    finally:
        server.shutdown()
