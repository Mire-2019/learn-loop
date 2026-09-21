# learn-loop

**让 agent 学会一个领域的最小闭环**：资料摄入 → 可核对笔记 → 跨条目综合考题 → 判据机检 → 判据写回技能。

一句话：**笔记好看不等于学会；能被机器判对不对，才算学会。**

---

## 为什么要做这个（三个真实痛点）

1. **模型看视频链接只拿到 3 帧。** 无论你说“仔细看这段视频”，模型通常只拿到平台给的三五张
   关键帧，而且**帧上没有时间信息** —— 于是它写“视频第 12 秒讲了 X”时，那个 12 秒是编的。
   本项目的做法：抽帧时用 ffmpeg `drawtext` 把 **真实 PTS 烧进像素**
   （`text='%{pts\:hms}'`），再把帧拼成 `cols×rows` 接触表。时间戳就在画面上，谁也赖不掉。

2. **字幕经常错配。** 时间轴偏移、抽错语言轨、只有开头有字幕……这些都不是“内容问题”，
   而是**物理问题**，可以机检：句数、首末点、覆盖率、有没有 >20s 跳段。
   覆盖率 >105%（字幕比视频还长）或 <50%（大片空白）或存在跳段 ⇒ **判可疑并非零退出**，
   而不是继续拿它编笔记。

3. **笔记漂亮 ≠ 学会。** 把资料改写成通顺的 Markdown 是最容易的一步，也是最没价值的一步。
   所以这里的每条结论都必须带**可解析的引用**（视频→时间戳+帧文件；文档→行号），
   而且有判据机检兜底：引用不存在的 ID、指向不存在的文件、出现禁用词、覆盖率报告缺失，一律退出码非零。
   再往后，还要能出**跨条目综合题**，用 `--recheck` 防回归（上次通过的题现在失败会显式报 `REGRESSION`）。

---

## 安装 / 依赖

- Python ≥ 3.10，**运行时零第三方依赖**（只用标准库）
- 外部命令：`ffmpeg` / `ffprobe`（视频抽帧与接触表必需）；`pdftotext`（可选，只有读 PDF 时才需要，缺了会**如实提示并跳过**）
- 模型调用是可选的：不设密钥时自动进入 `--mock` 模式，用确定性假响应跑通全流程

```bash
git clone <this-repo> && cd learn-loop
./bin/learn-loop --version      # 直接可用，不需要 pip install
python3 -m unittest discover -s tests -t . -v   # 自测（32 个用例；无 ffmpeg / 无 demo.mp4 时相关用例自动 skip）
```

真实模型模式（可选，任何 OpenAI 兼容端点）：

```bash
export OPENAI_BASE_URL="https://api.openai.com/v1"
export OPENAI_API_KEY="sk-..."
export MODEL="gpt-4o-mini"
```

> 不设 `OPENAI_API_KEY` 或显式加 `--mock` ⇒ 确定性 mock；`LEARN_LOOP_MOCK=1` 也能强制 mock。

---

## 三条命令

### 1. 一条命令跑完整闭环

```bash
# 先造一段 30 秒测试视频（testsrc 画面 + 正弦音轨）
bash tests/make_demo_video.sh

./bin/learn-loop run tests/demo.mp4 tests/demo.srt tests/doc_notes.md \
    --mock --out /tmp/ll-demo
```

`run` = `ingest` → `notes` → `exam` → `exam --recheck` → `gate`，最后打印每个阶段的退出码。

### 2. 判据机检（可单独跑，退出码就是结论）

```bash
./bin/learn-loop gate --out /tmp/ll-demo ; echo "exit=$?"
```

### 3. 把学到的判据写回技能文件（写入后回读逐字节比对）

```bash
./bin/learn-loop evolve --target SKILL.md --append-file my_new_criteria.md ; echo "exit=$?"
```

其它子命令：`ingest` / `notes` / `exam`（`--quiz` 只出题不给答案、`--recheck` 复跑旧题）。
完整演示见 `examples/run_demo.sh`。

---

## 产物长什么样

```
/tmp/ll-demo/
├── evidence/index.json          # 证据索引：每条都有 id / 定位符 / 产物文件路径
├── frames/demo.mp4/f-000001.jpg # 抽出的帧，左上角烧着真实时间戳（00:00:00.000 …）
├── frames/demo.mp4/index.json   # 帧号 ↔ 时间 ↔ 接触表格子 的映射
├── sheets/demo.mp4-sheet-01.jpg # 3×3 接触表（九宫格）
├── subtitles/demo.srt.checks.json  # 字幕物理体检：句数/首末点/覆盖率/跳段/可疑
├── docs/doc_notes.md.txt        # 文档抽出的纯文本（引用行号指向它）
├── notes.md / notes.json        # 可核对笔记（每条 = 结论 + 证据引用 + 置信度）
├── exam.jsonl                   # 题目（含覆盖范围，**不含答案**）
├── exam_key.jsonl               # 参考答案 + sha256（与题目分开存）
├── exam.md / exam_key.md        # 人看的版本；--quiz 只打印题目
├── exam_recheck.json            # 最近一次复跑结果（含 REGRESSION 列表）
├── gate/report.json|report.md   # 判据机检报告
└── ledger.jsonl                 # 全流程台账（ingest/notes/exam/gate 事件）
```

