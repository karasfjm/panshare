"""天翼云盘（189）：多账号扫码登录 / 目录浏览 / strm 生成 / 302 转发。

能力来自 `cloudpan189-share <https://github.com/xxcheng123/cloudpan189-share>`_
里抽出的最小可跑链路（原脚本 ``cloud189_strm.py``），集成进 PanShare 之后：

* **多账号**：账号存在 ``cloud189_accounts`` 表里，每条生成配置绑定一个账号；
  播放地址里带账号短 id，老 strm（不带 id）按文件索引反查归属；
* **家庭云**：目录树顶层同时列「个人云」和「家庭云」，家庭云的文件走
  ``/open/family/file/...`` 那套接口（取直链必须带 ``familyId``）；
* **自动续期**：登录时把 ``sessionKey`` 一起存下来，token 快到期（或接口
  报 token 失效）时用它静默换一枚新的 —— 天翼没有 refreshToken 接口，
  ``sessionKey`` 就是唯一能续的凭据；
* strm 内容写成 ``<请求头>/p189/play/<账号>/<fileId>``，播放时由本服务实时
  换新鲜直链再 302 —— 直链带时效、且 URL 里挂着账号凭据，绝不能直接落盘；
* 生成模式和「115 STRM」保持同一套语义：增量 / 全量 / 全新。

为什么不用 ``requests``：整条链路只需要 GET + 签名，标准库够用，
少一个依赖就少一处容器里装不上的风险。
"""

from __future__ import annotations

import hashlib
import hmac
import html as html_lib
import json
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib
from pathlib import Path
from typing import Any

from .strm import file_ext, rel_join, safe_component, strm_rel_path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 一、常量（对齐 cloudpan189-interface/client/consts.go）
# ---------------------------------------------------------------------------

API_URL = "https://api.cloud.189.cn"

TV_APP_KEY = "600100885"
TV_APP_SIGNATURE_SECRET = "fe5734c74c2f96a38157f420b32dc995"
TV_VERSION = "6.5.5"
TV_CHANNEL_ID = "home02"
ANDROID_TV = "FAMILY_TV"

USER_AGENT = "EcloudTV/6.5.5 (PJX110; unknown; home02) Android/35"
ACCEPT = "application/json;charset=UTF-8"
DEFAULT_TIMEOUT = 30
RETRY_COUNT = 3

API_LIST_FILES = "/open/file/listFiles.action"
API_GET_FILE_DOWNLOAD_URL = "/open/file/getFileDownloadUrl.action"
API_GET_FILE_INFO = "/open/file/getFileInfo.action"
API_GET_USER_INFO = "/open/user/getUserInfo.action"

# 家庭云：这三条是实测可用的 open 接口（2026-10 验证）
#   列表   /open/family/manage/getFamilyList.action  → familyInfoResp[]
#   目录   /open/family/file/listFiles.action        → 需要 familyId
#   直链   /open/family/file/getFileDownloadUrl.action → 需要 familyId
API_FAMILY_LIST = "/open/family/manage/getFamilyList.action"
API_FAMILY_LIST_FILES = "/open/family/file/listFiles.action"
API_FAMILY_DOWNLOAD_URL = "/open/family/file/getFileDownloadUrl.action"

API_LOGIN_QR_UUID = "/family/manage/getQrCodeUUID.action"
API_LOGIN_QR_RESULT = "/family/manage/qrcodeLoginResult.action"
API_LOGIN_FAMILY_MERGE = "/family/manage/loginFamilyMerge.action"
API_GET_TOKEN_BY_SSKEY = "/open/oauth2/getAccessTokenBySsKey.action"

# 个人云根目录（和 cloudpan189-share 保持一致）
ROOT_FOLDER_ID = "-11"
# 目录选择器里的「家庭云」虚拟节点（个人云根下多出来的那一项）
FAMILY_NODE = "fam"
FAMILY_LABEL = "家庭云"

QR_CONTENT_FORMAT = (
    "https://cloud.189.cn/api/portal/loginUrl.action"
    "?redirectURL=https://cloud.189.cn&uuid=%s"
)

# 播放地址：/p189/play/<账号>/[fam/<家庭id>/]<fileId>[/<名字>]，名字只为可读性
PLAY_PREFIX = "/p189/play/"
PLAY_FAMILY = "fam"

LINK_CACHE_SECONDS = 120.0   # 云盘直链缓存（对齐原实现 shared.ShareCache 2 分钟）
MAX_DEPTH = 100              # 递归上限，防云端数据异常导致栈溢出

# token 剩这么多就提前续（秒）。天翼 accessToken 一般 30 天，提前一天续
# 完全够用，又不会把接口打成流水。
RENEW_AHEAD_SECONDS = 24 * 3600
# 后台巡检间隔（秒）
AUTO_REFRESH_INTERVAL = 1800

DEFAULT_EXTS = (
    ".mp4,.mkv,.ts,.m2ts,.mts,.iso,.avi,.wmv,.flv,.mov,.rmvb,.rm,"
    ".webm,.mpg,.mpeg,.m4v,.3gp,.asf,.vob,.divx,.f4v,.ogv"
)

# token 失效类错误（命中就静默续期再重试一次）
TOKEN_ERR_RE = re.compile(
    r"InvalidAccessToken|AccessTokenExpired|TokenExpired|InvalidToken"
    r"|token.*(expire|invalid)|登录.*(失效|过期)", re.I)


class Cloud189Error(Exception):
    """接口返回的业务错误（对应 Go 端的 RespErr）。"""


# ---------------------------------------------------------------------------
# 二、HTTP 基础
# ---------------------------------------------------------------------------

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE  # 对齐 Go 端 InsecureSkipVerify


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随，用来读出云盘直链的真实 Location。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_SSL_CTX))
_OPENER_NO_REDIRECT = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_SSL_CTX), _NoRedirect
)


