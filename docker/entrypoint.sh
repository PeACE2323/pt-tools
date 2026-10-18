#!/usr/bin/env sh
# =============================================================================
# PTtools entrypoint
# -----------------------------------------------------------------------------
# 设计原则：**任何情况下都要有输出**。
# 之前用 `set -eu` + 早于 echo 的命令，一旦 mkdir 因权限失败就静默退出，
# 表现为「容器没起来且 docker logs 为空」，极难排查。
#
# 现在：
#   * 去掉 set -e，逐步检查并显式报告失败原因
#   * 第一条命令就是 echo，确保日志一定存在
# =============================================================================

echo "[entrypoint] ===== PTtools starting ====="
echo "[entrypoint] pwd=$(pwd)  uid=$(id -u 2>/dev/null || echo '?')  user=$(id -un 2>/dev/null || echo '?')"

DATA_DIR="${DATA_DIR:-/app/data}"
echo "[entrypoint] DATA_DIR=${DATA_DIR}"

# ---- 数据目录（失败要明确报错，而不是静默退出） ----
if ! mkdir -p "${DATA_DIR}" 2>/tmp/mkdir_err; then
  echo "[entrypoint] !! 无法创建数据目录 ${DATA_DIR}" >&2
  echo "[entrypoint] !! 原因: $(cat /tmp/mkdir_err 2>/dev/null)" >&2
  echo "[entrypoint] !! 通常是宿主机挂载目录权限问题。" >&2
  echo "[entrypoint] !! 解决办法：在宿主机执行" >&2
  echo "[entrypoint] !!   chown -R 1000:1000 <你的宿主机数据目录>" >&2
  echo "[entrypoint] !! 或以 --user root 运行容器" >&2
  exit 1
fi

if ! mkdir -p "${DATA_DIR}/logs" 2>/tmp/mkdir_err2; then
  echo "[entrypoint] !! 无法创建日志目录 ${DATA_DIR}/logs" >&2
  echo "[entrypoint] !! 原因: $(cat /tmp/mkdir_err2 2>/dev/null)" >&2
  exit 1
fi

# 目录可写性自检：能否真正写入文件
if ! (echo test > "${DATA_DIR}/.write_test" 2>/dev/null); then
  echo "[entrypoint] !! 数据目录不可写: ${DATA_DIR}" >&2
  echo "[entrypoint] !! 请执行: chown -R 1000:1000 <宿主机目录>" >&2
  exit 1
fi
rm -f "${DATA_DIR}/.write_test" 2>/dev/null || true

echo "[entrypoint] 数据目录可写: OK"

# ---- 115 cookies 文件权限收敛 ----
COOKIE_FILE="${DATA_DIR}/115-cookies.txt"
if [ -f "${COOKIE_FILE}" ]; then
  chmod 600 "${COOKIE_FILE}" 2>/dev/null || true
  echo "[entrypoint] 已处理 cookie 文件权限"
fi

# ---- Python / 依赖自检 ----
echo "[entrypoint] Python: $(python -V 2>&1)"
python - <<'PY' 2>/dev/null || echo "[entrypoint] !! 部分 Python 依赖导入失败（见上）"
import importlib
for mod in ("fastapi", "uvicorn", "sqlalchemy", "grpc", "pydantic"):
    try:
        m = importlib.import_module(mod)
        print(f"[entrypoint]   {mod:12s} {getattr(m, '__version__', 'ok')}")
    except Exception as exc:
        print(f"[entrypoint]   {mod:12s} 缺失/异常: {exc}")
PY

# ---- proto 生成代码检查（缺失则尝试编译） ----
PROTO_DIR="/app/app/proto"
if [ ! -f "${PROTO_DIR}/clouddrive_pb2_grpc.py" ]; then
  echo "[entrypoint] clouddrive_pb2*.py 缺失，开始编译 proto ..."
  if python /app/tools/fetch_proto.py --out "${PROTO_DIR}" --offline; then
    echo "[entrypoint] proto 编译成功"
  else
    echo "[entrypoint] !! proto 编译失败，CD2 相关功能将不可用" >&2
    echo "[entrypoint] !! 但这不影响 WebUI 启动" >&2
  fi
else
  echo "[entrypoint] proto 生成代码已就绪"
fi

# ---- PT 流量监控：纯 Python 实现，无需额外二进制 ----
echo "[entrypoint] PT 流量监控 : 内置模块（无需子进程）"

# ---- 数据库路径兜底 ----
export GUARD_DB_PATH="${GUARD_DB_PATH:-${DATA_DIR}/guard.db}"
echo "[entrypoint] database  : ${GUARD_DB_PATH}"

# ---- 数据卷检查 ----
# 如果这里是空库，几乎一定是 -v 没挂到 /app/data：
# 容器每次重建都会从零开始，用户会看到「配置全丢了」。
if [ -f "${GUARD_DB_PATH}" ]; then
  DB_SIZE=$(wc -c < "${GUARD_DB_PATH}" 2>/dev/null || echo 0)
  echo "[entrypoint] 数据库已存在 : ${DB_SIZE} 字节（配置会保留）"
else
  echo "[entrypoint] 数据库不存在 : 将新建空库"
  echo "[entrypoint] !! 如果你之前保存过配置，请确认启动命令包含"
  echo "[entrypoint] !!   -v /你的目录:/app/data"
  echo "[entrypoint] !! 否则容器重建后配置会全部丢失"
fi
# 挂载卷可写性
if [ ! -w "${DATA_DIR}" ]; then
  echo "[entrypoint] !! 数据目录不可写 (${DATA_DIR})，配置将无法保存" >&2
fi

echo "[entrypoint] starting  : $*"
echo "[entrypoint] ===== exec ===== "

exec "$@"
