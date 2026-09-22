# 模型引用审计：让大模型读同一份材料，再逐字核验它写的引用

> 一句话：**看得懂**和**真读过**不是一回事。这个工具把"看起来对"换成"能不能核对"。

## 它解决什么

大模型交回来的笔记永远好看：结构清楚、术语对齐、还带引号。
问题在引号里那句话——去原文里搜，常常根本没有。
这是幻觉最朴素、也最容易被放过的一种形态，因为**没有人会去逐字搜**。

`learn_loop/cite_audit.py` 干的事：给 N 个模型同一份材料，要求它们输出带原文片段的 JSON 笔记，
然后由**机器**去源文件里做逐字检索——不看模型脸色，也不看作者脸色。

## 判据（三档，机械执行）

| 判定 | 含义 |
|---|---|
| **逐字** | 源文本（归一化后）里能直接找到。归一化只抹掉空白与 markdown 强调符 `**` `` ` `` `#` |
| **省略** | 整句找不到，但拆成片段后在源里**按顺序**都能找到 → 模型抄漏了中间一句，仍算有据 |
| **编造** | 有片段在源里找不到 → 判编造 |

**有据率 = (逐字 + 省略) / 笔记条数**。

为什么要有"省略"这一档：第一版判据把省略也算编造，结果冤枉了一批模型。
判据的公信力比结论值钱——所以宁可多一档，也不拿判据去冤枉人。

## 本轮结果（2026-09-22，材料 6 份 / 15,058 字，全部当天新造）

第一轮，每模型 `max_tokens=8192`：

| 模型 | 笔记 | 逐字 | 省略 | 编造 | 有据率 |
|---|---|---|---|---|---|
| `kimi-k2.6` | 15 | 15 | 0 | 0 | 100% |
| `qwen-plus-latest` | 12 | 12 | 0 | 0 | 100% |
| `qwen3-next-80b-a3b-instruct` | 12 | 12 | 0 | 0 | 100% |
| `qwen3-coder-plus` | 8 | 7 | 1 | 0 | 100% |
| `glm-5` | 8 | 8 | 0 | 0 | 100% |
| `qwen3-30b-a3b-instruct-2507` | 8 | 8 | 0 | 0 | 100% |
| `deepseek-v3.2` | 12 | 11 | 0 | 1 | 92% |
| `kimi-k2-thinking` | 16 | 13 | 1 | 2 | 88% |
| `qwen3-32b` | 8 | 6 | 1 | 1 | 88% |
| `deepseek-v4-pro` | 14 | 12 | 0 | 2 | 86% |
| `deepseek-r1` | 10 | 5 | 0 | 5 | 50% |

没交卷的 9 个（**是额度/开通/超时，不是判它们不行**）：`deepseek-v4.1-flash`、`glm-5.3`（8192 token
全烧在推理上，没吐出笔记）、`qwen3-max`、`qwen3-235b-a22b`（免费额度耗尽 403）、`kimi-k3`（超时）、
`ZHIPU/GLM-5.3-FlashX`、`MiniMax/MiniMax-M2.7`、`MiniMax/MiniMax-M3`、`xiaomi/mimo-v2.5-pro`（未开通）。

第二轮把预算从 8192 提到 32768 重跑四个模型（`data/audit-2026-09-22/round2-32k-budget.json`）：

- `deepseek-v4.1-flash`：**15 条笔记，有据率 100%** —— 它不是不会，是 8192 的预算不够它想完再写。
- `glm-5.3`：仍未产出笔记；`kimi-k3`：仍超时；`qwen3-max`：额度仍耗尽。

> ⚠️ 局限：单轮单次测试，材料是六份中文技术文档。**不构成对任何模型通用能力的排名。**
> 预算、语言、材料长度任一变化，结果都可能变。稳的是判据本身：引用在不在原文里，只有两种答案。

## 怎么用（四条命令）

```bash
git clone https://github.com/Mire-2019/learn-loop && cd learn-loop

# 1) 只看这一轮的真数据（不需要任何 key）
python -m learn_loop.cite_audit --source data/audit-2026-09-22/source \
       --show data/audit-2026-09-22/round1-8k-budget.json --model deepseek-r1

# 2) 看总表
python -m learn_loop.cite_audit --source data/audit-2026-09-22/source \
       --table --show data/audit-2026-09-22/round1-8k-budget.json

# 3) 拿自己的材料、自己的模型跑一轮（需要 OpenAI 兼容端点）
export OPENAI_BASE_URL=https://your-endpoint/v1
export OPENAI_API_KEY=sk-...
python -m learn_loop.cite_audit --source ./我的材料 --models "model-a,model-b" --out my.json

# 4) 看某一家的逐条判定（√ 逐字 / ≈ 省略 / ✗ 编造）
python -m learn_loop.cite_audit --source ./我的材料 --show my.json --model model-a
```

材料最好是**新造的**：如果它出现在训练数据里，模型可以靠记忆答题，那就测不出"有没有读"。

## 数据说明

`data/audit-2026-09-22/` 里是**本轮全部真实产物**，未做删改（仅脱敏内部地址）：

- `source/` —— 当轮使用的 6 份材料
- `round1-8k-budget.json` / `round2-32k-budget.json` —— 每个模型的笔记、逐条判定、token 用量与原始输出

想自己核对？`jq '.models."deepseek-r1".notes' data/audit-2026-09-22/round1-8k-budget.json`
里的每条 `quote`，都可以拿去 `grep` 源文件——这就是这套判据的全部秘密。