def _http_get(url: str, headers: dict | None = None, *, raw_opener: bool = False) -> bytes:
    """GET，带重试；返回原始 body（业务错误放在 body 里，交给上层解析）。"""
    last_err: Exception | None = None
    opener = _OPENER_NO_REDIRECT if raw_opener else _OPENER
    for attempt in range(RETRY_COUNT):
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        try:
            with opener.open(req, timeout=DEFAULT_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            # 3xx 在 _NoRedirect 下会抛成 HTTPError，调用方要读 Location
            if 300 <= exc.code < 400:
                raise
            body = b""
            try:
                body = exc.read()
            except Exception:  # noqa: BLE001
                pass
            if body.lstrip()[:1] in (b"{", b"["):
                return body
            last_err = exc
        except Exception as exc:  # noqa: BLE001 网络类异常统一重试
            last_err = exc
        if attempt + 1 < RETRY_COUNT:
            time.sleep(0.6 * (attempt + 1))
    raise Cloud189Error(f"请求失败: {url} ({last_err})")


def _parse_body(body: bytes) -> dict:
    """接口可能返回 JSON，也可能返回 XML 错误体，统一成 dict。"""
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return {}
    if text.startswith("<"):
        out: dict = {}
        for tag in ("code", "message", "msg", "res_code", "res_message"):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
            if m:
                out[tag] = m.group(1).strip()
        return out
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Cloud189Error(f"响应解析失败: {text[:200]}") from exc
    return data if isinstance(data, dict) else {"data": data}


def _first_str(data: dict, *keys: str) -> str:
    """按顺序取第一个非空字符串字段（不同接口字段名不一致，别死认一个）。"""
    for key in keys:
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val:
            return str(val)
    return ""


def _qr_pending(msg: str) -> bool:
    """这个错误是不是「还没扫码 / 还没确认」—— 是的话继续等就行。"""
    if not msg:
        return True
    return bool(re.search(r"QrCodeRollLoginFail|未扫码|未确认", msg, re.I))


def _resp_error(data: dict) -> str | None:
    """从响应里认出错误信息；没有错误返回 None。"""
    rc = data.get("res_code")
    if isinstance(rc, (int, float)) and rc != 0:
        return f"res_code={rc} res_message={data.get('res_message', '')}"
    if isinstance(rc, str) and rc != "":
        return f"res_code={rc} res_message={data.get('res_message', '')}"
    code = data.get("code")
    if code and code != "SUCCESS":
        return f"code={code} msg={data.get('msg') or data.get('message') or ''}"
    for key in ("errorCode", "error"):
        if data.get(key):
            return f"{key}={data[key]} msg={data.get('errorMsg') or data.get('message', '')}"
    return None


def client_suffix() -> dict[str, str]:
    """client/lib.go: clientSuffix()"""
    return {
        "clientType": ANDROID_TV,
        "version": TV_VERSION,
        "channelId": TV_CHANNEL_ID,
        "clientSn": "unknown",
        "model": "PJX110",
        "osFamily": "Android",
        "osVersion": "35",
        "networkAccessMode": "WIFI",
        "telecomsOperator": "46011",
    }


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def now_ms() -> int:
    return int(time.time() * 1000)


def sign_query(params: dict[str, str], access_token: str | None) -> tuple[str, dict]:
    """业务接口签名：AccessToken/Timestamp/AppKey 混进 query，按键名排序拼串后 MD5。"""
    values: dict[str, list[str]] = {k: [str(v)] for k, v in params.items()}
    ts = str(now_ms())
    if access_token is not None:
        values["AccessToken"] = [access_token]
    values["Timestamp"] = [ts]
    values["AppKey"] = [TV_APP_KEY]

    raw = "&".join(f"{k}={v}" for k in sorted(values) for v in sorted(values[k]))
    headers = {
        "Signature": md5_hex(raw),
        "Sign-Type": "1",
        "Timestamp": ts,
        "AppKey": TV_APP_KEY,
    }
    if access_token is not None:
        headers["AccessToken"] = access_token
    query = urllib.parse.urlencode(
        [(k, v) for k in sorted(values) for v in sorted(values[k])],
        quote_via=urllib.parse.quote,
    )
    return query, headers


def app_key_signature_header(full_url: str, method: str) -> dict[str, str]:
    """登录类接口用的 HMAC-SHA1 签名。"""
    ts = now_ms()
    path_match = re.search(r"://[^/]+((/[^/\s?#]+)*)", full_url)
    request_uri = path_match.group(1) if path_match else full_url
    data_str = (
        f"AppKey={TV_APP_KEY}&Operate={method.upper()}"
        f"&RequestURI={request_uri}&Timestamp={ts}"
    )
    mac = hmac.new(
        TV_APP_SIGNATURE_SECRET.encode(), data_str.encode(), hashlib.sha1
    ).hexdigest().upper()
    return {
        "Timestamp": str(ts),
        "X-Request-ID": str(uuidlib.uuid4()),
        "AppKey": TV_APP_KEY,
        "AppSignature": mac,
    }


def signature_header_c(params: dict[str, str], token_type: str,
                       token: str) -> tuple[str, dict]:
    """换 accessToken 时用：token 以自定义键名参与签名。"""
    values: dict[str, list[str]] = {k: [str(v)] for k, v in params.items()}
    ts = str(now_ms())
    values[token_type] = [token]
    values["Timestamp"] = [ts]
    values["AppKey"] = [TV_APP_KEY]

    raw = "&".join(f"{k}={v}" for k in sorted(values) for v in sorted(values[k]))
    headers = {
        "Signature": md5_hex(raw),
        "Sign-Type": "1",
        "Timestamp": ts,
        "AppKey": TV_APP_KEY,
        token_type: token,
    }
    query = urllib.parse.urlencode(
        [(k, v) for k in sorted(values) for v in sorted(values[k])],
        quote_via=urllib.parse.quote,
    )
    return query, headers


# ---------------------------------------------------------------------------
# 三、二维码
# ---------------------------------------------------------------------------


def qr_content(raw: str) -> str:
    """二维码里要编的字符串：新接口直接返回完整 URL，不能再套模板。"""
    if raw.lower().startswith("http"):
        return raw
    return QR_CONTENT_FORMAT % raw


def qr_poll_uuid(raw: str) -> str:
    """轮询时回传给 qrcodeLoginResult 的 uuid（新接口要从 URL 里取 new_uuid）。"""
    if not raw.lower().startswith("http"):
        return raw
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(raw).query)
    paras = (query.get("paras") or [""])[0]
    if paras.startswith("new_uuid="):
        return paras[len("new_uuid="):]
    return raw


def parse_session_key(raw: str) -> str:
    """从天翼网页版的 cookie 里抠出 SESSION_KEY。

    支持三种粘法：
    - 整条 ``Cookie: a=1; COOKIE_LOGIN_USER=xxx``
    - 只粘 ``COOKIE_LOGIN_USER`` 的值（urlencode 过的 ``...&SESSION_KEY=yyy``）
    - 直接粘 SESSION_KEY 本身
    """
    text = str(raw or "").strip()
    if not text:
        raise Cloud189Error("cookie / SESSION_KEY 不能为空")
    text = re.sub(r"^\s*cookie\s*:\s*", "", text, flags=re.I).strip()

    value = text
    for part in text.split(";"):
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        if key.strip().upper() == "COOKIE_LOGIN_USER":
            value = val.strip()
            break

    for candidate in (urllib.parse.unquote(value), value):
        m = re.search(r"SESSION_KEY=([^&;\s'\"]+)", candidate)
        if m:
            return m.group(1).strip()

    # 直接就是 sessionKey（天翼的一串大写字母数字，通常 40+ 位）
    plain = urllib.parse.unquote(value).strip().strip("'\"")
    if re.fullmatch(r"[A-Za-z0-9._\-]{28,}", plain):
        return plain
    raise Cloud189Error("没能从里面找到 SESSION_KEY：请复制网页版 cookies 里的 "
                        "COOKIE_LOGIN_USER（或它里面的 SESSION_KEY）")


# ---------------------------------------------------------------------------
# 三、目录引用（个人云 / 家庭云）
# ---------------------------------------------------------------------------

def parse_remote(remote_id: str) -> tuple[str, str, str]:
    """把目录引用拆成 ``(scope, family_id, folder_id)``。

    目录引用写在配置的 ``remote_id`` 里，只有字符串一列，所以用前缀区分：

    ==================  ============  =========  =====================
    取值                scope         family_id  folder_id
    ==================  ============  =========  =====================
    ``""`` / ``-11``    personal      ""         -11（个人云根）
    ``313079017``       personal      ""         313079017
    ``fam``             family_list   ""         ""（整个家庭云，逐个家庭走）
    ``fam:579739066``   family        579739066 ""（家庭云根）
    ``fam:579:folder``  family        579        folder
    ==================  ============  =========  =====================
    """
    raw = str(remote_id or "").strip()
    if not raw:
        return "personal", "", ROOT_FOLDER_ID
    if raw == FAMILY_NODE:
        return "family_list", "", ""
    if raw.startswith(FAMILY_NODE + ":"):
        parts = raw.split(":")
        family_id = parts[1].strip() if len(parts) > 1 else ""
        folder_id = parts[2].strip() if len(parts) > 2 else ""
        if not family_id:
            return "family_list", "", ""
        return "family", family_id, folder_id
    return "personal", "", raw


def build_remote(scope: str, family_id: str = "", folder_id: str = "") -> str:
    """``parse_remote`` 的逆运算（生成播放地址 / 拼目录 id 用）。"""
    if scope == "family_list":
        return FAMILY_NODE
    if scope == "family":
        if not family_id:
            return FAMILY_NODE
        return f"{FAMILY_NODE}:{family_id}:{folder_id}" if folder_id else \
            f"{FAMILY_NODE}:{family_id}"
    return folder_id or ROOT_FOLDER_ID


def _unhtml(url: str) -> str:
    """天翼直链里的 ``&`` 会被转义成 ``&amp;``，不还原就是 403。"""
    return html_lib.unescape(str(url or "")).replace("&amp;", "&")


def _folder_entry(item: dict, *, family_id: str = "") -> dict:
    return {
        "id": str(item.get("id")),
        "name": item.get("name") or "",
        "size": int(item.get("size") or 0),
        "is_dir": True,
        "family_id": family_id,
        "file_count": int(item.get("fileCount") or 0),
    }


# (块数, 每块总码字, 每块数据码字) × N，顺序 L/M/Q/H
_RS_BLOCK_TABLE = (
    ((1, 26, 19), (1, 26, 16), (1, 26, 13), (1, 26, 9)),
    ((1, 44, 34), (1, 44, 28), (1, 44, 22), (1, 44, 16)),
    ((1, 70, 55), (1, 70, 44), (2, 35, 17), (2, 35, 13)),
    ((1, 100, 80), (2, 50, 32), (2, 50, 24), (4, 25, 9)),
    ((1, 134, 108), (2, 67, 43), (2, 33, 15, 2, 34, 16), (2, 33, 11, 2, 34, 12)),
    ((2, 86, 68), (4, 43, 27), (4, 43, 19), (4, 43, 15)),
    ((2, 98, 78), (4, 49, 31), (2, 32, 14, 4, 33, 15), (4, 39, 13, 1, 40, 14)),
    ((2, 121, 97), (2, 60, 38, 2, 61, 39), (4, 40, 18, 2, 41, 19),
     (4, 40, 14, 2, 41, 15)),
    ((2, 146, 116), (3, 58, 36, 2, 59, 37), (4, 36, 16, 4, 37, 17),
     (4, 36, 12, 4, 37, 13)),
    ((2, 86, 68, 2, 87, 69), (4, 69, 43, 1, 70, 44), (6, 43, 19, 2, 44, 20),
     (6, 43, 15, 2, 44, 16)),
    ((4, 101, 81), (1, 80, 50, 4, 81, 51), (4, 50, 22, 4, 51, 23),
     (3, 36, 12, 8, 37, 13)),
    ((2, 116, 92, 2, 117, 93), (6, 58, 36, 2, 59, 37), (4, 46, 20, 6, 47, 21),
     (7, 42, 14, 4, 43, 15)),
    ((4, 133, 107), (8, 59, 37, 1, 60, 38), (8, 44, 20, 4, 45, 21),
     (12, 33, 11, 4, 34, 12)),
    ((3, 145, 115, 1, 146, 116), (4, 64, 40, 5, 65, 41), (11, 36, 16, 5, 37, 17),
     (11, 36, 12, 5, 37, 13)),
    ((5, 109, 87, 1, 110, 88), (5, 65, 41, 5, 66, 42), (5, 54, 24, 7, 55, 25),
     (11, 36, 12, 7, 37, 13)),
)

