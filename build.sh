#!/usr/bin/env sh
# 一键构建 PanShare 镜像。
#
# 用法：
#   ./build.sh                                    # 本机架构，构建到本地镜像库
#   ./build.sh -o panshare.tar                   # 额外导出 tar，供离线机器 docker load
#   ./build.sh -p linux/amd64                     # 指定架构（NAS / 服务器）
#   ./build.sh -p linux/amd64,linux/arm64 --push -t user/panshare:latest
#   ./build.sh -i https://pypi.tuna.tsinghua.edu.cn/simple   # 用国内 pip 源
#   ./build.sh -b docker.m.daocloud.io/library/python:3.12-slim  # 拉不到 docker.io 时换镜像站
#
# 也可用环境变量：IMAGE PLATFORM OUTPUT PUSH PIP_INDEX_URL BASE_IMAGE NO_CACHE
set -eu

IMAGE="${IMAGE:-panshare:latest}"
PLATFORM="${PLATFORM:-}"
OUTPUT="${OUTPUT:-}"
PUSH="${PUSH:-0}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"
BASE_IMAGE="${BASE_IMAGE:-}"
NO_CACHE="${NO_CACHE:-0}"

cd "$(dirname "$0")"

usage() {
    cat <<'EOF'
一键构建 PanShare 镜像

  -t, --tag <name>        镜像名，默认 panshare:latest
  -p, --platform <plat>   目标平台，如 linux/amd64、linux/arm64，逗号分隔可多架构
  -o, --output <file>     导出镜像 tar（可拷到离线机器 docker load）
  -i, --index <url>       pip 源，默认官方源
  -b, --base-image <ref>  基础镜像，默认 python:3.12-slim（拉不到 docker.io 时换镜像站）
      --push              构建后推送到仓库（多架构必须）
      --no-cache          不使用构建缓存
  -h, --help              显示帮助
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -o|--output) OUTPUT="${2:?--output 需要路径}"; shift 2 ;;
        -t|--tag) IMAGE="${2:?--tag 需要镜像名}"; shift 2 ;;
        -p|--platform) PLATFORM="${2:?--platform 需要平台}"; shift 2 ;;
        -i|--index) PIP_INDEX_URL="${2:?--index 需要 url}"; shift 2 ;;
        -b|--base-image) BASE_IMAGE="${2:?--base-image 需要镜像名}"; shift 2 ;;
        --push) PUSH=1; shift ;;
        --no-cache) NO_CACHE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; usage >&2; exit 1 ;;
    esac
done

if ! command -v docker >/dev/null 2>&1; then
    cat >&2 <<'EOF'
X 找不到 docker 命令。

本机没有 Docker 时，可以用下面任一方式构建：
  1) 装 Docker Desktop / OrbStack / docker-ce 后重跑本脚本；
  2) 把项目推到 GitHub：.github/workflows/docker.yml 会自动构建多架构镜像并推送 GHCR，
     同时在 Actions 的 Artifacts 里给出可直接 docker load 的 tar；
  3) 在任意有 Docker 的机器上执行：docker build -t panshare:latest .
EOF
    exit 1
fi

BUILDX=0
if docker buildx version >/dev/null 2>&1; then
    BUILDX=1
fi

MULTI=0
case "$PLATFORM" in
    *,*) MULTI=1 ;;
esac
if [ "$MULTI" = 1 ] && [ -z "$OUTPUT" ] && [ "$PUSH" != 1 ]; then
    echo "X 多架构构建无法导入本地镜像库，请加 --push，或用 -o xxx.tar 导出。" >&2
    exit 1
fi

BUILD_ARGS=""
if [ -n "$PIP_INDEX_URL" ]; then
    BUILD_ARGS="--build-arg PIP_INDEX_URL=$PIP_INDEX_URL"
fi
if [ -n "$BASE_IMAGE" ]; then
    BUILD_ARGS="$BUILD_ARGS --build-arg BASE_IMAGE=$BASE_IMAGE"
fi
if [ "$NO_CACHE" = 1 ]; then
    BUILD_ARGS="$BUILD_ARGS --no-cache"
fi

echo "-> 镜像：$IMAGE"
echo "-> 平台：${PLATFORM:-本机默认}"
if [ -n "$OUTPUT" ]; then echo "-> 导出：$OUTPUT"; fi
if [ "$PUSH" = 1 ]; then echo "-> 推送：是"; fi

if [ "$BUILDX" = 1 ] && [ "$MULTI" = 1 ]; then
    # shellcheck disable=SC2086
    docker buildx build --platform "$PLATFORM" --tag "$IMAGE" --push $BUILD_ARGS .
elif [ "$BUILDX" = 1 ] && [ -n "$OUTPUT" ] && [ "$PUSH" != 1 ]; then
    # shellcheck disable=SC2086
    docker buildx build \
        --platform "${PLATFORM:-linux/amd64}" \
        --tag "$IMAGE" \
        --output "type=docker,dest=$OUTPUT" \
        --provenance=false --sbom=false \
        $BUILD_ARGS .
    echo "OK 已导出：$OUTPUT"
    echo "   目标机器执行：docker load -i $OUTPUT"
else
    if [ -n "$PLATFORM" ] && [ "$MULTI" = 0 ]; then
        # shellcheck disable=SC2086
        docker build --platform "$PLATFORM" --tag "$IMAGE" $BUILD_ARGS .
    else
        # shellcheck disable=SC2086
        docker build --tag "$IMAGE" $BUILD_ARGS .
    fi
    if [ "$PUSH" = 1 ]; then
        docker push "$IMAGE"
    fi
    if [ -n "$OUTPUT" ]; then
        docker save -o "$OUTPUT" "$IMAGE"
        echo "OK 已导出：$OUTPUT"
        echo "   目标机器执行：docker load -i $OUTPUT"
    fi
fi

echo "OK 完成：$IMAGE"
echo "  启动：docker run -d --name panshare -p 9527:9527 \\"
echo "          -v \$PWD/config:/config -v /your/strm:/strm \\"
echo "          -e PUBLIC_BASE_URL=http://<你的地址>:9527 $IMAGE"
