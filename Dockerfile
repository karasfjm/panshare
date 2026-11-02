# syntax=docker/dockerfile:1
#
# 构建原则：镜像内**不做任何 apt-get**。
# 之前 apt 层在国内网络下经常 exit code 100（Debian 源不可达 / DNS 被劫持 / 代理超时），
# 现在所需的几样东西全部由基础镜像或 Python 自带能力提供：
#   - 时区：ENV TZ + 入口脚本兜底成 POSIX 写法 CST-8（不依赖 tzdata 包）
#   - 健康检查：scripts/healthcheck.py（Python urllib，替代 curl）
#   - PUID/PGID 降权：scripts/runas.py（os.setuid，替代 gosu / useradd）
#
# 国内拉不到基础镜像时换镜像站：
#   docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/config \
    STRM_ROOT=/strm \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=9527

WORKDIR /app

# 国内网络构建时可传 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple

COPY requirements.txt ./
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

COPY app ./app
COPY plugins ./plugins
COPY scripts ./scripts
COPY web ./web
RUN chmod +x scripts/entrypoint.sh scripts/install_browser.sh \
    && mkdir -p /config /strm

# 可选插件（天翼云盘 / 通知与联动 / 企微可信IP）的源码就在 plugins/ 里，很小；
# 它们的**重依赖**不在这里装 —— 网页「插件」页点安装，才会把 playwright +
# Chromium 下到 /config/plugins/<id>/（配置卷，容器重建不用重装）。
#
# 下面这个 WITH_BROWSER 是给「完全离线、宁可镜像大一点」的场景留的后路：
# 传 --build-arg WITH_BROWSER=1 会把 Chromium 打进镜像（+约 400MB）。
# 插件装依赖时会发现 /root/.cache/ms-playwright 已经有浏览器，直接复用、不再下载。
# 注意：构建机访问 Debian 源慢的时候这一层会很慢。
ARG WITH_BROWSER=0
RUN if [ "$WITH_BROWSER" = "1" ]; then \
        set -e; \
        apt-get update; \
        apt-get install -y --no-install-recommends \
            libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
            libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
            libgbm1 libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 \
            fonts-liberation; \
        rm -rf /var/lib/apt/lists/*; \
        pip install --no-cache-dir "playwright==1.47.0"; \
        python -m playwright install chromium; \
    fi

VOLUME ["/config", "/strm"]
EXPOSE 9527

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/scripts/healthcheck.py"]

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["python", "-m", "app"]
