#!/bin/sh
# 安装「企微可信 IP」需要的 Chromium（可选功能，不装也不影响其它页面）。
#
# v1.19.0 起这个脚本一般用不上了：网页「插件」页点「安装依赖」即可，
# 而且会装到 /config 卷上（容器重建不用重装）。留着它是给两种情况用：
#   * 想手工在容器里跑一遍看输出；
#   * 插件页点安装失败、想自己排查 apt / pip 的问题。
#
# 用法（在 NAS 宿主机上）：
#   docker exec -it panshare sh /app/scripts/install_browser.sh
#
# 会做三件事：补 Chromium 依赖的系统库 → 装 playwright → 下载浏览器内核。
# NAS 上 Debian 源很慢时 apt 会卡很久，耐心等；playwright 的 CDN 通常很快。
set -e

DEBIAN_FRONTEND=noninteractive
export DEBIAN_FRONTEND

echo "==> 1/3 安装 Chromium 依赖（Debian/Ubuntu）"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
        fonts-liberation
    rm -rf /var/lib/apt/lists/*
else
    echo "    没有 apt-get，跳过（Alpine 需要 apk 装 nss/atk/gbm 等）"
fi

echo "==> 2/3 安装 playwright"
python -m pip install --no-cache-dir "playwright==1.47.0"

echo "==> 3/3 下载 Chromium 内核"
python -m playwright install chromium

echo "==> 完成。重启容器后，页面上「企微可信IP」就能扫码登录了："
echo "    docker restart panshare"
