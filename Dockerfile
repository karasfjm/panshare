# ---------- 1. 前端构建 ----------
FROM node:22-alpine AS web
WORKDIR /web
# 国内网络拉 npm 慢或失败时可以覆盖：--build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=https://registry.npmjs.org
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm config set registry "${NPM_REGISTRY}" \
 && npm install --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

# ---------- 2. 后端 + 插件编译 ----------
FROM golang:1.23-alpine AS build
WORKDIR /src

# Go 模块代理。默认走国内镜像：容器里直连 proxy.golang.org 基本会超时，
# 表现为 `go mod download did not complete successfully: exit code 1`。
# 海外机器可以覆盖成 https://proxy.golang.org,direct
ARG GOPROXY=https://goproxy.cn,direct
# go.sum 已经锁了全部依赖哈希，这里关掉校验和数据库查询（它要去 sum.golang.org）。
ARG GOSUMDB=off
ENV CGO_ENABLED=0 \
    GOOS=linux \
    GOTOOLCHAIN=local \
    GOFLAGS=-mod=mod \
    GOPROXY=${GOPROXY} \
    GOSUMDB=${GOSUMDB}

COPY backend/go.mod backend/go.sum ./
# 先把依赖环境打出来：构建失败时日志里能直接看出用的是哪个代理
RUN go env GOPROXY GOSUMDB GOTOOLCHAIN \
 && go mod download

COPY backend/ ./
ARG VERSION=dev
RUN go build -trimpath -ldflags "-s -w -X panshare/internal/server.Version=${VERSION}" \
        -o /out/panshare ./cmd/panshare \
 && go build -trimpath -ldflags "-s -w" -o /out/cloud189 ./plugins/cloud189 \
 && go build -trimpath -ldflags "-s -w" -o /out/p115probe ./cmd/p115probe

# wxip 插件自带 go.mod（依赖 chromedp，不污染主服务的依赖树），单独拉包编译
RUN cd plugins/wxip \
 && go mod download \
 && go build -trimpath -ldflags "-s -w" -o /out/wxip .

# ---------- 3. 运行镜像 ----------
FROM alpine:3.20
# 注意：不在此处安装 chromium。「动态企微可信IP」插件需要无头浏览器，
# 但体积大（约 600MB），改由插件首次启动时用国内 apk 源按需下载，
# 这样不用浏览器的人镜像保持精简。下载逻辑见 plugins/wxip/core/browser.go 的 EnsureChromium。
RUN apk add --no-cache ca-certificates tzdata su-exec
ENV TZ=Asia/Shanghai \
    PS_DATA_DIR=/config \
    PS_STRM_ROOT=/strm \
    PS_PLUGIN_DIR=/app/plugins \
    PS_ADDR=:9527
WORKDIR /app
COPY --from=build /out/panshare /app/panshare
COPY --from=web /web/dist /app/web
# 内置插件（可安装，不装不启动）
COPY --from=build /out/cloud189 /app/plugins/cloud189/cloud189
COPY --from=build /out/wxip /app/plugins/wxip/wxip
# 排查取直链用的诊断工具（docker exec panshare-go /app/p115probe -h）
COPY --from=build /out/p115probe /app/p115probe
COPY backend/plugins/cloud189/manifest.json /app/plugins/cloud189/manifest.json
COPY backend/plugins/wxip/manifest.json /app/plugins/wxip/manifest.json
RUN mkdir -p /config /strm && chmod +x /app/plugins/cloud189/cloud189 /app/plugins/wxip/wxip

VOLUME ["/config", "/strm"]
# 9527 控制台；7259 Emby 反代（PS_EMBY_PROXY_PORT 可改）
EXPOSE 9527 7259
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD wget -qO- http://127.0.0.1:9527/healthz >/dev/null || exit 1

ENTRYPOINT ["/app/panshare"]
CMD ["-web", "/app/web"]