_ALIGN_POS = (
    [],
    [6, 18], [6, 22], [6, 26], [6, 30], [6, 34],
    [6, 22, 38], [6, 24, 42], [6, 26, 46], [6, 28, 50], [6, 30, 54],
    [6, 32, 58], [6, 34, 62], [6, 26, 46, 66], [6, 26, 48, 70],
)

_GF_EXP = [0] * 512
_GF_LOG = [0] * 256
_x = 1
for _i in range(255):
    _GF_EXP[_i] = _x
    _GF_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _rs_ecc(data: list[int], nsym: int) -> list[int]:
    gen = [1]
    for i in range(nsym):
        factor = _GF_EXP[i]
        nxt = [0] * (len(gen) + 1)
        for j, coef in enumerate(gen):
            nxt[j] ^= coef
            nxt[j + 1] ^= _gf_mul(coef, factor)
        gen = nxt
    res = list(data) + [0] * nsym
    for i in range(len(data)):
        coef = res[i]
        if coef:
            for j, g in enumerate(gen):
                res[i + j] ^= _gf_mul(g, coef)
    return res[len(data):]


def _bch_type_info(data: int) -> int:
    d = data << 10
    while d.bit_length() - 11 >= 0:  # 除以生成多项式 G15 = 0x537
        d ^= 0x537 << (d.bit_length() - 11)
    return ((data << 10) | d) ^ 0x5412


def _bch_version_info(version: int) -> int:
    d = version << 12
    while d.bit_length() - 13 >= 0:  # G18 = 0x1F25
        d ^= 0x1F25 << (d.bit_length() - 13)
    return (version << 12) | d


def _ec_blocks(version: int, ec_index: int) -> list[tuple[int, int]]:
    spec = _RS_BLOCK_TABLE[version - 1][ec_index]
    blocks: list[tuple[int, int]] = []
    for i in range(0, len(spec), 3):
        cnt, total, dcw = spec[i], spec[i + 1], spec[i + 2]
        blocks.extend([(total, dcw)] * cnt)
    return blocks


