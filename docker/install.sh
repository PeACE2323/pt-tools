#!/usr/bin/env sh
# =============================================================================
# 本地（非 Docker）安装脚本：创建虚拟环境、装依赖、编译 proto
# =============================================================================
set -eu

cd "$(dirname "$0")"

echo "==> 1/4 创建虚拟环境 .venv"
python3 -m venv .venv
. .venv/bin/activate

echo "==> 2/4 安装依赖"
pip install --upgrade pip
pip install -r requirements.txt

echo "==> 3/4 拉取并编译 clouddrive.proto"
python tools/fetch_proto.py --out app/proto || {
  echo "官方 proto 拉取失败，改用内置副本编译"
  python tools/fetch_proto.py --out app/proto --offline
}

echo "==> 4/4 静态自检"
python tools/check_proto.py app/proto/clouddrive.proto

mkdir -p data
echo
echo "安装完成。启动命令："
echo "  . .venv/bin/activate"
echo "  uvicorn app.main:app --host 0.0.0.0 --port 8899"
