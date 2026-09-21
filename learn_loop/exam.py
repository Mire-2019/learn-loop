"""跨条目综合考题。

- 每 N 条结论（默认 5 条）合成一道综合题，题目强制跨条目（每题覆盖 ≥2 条结论，尽量跨来源）。
- **题目与答案分开存**：exam.jsonl 里只有题目与覆盖范围；参考答案在 exam_key.jsonl（带 sha256）。
- `--recheck` 复跑旧题：用当前证据重新推导答案，与存档参考答案比对，结果写进 ledger。
  某题上次 pass、这次 fail ⇒ 显式报 **REGRESSION**，退出码非零（防回归）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import util
from .llm import LLM
from .notes import CONF_RANK, WEAK_KIND_BONUS, load_index
from .util import eprint, now_iso, write_json, write_text

EXAM_JSONL = "exam.jsonl"
KEY_JSONL = "exam_key.jsonl"


def _notes_by_id(out: Path) -> dict[str, dict]:
    obj = util.read_json(out / "notes.json", default=None)
    if not obj:
        raise SystemExit(f"[exam] 找不到 {out / 'notes.json'}，请先跑 notes")
    return {n["id"]: n for n in obj.get("items", [])}


def _weakness(note: dict, index: dict) -> tuple[int, str]:
    """证据强弱评分：置信度越低越弱；纯结构性引用额外 +1。"""
    score = CONF_RANK.get(note.get("confidence", "low"), 2)
    kinds = []
    for cid in note.get("citations", []):
        it = next((i for i in index["items"] if i["id"] == cid), None)
        if it:
            kinds.append(it["kind"])
    if kinds and all(WEAK_KIND_BONUS.get(k, 0) for k in kinds):
        score += 1
    why = f"置信度={note.get('confidence')}"
    if kinds and all(WEAK_KIND_BONUS.get(k, 0) for k in kinds):
        why += "，且引用全部是报告类条目（间接证据）"
    if not note.get("citations"):
        why = "无任何引用"
        score += 5
    return score, why


def build_question(qid: str, group: list[dict], sources: list[str]) -> str:
    lines = [f"【综合题 {qid}】覆盖 {len(group)} 条结论；涉及来源：{', '.join(sources)}", ""]
    lines.append("以下结论分别出自哪一条证据？逐条作答，要求：")
    lines.append("(1) 给出支撑该结论的证据 ID 与定位符；")
    lines.append("(2) 指出这一组里**证据最弱**的一条并说明理由；")
    lines.append("(3) 说明这组结论合起来能回答一个什么问题（一句话）。")
    lines.append("")
    for i, n in enumerate(group, 1):
        lines.append(f"{i}. {n['claim']}")
    return "\n".join(lines)


def derive_answer(qid: str, group: list[dict], index: dict) -> str:
    """由 notes.json + evidence/index.json 确定性推导参考答案（不受模型随机性影响）。"""
    by_id = {i["id"]: i for i in index["items"]}
    lines = [f"【{qid} 参考答案】（由 notes.json + evidence/index.json 确定性推导）", ""]
    for i, n in enumerate(group, 1):
        lines.append(f"{i}) {n['claim']}")
        if not n.get("citations"):
            lines.append("   证据: 无（本条应被判据机检打回）")
        for cid in n["citations"]:
            it = by_id.get(cid)
            if it is None:
                lines.append(f"   证据: {cid} —— 索引中不存在（引用无效）")
            else:
                lines.append(f"   证据: {cid} @ {it['locator']}"
                             f"（{it.get('artifact') or it.get('sheet') or '无产物文件'}）")
    scores = [(_weakness(n, index), n) for n in group]
    scores.sort(key=lambda t: (-t[0][0], t[1]["id"]))
    (score, why), weakest = scores[0]
    lines.append("")
    lines.append(f"最弱证据: {weakest['id']}（{why}）")
    srcs = sorted({s for n in group for s in n.get("sources", [])})
    lines.append(f"综合回答: 这组结论合起来刻画了 {'、'.join(srcs) or '该资料'} 的可核对状态，"
                 f"并标出了其中最需要人工或真实模型复核的一条。")
    return "\n".join(lines)


def generate(out: str | Path, every: int = 5, mock: bool = False, model: str | None = None,
             verbose: bool = True, force: bool = False) -> dict:
    out = Path(out)
    index = load_index(out)
    notes = list(_notes_by_id(out).values())
    if not notes:
        raise SystemExit("[exam] notes.json 里没有结论，无法出题")
    exam_path = out / EXAM_JSONL
    key_path = out / KEY_JSONL
    if exam_path.exists() and not force:
        prev = util.read_jsonl(exam_path)
        if prev and verbose:
            print(f"[exam] {exam_path.name} 已存在 {len(prev)} 道题；如需重出题请加 --force")
        return {"exit_code": 0, "questions": prev, "skipped": True}

    every = max(2, int(every))          # 至少 2 条才算“跨条目”
    groups = [notes[i:i + every] for i in range(0, len(notes), every)]
    if len(groups) > 1 and len(groups[-1]) < 2:
        # 末尾不足 2 条的尾巴并入上一组，保证每题都跨条目
        groups[-2].extend(groups.pop())

    llm = LLM(model=model, mock=mock)
    questions, keys = [], []
    for gi, group in enumerate(groups, 1):
        qid = f"q{gi:03d}"
        sources = sorted({s for n in group for s in n.get("sources", [])})
        question = build_question(qid, group, sources)
        answer = derive_answer(qid, group, index)
        if not llm.mock:
            # 真实模式下让模型补一段“综合回答”，但答案骨架仍由证据确定性推导
            user = (f"{question}\n\n可用的证据条目：\n" + "\n".join(
                f"{i['id']} | {i['kind']} | {i['locator']}" for i in index["items"][:200]) +
                f"\n\n确定性推导出的答案骨架（必须保持一致）：\n{answer}\n"
                "请只输出 JSON：{\"extra\":\"补充说明，不得引入新事实，200 字内\"}")
            try:
                obj = llm.chat_json("你只做证据归纳，禁止引入新事实。", user,
                                    mock_fn=lambda _u: {"extra": ""}, max_tokens=800)
                extra = str((obj or {}).get("extra", "")).strip()
                if extra:
                    answer += f"\n模型补充说明: {extra}"
            except Exception as exc:  # noqa: BLE001
                eprint(f"[exam] 警告: {qid} 模型补充失败，保留确定性答案: {exc}")
        questions.append({
            "id": qid, "question": question, "covers": [n["id"] for n in group],
            "sources": sources,
            "evidence_ids": sorted({c for n in group for c in n.get("citations", [])}),
            "created_at": now_iso(), "answer_ref": f"{KEY_JSONL}#{qid}",
            "examiner": llm.describe(),
        })
        keys.append({"id": qid, "answer": answer,
                     "answer_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest(),
                     "created_at": now_iso()})

    util.write_jsonl(exam_path, questions)
    util.write_jsonl(key_path, keys)
    write_text(out / "exam.md", "# 综合考题（不含答案）\n\n" +
               "\n\n---\n\n".join(q["question"] for q in questions) + "\n")
    write_text(out / "exam_key.md", "# 综合考题参考答案\n\n" +
               "\n\n---\n\n".join(f"## {k['id']}\n\n```\n{k['answer']}\n```" for k in keys) + "\n")
    util.append_jsonl(out / "ledger.jsonl", {
        "ts": now_iso(), "event": "exam_generate", "count": len(questions),
        "mode": "mock" if llm.mock else "model", "every": every,
    })
    if verbose:
        print(f"[exam] 出题 {len(questions)} 道（每 {every} 条结论一道，均跨条目）→ "
              f"{exam_path.name} / {key_path.name}")
        for q in questions:
            print(f"  · {q['id']}: 覆盖 {len(q['covers'])} 条结论，来源 {', '.join(q['sources'])}")
    return {"exit_code": 0, "questions": questions, "keys": keys, "skipped": False}


def recheck(out: str | Path, verbose: bool = True, limit: int | None = None) -> dict:
    """复跑旧题：重新推导答案并与存档参考答案比对，写 ledger；pass→fail 报 REGRESSION。"""
    out = Path(out)
    index = load_index(out)
    notes_map = _notes_by_id(out)
    questions = util.read_jsonl(out / EXAM_JSONL)
    keys = {k["id"]: k for k in util.read_jsonl(out / KEY_JSONL)}
    if not questions:
        raise SystemExit(f"[exam] {out / EXAM_JSONL} 为空，先跑 exam 出题")
    ledger = util.read_jsonl(out / "ledger.jsonl")
    last_status: dict[str, str] = {}
    for row in ledger:
        if row.get("event") == "exam_recheck" and row.get("qid"):
            last_status[row["qid"]] = row.get("status")

    results, regressions, failures = [], [], []
    for q in questions[:limit] if limit else questions:
        qid = q["id"]
        key = keys.get(qid)
        group = [notes_map[c] for c in q.get("covers", []) if c in notes_map]
        derived = derive_answer(qid, group, index)
        derived_hash = hashlib.sha256(derived.encode("utf-8")).hexdigest()
        golden = (key or {}).get("answer", "")
        golden_hash = hashlib.sha256(golden.encode("utf-8")).hexdigest()
        # 不信任存档哈希本身：用答案文本重新算，能同时抓出“答案文件被改动”和“证据漂移”
        key_intact = bool(key) and (key.get("answer_sha256") == golden_hash)
        status = "pass" if (key_intact and derived_hash == golden_hash) else "fail"
        prev = last_status.get(qid)
        regression = bool(prev == "pass" and status == "fail")
        reason = None
        if status == "fail":
            if not key:
                reason = "exam_key.jsonl 里没有这道题的参考答案"
            elif not key_intact:
                reason = ("参考答案文本与自身记录的 sha256 不一致 —— 答案文件被改动过")
            else:
                reason = "当前证据重新推导出的答案与存档参考答案不一致（笔记/证据/答案之一被改动）"
        row = {"ts": now_iso(), "event": "exam_recheck", "qid": qid, "status": status,
               "prev_status": prev, "regression": regression,
               "derived_sha256": derived_hash, "golden_sha256": golden_hash,
               "key_intact": key_intact, "covers": q.get("covers", []), "reason": reason}
        util.append_jsonl(out / "ledger.jsonl", row)
        results.append(row)
        if status == "fail":
            failures.append(qid)
        if regression:
            regressions.append(qid)
        if verbose:
            mark = "✓" if status == "pass" else "✗"
            extra = ""
            if regression:
                extra = "  ← REGRESSION"
            print(f"  {mark} {qid}: {status}（上次 {prev or '无'}）{extra}")
            if status == "fail" and reason:
                print(f"      ! {reason}")

    write_json(out / "exam_recheck.json", {
        "ts": now_iso(), "checked": len(results), "failed": failures,
        "regressions": regressions, "results": results,
    })
    if verbose:
        print(f"[exam] --recheck 完成：{len(results)} 题，失败 {len(failures)}，"
              f"退化 {len(regressions)}")
        for qid in regressions:
            print(f"[exam] !! REGRESSION {qid}: 上次通过，本次失败（防回归拦截）")
    code = 0
    if failures:
        code = 1
    return {"exit_code": code, "results": results, "failed": failures,
            "regressions": regressions}


def quiz(out: str | Path) -> int:
    """只打印题目（不给答案），用于自测。"""
    out = Path(out)
    questions = util.read_jsonl(out / EXAM_JSONL)
    if not questions:
        eprint(f"[exam] {out / EXAM_JSONL} 为空")
        return 1
    for q in questions:
        print(q["question"])
        print("\n" + "-" * 60 + "\n")
    print(f"（共 {len(questions)} 题；答案见 {out / KEY_JSONL}，不要偷看）")
    return 0