`notes.md` 每条的形态：

```markdown
## 结论 n001 — 视频 demo.mp4 时长 30.000s，共抽出 9 帧、拼成 1 张 3×3 接触表…

- **结论**：视频 demo.mp4 时长 30.000s，共抽出 9 帧、拼成 1 张 3×3 接触表；时间戳已烧进像素，可逐格核对。
- **置信度**：high
- **为什么**：ffprobe 时长 + ffmpeg 产物文件计数，均可机检
- **证据引用**：
  - `ev:r:demo.mp4:frames` @ 9 frames / 1 sheet(s)  → frames/demo.mp4/index.json
  - `ev:sh:demo.mp4:001` @ sheet 1 00:00:00.000→00:00:26.667  → sheets/demo.mp4-sheet-01.jpg
- **来源**：demo.mp4
```

判据机检的 8 条规则（全部可机检，不依赖模型）：

| 规则 | 内容 |
|---|---|
| R1 | 笔记存在且非空 |
| R2 | 每条结论都有引用 |
| R3 | 每条引用都能在 `evidence/index.json` 里解析到 |
| R4 | 引用指向的文件真实存在；视频引用的时间戳落在时长范围内 |
| R5 | 笔记里不出现禁用词（默认 10 个，见 `learn_loop/rules.py`，可用 `--rules` / `--add-forbidden` 扩展） |
| R6 | 每个字幕源都有覆盖率报告，且没被判可疑 |
| R7 | 考题跨条目（每题 ≥2 条结论、≥2 个来源）、引用有效、参考答案哈希自洽 |
| R8 | `notes.md` 与 `notes.json` 结论条数一致 |

退出码一览：`0` 成功 ｜ `1` 判据失败 / 复跑出现 REGRESSION ｜ `2` 字幕被判可疑 ｜
`3` 写回回读不一致并已回滚 ｜ `5` 模型调用失败（**不会产出任何伪造内容**，提示改用 `--mock`）。

---

## 局限（如实说明）

- **mock 模式不会假装看懂画面。** 无密钥时产出的是**结构性结论**（时长、帧数、行号、逐字引用、
  物理体检结果），并显式用 `confidence: low` 标注“画面语义未判断”。语义结论需要真实模型或人工看图。
- **真实模型路径已实现，但只验证到“失败要显式报错”这一层**（本机没有可用密钥）：`--mock` 之外
  的分支会走 `urllib` 调 `{OPENAI_BASE_URL}/chat/completions`，校验模型给出的引用 ID（不存在的
  引用会被丢弃并计入 `notes.json` 的 `warnings`）；端点不可达时干净退出码 `5`，绝不伪造笔记
  （`tests/test_learn_loop.py::RealModelPathTest` 覆盖了这两点）。
  **未验证的是**“拿到真实密钥后模型返回的笔记质量”，请第一次使用时留意 `notes.json`。
- **PDF 需要 `pdftotext`**（poppler-utils）。没有它就直接跳过并提示，不做“猜内容”式解析。
- **抽帧时间戳有毫秒级对齐误差**：`-copyts -ss T` 抽到的是 **≥T 的那一帧**，所以请求 3.333s
  可能烧出 `00:00:03.360`。索引里记录的是**实际烧入的时间戳**，引用以它为准。
- **覆盖率用「字幕时长求和 / 视频时长」**，不做并集。这意味着时间轴重叠会把覆盖率推高到 >105%，
  这是**有意为之**：叠句和错配正是要抓的异常。
- **`[mm:ss] 文本` 格式没有结束时间**，末点由下一条起点推断（末条兜底 +2s），
  因此这条格式的覆盖率天然偏保守。
- **`evolve` 的回读比对是“写完再读回校验”，不是原子写**。它能抓住“落盘内容与预期不一致”
  并回滚，但如果磁盘在回滚时也坏了，就只剩 `.bak` 可用。
- **`--inject-mismatch` 是自检开关**，专门用来演练回滚路径（会让本次写回必定失败并还原）。
- **接触表末格不足会用重复帧补齐**，并在告警里说明（不会静默凑数）。
