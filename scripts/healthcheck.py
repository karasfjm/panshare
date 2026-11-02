#!/usr/bin/env python3
"""容器健康检查：探 /healthz。

不用 curl（镜像里不装 apt 包），只用标准库 urllib。
"""

from __future__ import annotations

import os
import sys
import urllib.request


def main() -> int:
    host = os.environ.get("HEALTHCHECK_HOST") or "127.0.0.1"
    port = os.environ.get("WEB_PORT") or "9527"
    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:  # noqa: S310 - 固定本地地址
            return 0 if resp.status == 200 else 1
    except Exception:  # noqa: BLE001 - 探测失败一律 unhealthy
        return 1


if __name__ == "__main__":
    sys.exit(main())
