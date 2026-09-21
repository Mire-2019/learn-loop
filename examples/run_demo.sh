#!/usr/bin/env bash
# learn-loop 端到端演示：视频 + SRT 字幕 + [mm:ss] 字幕 + 文档，全程 mock，不需要任何密钥。
#
#   bash examples/run_demo.sh [产物目录]
#
# 产物目录默认 ./out-demo（在仓库里，已 gitignore）。
set -euo pipefail

HERE="$(cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(cd -- "$HERE/.." && pwd)"
OUT="${1:-$ROOT/out-demo}"
CLI="$ROOT/bin/learn-loop"

chmod +x "$CLI" 2>/dev/null || true
bash "$ROOT/tests/make_demo_video.sh" "$ROOT/tests/demo.mp4" >/dev/null

rm -rf "$OUT"
echo "### 1) 一条命令跑完整闭环（ingest → notes → exam → recheck → gate）"
"$CLI" run \
  "$ROOT/tests/demo.mp4" \
  "$ROOT/tests/demo.srt" \
  "$ROOT/tests/demo_bracket.txt" \
  "$ROOT/tests/doc_notes.md" \
  --mock --out "$OUT"

echo
echo "### 2) 产物清单"
find "$OUT" -type f | sort | sed "s|$OUT/||"

echo
echo "### 3) 单独跑一次判据机检（退出码）"
set +e
"$CLI" gate --out "$OUT" >/dev/null 2>&1
echo "gate 退出码 = $?"
set -e

echo
echo "### 4) 故意破坏一条结论的引用，再跑判据机检"
cp "$OUT/notes.json" "$OUT/notes.json.good"
python3 - "$OUT" <<'PY'
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
obj = json.loads((out / "notes.json").read_text(encoding="utf-8"))
obj["items"][0]["citations"] = ["ev:does-not-exist:v1"]
(out / "notes.json").write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"已把 {obj['items'][0]['id']} 的引用改成不存在的 ev:does-not-exist:v1")
PY
set +e
"$CLI" gate --out "$OUT" 2>&1 | grep -E "^  [✓✗]|^      · (无法解析|例如)|^\[gate\]"
set -e

echo
echo "### 5) 还原被破坏的引用，再跑一次（退出码应回到 0）"
mv "$OUT/notes.json.good" "$OUT/notes.json"
set +e
"$CLI" gate --out "$OUT" --quiet
code=$?
set -e
echo "gate 退出码 = $code"

echo
echo "### 6) 把新学到的判据写回技能文件（写入后回读逐字节比对）"
FRAG=$(mktemp)
cat > "$FRAG" <<'MD'
- 新判据：视频引用必须落在视频时长内，且帧文件真实存在。
- 新判据：字幕覆盖率 >105% / <50% / 存在 >20s 跳段时，不得用该字幕写笔记。
- 新判据：综合题必须跨条目，每题覆盖 ≥2 条结论、≥2 个来源。
MD
cp "$ROOT/SKILL.md" "$OUT/SKILL.md"
cp "$ROOT/SKILL.md" "$OUT/SKILL.orig.md"          # 为第 7 步的回滚校验留一份原件
"$CLI" evolve --target "$OUT/SKILL.md" --append-file "$FRAG"
echo "写入后与原件差异（应只有追加部分）:"
diff <(cat "$OUT/SKILL.orig.md") <(cat "$OUT/SKILL.md") | head -20 || true

echo
echo "### 7) 回滚演练（--inject-mismatch：故意让回读不一致）"
cp "$OUT/SKILL.orig.md" "$OUT/SKILL4.md"
sha_before=$(sha256sum "$OUT/SKILL4.md" | cut -d' ' -f1)
set +e
"$CLI" evolve --target "$OUT/SKILL4.md" --append "- 这条会被回滚。" --inject-mismatch
evolve_code=$?
set -e
echo "evolve 退出码 = $evolve_code"
sha_after=$(sha256sum "$OUT/SKILL4.md" | cut -d' ' -f1)
echo "回滚前 sha256 = $sha_before"
echo "回滚后 sha256 = $sha_after"
if [ "$sha_before" = "$sha_after" ]; then echo "✓ 回滚后与回滚前逐字节一致"; else echo "✗ 回滚校验失败"; fi
cmp "$OUT/SKILL4.md" "$OUT/SKILL.orig.md" && echo "✓ cmp：回滚后的文件与原件逐字节相同"

echo
echo "演示结束。产物在 $OUT"

