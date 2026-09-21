"""判据机检：跑一组机器可判的规则，输出通过/失败清单与退出码。

规则（都可机检，不依赖模型）：
  R1 笔记存在且非空
  R2 每条结论都有引用
  R3 每条引用都能在 evidence/index.json 里解析到
  R4 引用指向的文件真实存在；视频引用的时间戳落在时长范围内
  R5 笔记里不出现禁用词表里的词
  R6 每个字幕源都有覆盖率报告（含覆盖率/句数/可疑标记）
  R7 考题整体一致（每题跨 ≥2 条结论、引用有效、参考答案哈希对得上）
  R8 notes.md 与 notes.json 结论条数一致
退出码：全部通过 0，否则 1。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import util
from .rules import DEFAULT_FORBIDDEN, load_forbidden
from .util import now_iso, write_json, write_text

RULES_VERSION = "gate-rules/1"


def _load_inputs(out: Path) -> tuple[dict, dict, dict]:
    index = util.read_json(out / "evidence" / "index.json", default={}) or {}
    notes = util.read_json(out / "notes.json", default={}) or {}
    return index, notes, {}


def _check_notes_present(out: Path, index: dict, notes: dict) -> tuple[bool, str, list[str]]:
    md = out / "notes.md"
    items = notes.get("items") or []
    detail = [f"notes.md {'存在' if md.exists() else '缺失'}"
              f"（{md.stat().st_size if md.exists() else 0} 字节）",
              f"notes.json 结论 {len(items)} 条"]
    ok = md.exists() and md.stat().st_size > 0 and len(items) > 0
    if not ok:
        detail.append("笔记文件或结论为空")
    return ok, "R1 笔记存在且非空", detail


def _check_citations_present(out: Path, index: dict, notes: dict) -> tuple[bool, str, list[str]]:
    items = notes.get("items") or []
    bad = [n["id"] for n in items if not (n.get("citations") or [])]
    detail = [f"有引用的结论 {len(items) - len(bad)}/{len(items)}"]
    if bad:
        detail.append("缺引用: " + ", ".join(bad[:8]) + ("…" if len(bad) > 8 else ""))
    return (not bad) and bool(items), "R2 每条结论都有引用", detail


def _check_citations_resolve(out: Path, index: dict, notes: dict) -> tuple[bool, str, list[str]]:
    valid = {i["id"] for i in index.get("items", [])}
    missing: list[str] = []
    for n in notes.get("items") or []:
        for c in n.get("citations") or []:
            if c not in valid:
                missing.append(f"{c}（结论 {n['id']}）")
    detail = [f"证据索引条目 {len(valid)} 条",
              f"无法解析的引用 {len(missing)} 个"]
    if missing:
        detail.append("例如: " + "; ".join(missing[:6]))
    return not missing, "R3 引用可解析到证据索引", detail


def _check_artifacts_exist(out: Path, index: dict, notes: dict) -> tuple[bool, str, list[str]]:
    by_id = {i["id"]: i for i in index.get("items", [])}
    src_meta = index.get("sources", {})
    used: set[str] = set()
    for n in notes.get("items") or []:
        used.update(n.get("citations") or [])
    missing: list[str] = []
    out_of_range: list[str] = []
    for cid in sorted(used):
        it = by_id.get(cid)
        if it is None:
            continue  # 交给 R3 报
        for key in ("artifact", "sheet"):
            rel = it.get(key)
            if rel and not (out / rel).exists():
                missing.append(f"{cid}:{rel}")
        secs = it.get("seconds")
        dur = (src_meta.get(it.get("source_id"), {}) or {}).get("duration")
        if secs is not None and dur:
            if secs < -0.001 or secs > float(dur) + 0.5:
                out_of_range.append(f"{cid}={secs}s 超出 [0,{dur}s]")
    detail = [f"被引用证据 {len(used)} 条", f"缺产物文件 {len(missing)} 个",
              f"时间戳越界 {len(out_of_range)} 个"]
    if missing:
        detail.append("缺失: " + "; ".join(missing[:6]))
    if out_of_range:
        detail.append("越界: " + "; ".join(out_of_range[:6]))
    return not missing and not out_of_range, "R4 引用指向的文件/时间戳真实存在", detail


def _check_forbidden(out: Path, index: dict, notes: dict, words: list[str]) -> tuple[bool, str, list[str]]:
    md = out / "notes.md"
    text = util.read_text(md) if md.exists() else ""
    hits = [w for w in words if w and w in text]
    detail = [f"禁用词表 {len(words)} 条", f"命中 {len(hits)} 个"]
    if hits:
        for w in hits:
            m = re.search(r".{0,30}" + re.escape(w) + r".{0,30}", text)
            detail.append(f"命中「{w}」上下文: …{m.group(0).strip()}…" if m else f"命中「{w}」")
    return not hits, "R5 笔记里不出现禁用词", detail


def _check_coverage_reports(out: Path, index: dict, notes: dict) -> tuple[bool, str, list[str]]:
    subs = {sid: s for sid, s in (index.get("sources") or {}).items() if s.get("kind") == "subtitle"}
    problems: list[str] = []
    detail: list[str] = []
    for sid in subs:
        p = out / "subtitles" / f"{sid}.checks.json"
        if not p.exists():
            problems.append(f"{sid}: 缺覆盖率报告")
            continue
        obj = util.read_json(p, default={}) or {}
        need = ["cue_count", "first", "last", "coverage", "coverage_pct", "jumps", "suspicious"]
        lack = [k for k in need if k not in obj]
        detail.append(f"{sid}: 覆盖率 {obj.get('coverage_pct')}%，可疑={obj.get('suspicious')}")
        if lack:
            problems.append(f"{sid}: 报告缺字段 {lack}")
        if obj.get("suspicious"):
            problems.append(f"{sid}: 报告判定可疑（{'；'.join(obj.get('hard_reasons') or [])}）")
    if not subs:
        detail.append("本次资料里没有字幕源（该规则空过）")
    if problems:
        detail.extend(problems)
    return not problems, "R6 字幕覆盖率报告齐全且不判可疑", detail


def _check_exam(out: Path, index: dict, notes: dict) -> tuple[bool, str, list[str]]:
    exam_path = out / "exam.jsonl"
    if not exam_path.exists():
        return True, "R7 考题一致性（本次无考题，空过）", ["exam.jsonl 不存在"]
    qs = util.read_jsonl(exam_path)
    keys = {k["id"]: k for k in util.read_jsonl(out / "exam_key.jsonl")}
    note_ids = {n["id"] for n in notes.get("items") or []}
    note_src = {n["id"]: set(n.get("sources") or []) for n in notes.get("items") or []}
    multi_src = len({s for v in note_src.values() for s in v}) >= 2
    valid_ev = {i["id"] for i in index.get("items", [])}
    import hashlib
    problems: list[str] = []
    for q in qs:
        if len(q.get("covers") or []) < 2:
            problems.append(f"{q['id']}: 只覆盖 {len(q.get('covers') or [])} 条结论，未跨条目")
        q_srcs = {s for c in (q.get("covers") or []) for s in note_src.get(c, set())}
        if multi_src and len(q_srcs) < 2:
            problems.append(f"{q['id']}: 只覆盖 1 个来源（{sorted(q_srcs)}），未跨来源")
        unknown = [c for c in (q.get("covers") or []) if c not in note_ids]
        if unknown:
            problems.append(f"{q['id']}: 覆盖了不存在的结论 {unknown}")
        bad_ev = [e for e in (q.get("evidence_ids") or []) if e not in valid_ev]
        if bad_ev:
            problems.append(f"{q['id']}: 引用不存在的证据 {bad_ev[:3]}")
        k = keys.get(q["id"])
        if not k:
            problems.append(f"{q['id']}: 缺参考答案")
        elif k.get("answer_sha256") != hashlib.sha256(k.get("answer", "").encode("utf-8")).hexdigest():
            problems.append(f"{q['id']}: 参考答案与哈希不一致（被改动过）")
    detail = [f"考题 {len(qs)} 道", f"参考答案 {len(keys)} 份",
              "每题覆盖条数: " + ", ".join(f"{q['id']}={len(q.get('covers') or [])}" for q in qs)]
    if problems:
        detail.extend(problems)
    return not problems, "R7 考题跨条目且答案自洽", detail


def _check_notes_consistency(out: Path, index: dict, notes: dict) -> tuple[bool, str, list[str]]:
    md = out / "notes.md"
    text = util.read_text(md) if md.exists() else ""
    headings = len(re.findall(r"^## 结论 ", text, flags=re.M))
    items = notes.get("items") or []
    detail = [f"notes.md 结论小标题 {headings} 个", f"notes.json 结论 {len(items)} 条"]
    ok = md.exists() and headings == len(items) and len(items) > 0
    if not ok:
        detail.append("两边数量不一致（可能是手工改坏了其中一个）")
    return ok, "R8 notes.md 与 notes.json 一致", detail


def run_gate(out: str | Path, rules_path: str | None = None,
             extra_forbidden: list[str] | None = None, verbose: bool = True,
             json_out: str | None = None) -> dict:
    out = Path(out)
    index, notes, _ = _load_inputs(out)
    words = load_forbidden(rules_path, extra_forbidden)

    checks = [
        _check_notes_present(out, index, notes),
        _check_citations_present(out, index, notes),
        _check_citations_resolve(out, index, notes),
        _check_artifacts_exist(out, index, notes),
        _check_forbidden(out, index, notes, words),
        _check_coverage_reports(out, index, notes),
        _check_exam(out, index, notes),
        _check_notes_consistency(out, index, notes),
    ]
    results = [{"rule": name.split()[0], "name": name, "ok": ok, "details": details}
               for (ok, name, details) in checks]
    passed = [r for r in results if r["ok"]]
    failed = [r for r in results if not r["ok"]]
    exit_code = 0 if not failed else 1

    report = {
        "ts": now_iso(), "out": str(out.resolve()), "rules_version": RULES_VERSION,
        "forbidden_words": words, "total": len(results), "passed": len(passed),
        "failed": len(failed), "exit_code": exit_code, "results": results,
    }
    write_json(out / "gate" / "report.json", report)
    md_lines = [f"# 判据机检报告（{RULES_VERSION}）", "",
                f"- 目录：`{out.resolve()}`",
                f"- 时间：{report['ts']}",
                f"- 结果：**{len(passed)}/{len(results)} 通过**，退出码 {exit_code}", ""]
    for r in results:
        md_lines.append(f"## {'✓' if r['ok'] else '✗'} {r['name']}")
        for d in r["details"]:
            md_lines.append(f"- {d}")
        md_lines.append("")
    write_text(out / "gate" / "report.md", "\n".join(md_lines))
    if json_out:
        write_json(json_out, report)

    if verbose:
        print(f"[gate] 判据机检（{len(results)} 条规则，版本 {RULES_VERSION}）")
        for r in results:
            mark = "✓" if r["ok"] else "✗"
            print(f"  {mark} {r['name']}")
            for d in r["details"]:
                print(f"      · {d}")
        print(f"[gate] 通过 {len(passed)}/{len(results)} → 退出码 {exit_code}")
        if failed:
            print(f"[gate] ✗ 失败规则: {', '.join(r['rule'] for r in failed)}")

    util.append_jsonl(out / "ledger.jsonl", {
        "ts": now_iso(), "event": "gate", "passed": len(passed), "total": len(results),
        "failed_rules": [r["rule"] for r in failed], "exit_code": exit_code,
    })
    return {"exit_code": exit_code, "report": report, "failed": [r["rule"] for r in failed]}
