"""企业微信回调加解密（纯 Python，零额外依赖）。

企业微信「接收消息服务器」的协议：

* ``msg_signature = sha1(sorted([token, timestamp, nonce, encrypt]))``
* ``AESKey = base64decode(EncodingAESKey + "=")`` —— 32 字节，
  直接当 AES-256 的 key，IV 取它前 16 字节
* 明文结构：``random(16) + msg_len(4, 大端) + msg + receiveid``，
  再按 **32 字节** 做 PKCS#7 补齐

容器里没有 cryptography / pycryptodome，也不想为一个回调多装一个带编译
依赖的包，所以这里自带一份 AES-256（只用到 CBC），并用 NIST 标准向量做了
单测（见 tests/test_notify.py）。
"""

from __future__ import annotations

import base64
import hashlib
import struct

# ---------------------------------------------------------------- AES 基础表
SBOX = (
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b,
    0xfe, 0xd7, 0xab, 0x76, 0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0,
    0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0, 0xb7, 0xfd, 0x93, 0x26,
    0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2,
    0xeb, 0x27, 0xb2, 0x75, 0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0,
    0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84, 0x53, 0xd1, 0x00, 0xed,
    0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f,
    0x50, 0x3c, 0x9f, 0xa8, 0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5,
    0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2, 0xcd, 0x0c, 0x13, 0xec,
    0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14,
    0xde, 0x5e, 0x0b, 0xdb, 0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c,
    0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79, 0xe7, 0xc8, 0x37, 0x6d,
    0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f,
    0x4b, 0xbd, 0x8b, 0x8a, 0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e,
    0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e, 0xe1, 0xf8, 0x98, 0x11,
    0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f,
    0xb0, 0x54, 0xbb, 0x16,
)
# 逆 S 盒由正表反查生成，避免手抄第二张表出错
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i
INV_SBOX = tuple(INV_SBOX)

RCON = (0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB)


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    """GF(2^8) 乘法（0x11B 不可约多项式）。"""
    out = 0
    while b:
        if b & 1:
            out ^= a
        a = _xtime(a)
        b >>= 1
    return out