def qr_matrix(text: str, ec: str = "M") -> list[list[bool]]:
    """生成二维码矩阵（True = 深色）。byte 模式 + 固定 mask 0。"""
    payload = text.encode("utf-8")
    ec_index = "LMQH".index(ec)
    ec_format = (1, 0, 3, 2)[ec_index]  # 格式信息：L=01 M=00 Q=11 H=10

    version = 0
    for v in range(1, len(_RS_BLOCK_TABLE) + 1):
        cap = sum(b[1] for b in _ec_blocks(v, ec_index)) * 8
        count_bits = 8 if v < 10 else 16
        if 4 + count_bits + len(payload) * 8 <= cap:
            version = v
            break
    if version == 0:
        raise ValueError("二维码内容过长")

    blocks = _ec_blocks(version, ec_index)
    data_cw = sum(b[1] for b in blocks)
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)  # byte 模式
    put(len(payload), 8 if version < 10 else 16)
    for byte in payload:
        put(byte, 8)

    capacity_bits = data_cw * 8
    put(0, min(4, capacity_bits - len(bits)))
    while len(bits) % 8:
        bits.append(0)

    codewords = [
        int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)
    ]
    pad_idx = 0
    while len(codewords) < data_cw:
        codewords.append(0xEC if pad_idx % 2 == 0 else 0x11)
        pad_idx += 1

    data_blocks, ecc_blocks = [], []
    offset = 0
    for total, dcw in blocks:
        chunk = codewords[offset:offset + dcw]
        offset += dcw
        data_blocks.append(chunk)
        ecc_blocks.append(_rs_ecc(chunk, total - dcw))

    interleaved: list[int] = []
    for i in range(max(len(b) for b in data_blocks)):
        for block in data_blocks:
            if i < len(block):
                interleaved.append(block[i])
    for i in range(max(len(b) for b in ecc_blocks)):
        for block in ecc_blocks:
            if i < len(block):
                interleaved.append(block[i])

    size = version * 4 + 17
    matrix = [[False] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def set_module(row: int, col: int, dark: bool) -> None:
        if 0 <= row < size and 0 <= col < size:
            matrix[row][col] = dark
            fixed[row][col] = True

    def draw_finder(top: int, left: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                row, col = top + r, left + c
                if not (0 <= row < size and 0 <= col < size):
                    continue
                inside = 0 <= r <= 6 and 0 <= c <= 6
                dark = inside and (
                    r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4))
                set_module(row, col, dark)

    draw_finder(0, 0)
    draw_finder(0, size - 7)
    draw_finder(size - 7, 0)

    for i in range(8, size - 8):
        dark = i % 2 == 0
        set_module(6, i, dark)
        set_module(i, 6, dark)

    centers = _ALIGN_POS[version - 1]
    for r in centers:
        for c in centers:
            if (r, c) in ((6, 6), (6, size - 7), (size - 7, 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    dark = max(abs(dr), abs(dc)) != 1
                    set_module(r + dr, c + dc, dark)

    for i in range(9):
        if i != 6:
            set_module(8, i, False)
            set_module(i, 8, False)
    for i in range(8):
        set_module(8, size - 1 - i, False)
        set_module(size - 1 - i, 8, False)
    set_module(size - 8, 8, True)

    if version >= 7:
        vbits = _bch_version_info(version)
        for i in range(18):
            dark = (vbits >> i) & 1 == 1
            set_module(size - 11 + i % 3, i // 3, dark)
            set_module(i // 3, size - 11 + i % 3, dark)

    mask_bit = lambda r, c: (r + c) % 2 == 0  # noqa: E731  mask 0
    idx = 0
    total_bits = len(interleaved) * 8
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if fixed[row][c]:
                    continue
                dark = False
                if idx < total_bits:
                    dark = (interleaved[idx // 8] >> (7 - idx % 8)) & 1 == 1
                    idx += 1
                matrix[row][c] = dark ^ mask_bit(row, c)
        upward = not upward
        col -= 2

    fmt = _bch_type_info((ec_format << 3) | 0)
    for i in range(15):
        dark = (fmt >> i) & 1 == 1
        if i < 6:
            matrix[i][8] = dark
        elif i < 8:
            matrix[i + 1][8] = dark
        else:
            matrix[size - 15 + i][8] = dark

        if i < 8:
            matrix[8][size - i - 1] = dark
        elif i < 9:
            matrix[8][15 - i] = dark
        else:
            matrix[8][14 - i] = dark

    return matrix


def qr_svg_rows(text: str) -> list[str]:
    """把二维码矩阵压成「每行一串 0/1」，前端按它画 SVG（不用引第三方库）。"""
    try:
        matrix = qr_matrix(text)
    except Exception as exc:  # noqa: BLE001 画不出来也不该挡住登录
        log.warning("二维码生成失败: %s", exc)
        return []
    return ["".join("1" if cell else "0" for cell in row) for row in matrix]


# ---------------------------------------------------------------------------
# 四、客户端
# ---------------------------------------------------------------------------


class Cloud189Client:
    def __init__(self, access_token: str = "", expires_at: int = 0,
                 refresh_cb=None):
        self.access_token = access_token
        self.expires_at = expires_at  # 毫秒时间戳
        # 接口回「token 失效」时回调它换一枚新的（由 Manager 提供，见 _get）
        self.refresh_cb = refresh_cb

    # ---- 登录 -------------------------------------------------------------
    @staticmethod
    def login_init() -> str:
        """扫码第一步：拿二维码 uuid。"""
        url = API_URL + API_LOGIN_QR_UUID
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": ACCEPT,
            **app_key_signature_header(url, "GET"),
        }
        query = urllib.parse.urlencode(client_suffix())
        data = _parse_body(_http_get(f"{url}?{query}", headers))
        err = _resp_error(data)
        if err:
            raise Cloud189Error(f"获取二维码失败: {err}")
        qr_uuid = data.get("uuid")
        if not qr_uuid:
            raise Cloud189Error("获取二维码失败: 响应中没有 uuid")
        return str(qr_uuid)

    @staticmethod
    def _call(step: str, url: str, query: str, headers: dict,
              trace: list | None) -> dict:
        """发一次请求并把原始响应记进 trace（排查扫码问题全靠它）。"""
        try:
            raw = _http_get(f"{url}?{query}", headers)
            text = raw.decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 记下来再抛，否则前端只能看到「无反应」
            if trace is not None:
                trace.append({"step": step, "error": f"{type(exc).__name__}: {exc}",
                              "url": f"{url}?{query}"[:300]})
            raise
        text = text.strip()
        if trace is not None:
            trace.append({
                "step": step,
                "len": len(text),
                "ctype": "html" if text[:1] == "<" else "json",
                "body": text[:400],
            })
        return _parse_body(raw)

    @staticmethod
    def login_query(qr_uuid: str, trace: list | None = None) -> tuple[str, int, str]:
        """扫码第二步：uuid → e189AccessToken → sessionKey → accessToken。

        uuid 有两种口径：服务端下发的原值（通常是整条 URL），和从 URL 里拆出来的
        ``new_uuid``。社区实现是把原值原样回传，原脚本却拆了 —— 到底哪个对没验证过，
        所以这里两条都试一次，谁先给我 token 就用谁。

        ``trace`` 传一个 list 进来，会把每一步的原始响应塞进去，供页面/接口排查。

        返回值是 ``(accessToken, expiresIn, sessionKey)`` —— sessionKey 一定要
        一起带出去并存下来：天翼没有 refreshToken 接口，token 到期后**只能**用它
        再换一枚新的，丢了就只能重新扫码。
        """
        url = API_URL + API_LOGIN_QR_RESULT
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": ACCEPT,
            **app_key_signature_header(url, "GET"),
        }
        e189_token = ""
        last_err = "未扫码或未确认"
        seen: list[str] = []
        for cand in (qr_uuid, qr_poll_uuid(qr_uuid)):
            if not cand or cand in seen:
                continue
            seen.append(cand)
            query = urllib.parse.urlencode({**client_suffix(), "uuid": cand})
            data = Cloud189Client._call(
                f"qrcodeLoginResult(uuid={cand[-16:]})", url, query, headers, trace)
            e189_token = _first_str(
                data, "accessToken", "access_token", "e189AccessToken", "token")
            if e189_token:
                break
            err = _resp_error(data)
            if err and not _qr_pending(err):
                raise Cloud189Error(err)
            jump = _first_str(data, "toUrl", "redirectUrl", "url", "location")
            if jump:
                raise Cloud189Error(
                    f"扫码已确认但返回的是跳转地址（需要 ticket 换 token）: {jump[:200]}")
            last_err = err or last_err
        if not e189_token:
            raise Cloud189Error(last_err)

        url = API_URL + API_LOGIN_FAMILY_MERGE
        query = urllib.parse.urlencode(
            {**client_suffix(), "e189AccessToken": e189_token})
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": ACCEPT,
            **app_key_signature_header(url, "GET"),
        }
        data = Cloud189Client._call("loginFamilyMerge", url, query, headers, trace)
        session_key = _first_str(data, "sessionKey", "session_key")
        if not session_key:
            err = _resp_error(data)
            raise Cloud189Error(f"换取 sessionKey 失败: {err or '响应中没有 sessionKey'}")

        token, expires = Cloud189Client.token_by_sessions_key(session_key, trace)
        return token, expires, session_key

    @staticmethod
    def token_by_sessions_key(session_key: str,
                              trace: list | None = None) -> tuple[str, int]:
        """sessionKey → accessToken（Cookie 登录和扫码登录共用这一步）。"""
        session_key = str(session_key or "").strip()
        if not session_key:
            raise Cloud189Error("sessionKey 为空")
        url = API_URL + API_GET_TOKEN_BY_SSKEY
        query, headers = signature_header_c({}, "sessionKey", session_key)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": ACCEPT,
            "Content-Type": "application/json",
            **headers,
        }
        data = Cloud189Client._call("getAccessTokenBySsKey", url, query, headers, trace)
        token_val = _first_str(data, "accessToken", "access_token", "token")
        if not token_val:
            err = _resp_error(data)
            raise Cloud189Error(f"换取 accessToken 失败: {err or '响应中没有 accessToken'}")
        exp = data.get("expiresIn") or data.get("expires_in") or 0
        return str(token_val), int(exp or 0)

    # ---- 通用请求 ---------------------------------------------------------
    def _get(self, api: str, params: dict[str, str]) -> dict:
        if not self.access_token:
            raise Cloud189Error("尚未登录天翼云盘，请先在页面上扫码登录")
        data = self._get_once(api, params)
        err = _resp_error(data)
        if err and TOKEN_ERR_RE.search(err) and self.refresh_cb is not None:
            # token 被服务端提前作废（换了设备 / 被踢下线 / 到期）：静默续一次再重试，
            # 续期失败就把原错误抛出去，别让用户看到「续期失败」盖住真实原因。
            try:
                token = self.refresh_cb()
            except Cloud189Error as exc:
                raise Cloud189Error(f"{api} 调用失败: {err}（自动续期也失败：{exc}）") \
                    from exc
            if token:
                self.access_token = token
                data = self._get_once(api, params)
                err = _resp_error(data)
        if err:
            raise Cloud189Error(f"{api} 调用失败: {err}")
        return data

    def _get_once(self, api: str, params: dict[str, str]) -> dict:
        url = API_URL + api
        query, headers = sign_query(params, self.access_token)
        headers = {"User-Agent": USER_AGENT, "Accept": ACCEPT, **headers}
        return _parse_body(_http_get(f"{url}?{query}", headers))

    # ---- 业务接口 ---------------------------------------------------------
    def user_info(self) -> dict:
        return self._get(API_GET_USER_INFO, {})

    def list_families(self) -> list[dict]:
        """家庭云列表（一个账号可能加入多个家庭）。"""
        data = self._get(API_FAMILY_LIST, {})
        items = data.get("familyInfoResp") or data.get("familyInfoList") or []
        out: list[dict] = []
        for item in items if isinstance(items, list) else []:
            fid = str(item.get("familyId") or "").strip()
            if not fid:
                continue
            out.append({
                "family_id": fid,
                "name": str(item.get("remarkName") or item.get("familyName")
                            or f"家庭云 {fid}"),
                "count": int(item.get("count") or 0),
                "user_role": item.get("userRole"),
            })
        return out

    def list_files(self, folder_id: str, family_id: str = "",
                   page_size: int = 100) -> list[dict]:
        """列目录，文件夹在前；自动翻页。

        ``family_id`` 非空时走家庭云接口（家庭云根目录习惯上不传 folderId，
        传了 ``-11`` 会列出「同步盘 / 我的图片」这类系统目录）。
        """
        page, items = 1, []
        while True:
            params = {
                "pageNum": str(page),
                "pageSize": str(page_size),
                "mediaType": "0",
                "folderId": str(folder_id),
                "iconOption": "5",
                "orderBy": "lastOpTime",
                "descending": "true",
            }
            if family_id:
                params["familyId"] = str(family_id)
                if not folder_id:
                    params.pop("folderId")
                api = API_FAMILY_LIST_FILES
            else:
                api = API_LIST_FILES
            data = self._get(api, params)
            ao = data.get("fileListAO") or {}
            total = int(ao.get("count") or 0)
            for folder in ao.get("folderList") or []:
                items.append(_folder_entry(folder, family_id=family_id))
            for item in ao.get("fileList") or []:
                items.append({
                    "id": str(item.get("id")),
                    "name": item.get("name") or "",
                    "size": int(item.get("size") or 0),
                    "is_dir": False,
                    "family_id": family_id,
                })
            if len(items) >= total or not (ao.get("fileList") or ao.get("folderList")):
                break
            page += 1
            if page > 1000:  # 防御
                break
        return items

    def file_info(self, file_id: str, family_id: str = "") -> dict:
        if family_id:
            # 家庭云没有单独的文件信息接口，列父目录找一遍太贵，直链优先
            return {"id": str(file_id), "familyId": str(family_id)}
        return self._get(API_GET_FILE_INFO, {"fileId": str(file_id)})

    def raw_download_url(self, file_id: str, family_id: str = "") -> str:
        if family_id:
            data = self._get(API_FAMILY_DOWNLOAD_URL,
                             {"fileId": str(file_id), "familyId": str(family_id)})
        else:
            data = self._get(
                API_GET_FILE_DOWNLOAD_URL, {"fileId": str(file_id), "dt": "1"})
        url = data.get("fileDownloadUrl")
        if not url:
            raise Cloud189Error(f"文件 {file_id} 没有下载地址")
        return _unhtml(str(url))

    def download_url(self, file_id: str, family_id: str = "") -> str:
        """跟一次 302 拿 CDN 真实直链（自带跳转域名会泄露 token）。"""
        raw = self.raw_download_url(file_id, family_id)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
            )
        }
        try:
            _http_get(raw, headers, raw_opener=True)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                location = exc.headers.get("Location")
                if location:
                    return str(location)
        except Exception:  # noqa: BLE001 解析失败就退回原链
            pass
        return raw


# ---------------------------------------------------------------------------
# 五、管理器：token / 登录 / 目录 / 生成 / 直链
# ---------------------------------------------------------------------------


def _expires_to_ms(raw: Any) -> int:
    """``expiresIn`` 在不同接口里有三种口径：时长秒数 / 秒级时间戳 / 毫秒时间戳。

    原脚本直接把它当毫秒存（``expiresAt``），小数值时会算出 1970 年，
    页面就会一直提示「已过期」。这里按量级判断，三种都能落对。
    """
    try:
        value = int(float(raw or 0))
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    if value > 10**11:      # 已经是毫秒时间戳
        return value
    if value > 10**9:       # 秒级时间戳
        return value * 1000
    return now_ms() + value * 1000   # 有效期（秒）


def split_exts(raw: str) -> set[str]:
    """``mp4,mkv`` / ``.mp4,.mkv`` / ``mp4 mkv`` → ``{'.mp4','.mkv'}``"""
    out: set[str] = set()
    for part in str(raw or "").replace(";", ",").replace("，", ",").split(","):
        for token in part.split():
            token = token.strip().lower()
            if not token:
                continue
            out.add(token if token.startswith(".") else "." + token)
    return out


def _account_label(row: dict | None) -> str:
    """账号显示名：昵称 → 登录名 → id。"""
    if not row:
        return ""
    return str(row.get("name") or row.get("login_name") or row.get("id") or "")


def _fmt_ms(ms: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000)) if ms else ""


