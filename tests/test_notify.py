"""通知（企业微信 / TG）与指令处理的回归测试。

* AES-256 + 企业微信加解密用 NIST 标准向量锁住（容器里没有 crypto 依赖，
  这套实现是手写的，必须证明它能解开真实密文）；
* 指令解析覆盖 /help /status /ls /run /find /pending /link /notify；
* 下发给前端的配置必须打码，不能把 secret / token 原样吐出去。
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import wxcrypt
from app.config import Config
from app.db import Database
from app.notify import Notifier, _chunks, _merged


def _db() -> Database:
    tmp = tempfile.mkdtemp(prefix="panshare-notify-db-")
    return Database(str(Path(tmp) / "t.db"))


def _notifier(db: Database | None = None, engines=None) -> Notifier:
    cfg = Config()
    return Notifier(cfg, db or _db(), engines=engines or list)


AES_KEY_B64 = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
AES_KEY = AES_KEY_B64[:43]           # 企业微信用 43 位 EncodingAESKey


# ------------------------------------------------------------------ 加密实现
def test_aes256_nist_vector():
    """NIST FIPS-197 / SP800-38A 的 AES-256 单块向量。"""
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f"
                        "101112131415161718191a1b1c1d1e1f")
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    keys = wxcrypt._expand_key(key)
    cipher = wxcrypt.aes_encrypt_block(plain, keys)
    assert cipher.hex() == "8ea2b7ca516745bfeafc49904b496089"
    assert wxcrypt.aes_decrypt_block(cipher, keys) == plain


def test_aes_cbc_roundtrip():
    key = b"k" * 32
    iv = b"i" * 16
    data = b"A" * 16 + b"B" * 16
    assert wxcrypt.aes_cbc_decrypt(key, iv, wxcrypt.aes_cbc_encrypt(key, iv, data)) == data


def test_wecom_message_roundtrip():
    text = "<xml><Content>/status</Content></xml>"
    enc = wxcrypt.encrypt_message(AES_KEY, text, "ww123456")
    assert wxcrypt.decrypt_message(AES_KEY, enc, "ww123456") == text
    assert wxcrypt.verify_url(AES_KEY, enc, "ww123456") == text
    # corp_id 不匹配要报错（配置错了宁可失败也不要静默收错消息）
    try:
        wxcrypt.decrypt_message(AES_KEY, enc, "ww-other")
    except ValueError as exc:
        assert "receiveid" in str(exc)
    else:  # pragma: no cover - 走到这里说明校验失效
        raise AssertionError("corp_id 不匹配却没有报错")


def test_wecom_signature():
    sig = wxcrypt.signature("tok", "123", "abc", "ENC")
    assert sig == wxcrypt.signature("tok", "123", "abc", "ENC")
    assert sig != wxcrypt.signature("tok", "124", "abc", "ENC")


# ------------------------------------------------------------------ 配置
def test_settings_defaults_and_merge():
    db = _db()
    n = _notifier(db)
    s = n.settings()
    assert s["enabled"] is False
    assert s["events"]["run_done"] is True
    assert s["wecom"]["enabled"] is False

    n.save_settings({"enabled": True, "events": {"run_start": True},
                     "tg": {"enabled": True, "chat_id": "42"}})
    s = n.settings()
    assert s["enabled"] is True and s["events"]["run_start"] is True
    assert s["events"]["run_done"] is True          # 没传的字段保持不变
    assert s["tg"]["chat_id"] == "42"


def test_merged_survives_garbage():
    out = _merged({"events": "oops", "wecom": {"enabled": "yes"}, "tg": None})
    assert out["events"]["run_done"] is True
    assert out["wecom"]["enabled"] is True
    assert out["tg"]["enabled"] is False


def test_status_masks_secrets():
    n = _notifier()
    n.save_settings({
        "wecom": {"enabled": True, "secret": "SUPERSECRETVALUE123",
                  "aes_key": "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcde",
                  "webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=HOOKSECRET999"},
        "tg": {"enabled": True, "token": "123456:AAHHBBCCDDEEFFGG"},
    })
    dumped = json.dumps(n.status()["settings"], ensure_ascii=False)
    for secret in ("SUPERSECRETVALUE123", "HOOKSECRET999",
                   "123456:AAHHBBCCDDEEFFGG"):
        assert secret not in dumped


# ------------------------------------------------------------------ 发送
def test_send_skipped_when_disabled():
    n = _notifier()
    out = n.send("hello", event="run_done")
    assert out["ok"] is False and out["skipped"]


def test_send_respects_event_switch():
    n = _notifier()
    n.save_settings({"enabled": True})
    out = n.send("hello", event="scan")      # 默认不推送扫描
    assert out["ok"] is False and "scan" in out["skipped"]


def test_chunks_by_bytes():
    text = "中文" * 10                        # 每个汉字 3 字节
    parts = _chunks(text, 12)
    assert all(len(p.encode("utf-8")) <= 12 for p in parts)
    assert "".join(parts) == text


# ------------------------------------------------------------------ 指令
def test_command_help_and_unknown():
    n = _notifier()
    assert "/status" in n.handle_command("/help")
    assert "不认识的指令" in n.handle_command("/nope")


def test_command_status_uses_provider():
    db = _db()
    n = _notifier(db)
    n.status_provider = lambda: {
        "version": "1.17.0",
        "db": {"files": 12, "dirs": 3, "strm": 11},
        "scan": {"finished_at": 1700000000, "files": 2, "written": 1},
        "watch": {"mode": "interval"},
    }
    out = n.handle_command("/status")
    assert "1.17.0" in out and "12" in out and "定时增量" in out


def test_command_ls_lists_configs():
    db = _db()
    db.upsert_strm_config({
        "id": "cfg1", "account": "", "name": "国漫",
        "remote_cid": "1", "remote_cids": "1", "remote_path": "/影音/国漫",
        "remote_paths": "/影音/国漫", "local_dir": "/strm/国漫", "path_dirs": "",
        "base_url": "", "media_exts": "", "meta_exts": "",
        "download_meta": 1, "event_watch": 1, "api_pull": 1, "redirect": 1,
        "status": "idle", "last_run": 0, "files": 9, "created_at": 1,
    })
    out = _notifier(db).handle_command("/ls")
    assert "国漫" in out and "9 个文件" in out


def test_command_run_triggers_callback():
    db = _db()
    db.upsert_strm_config({
        "id": "cfg1", "account": "", "name": "电影",
        "remote_cid": "1", "remote_cids": "1", "remote_path": "/电影",
        "remote_paths": "/电影", "local_dir": "/strm/电影", "path_dirs": "",
        "base_url": "", "media_exts": "", "meta_exts": "",
        "download_meta": 1, "event_watch": 1, "api_pull": 1, "redirect": 1,
        "status": "idle", "last_run": 0, "files": 0, "created_at": 1,
    })
    calls: list[tuple[str, str]] = []
    n = _notifier(db)
    n.run_config = lambda cid, mode: calls.append((cid, mode))
    out = n.handle_command("/run 电影 full")
    assert calls == [("cfg1", "full")]
    assert "已提交" in out
    # 不存在的配置要给出可读提示
    assert "没找到配置" in n.handle_command("/run 不存在")
    # 非法模式
    assert "inc / full / rebuild" in n.handle_command("/run 电影 weird")


def test_command_find_searches_index():
    db = _db()
    db.upsert_file({"id": 7, "parent_id": 0, "name": "斗罗大陆 01.mkv",
                    "path": "国漫/斗罗大陆 01.mkv", "strm_path": ""})
    eng = SimpleNamespace(name="", db=db)
    out = _notifier(db, engines=lambda: [eng]).handle_command("/find 斗罗")
    assert "斗罗大陆 01.mkv" in out


class FakeLink:
    def __init__(self) -> None:
        self.cur = {"local_to_remote": False, "remote_to_local": True,
                    "scrape_clean": True, "dry_run": False,
                    "grace_seconds": 300, "interval": 120}
        self.saved: list[dict] = []

    def settings(self) -> dict:
        return dict(self.cur)

    def save_settings(self, patch: dict) -> dict:
        self.cur.update(patch)
        self.saved.append(patch)
        return dict(self.cur)

    def pending(self, limit: int = 20) -> list[dict]:
        return [{"name": "a.mkv", "age": 400}]

    def status(self) -> dict:
        return {"settings": self.cur, "last": {"at": 1, "deleted": 2}, "pending": 1}


def test_command_link_and_pending():
    n = _notifier()
    n.linker = FakeLink()
    assert "关" in n.handle_command("/link")
    out = n.handle_command("/link on")
    assert n.linker.cur["local_to_remote"] is True
    assert "a.mkv" in n.handle_command("/pending")
    assert "待删除" in out or "联动状态" in out


def test_command_notify_toggle():
    n = _notifier()
    assert "开启" in n.handle_command("/notify on")
    assert n.settings()["enabled"] is True
    assert "关闭" in n.handle_command("/notify off")
    assert n.settings()["enabled"] is False


# ------------------------------------------------------------------ 企微回调
def test_wecom_verify_requires_signature():
    n = _notifier()
    n.save_settings({"wecom": {"token": "tok", "aes_key": AES_KEY,
                               "corp_id": "ww123456"}})
    echo = wxcrypt.encrypt_message(AES_KEY, "hello", "ww123456")
    q = {"msg_signature": wxcrypt.signature("tok", "1", "2", echo),
         "timestamp": "1", "nonce": "2", "echostr": echo}
    assert n.wecom_verify(q) == "hello"

    bad = dict(q, msg_signature="deadbeef")
    try:
        n.wecom_verify(bad)
    except ValueError as exc:
        assert "签名" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("签名错误却通过了")


def test_wecom_receive_runs_command():
    n = _notifier()
    n.save_settings({"enabled": True, "wecom": {"token": "tok", "aes_key": AES_KEY,
                                                "corp_id": "ww123456"}})
    inner = ("<xml><FromUserName>zhangsan</FromUserName>"
             "<Content>/help</Content></xml>")
    enc = wxcrypt.encrypt_message(AES_KEY, inner, "ww123456")
    body = f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>".encode()
    q = {"msg_signature": wxcrypt.signature("tok", "1", "2", enc),
         "timestamp": "1", "nonce": "2"}
    assert n.wecom_receive(body, q) == "success"
    assert "/status" in n.last_command or "zhangsan" in n.last_command


def test_wecom_receive_ignores_bad_xml():
    n = _notifier()
    n.save_settings({"wecom": {"token": "tok", "aes_key": AES_KEY}})
    assert n.wecom_receive(b"not-xml", {}) == "success"


def test_tg_allowed_rules():
    tg = {"chat_id": "100", "allow": ""}
    assert Notifier._tg_allowed("100", {"id": 1}, tg) is True
    assert Notifier._tg_allowed("999", {"id": 1}, tg) is False
    tg2 = {"chat_id": "100", "allow": "@Alice, 42"}
    assert Notifier._tg_allowed("999", {"id": 42}, tg2) is True
    assert Notifier._tg_allowed("999", {"username": "alice"}, tg2) is True
    assert Notifier._tg_allowed("999", {"id": 7, "username": "bob"}, tg2) is False


# ------------------------------------------------------------------ Webhook
class _Hook:
    """本地小 HTTP 服务：收 POST 记下来，返回 204。"""

    def __init__(self) -> None:
        import http.server
        import threading

        record = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                record.body = self.rfile.read(length).decode("utf-8")
                record.headers = dict(self.headers)
                record.path = self.path
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args) -> None:
                pass

        self.body = ""
        self.headers: dict[str, str] = {}
        self.path = ""
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/hook"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_webhook_off_by_default_and_roundtrip():
    n = _notifier()
    assert n.settings()["webhook"]["enabled"] is False
    saved = n.save_settings({
        "webhook": {"enabled": True, "url": "http://127.0.0.1:1/hook",
                    "server": "", "headers": "X-A: 1", "template": ""},
    })
    assert saved["webhook"]["enabled"] is True
    assert saved["webhook"]["url"].endswith("/hook")
    assert n.settings()["webhook"]["headers"] == "X-A: 1"


def test_webhook_payload_escapes_and_maps_level():
    n = _notifier()
    conf = {"template": "", "timeout": 8}
    payload = n._webhook_payload(conf, '[错误] 生成失败：路径 "A/B" 不存在',
                                 "run_error", "err")
    assert payload["Level"] == "error"
    assert payload["Event"] == "panshare.run_error"
    assert '"A/B"' in payload["Description"]
    assert payload["Title"].startswith("[错误]")
    assert payload["Server"]["Name"] == "PanShare"
    # 能序列化回 JSON（正文里的引号不会把 payload 拼坏）
    assert json.loads(json.dumps(payload, ensure_ascii=False))["Level"] == "error"


def test_webhook_bad_template_falls_back():
    n = _notifier()
    payload = n._webhook_payload({"template": "{ 不是 json"}, "正文", "run_done", "info")
    assert payload["Title"] == "正文" and payload["Description"] == "正文"


def test_webhook_posts_json_with_headers():
    hook = _Hook()
    try:
        n = _notifier()
        conf = n.settings()["webhook"] | {
            "enabled": True, "url": hook.url, "headers": "X-Trace: abc",
        }
        got = n._send_webhook("PanShare 测试", conf, "run_done", "info")
        assert got["ok"] is True, got
        body = json.loads(hook.body)
        assert body["Description"] == "PanShare 测试"
        assert body["Level"] == "info"
        assert hook.headers.get("X-Trace") == "abc"
    finally:
        hook.close()


def test_webhook_uses_media_server_token():
    hook = _Hook()
    try:
        db = _db()
        db.upsert_media_server({"id": "emby1", "name": "客厅 Emby",
                                "host": hook.url.rsplit("/", 1)[0],
                                "api_key": "k-123", "enabled": True})
        n = _notifier(db)
        conf = n.settings()["webhook"] | {"enabled": True, "url": "", "server": "emby1"}
        got = n._send_webhook("联动删除 1 个文件", conf, "link", "info")
        assert got["ok"] is True, got
        assert hook.path == "/emby/Notifications/Admin", hook.path
        assert hook.headers.get("X-Emby-Token") == "k-123"
    finally:
        hook.close()


def test_webhook_send_only_when_enabled():
    n = _notifier()
    n.save_settings({"enabled": True})
    got = n.send("hello", "run_done")
    assert got["ok"] is False and got.get("skipped") == "没有启用任何渠道"