def _expand_key(key: bytes) -> list[list[int]]:
    """AES-256 密钥扩展 → 15 组轮密钥（每组 16 字节）。"""
    if len(key) != 32:
        raise ValueError("AES-256 需要 32 字节密钥")
    nk, nr = 8, 14
    words = [list(key[i * 4:i * 4 + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= RCON[i // nk]
        elif i % nk == 4:
            temp = [SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return [[b for word in words[r * 4:r * 4 + 4] for b in word]
            for r in range(nr + 1)]


def _add_round_key(state: list[int], key: list[int]) -> list[int]:
    return [s ^ k for s, k in zip(state, key)]


def _sub_bytes(state: list[int], box=tuple(SBOX)) -> list[int]:
    return [box[b] for b in state]


def _shift_rows(state: list[int]) -> list[int]:
    out = state[:]
    for r in range(1, 4):
        for c in range(4):
            out[r + 4 * c] = state[r + 4 * ((c + r) % 4)]
    return out


def _inv_shift_rows(state: list[int]) -> list[int]:
    out = state[:]
    for r in range(1, 4):
        for c in range(4):
            out[r + 4 * ((c + r) % 4)] = state[r + 4 * c]
    return out


def _mix_columns(state: list[int]) -> list[int]:
    out = state[:]
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = state[i:i + 4]
        out[i] = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
        out[i + 1] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
        out[i + 2] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
        out[i + 3] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)
    return out


def _inv_mix_columns(state: list[int]) -> list[int]:
    out = state[:]
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = state[i:i + 4]
        out[i] = _mul(a0, 14) ^ _mul(a1, 11) ^ _mul(a2, 13) ^ _mul(a3, 9)
        out[i + 1] = _mul(a0, 9) ^ _mul(a1, 14) ^ _mul(a2, 11) ^ _mul(a3, 13)
        out[i + 2] = _mul(a0, 13) ^ _mul(a1, 9) ^ _mul(a2, 14) ^ _mul(a3, 11)
        out[i + 3] = _mul(a0, 11) ^ _mul(a1, 13) ^ _mul(a2, 9) ^ _mul(a3, 14)
    return out


def aes_encrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = list(block)
    state = _add_round_key(state, round_keys[0])
    for rnd in range(1, 14):
        state = _sub_bytes(state)
        state = _shift_rows(state)
        state = _mix_columns(state)
        state = _add_round_key(state, round_keys[rnd])
    state = _sub_bytes(state)
    state = _shift_rows(state)
    state = _add_round_key(state, round_keys[14])
    return bytes(state)


def aes_decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = list(block)
    state = _add_round_key(state, round_keys[14])
    for rnd in range(13, 0, -1):
        state = _inv_shift_rows(state)
        state = _sub_bytes(state, INV_SBOX)
        state = _add_round_key(state, round_keys[rnd])
        state = _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    state = _sub_bytes(state, INV_SBOX)
    state = _add_round_key(state, round_keys[0])
    return bytes(state)


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def aes_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("CBC 明文长度必须是 16 的倍数")
    keys = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = _xor(data[i:i + 16], prev)
        prev = aes_encrypt_block(block, keys)
        out += prev
    return bytes(out)


def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("CBC 密文长度必须是 16 的倍数")
    keys = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = data[i:i + 16]
        out += _xor(aes_decrypt_block(block, keys), prev)
        prev = block
    return bytes(out)


# ------------------------------------------------------------------ 企业微信
def pkcs7_pad(data: bytes, block: int = 32) -> bytes:
    n = block - len(data) % block
    return data + bytes([n]) * n


def pkcs7_unpad(data: bytes, block: int = 32) -> bytes:
    if not data:
        raise ValueError("空数据")
    n = data[-1]
    if n < 1 or n > block or len(data) < n:
        raise ValueError("PKCS#7 填充不合法")
    return data[:-n]


def signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """企业微信回调签名：四个值字典序拼接后 sha1。"""
    items = sorted([str(token or ""), str(timestamp or ""),
                    str(nonce or ""), str(encrypt or "")])
    return hashlib.sha1("".join(items).encode("utf-8")).hexdigest()


def decode_aes_key(encoding_aes_key: str) -> bytes:
    raw = base64.b64decode((encoding_aes_key or "") + "=")
    if len(raw) != 32:
        raise ValueError("EncodingAESKey 长度不对（应为 43 位）")
    return raw


def decrypt_message(encoding_aes_key: str, encrypt: str,
                    corp_id: str = "") -> str:
    """解开回调里的 Encrypt 字段，返回 XML 明文。"""
    key = decode_aes_key(encoding_aes_key)
    raw = base64.b64decode(encrypt)
    plain = pkcs7_unpad(aes_cbc_decrypt(key, key[:16], raw), 32)
    if len(plain) < 20:
        raise ValueError("解密结果太短")
    msg_len = struct.unpack(">I", plain[16:20])[0]
    msg = plain[20:20 + msg_len]
    receive_id = plain[20 + msg_len:]
    if corp_id and receive_id.decode("utf-8", "ignore") != corp_id:
        raise ValueError("receiveid 不匹配（corp_id 配置错？）")
    return msg.decode("utf-8", "ignore")


def encrypt_message(encoding_aes_key: str, text: str,
                    corp_id: str = "", random16: bytes = b"0123456789abcdef") -> str:
    """回调测试 / 单测用：把明文按企业微信格式加密回 base64。"""
    key = decode_aes_key(encoding_aes_key)
    body = text.encode("utf-8")
    plain = random16 + struct.pack(">I", len(body)) + body + corp_id.encode("utf-8")
    return base64.b64encode(aes_cbc_encrypt(key, key[:16], pkcs7_pad(plain, 32))).decode()


def verify_url(encoding_aes_key: str, echostr: str, corp_id: str = "") -> str:
    """GET 验证：解密 echostr 并原样返回明文（结构与消息体一致）。"""
    return decrypt_message(encoding_aes_key, echostr, corp_id)


__all__ = [
    "aes_cbc_decrypt", "aes_cbc_encrypt", "aes_decrypt_block", "aes_encrypt_block",
    "decode_aes_key", "decrypt_message", "encrypt_message", "pkcs7_pad",
    "pkcs7_unpad", "signature", "verify_url",
]
