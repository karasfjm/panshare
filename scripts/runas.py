#!/usr/bin/env python3
"""以指定 uid/gid 运行子进程，替代 gosu / su-exec / useradd。

镜像里不装 gosu 和 passwd 包（apt 在受限网络下经常失败），
直接用 os.setgid + os.setuid 降权，然后 exec 目标进程。

用法：runas.py <uid> <gid> <cmd> [args...]
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 4:
        print("用法: runas.py <uid> <gid> <cmd> [args...]", file=sys.stderr)
        return 2

    try:
        uid, gid = int(argv[1]), int(argv[2])
    except ValueError:
        print(f"[entrypoint] PUID/PGID 必须是数字：{argv[1]!r} / {argv[2]!r}", file=sys.stderr)
        return 2

    cmd = argv[3:]

    # 非 Linux（本地调试 / Windows）没有 setuid，直接透传
    if not hasattr(os, "setuid"):
        print("[entrypoint] 当前平台不支持 setuid，跳过降权直接运行", flush=True)
        os.execvp(cmd[0], cmd)
        return 1

    euid = os.geteuid()
    # 已经通过 user: 指令降过权（或 rootless 容器），没必要也不能再 setuid
    if euid != 0:
        print(
            f"[entrypoint] 当前 uid={euid} 不是 root，跳过降权直接运行；"
            "若文件属主不对请改用 PUID=0 或调整挂载目录权限",
            flush=True,
        )
        os.execvp(cmd[0], cmd)
        return 1

    try:
        os.setgroups([])
    except OSError:
        pass  # 少数环境下没有 CAP_SETGID，忽略即可
    os.setgid(gid)
    os.setuid(uid)

    if os.geteuid() != uid:
        print(f"[entrypoint] 降权失败，仍在 uid={os.geteuid()}", file=sys.stderr)
        return 1

    os.environ.setdefault("HOME", "/tmp")
    os.execvp(cmd[0], cmd)
    return 1  # execvp 成功时不会走到这里


if __name__ == "__main__":
    sys.exit(main(sys.argv))
