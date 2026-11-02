#!/bin/sh
# 入口脚本：可选以 PUID/PGID 降权运行（NAS 场景常用）。
#
# 镜像刻意不装 gosu / passwd（apt 在受限网络下经常 exit 100），
# 降权改用 Python 的 os.setuid 实现（scripts/runas.py），零外部依赖。
set -e

DATA_DIR="${DATA_DIR:-/config}"
STRM_LIB="${STRM_ROOT:-/strm}"

# 基础镜像若没带 tzdata，命名时区会静默退化成 UTC；这里兜底成 UTC+8 的 POSIX 写法。
if [ -n "${TZ:-}" ] && [ ! -e "/usr/share/zoneinfo/${TZ}" ]; then
    echo "[entrypoint] 镜像内没有 /usr/share/zoneinfo/${TZ}，TZ 改用 CST-8（UTC+8）"
    export TZ="CST-8"
fi

mkdir -p "$DATA_DIR" "$STRM_LIB" 2>/dev/null || true

if [ -n "${PUID:-}" ] && [ "${PUID}" != "0" ]; then
    PGID="${PGID:-$PUID}"
    # 默认只改目录本身属主：strm 库可能几十万文件，每次启动 chown -R 会拖慢启动。
    # 需要把历史文件也改过来时设 CHOWN_RECURSIVE=1。
    if [ "${CHOWN_RECURSIVE:-0}" = "1" ]; then
        chown -R "${PUID}:${PGID}" "$DATA_DIR" "$STRM_LIB" 2>/dev/null || true
    else
        chown "${PUID}:${PGID}" "$DATA_DIR" "$STRM_LIB" 2>/dev/null || true
    fi
    echo "[entrypoint] 以 ${PUID}:${PGID} 运行"
    exec python3 /app/scripts/runas.py "$PUID" "$PGID" "$@"
fi

exec "$@"