class Cloud189Manager:
    """天翼云盘的登录、目录浏览、strm 生成与直链解析。

    * 账号存在 ``cloud189_accounts`` 表，一条生成配置绑定一个账号；
    * token 到期靠 ``sessionKey`` 静默续期（天翼没有 refreshToken 接口）；
    * 与 115 的 Engine 完全独立，唯一共享的是 ``StrmStore``（同一个 STRM_ROOT）。
    """

    META_TOKEN = "cloud189_token"       # 老版本：单人份 token（启动时迁移）
    META_ACCOUNT = "cloud189_account"   # 老版本：昵称

    def __init__(self, cfg, db, store) -> None:
        self.cfg = cfg
        self.db = db
        self.store = store
        self._lock = threading.RLock()
        self._link_cache: dict[str, tuple[str, float]] = {}
        self._running: set[str] = set()
        self._qr_log: list[dict] = []   # 扫码轮询的原始往来（排查用）
        self._stop = threading.Event()
        self._auto_thread: threading.Thread | None = None
        self._migrate_legacy()

    # ------------------------------------------------------------- 迁移
    def _migrate_legacy(self) -> None:
        """老版本只有一份 token 存在 meta 里 → 转成一条账号记录。"""
        if self.db.list_cloud189_accounts():
            return
        raw = self.db.get_meta(self.META_TOKEN, "")
        if not raw:
            return
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        token = str(data.get("accessToken") or "")
        if not token:
            return
        aid = "c189main"
        self.db.upsert_cloud189_account({
            "id": aid,
            "name": self.db.get_meta(self.META_ACCOUNT, ""),
            "access_token": token,
            "expires_at": int(data.get("expiresAt") or 0),
            "login_at": int(data.get("loginAt") or 0),
            "source": str(data.get("source") or "qrcode"),
            "is_default": 1,
        })
        for row in self.db.list_cloud189_configs():
            if not row.get("account"):
                self.db.update_cloud189_config(row["id"], account=aid)
        self.db.set_meta(self.META_TOKEN, "")
        log.info("天翼云盘：老的单人份 token 已迁移为账号 %s", aid)

    # ------------------------------------------------------------- 账号
    def accounts(self) -> list[dict]:
        return self.db.list_cloud189_accounts()

    def account(self, account_id: str = "") -> dict | None:
        """按 id 取账号；``account_id`` 为空时返回默认账号。"""
        if account_id:
            return self.db.get_cloud189_account(str(account_id))
        return self.db.default_cloud189_account()

    def is_account(self, account_id: str) -> bool:
        return bool(account_id) and self.db.get_cloud189_account(
            str(account_id)) is not None

    def default_id(self) -> str:
        row = self.account("")
        return str(row["id"]) if row else ""

    def _ensure_default(self) -> None:
        """保证「默认账号」恒有一个。

        删掉默认账号之后剩下的账号都没打标记，页面就没有任何一条带「默认」，
        而新建配置默认选谁就变得看运气 —— 这里把第一个（最早的）补成默认。
        """
        accs = self.accounts()
        if not accs or any(r.get("is_default") for r in accs):
            return
        self.db.set_default_cloud189_account(str(accs[0]["id"]))

    def set_default(self, account_id: str) -> dict:
        row = self.account(account_id)
        if row is None:
            raise Cloud189Error("没有这个天翼云盘账号")
        self.db.set_default_cloud189_account(str(row["id"]))
        self.event("account_default",
                   f"默认账号已切换为：{_account_label(row)}")
        return {"ok": True, "default": str(row["id"])}

    @staticmethod
    def _expired(row: dict | None) -> bool:
        if not row:
            return False
        expires = int(row.get("expires_at") or 0)
        return bool(expires) and expires / 1000 <= time.time()

    def _logged_in(self, row: dict | None) -> bool:
        return bool(row and row.get("access_token") and not self._expired(row))

    def _public(self, row: dict) -> dict:
        expires = int(row.get("expires_at") or 0)
        remain = int(expires / 1000 - time.time()) if expires else 0
        return {
            "id": str(row["id"]),
            "name": row.get("name") or "",
            "login_name": row.get("login_name") or "",
            "display": _account_label(row),
            "expires_at": expires,
            "expires_text": _fmt_ms(expires),
            "remaining_hours": max(0, round(remain / 3600)) if expires else 0,
            "login_at": int(row.get("login_at") or 0),
            "source": row.get("source") or "",
            "is_default": bool(row.get("is_default")),
            "auto_refresh": bool(row.get("auto_refresh")),
            "can_refresh": bool(row.get("session_key")),
            "logged_in": self._logged_in(row),
            "expired": self._expired(row),
            "status": row.get("status") or "ok",
            "last_error": row.get("last_error") or "",
            "last_refresh": int(row.get("last_refresh") or 0),
            "refresh_count": int(row.get("refresh_count") or 0),
            "configs": self.db.count_cloud189_configs(str(row["id"])),
        }

    # ------------------------------------------------------------- 状态
    def status(self) -> dict:
        self._ensure_default()     # 老数据 / 删账号后可能一条默认都没有
        accs = self.accounts()
        default = self.account("")
        expires = int(default.get("expires_at") or 0) if default else 0
        return {
            # 老字段（页面顶部统计、老前端都用这几个）——都按默认账号算
            "logged_in": any(self._logged_in(r) for r in accs),
            "has_token": any(bool(r.get("access_token")) for r in accs),
            "expired": self._expired(default),
            "expires_at": expires,
            "expires_text": _fmt_ms(expires),
            "login_at": int(default.get("login_at") or 0) if default else 0,
            "account": _account_label(default),
            # 多账号
            "default": str(default["id"]) if default else "",
            "accounts": [self._public(r) for r in accs],
            "renew_ahead_hours": RENEW_AHEAD_SECONDS // 3600,
            "auto_renew": True,
            # 其它
            "root": str(self.store.root),
            "configs": self.db.list_cloud189_configs(),
            "files": self.db.cloud189_file_count(),
        }

    # ------------------------------------------------------------- token
    def _save_login(self, account_id: str, token: str, expires_in: Any,
                    session_key: str, source: str) -> dict:
        """把一次登录结果写进账号表（没有就建一条）。返回账号行。

        注意 ``account_id`` 为空表示**新建账号**，不能拿去查默认账号 ——
        否则「添加第二个账号」会把默认账号覆盖掉。
        """
        row = self.account(account_id) if account_id else None
        fields: dict[str, Any] = {
            "access_token": str(token),
            "expires_at": _expires_to_ms(expires_in),
            "login_at": now_ms(),
            "source": source,
            "status": "ok",
            "last_error": "",
        }
        if session_key:
            fields["session_key"] = str(session_key)
        if row is None:
            aid = str(account_id or ("c189" + uuidlib.uuid4().hex[:8]))
            self.db.upsert_cloud189_account(
                {"id": aid, "is_default": 0 if self.accounts() else 1, **fields})
        else:
            aid = str(row["id"])
            self.db.update_cloud189_account(aid, **fields)
        with self._lock:
            self._link_cache.clear()
        self._fill_profile(aid)
        return self.db.get_cloud189_account(aid) or {}

    def _fill_profile(self, account_id: str) -> str:
        """拉一次用户信息，补昵称 / 登录名（失败不影响主流程）。"""
        row = self.account(account_id)
        if row is None:
            return ""
        try:
            info = Cloud189Client(str(row.get("access_token") or ""),
                                  int(row.get("expires_at") or 0)).user_info()
        except Cloud189Error as exc:
            log.debug("读取天翼云盘账号信息失败: %s", exc)
            return _account_label(row)
        name = ""
        for key in ("nickName", "nickname", "userName", "name", "email"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
        login_name = str(info.get("loginName") or info.get("loginname") or "")
        fields: dict[str, Any] = {}
        if name:
            fields["name"] = name
        if login_name:
            fields["login_name"] = login_name
        if fields:
            self.db.update_cloud189_account(str(row["id"]), **fields)
        return _account_label(self.db.get_cloud189_account(str(row["id"])) or row)

    def access_token_for(self, account_id: str = "") -> str:
        row = self.account(account_id)
        return str(row.get("access_token") or "") if row else ""

    def refresh_account(self, account_id: str = "", *, reason: str = "auto") -> dict:
        """用保存下来的 sessionKey 换一枚新 token（天翼唯一的续期方式）。

        天翼**没有** refreshToken 接口（``/open/oauth2/refreshToken.action`` 是 404），
        ``/open/oauth2/getAccessTokenBySsKey.action`` 才是唯一能静默换 token 的入口，
        所以登录时一定要把 sessionKey 存下来。sessionKey 失效（改密码 / 主动退出 /
        网页端登出）就只能重新扫码。
        """
        row = self.account(account_id)
        if row is None:
            raise Cloud189Error("没有这个天翼云盘账号")
        label = _account_label(row)
        session_key = str(row.get("session_key") or "")
        if not session_key:
            raise Cloud189Error(
                "这个账号没有保存会话密钥（sessionKey），无法自动续期。"
                "用「扫码登录」或「Cookie 登录」再登录一次即可打开自动续期")
        trace: list = []
        try:
            token, expires = Cloud189Client.token_by_sessions_key(session_key, trace)
        except Cloud189Error as exc:
            self.db.update_cloud189_account(
                str(row["id"]), status="expired", last_error=str(exc)[:300])
            self.event("refresh_fail", f"token 续期失败：{label} · {exc}",
                       level="warn")
            raise Cloud189Error(str(exc)) from exc
        self.db.update_cloud189_account(
            str(row["id"]),
            access_token=token,
            expires_at=_expires_to_ms(expires),
            status="ok",
            last_error="",
            last_refresh=int(time.time()),
            refresh_count=int(row.get("refresh_count") or 0) + 1,
        )
        with self._lock:
            self._link_cache.clear()
        fresh = self.db.get_cloud189_account(str(row["id"])) or row
        self.event(
            "refresh",
            f"token 已{'手动' if reason == 'manual' else '自动'}续期：{label}"
            f" · 有效期至 {_fmt_ms(int(fresh.get('expires_at') or 0))}",
        )
        log.info("天翼云盘 token 续期成功：%s", label)
        return fresh

    def _ensure_fresh(self, row: dict) -> dict:
        """token 快到期就先续；续不动但还没过期就继续用旧的。"""
        expires = int(row.get("expires_at") or 0)
        if not expires:
            return row                      # 有效期未知（手动填的 token），不猜
        remain = expires / 1000 - time.time()
        if remain > RENEW_AHEAD_SECONDS:
            return row
        expired = remain <= 0
        if not row.get("session_key"):
            if expired:
                raise Cloud189Error(
                    "天翼云盘 token 已过期，且这个账号没有会话密钥、无法自动续期，"
                    "请重新登录")
            return row
        try:
            return self.refresh_account(str(row["id"]), reason="auto")
        except Cloud189Error as exc:
            if expired:
                raise Cloud189Error(
                    f"天翼云盘 token 已过期，自动续期失败：{exc}。请重新登录") from exc
            log.debug("天翼云盘提前续期失败（继续用旧 token）: %s", exc)
            return row

    def client(self, account_id: str = "") -> Cloud189Client:
        row = self.account(account_id)
        if row is None:
            raise Cloud189Error("尚未登录天翼云盘，请先在「天翼云盘」页扫码登录")
        if not row.get("access_token"):
            raise Cloud189Error(
                f"账号「{_account_label(row)}」还没有登录，请重新扫码或填 Cookie")
        row = self._ensure_fresh(row)
        token = str(row.get("access_token") or "")
        if self._expired(row) or not token:
            raise Cloud189Error(
                f"账号「{_account_label(row)}」的 token 已过期，请重新登录")
        return Cloud189Client(
            token, int(row.get("expires_at") or 0),
            refresh_cb=lambda: str(
                self.refresh_account(str(row["id"]), reason="auto")
                .get("access_token") or ""))

    # ------------------------------------------------------------- 自动续期
    def start_auto_refresh(self) -> None:
        """启动后台巡检线程：每天一次把 24 小时内到期的 token 换掉。"""
        with self._lock:
            if self._auto_thread is not None:
                return
            self._stop.clear()
            self._auto_thread = threading.Thread(
                target=self._auto_loop, name="cloud189-renew", daemon=True)
            self._auto_thread.start()

    def stop_auto_refresh(self) -> None:
        self._stop.set()
        with self._lock:
            self._auto_thread = None

    def _auto_loop(self) -> None:
        if self._stop.wait(60):     # 启动后先缓一分钟，别和启动流程抢接口
            return
        while True:
            try:
                self.auto_refresh_tick()
            except Exception as exc:  # noqa: BLE001 巡检线程绝不能死
                log.debug("天翼云盘 token 巡检失败: %s", exc)
            if self._stop.wait(AUTO_REFRESH_INTERVAL):
                return

    def auto_refresh_tick(self) -> list[dict]:
        """检查一遍所有账号，快到期/已过期且有 sessionKey 的就续期。"""
        now = time.time()
        out: list[dict] = []
        for row in self.accounts():
            if not int(row.get("auto_refresh") or 0):
                continue
            expires = int(row.get("expires_at") or 0)
            if not expires:
                continue
            remain = expires / 1000 - now
            if remain > RENEW_AHEAD_SECONDS:
                continue
            label = _account_label(row)
            if not row.get("session_key"):
                if remain <= 0 and (row.get("status") or "") != "expired":
                    self.db.update_cloud189_account(
                        str(row["id"]), status="expired",
                        last_error="token 已过期；没有会话密钥，需重新登录")
                    self.event("refresh_fail",
                               f"token 已过期且没有会话密钥，无法自动续期：{label}",
                               level="warn")
                out.append({"id": str(row["id"]), "ok": False,
                            "message": "没有会话密钥"})
                continue
            try:
                self.refresh_account(str(row["id"]), reason="auto")
                out.append({"id": str(row["id"]), "ok": True})
            except Cloud189Error as exc:
                out.append({"id": str(row["id"]), "ok": False,
                            "message": str(exc)})
        return out

    # ------------------------------------------------------------- 登录
    def login_start(self) -> dict:
        """取二维码：返回 uuid / 内容 / 矩阵（前端画 SVG）。"""
        raw = Cloud189Client.login_init()
        content = qr_content(raw)
        rows = qr_svg_rows(content)
        self.event("login_start", "已生成登录二维码，等待手机确认")
        return {
            "ok": True,
            "uuid": raw,
            "content": content,
            "matrix": rows,
            "size": len(rows),
        }

    def login_poll(self, qr_uuid: str, account_id: str = "") -> dict:
        """轮询扫码结果。waiting / ok / expired / error。

        ``account_id`` 为空表示「新建一个账号」，非空表示给已有账号换 token
        （「重新登录」按钮走的就是这条）。
        """
        if not qr_uuid:
            raise Cloud189Error("缺少二维码标识")
        if account_id and not self.is_account(account_id):
            raise Cloud189Error("要重新登录的账号不存在，刷新一下页面")
        trace: list = []
        try:
            token, expires_in, session_key = Cloud189Client.login_query(qr_uuid, trace)
        except Cloud189Error as exc:
            msg = str(exc)
            self._remember_qr(qr_uuid, msg, trace)
            # 未扫码 / 未确认是正常状态，继续等
            if _qr_pending(msg):
                return {"status": "waiting"}
            if "扫码已确认" in msg:
                # 手机点了确认，但服务端拿不到 accessToken —— 这是真问题，别让页面干等
                self.event("login_fail", msg[:300], level="warn")
                return {"status": "error",
                        "message": "手机已确认，但云盘没返回 accessToken。已记录原始响应，"
                                   "请改用「手动填 token」，或把这个现象告诉我。",
                        "detail": msg[:300]}
            if re.search(r"expire|invalid|超时|失效|过期|not exist", msg, re.I):
                self.event("login_fail", f"二维码已失效：{msg}", level="warn")
                return {"status": "expired", "message": msg}
            self.event("login_fail", msg[:300], level="warn")
            return {"status": "error", "message": msg}
        except Exception as exc:  # noqa: BLE001 网络类异常也要留下证据
            self._remember_qr(qr_uuid, f"{type(exc).__name__}: {exc}", trace)
            raise Cloud189Error(f"轮询失败: {exc}") from exc
        self._remember_qr(qr_uuid, "ok", trace)
        row = self._save_login(account_id, token, expires_in, session_key, "qrcode")
        label = _account_label(row)
        self.event("login", f"天翼云盘登录成功{('：' + label) if label else ''}")
        return {"status": "ok", "account": label, "account_id": str(row.get("id") or ""),
                "expires_text": _fmt_ms(int(row.get("expires_at") or 0))}

    def _remember_qr(self, qr_uuid: str, result: str, trace: list) -> None:
        """最近 40 次轮询的原始往来，供 /api/cloud189/login/debug 查看。"""
        with self._lock:
            self._qr_log.append({
                "at": now_ms(),
                "uuid": qr_poll_uuid(qr_uuid)[-24:],
                "result": result[:200],
                "steps": trace,
            })
            del self._qr_log[:-40]

    def qr_log(self) -> list[dict]:
        with self._lock:
            return list(self._qr_log)

    def set_cookie(self, cookie: str, account_id: str = "") -> dict:
        """用网页版 cookie 里的 SESSION_KEY 换 token（扫码不通时的可靠兜底）。"""
        session_key = parse_session_key(cookie)
        trace: list = []
        try:
            token, expires_in = Cloud189Client.token_by_sessions_key(session_key, trace)
        except Cloud189Error as exc:
            self._remember_qr(f"cookie:{session_key[:8]}", str(exc), trace)
            raise Cloud189Error(f"这个 cookie 换不到 token：{exc}") from exc
        row = self._save_login(account_id, token, expires_in, session_key, "cookie")
        label = _account_label(row)
        self.event("login", f"已通过 Cookie 登录{('：' + label) if label else ''}")
        return {"ok": True, "account": label, "account_id": str(row.get("id") or ""),
                "expires_text": _fmt_ms(int(row.get("expires_at") or 0))}

    def set_token(self, token: str, expires_in: Any = 0,
                  account_id: str = "") -> dict:
        """手动填 accessToken（兜底入口）。

        没有 sessionKey，所以这个账号**不能**自动续期 —— 页面会标出来。
        """
        token = str(token or "").strip()
        if not token:
            raise Cloud189Error("token 不能为空")
        # 先验证一次，错的 token 不要写进库
        probe = Cloud189Client(token, 0)
        try:
            probe.user_info()
        except Cloud189Error as exc:
            raise Cloud189Error(f"这个 token 用不了：{exc}") from exc
        row = self._save_login(account_id, token, expires_in, "", "token")
        label = _account_label(row)
        self.event("login", f"已通过手动填写 token 登录{('：' + label) if label else ''}"
                            "（没有会话密钥，不能自动续期）")
        return {"ok": True, "account": label, "account_id": str(row.get("id") or ""),
                "can_refresh": False}

    def logout(self, account_id: str = "") -> dict:
        """退出登录：清掉 token 与会话，但保留账号记录（名下的配置继续可用）。"""
        row = self.account(account_id)
        if row is None:
            return {"ok": True}
        self.db.update_cloud189_account(
            str(row["id"]), access_token="", session_key="", expires_at=0,
            status="logged_out", last_error="")
        with self._lock:
            self._link_cache.clear()
        self.event("logout", f"已退出天翼云盘账号：{_account_label(row)}")
        return {"ok": True, "id": str(row["id"])}

    def delete_account(self, account_id: str, *, force: bool = False) -> dict:
        """彻底删账号。名下有配置时必须 ``force``（配置一起删，strm 保留）。"""
        row = self.account(account_id)
        if row is None:
            raise Cloud189Error("没有这个天翼云盘账号")
        n = self.db.count_cloud189_configs(str(row["id"]))
        if n and not force:
            raise Cloud189Error(
                f"这个账号下还有 {n} 条生成配置；先改到别的账号或删掉配置，"
                "再删账号（也可以选择「连配置一起删」）")
        removed = self.db.delete_cloud189_account(str(row["id"]), drop_configs=force)
        self._ensure_default()
        self.event("account_delete",
                   f"删除账号：{_account_label(row)}"
                   + (f"（同时删除 {removed} 条配置，strm 保留）" if removed else ""))
        return {"ok": True, "removed_configs": removed}

    def set_auto_refresh(self, account_id: str, enabled: bool) -> dict:
        row = self.account(account_id)
        if row is None:
            raise Cloud189Error("没有这个天翼云盘账号")
        self.db.update_cloud189_account(str(row["id"]),
                                       auto_refresh=1 if enabled else 0)
        self.event("account_update",
                   f"{_account_label(row)} · 自动续期已{'开启' if enabled else '关闭'}")
        return {"ok": True, "auto_refresh": bool(enabled)}

    # ------------------------------------------------------------- 目录
    def list_dirs(self, ref: str = "", account_id: str = "") -> dict:
        """列云端目录（个人云 / 家庭云统一入口）。

        ``ref`` 见 :func:`parse_remote`：个人云根下会多出一个「家庭云」虚拟目录。
        """
        scope, family_id, folder_id = parse_remote(ref)
        client = self.client(account_id)
        if scope == "family_list":
            fams = client.list_families()
            return {
                "scope": "family_list",
                "family_id": "",
                "folder_id": FAMILY_NODE,
                "dirs": [{"id": build_remote("family", f["family_id"]),
                          "name": f["name"], "kind": "family",
                          "file_count": f.get("count") or 0} for f in fams],
                "file_count": 0,
                "hint": ("点进某个家庭只生成那一个；也可以直接「用这个目录」"
                         "把整个家庭云都生成（每个家庭一个同名子目录）"
                         if fams else "这个账号没有加入任何家庭云"),
            }
        items = client.list_files(folder_id, family_id)
        dirs = [
            {"id": build_remote(scope, family_id, str(x["id"])),
             "name": x.get("name") or "", "kind": scope,
             "file_count": x.get("file_count") or 0}
            for x in items if x.get("is_dir")
        ]
        if scope == "personal" and (folder_id or ROOT_FOLDER_ID) == ROOT_FOLDER_ID:
            # 个人云根：把家庭云挂在最上面，用户点进去再选具体的家庭
            dirs.insert(0, {"id": FAMILY_NODE, "name": FAMILY_LABEL,
                            "kind": "family_list", "file_count": 0})
        return {
            "scope": scope,
            "family_id": family_id,
            # 家庭云根目录本来就没有 folderId，别在响应里瞎填 -11
            "folder_id": folder_id or (ROOT_FOLDER_ID if scope == "personal" else ""),
            "dirs": dirs,
            "file_count": len([x for x in items if not x.get("is_dir")]),
            "hint": "",
        }

    # ------------------------------------------------------------- 地址
    def play_url(self, file_id: str, name: str = "", base_url: str = "",
                 account_id: str = "", family_id: str = "") -> str:
        base = (base_url or self.cfg.base_url).rstrip("/")
        url = f"{base}{PLAY_PREFIX}"
        if account_id:
            url += urllib.parse.quote(str(account_id), safe="") + "/"
        if family_id:
            url += f"{PLAY_FAMILY}/{urllib.parse.quote(str(family_id), safe='')}/"
        url += str(file_id)
        if name:
            url += "/" + urllib.parse.quote(name, safe="")
        if self.cfg.auth_token:
            url += "?token=" + urllib.parse.quote(self.cfg.auth_token)
        return url

    def resolve_link(self, file_id: str, account_id: str = "",
                     family_id: str = "") -> str:
        """取云盘直链（带 120s 缓存）：Emby 探测会连续打好几次。

        老 strm（``/p189/play/<fileId>``）里没有账号 / 家庭信息，
        用文件索引反查补出来。
        """
        file_id = str(file_id)
        if not account_id or not family_id:
            row = self.db.find_cloud189_file(file_id)
            if row:
                account_id = account_id or str(row.get("account") or "")
                family_id = family_id or str(row.get("family_id") or "")
        key = f"{account_id}|{family_id}|{file_id}"
        with self._lock:
            hit = self._link_cache.get(key)
            if hit and time.time() - hit[1] < LINK_CACHE_SECONDS:
                return hit[0]
        link = self.client(account_id).download_url(file_id, family_id)
        with self._lock:
            self._link_cache[key] = (link, time.time())
        return link

    def strm_play_url(self, strm_path: str, origin: str = "") -> str:
        """读一个天翼 strm 的内容，把「请求头」换成客户端真正访问得到的地址。

        天翼的 strm 内容是 ``<请求头>/p189/play/<账号>/[fam/<家庭id>/]<fileId>/<名字>``。
        生成时用的请求头是 ``BASE_URL``（容器里往往是 docker 网关
        ``172.17.0.1``），**局域网的播放器根本连不上** —— 115 的 strm 有反代
        兜底改写，天翼以前没有，所以 Emby 里点开就转圈。

        ``origin`` 是调用方（Emby 反代）观察到的客户端地址（例如
        ``http://192.168.1.10:7259``）。换掉 origin 之后，播放器请求的是
        **反代端口**，由反代现取新鲜直链再 302 出去 —— 视频流量仍然直连
        天翼 CDN，不经过 NAS。

        不是天翼 strm（内容里没有 ``/p189/play/``）就返回空串，交给别人处理。
        """
        if not strm_path:
            return ""
        # 只处理 strm：这里面可能是整部电影的路径（几十 GB），read_text 会把它
        # 整个读进内存 —— 必须先按扩展名拦掉，而且只读第一行。
        if not str(strm_path).lower().endswith(".strm"):
            return ""
        try:
            with open(strm_path, encoding="utf-8", errors="ignore") as fh:
                content = fh.readline()
        except OSError as exc:
            log.debug("读天翼 strm 失败 %s: %s", strm_path, exc)
            return ""
        url = content.strip()
        if PLAY_PREFIX not in url:
            return ""
        if not origin:
            return url
        try:
            parts = urllib.parse.urlsplit(url)
            oparts = urllib.parse.urlsplit(origin)
        except ValueError:
            return url
        if not oparts.netloc:
            return url
        if (parts.scheme, parts.netloc) == (oparts.scheme, oparts.netloc):
            return url
        fixed = urllib.parse.urlunsplit(
            (oparts.scheme, oparts.netloc, parts.path, parts.query, parts.fragment)
        )
        log.info("天翼直链 origin 换成客户端地址: %s -> %s",
                 parts.netloc, oparts.netloc)
        return fixed

    # ------------------------------------------------------------- 事件
    def event(self, kind: str, message: str, level: str = "info",
              config_id: str = "", detail: str = "") -> None:
        try:
            self.db.add_cloud189_event(kind, message, config_id=config_id,
                                       level=level, detail=detail)
        except Exception as exc:  # noqa: BLE001 - 记日志失败绝不影响主流程
            log.debug("写天翼云盘事件失败: %s", exc)

    # ------------------------------------------------------------- 生成
    def _local_prefix(self, local_dir: str) -> str:
        """把用户填的本地目录规整成「相对 STRM_ROOT」的前缀。

        用户可能填 ``天翼`` / ``/strm/天翼`` / ``strm/天翼``，三种都接受；
        填 STRM_ROOT 本身时等于空前缀（strm 直接落到根下）。
        """
        raw = str(local_dir or "").replace("\\", "/").strip().strip("/")
        root_name = self.store.root.name or "strm"
        if raw == root_name:
            return ""
        if raw.startswith(root_name + "/"):
            raw = raw[len(root_name) + 1:]
        # 绝对路径但不在 STRM_ROOT 下时，取最后一段，避免写到容器外
        if raw.startswith("/"):
            raw = raw.strip("/")
        return raw

    def run_config(self, config_id: str, mode: str = "inc") -> dict:
        """按配置跑一次生成。mode: inc / full / rebuild。"""
        row = self.db.get_cloud189_config(config_id)
        if row is None:
            raise Cloud189Error("没有这条配置")
        mode = (mode or "inc").lower()
        if mode not in ("inc", "full", "rebuild"):
            mode = "inc"
        with self._lock:
            if config_id in self._running:
                raise Cloud189Error("这条配置正在生成，请稍候")
            self._running.add(config_id)
        self.db.update_cloud189_config(config_id, status="running")
        threading.Thread(
            target=self._run, args=(row, mode), name=f"cloud189-{config_id}",
            daemon=True,
        ).start()
        return {"ok": True, "accepted": True, "mode": mode}

    def _run(self, row: dict, mode: str) -> None:
        cid = str(row["id"])
        label = str(row.get("name") or cid)
        account_id = str(row.get("account") or "")
        mode_text = {"inc": "增量生成", "full": "全量生成", "rebuild": "全新生成"}[mode]
        prefix = self._local_prefix(row.get("local_dir"))
        force = mode in ("full", "rebuild")
        scope, family_id, folder_id = parse_remote(row.get("remote_id"))
        stats = {"dirs": 0, "scanned": 0, "written": 0, "skipped": 0,
                 "filtered": 0, "failed": 0, "cleared": 0, "removed": 0}
        started = time.time()
        self.event("run_start", f"{mode_text}开始：{label}", config_id=cid)
        try:
            client = self.client(account_id)
            old_rows = {str(r["file_id"]): r for r in self.db.list_cloud189_files(cid)}
            if mode == "rebuild":
                # 全新：只删**这条配置索引里记录过**的 strm，绝不按目录整片删
                stats["cleared"] = self.store.remove_strm_paths(
                    [r.get("strm_path") for r in old_rows.values()])
                self.db.clear_cloud189_files(cid)
                old_rows = {}
            base = str(row.get("base_url") or "").rstrip("/") or self.cfg.base_url
            if row.get("all_files"):
                exts: set[str] = set()
            else:
                exts = split_exts(row.get("media_exts") or DEFAULT_EXTS)
            ctx = {
                "cid": cid,
                "account": account_id,
                "family": family_id if scope == "family" else "",
                "families": [],
                "prefix": prefix,
                "base": base,
                "exts": exts,
                "force": force,
                "seen": set(),
                "taken": set(),
            }
            if scope == "family_list":
                # 整个「家庭云」整选：每个家庭一个同名子目录，互不干扰
                fams = client.list_families()
                if not fams:
                    raise Cloud189Error(
                        "这个账号还没有加入任何家庭云，换一个账号或先点进具体家庭")
                for fam in fams:
                    ctx["family"] = str(fam["family_id"])
                    ctx["families"].append(ctx["family"])
                    self._walk(client, ctx, "",
                               safe_component(fam.get("name") or ctx["family"]),
                               stats, 0)
            else:
                self._walk(client, ctx, folder_id, "", stats, 0)
            seen = ctx["seen"]
            # 云端已删 / 改名：全量、全新时把本地残留一起收掉
            if force and not stats["failed"]:
                stale = [r for fid, r in old_rows.items() if fid not in seen]
                if stale:
                    stats["removed"] = self.store.remove_strm_paths(
                        [r.get("strm_path") for r in stale])
                    for r in stale:
                        self.db.delete_cloud189_file(cid, str(r["file_id"]))
            self.db.update_cloud189_config(
                cid, status="idle", last_run=int(time.time()),
                files=len(seen))
            cost = round(time.time() - started, 1)
            self.event(
                "run_done",
                f"{mode_text}完成：{label} · {len(seen)} 个文件 · "
                f"写入 {stats['written']}（跳过 {stats['skipped']}）"
                + (f" · 清理旧文件 {stats['cleared']}" if stats["cleared"] else "")
                + (f" · 移除残留 {stats['removed']}" if stats["removed"] else "")
                + f" · 用时 {cost}s",
                config_id=cid,
                detail=json.dumps({**stats, "mode": mode, "files": len(seen),
                                   "seconds": cost,
                                   "account": account_id,
                                   "family_id": (family_id
                                                 if scope == "family" else ""),
                                   "family_ids": ctx["families"],
                                   "family_dir": (family_id
                                                  if scope == "family" else "")},
                                  ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001 - 后台任务失败只记状态
            log.warning("天翼云盘配置 %s 生成失败: %s", cid, exc)
            self.db.update_cloud189_config(cid, status="error")
            self.event("run_error", f"{mode_text}失败：{label} · {exc}",
                       level="err", config_id=cid)
        finally:
            with self._lock:
                self._running.discard(cid)

    def _walk(self, client: Cloud189Client, ctx: dict, folder_id: str, rel: str,
              stats: dict, depth: int) -> None:
        cid, family_id = ctx["cid"], ctx["family"]
        if depth >= MAX_DEPTH:
            stats["failed"] += 1
            self.event("run_warn", f"递归超过 {MAX_DEPTH} 层，已跳过：{rel or '/'}",
                       level="warn", config_id=cid)
            return
        try:
            items = client.list_files(folder_id, family_id)
        except Cloud189Error as exc:
            stats["failed"] += 1
            self.event("run_warn", f"列目录失败 {rel or '/'}：{exc}",
                       level="warn", config_id=cid)
            return
        stats["dirs"] += 1
        for item in items:
            name = safe_component(item.get("name") or "")
            if item.get("is_dir"):
                self._walk(client, ctx, str(item["id"]), rel_join(rel, name),
                           stats, depth + 1)
                continue
            stats["scanned"] += 1
            if ctx["exts"] and file_ext(name) not in ctx["exts"]:
                stats["filtered"] += 1
                continue
            file_id = str(item["id"])
            strm_name = strm_rel_path(rel_join(rel, name))
            # 同目录 a.mkv / a.mp4 会撞到同一个 a.strm，撞了就用 id 后缀区分
            if strm_name in ctx["taken"]:
                strm_name = (strm_name.removesuffix(".strm")
                             + f" [{file_id[-6:]}].strm")
            ctx["taken"].add(strm_name)
            strm_rel = rel_join(ctx["prefix"], strm_name)
            url = self.play_url(file_id, name, base_url=ctx["base"],
                                account_id=ctx["account"],
                                family_id=item.get("family_id") or family_id)
            path, changed = self.store.write_url(
                rel_join(rel, name), url, name=name, strm_rel=strm_rel,
                force=ctx["force"])
            if changed:
                stats["written"] += 1
            else:
                stats["skipped"] += 1
            ctx["seen"].add(file_id)
            self.db.upsert_cloud189_file(
                cid, file_id, name=str(item.get("name") or ""),
                rel_path=strm_name, strm_path=path,
                size=int(item.get("size") or 0),
                account=ctx["account"], family_id=item.get("family_id") or family_id)

    # ------------------------------------------------------------- 统计
    def counts(self) -> dict:
        return {"configs": len(self.db.list_cloud189_configs()),
                "accounts": len(self.accounts()),
                "files": self.db.cloud189_file_count()}

    def local_abspath(self, local_dir: str) -> Path:
        return self.store.abspath(self._local_prefix(local_dir))
