---
name: learn-loop
description: Use when an agent must learn a domain from real sources. Ingests video/subs/docs into cited evidence, writes verifiable notes, generates cross-item exams, machine-gates the output, and writes proven rules back to a skill file.
license: MIT
---

# learn-loop：让 agent 学会一个领域的最小闭环

## 什么时候用

- 要让 agent（或自己）**真正掌握**一份资料，而不是产出一篇读起来很顺的摘要。
- 手上是**多模态资料**：视频 + 字幕（可能是错的）+ 文档。
- 需要**可核查**：每条结论都能落到时间戳/帧文件/行号。
- 需要**防回归**：上次学会的东西，这次不能被悄悄改坏。

## 核心原则（写回技能的那几条判据）

1. **证据先行**：先切证据（视频抽帧、字幕分句、文档分行），再写结论。
   证据索引里每条都有 `id / 定位符 / 产物文件路径`。
2. **时间戳必须烧进像素**：`drawtext=text='%{pts\:hms}'`。帧上没时间戳，一切时间引用都不可信。
3. **字幕先做物理体检**：句数、首末点、覆盖率、跳段。
   覆盖率 >105% 或 <50% 或存在 >20s 跳段 ⇒ 判可疑并**非零退出**，不要拿它编笔记。
4. **每条结论带引用 + 置信度**：引用必须能解析到证据索引里真实存在的条目，且产物文件真实存在。
5. **判据机检优先于人眼**：能机检的规则绝不靠“感觉不错”。
6. **写回技能必须回读逐字节比对**：不一致就回滚，并保留 `.bak`。
7. **综合考题跨条目**：每题覆盖 ≥2 条结论、≥2 个来源；题目与答案分开存；`--recheck` 报 `REGRESSION`。

## 快速用法

```bash
bash tests/make_demo_video.sh                    # 造 30s 测试视频
./bin/learn-loop run tests/demo.mp4 tests/demo.srt tests/doc_notes.md \
    --mock --out /tmp/ll-demo                    # 全流程，无需密钥
./bin/learn-loop gate --out /tmp/ll-demo ; echo "exit=$?"
./bin/learn-loop evolve --target SKILL.md --append-file criteria.md
```

## 陷阱

- `-copyts -ss T` 抽到的是 **≥T 的第一帧**；索引记录的是实际烧入的时间戳（可能比请求值大几十毫秒）。
- 没有 `pdftotext` 时 PDF 会被**跳过并提示**，不要假设它有文本。
- 无 `OPENAI_API_KEY` 时自动进入确定性 mock：只会产出结构性结论，不会假装看懂画面。
- `evolve` 遇到标记 `learn-loop:criteria v1`（HTML 注释形式，见 `learn_loop/evolve.py` 的 `MARKER`）已存在会跳过（幂等），不要靠重复执行来“追加更多”。
