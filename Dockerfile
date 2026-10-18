# syntax=docker/dockerfile:1
# =============================================================================
# PTtools — PT 站点运维工具箱
# -----------------------------------------------------------------------------
#   * CD2 守卫：CloudDrive2 上传任务秒传判定 / 超时拦截 / 备份重扫重试
#   * 115 监控：云下载超时清理
#   * BT 流量：qBittorrent / Transmission 上传流量监控与自动限速
#               （Go 子服务，由 Python 侧反向代理到 /btg/*）
#
# 注意：Python 基础镜像必须 >= 3.12，因为 p115client 声明 Requires-Python
#       >=3.12（3.11 上 pip 会直接拒绝安装）。
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: 编译 bt-traffic-guard（Go）
# -----------------------------------------------------------------------------
FROM golang:1.24-alpine AS gobuild

ARG GOPROXY=https://proxy.golang.org,direct

WORKDIR /src

# 先只拷依赖清单，最大化利用层缓存
COPY btguard/go.mod btguard/go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod \
    go mod download

# 再拷源码并静态编译
COPY btguard/ ./
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
        -o /out/bt-traffic-guard ./cmd/bt-traffic-guard

# -----------------------------------------------------------------------------
# Stage 2: Python 运行时
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    APP_HOME=/app \
    DATA_DIR=/app/data \
    BTG_BINARY=/app/btguard-bin/bt-traffic-guard \
    CD2_PROTO_URL=https://www.clouddrive2.com/api/clouddrive.proto

# ---- OS 依赖（curl 用于健康检查，tzdata 保证时区正确） ----
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR ${APP_HOME}

# ---- Python 依赖（独立层，源码改动不会让其失效） ----
COPY requirements.txt requirements-optional.txt ./

# 必需依赖：失败即中止
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

# 可选依赖（115 相关）：失败只告警，不阻断构建。
RUN pip install -r requirements-optional.txt \
 || echo "WARNING: 可选依赖(115 相关)安装失败，115 云下载功能将不可用"

# ---- 应用源码 ----
COPY . .

# ---- 放入 Go 子服务二进制 + 它的示例配置 ----
COPY --from=gobuild /out/bt-traffic-guard ${BTG_BINARY}
# 校验二进制可执行（-version 会打印版本后退出）
RUN chmod +x ${BTG_BINARY} \
 && ${BTG_BINARY} -version

# ---- 编译 clouddrive.proto -> clouddrive_pb2*.py ----
RUN python tools/fetch_proto.py --out app/proto

# ---- 非 root 运行用户 ----
RUN useradd --create-home --uid 1000 appuser \
 && mkdir -p ${DATA_DIR} \
 && chown -R appuser:appuser ${APP_HOME} \
 && chmod +x docker/entrypoint.sh

USER appuser

VOLUME ["/app/data"]
EXPOSE 8899

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8899/api/health || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8899", "--proxy-headers"]
