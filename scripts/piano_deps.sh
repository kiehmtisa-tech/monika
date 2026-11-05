#!/usr/bin/env bash
# 钢琴运行时依赖管理（在阿里云宿主机执行）
#
#   ./piano_deps.sh check    自检：持久化依赖是否齐全且可用
#   ./piano_deps.sh vendor   固化：把 fluidsynth + 动态库 + SoundFont + mido
#                            搬进 ./depends-data/piano-runtime（镜像重建不丢）
#   ./piano_deps.sh restore  恢复：容器重建后若系统依赖缺失，先 apt 装再固化
#
# 设计说明：容器 /MaiBot/.venv 与 apt 安装的 fluidsynth 都在镜像层，
# 镜像更新即丢失；depends-data 是 bind 挂载，因此把运行时整体搬过去。
# 插件启动时会优先使用这里的副本，缺失时才回退系统安装。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

CONTAINER="${CONTAINER:-maim-bot-core}"
INNER="$(pwd)/scripts/piano_deps_inner.sh"

run_inner() {
  docker cp "$INNER" "$CONTAINER":/tmp/piano_deps_inner.sh >/dev/null
  docker exec "$CONTAINER" bash /tmp/piano_deps_inner.sh "$1"
}

case "${1:-check}" in
  check)
    echo "== 容器 $CONTAINER 的钢琴运行时自检 =="
    run_inner check
    echo
    echo "== 宿主持久化目录 =="
    du -sh ./depends-data/piano-runtime 2>/dev/null || echo "  (尚未 vendor)"
    ;;
  vendor)
    echo "== 归档运行时到 depends-data =="
    run_inner vendor
    echo
    run_inner check
    ;;
  restore)
    echo "== 容器重建后的依赖恢复 =="
    if ! docker exec "$CONTAINER" test -x /usr/bin/fluidsynth; then
      echo "-- 系统缺少 fluidsynth，尝试 apt 安装 --"
      docker exec "$CONTAINER" bash -c \
        'apt-get update -qq && apt-get install -y -qq --no-install-recommends \
         fluidsynth timgm6mb-soundfont libgomp1 libwayland-client0' \
        || echo "apt 安装失败，若已有 depends-data/piano-runtime 则仍可用"
    else
      echo "-- 系统 fluidsynth 已存在，跳过 apt --"
    fi
    run_inner vendor
    ;;
  *)
    echo "用法: $0 check|vendor|restore"
    exit 2
    ;;
esac
