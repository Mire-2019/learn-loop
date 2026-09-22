#!/usr/bin/env python3
"""cite_audit.py —— 让 N 个模型读同一份材料，然后**逐字核验它们的引用**

为什么要这个东西
    大模型交回来的笔记，看起来永远是对的：结构清楚、术语对齐、还带着引号。
    问题在于引号里的那句话，原文里常常根本没有——这是幻觉最朴素、也最容易被放过的一种形态。
    本脚本把"看起来对"换成"能不能核对"：模型给出带原文片段的笔记，机器去源文件里做**逐字检索**。

判据（三档，机械执行，不看模型脸色）
    逐字：源的归一化文本里能直接找到（归一化只抹掉空白与 markdown 强调符 `**` `` ` `` `#`）
    省略：整句找不到，但拆成片段后在源里**按顺序**都能找到（模型抄漏了中间一句，属有据）
    编造：有片段在源里找不到 → 判编造

用法
    # 1) 准备材料：一个目录，放 .md / .txt / .json 都行
    # 2) 起跑（读 OPENAI_BASE_URL / OPENAI_API_KEY，或 --base/--key 显式传）
    python cite_audit.py --source ./材料目录 --models "model-a,model-b" --out result.json
    # 3) 只看某几家的原始输出与判定
    python cite_audit.py --source ./材料目录 --show result.json --model model-a

输出
    result.json：{"models": {"<模型>": {"n": 笔记条数, "rate": 逐字率, "elide": 省略数,
                                     "fake": 编造数, "verdicts": [...], "raw": "..."}}}
    同目录 result_v2.json 为用三档判据重算后的结果（同一批原始输出，不重新调模型）。

注意
    材料最好是**新造的**（模型训练数据里不可能有）：否则它可以靠记忆答题，测的就不是"有没有读"。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import json
import os
import re
import subprocess
import sys
import time

PROMPT = """你正在做一份**可核验**的读书笔记。材料在下面的 <材料> 里。

要求（违反任何一条，这份笔记就是废的）：
1. 输出 **JSON 数组**，每个元素形如 {{"point": "你的结论", "quote": "支撑它的原文片段", "file": "文件名"}}
2. `quote` 必须是**原文里连续存在的一段文字**（可以略掉中间一句，但不许改写、不许同义替换、不许加省略号糊弄）
3. 写 8~15 条。宁可少写，也不许编。
4. 只输出 JSON，不要解释，不要 markdown 代码块围栏。

