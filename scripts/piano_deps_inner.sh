#!/usr/bin/env bash
# 在 MaiBot 容器内运行：把钢琴运行时依赖固化到持久化卷 /MaiMBot/depends-data
# 用法: piano_deps_inner.sh vendor|check
# 说明: depends-data 是 bind 挂载（宿主 ./depends-data），镜像重建不会丢。
set -uo pipefail

RT=/MaiMBot/depends-data/piano-runtime
SITE=/MaiMBot/.venv/lib/python3.13/site-packages
SF=/usr/share/sounds/sf2/TimGM6mb.sf2
SF_NAME=TimGM6mb.sf2

vendor() {
  mkdir -p "$RT/bin" "$RT/lib" "$RT/pylibs" "$RT/soundfonts"

  if [ -x /usr/bin/fluidsynth ]; then
    cp -f /usr/bin/fluidsynth "$RT/bin/"
    chmod +x "$RT/bin/fluidsynth"
  else
    echo "WARN: 系统没有 /usr/bin/fluidsynth，无法归档二进制"
  fi

  # 动态库闭包：排除 glibc 核心，避免与未来镜像的 glibc 混用
  kept=0
  while read -r so; do
    [ -f "$so" ] || continue
    case "$so" in
      */ld-linux*|*/libc.so*|*/libm.so*|*/libdl.so*|*/libpthread.so*|\
      */librt.so*|*/libresolv.so*|*/libutil.so*|*/libnsl.so*) continue ;;
    esac
    cp -f "$so" "$RT/lib/" 2>/dev/null && kept=$((kept + 1))
  done < <(ldd /usr/bin/fluidsynth 2>/dev/null | awk '/=>/ {print $3}')

  if [ -f "$SF" ]; then
    cp -f "$SF" "$RT/soundfonts/$SF_NAME"
  else
    echo "WARN: 找不到 $SF"
  fi

  # mido 是纯 Python，连 typing_extensions 一起带上
  rm -rf "$RT/pylibs/mido" "$RT/pylibs"/mido-*.dist-info
  [ -d "$SITE/mido" ] && cp -a "$SITE/mido" "$RT/pylibs/"
  cp -a "$SITE"/mido-*.dist-info "$RT/pylibs/" 2>/dev/null
  [ -f "$SITE/typing_extensions.py" ] && cp -a "$SITE/typing_extensions.py" "$RT/pylibs/"
  cp -a "$SITE"/typing_extensions-*.dist-info "$RT/pylibs/" 2>/dev/null

  echo "vendored: bin=1 lib=${kept} soundfont=$([ -f "$RT/soundfonts/$SF_NAME" ] && echo yes || echo no)"
  du -sh "$RT" 2>/dev/null
}

check() {
  local rc=0
  echo "persistent runtime: $RT"
  [ -f "$RT/bin/fluidsynth" ] && echo "  [OK]   fluidsynth binary" \
    || { echo "  [MISS] fluidsynth binary"; rc=1; }
  [ -f "$RT/lib/libfluidsynth.so.3" ] && echo "  [OK]   动态库闭包 ($(ls "$RT/lib" | wc -l) 个)" \
    || echo "  [WARN] 未找到 libfluidsynth.so.3"
  [ -f "$RT/soundfonts/$SF_NAME" ] && echo "  [OK]   SoundFont" \
    || { echo "  [MISS] SoundFont"; rc=1; }

  # 用干净环境跑一次，确认不依赖镜像内的库
  if [ -x "$RT/bin/fluidsynth" ]; then
    ver=$(env -i PATH=/usr/bin:/bin LD_LIBRARY_PATH="$RT/lib" \
          "$RT/bin/fluidsynth" --version 2>&1 | head -1)
    case "$ver" in
      *FluidSynth*) echo "  [OK]   独立运行: $ver" ;;
      *) echo "  [MISS] 独立运行失败: $ver"; rc=1 ;;
    esac
  fi

  # mido 从持久化路径导入
  if PYTHONPATH="$RT/pylibs" python3 -c 'import mido' 2>/dev/null; then
    echo "  [OK]   mido 可从持久化路径导入"
  else
    echo "  [MISS] mido 无法从 $RT/pylibs 导入"
    rc=1
  fi

  # 系统回退能力
  echo "  --- 系统回退 ---"
  echo "  [INFO] /usr/bin/fluidsynth: $([ -x /usr/bin/fluidsynth ] && echo 有 || echo 无)"
  echo "  [INFO] $SF: $([ -f "$SF" ] && echo 有 || echo 无)"
  echo "  [INFO] venv mido: $([ -d "$SITE/mido" ] && echo 有 || echo 无)"
  return $rc
}

case "${1:-check}" in
  vendor) vendor ;;
  check)  check ;;
  *) echo "用法: $0 vendor|check"; exit 2 ;;
esac
