#!/usr/bin/env bash
# 生成验收用的 30 秒测试视频（testsrc 画面 + 正弦音轨），已存在则跳过。
set -euo pipefail

HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
OUT="${1:-$HERE/demo.mp4}"

if [ -f "$OUT" ] && [ "${FORCE:-0}" != "1" ]; then
  echo "已存在: $OUT（要重新生成请 FORCE=1 $0）"
  ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$OUT"
  exit 0
fi

command -v ffmpeg >/dev/null 2>&1 || { echo "需要 ffmpeg" >&2; exit 127; }
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc=size=640x360:rate=25:duration=30" \
  -f lavfi -i "sine=frequency=440:duration=30" \
  -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest \
  "$OUT"

echo "已生成: $OUT"
ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$OUT"