<材料>
{src}
</材料>
"""


def norm(s: str) -> str:
    """归一化：只允许空白差异与 markdown 强调符，**不允许**改字/同义替换。"""
    s = re.sub(r"[\s\u3000]+", "", s or "")
    return re.sub(r"[*`#>_]", "", s)


def read_source(path: str) -> tuple[str, int]:
    files = sorted(glob.glob(os.path.join(path, "**", "*"), recursive=True))
    files = [p for p in files if os.path.isfile(p) and not p.endswith(".json.bak")]
    chunks, total = [], 0
    for p in files:
        try:
            txt = open(p, encoding="utf-8").read()
        except UnicodeDecodeError:
            continue
        total += len(txt)
        chunks.append(f"### 文件：{os.path.basename(p)}\n{txt}")
    return "\n\n".join(chunks), total


def verify_quote(quote: str, src: str) -> str:
    """返回 逐字 / 省略 / 编造。"""
    q = norm(quote)
    if not q:
        return "编造"
    if q in src:
        return "逐字"
    parts = [p for p in re.split(r"[，。、；：,.;:!?？！\n]", norm(quote)) if len(p) >= 6]
    if parts:
        pos, ok = 0, True
        for p in parts:
            idx = src.find(p, pos)
            if idx < 0:
                ok = False
                break
            pos = idx + len(p)
        if ok:
            return "省略"
    return "编造"


def call(base: str, key: str, model: str, prompt: str, max_tokens: int = 8192, timeout: int = 300) -> dict:
    """走 curl：urllib 的 timeout 挡不住"对端挂着不发完"，而 curl --max-time 能硬断。"""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": max_tokens, "temperature": 0.2}, ensure_ascii=False)
    r = subprocess.run(["curl", "-sS", "--max-time", str(timeout), "-X", "POST",
                        base.rstrip("/") + "/chat/completions",
                        "-H", "Authorization: Bearer " + key,
                        "-H", "Content-Type: application/json",
                        "--data-binary", "@-"],
                       input=body.encode(), capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"curl rc={r.returncode}: {r.stderr.decode()[:160]}")
    return json.loads(r.stdout.decode("utf-8", "replace"))


def parse_notes(text: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M)
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [x for x in arr if isinstance(x, dict) and x.get("quote")]


def audit_one(base, key, model, prompt, src, max_tokens, timeout) -> dict:
    t0 = time.time()
    try:
        resp = call(base, key, model, prompt, max_tokens, timeout)
    except Exception as e:                                    # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"[:200], "secs": round(time.time() - t0, 1)}
    if "error" in resp:
        return {"error": str(resp["error"])[:200], "secs": round(time.time() - t0, 1)}
    ch = (resp.get("choices") or [{}])[0]
    text = (ch.get("message") or {}).get("content") or ""
    notes = parse_notes(text)
    verdicts = [{"point": n.get("point", "")[:160], "quote": n.get("quote", "")[:400],
                 "file": n.get("file", ""), "verdict": verify_quote(n.get("quote", ""), src)} for n in notes]
    n_verb = sum(1 for v in verdicts if v["verdict"] == "逐字")
    n_elid = sum(1 for v in verdicts if v["verdict"] == "省略")
    n_fake = sum(1 for v in verdicts if v["verdict"] == "编造")
    return {"n": len(notes), "verbatim": n_verb, "elide": n_elid, "fake": n_fake,
            "rate": round((n_verb + n_elid) / len(notes), 3) if notes else None,
            "finish": ch.get("finish_reason"), "verdicts": verdicts,
            "raw": text[:40000], "secs": round(time.time() - t0, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="", help="材料目录（跑新审计时必填）")
    ap.add_argument("--models", default="", help="逗号分隔的模型名（跑新审计时必填）")
    ap.add_argument("--base", default=os.environ.get("OPENAI_BASE_URL", ""))
    ap.add_argument("--key", default=os.environ.get("OPENAI_API_KEY", ""))
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="result.json")
    ap.add_argument("--show", default="", help="已有结果文件（只读，用来回看判定）")
    ap.add_argument("--model", default="", help="配合 --show：只列这个模型的逐条判定")
    ap.add_argument("--table", action="store_true", help="配合 --show：打印总表")
    a = ap.parse_args()

    # 只读模式：不回读材料、不联网，直接看已存结果
    if a.show:
        data = json.load(open(a.show, encoding="utf-8"))
        models = data.get("models") or {}
        if a.table:
            rows = [(m, v) for m, v in models.items() if v.get("n")]
            rows.sort(key=lambda kv: (-(kv[1].get("rate") or 0), -kv[1]["n"]))
            print(f"{'模型':<32} {'笔记':>4} {'逐字':>4} {'省略':>4} {'编造':>4}  有据率")
            for m, v in rows:
                t = v.get("tally") or {}
                print(f"{m:<32} {v['n']:>4} {t.get('逐字', 0):>4} {t.get('省略', 0):>4} "
                      f"{t.get('编造', v.get('fake', 0)):>4}  {round((v.get('rate') or 0) * 100)}%")
            for m, v in models.items():
                if not v.get("n"):
                    print(f"{m:<32}    ✗ {str(v.get('error') or v.get('note') or '未产出笔记')[:70]}")
            return 0
        if not a.model:
            print("--show 需要配 --model 或 --table", file=sys.stderr)
            return 2
        v = models.get(a.model) or {}
        if not v:
            print(f"结果里没有 {a.model}", file=sys.stderr)
            return 2
        nts = v.get("notes") or v.get("verdicts") or []
        print(f"{a.model}：{v.get('n')} 条笔记，有据率 {v.get('rate')}")
        for d in nts:
            mark = {"逐字": "√", "省略": "≈", "编造": "✗"}.get(d.get("verdict"), "?")
            print(f"  [{mark}] {d.get('verdict')}  {d.get('file', '')}")
            print(f"      它写的：{(d.get('quote') or '')[:150]}")
        return 0

    if not os.path.isdir(a.source):
        print("需要 --source（材料目录）", file=sys.stderr)
        return 2
    src_text, total = read_source(a.source)
    src_norm = norm(src_text)
    print(f"材料：{total} 字（归一化后 {len(src_norm)} 字）", flush=True)

    if not (a.base and a.key and a.models):
        print("需要 --base/--key/--models（或用 OPENAI_BASE_URL / OPENAI_API_KEY 环境变量）", file=sys.stderr)
        return 2

    prompt = PROMPT.replace("{src}", src_text[:120000])
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    out = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "source_chars": total, "models": {}}
    print(f"起跑：{len(models)} 个模型，并发 {a.workers}，max_tokens={a.max_tokens}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(audit_one, a.base, a.key, m, prompt, src_norm, a.max_tokens, a.timeout): m
                for m in models}
        for fu in concurrent.futures.as_completed(futs):
            m = futs[fu]
            r = fu.result()
            out["models"][m] = r
            if r.get("error"):
                print(f"  {m:<32} ✗ {r['error']}", flush=True)
            else:
                print(f"  {m:<32} 笔记 {r['n']:>2} 有据率 {r['rate']} 编造 {r['fake']} {r['secs']}s",
                      flush=True)
            json.dump(out, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"→ {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
