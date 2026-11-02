"""网页登录鉴权：账号密码由 compose 变量生成，会话用 HMAC 签名 Cookie。

设计要点：

- **不引入第三方依赖**：签名用标准库的 hmac/hashlib，镜像里不用多装包；
- **密码不落库**：只跟环境变量里的明文比对（``hmac.compare_digest`` 防时序攻击）；
- **会话无状态**：Cookie 里是 ``base64(payload).签名``，服务重启也不用重新登录
  （密钥由密码派生，所以改密码等于让所有人重新登录）；
- **只在配置了密码时才启用**：``UI_PASSWORD`` 为空则整套逻辑旁路，
  本地开发 / 冒烟测试不需要登录。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

COOKIE_NAME = "p115_session"
DEFAULT_TTL = 7 * 24 * 3600  # 7 天


class AuthManager:
    """账号密码校验 + 会话签发/校验。"""

    def __init__(self, username: str = "", password: str = "", ttl: int = DEFAULT_TTL):
        self.username = (username or "").strip()
        self.password = password or ""
        self.ttl = max(60, int(ttl or DEFAULT_TTL))
        # 密钥由密码派生：改密码 → 旧签名全部失效（等于强制重新登录）
        self._secret = hashlib.sha256(
            f"p115-strm|{self.username}|{self.password}".encode()
        ).digest()

    # ------------------------------------------------------------------ 开关
    @property
    def enabled(self) -> bool:
        """没有配置密码时不启用登录（本地开发 / 测试）。"""
        return bool(self.password)

    # ------------------------------------------------------------------ 密码
    def verify_password(self, username: str, password: str) -> bool:
        if not self.enabled:
            return True
        user_ok = hmac.compare_digest(
            (username or "").encode(), self.username.encode()
        )
        pass_ok = hmac.compare_digest(
            (password or "").encode(), self.password.encode()
        )
        return user_ok and pass_ok

    # ------------------------------------------------------------------ 会话
    def issue(self, username: str = "") -> str:
        """签发一个会话 token（payload = 用户名|过期时间戳）。"""
        expires = int(time.time()) + self.ttl
        payload = f"{username or self.username}|{expires}".encode()
        body = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str) -> dict[str, Any] | None:
        """校验会话 token；有效返回 ``{"username": ..., "expires": ...}``。"""
        if not self.enabled:
            return {"username": self.username, "expires": 0}
        token = (token or "").strip()
        if "." not in token:
            return None
        body, _, signature = token.rpartition(".")
        if not body or not signature:
            return None
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            padded = body + "=" * (-len(body) % 4)
            payload = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        username, _, expires = payload.partition("|")
        try:
            expires_at = int(expires)
        except ValueError:
            return None
        if expires_at and expires_at < int(time.time()):
            return None
        return {"username": username, "expires": expires_at}

    def _sign(self, body: str) -> str:
        digest = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).hexdigest()
        return digest[:32]

    # ------------------------------------------------------------------ Cookie
    def cookie_params(self, token: str) -> dict[str, Any]:
        return {
            "key": COOKIE_NAME,
            "value": token,
            "max_age": self.ttl,
            "httponly": True,
            "samesite": "lax",
            "path": "/",
        }

    def clear_cookie_params(self) -> dict[str, Any]:
        return {"key": COOKIE_NAME, "value": "", "max_age": 0, "path": "/"}


__all__ = ["COOKIE_NAME", "AuthManager"]
