"""可核对笔记：每条结论 = 断言 + 证据引用 + 置信度。

mock 模式（无密钥）下产出的是**结构性结论**：时长、帧数、行号、逐字引用、物理体检结果。
它不会假装看懂了画面语义 —— 语义结论必须由真实模型或人来补。
真实模式把证据摘要喂给模型，要求返回 JSON，并逐条校验引用 id 是否存在（不存在的引用直接丢弃）。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import util
from .llm import LLM
from .util import eprint, now_iso, rel_to, write_json, write_text

CONF_RANK = {"low": 2, "medium": 1, "high": 0}
WEAK_KIND_BONUS = {"video_report": 1, "subtitle_report": 1, "doc_report": 1, "video_sheet": 0}

SYSTEM_PROMPT = (
    "你是一个严谨的学习助手。你只能基于给定的证据条目写结论，禁止补充任何证据里没有的信息。"
    "每条结论必须携带至少一个证据 ID。如果证据不足以支撑语义判断，就把结论写成结构性事实，"
    "或者把置信度标为 low 并说明缺口。禁止使用“众所周知/显而易见/据说”这类无证据措辞。"
    "只输出 JSON。"
)


def load_index(out: Path) -> dict:
    idx = util.read_json(Path(out) / "evidence" / "index.json", default=None)
    if not idx:
        raise SystemExit(f"[notes] 找不到证据索引 {Path(out) / 'evidence' / 'index.json'}，请先跑 ingest")
    return idx


def group_items(index: dict) -> dict[str, list[dict]]:
    by_src: dict[str, list[dict]] = {}
    for it in index.get("items", []):
        by_src.setdefault(it.get("source_id", "?"), []).append(it)
    return by_src


# --------------------------------------------------------------------- mock 生成

def _mock_notes(index: dict, max_notes: int | None = None) -> list[dict]:
    notes: list[dict] = []
    by_src = group_items(index)
    src_meta = index.get("sources", {})

    def add(claim: str, citations: list[str], confidence: str, sources: list[str], why: str):
        notes.append({
            "id": f"n{len(notes) + 1:03d}", "claim": claim, "confidence": confidence,
            "citations": [c for c in citations if c], "sources": sorted(set(sources)),
            "rationale": why,
        })

    for sid, items in by_src.items():
        meta = src_meta.get(sid, {})
        kind = meta.get("kind") or (items[0]["id"].split(":")[1] if items else "?")
        rep = next((i for i in items if i["kind"].endswith("_report")), None)

        if kind == "video" or rep and rep["kind"] == "video_report":
            dur = meta.get("duration")
            frames = [i for i in items if i["kind"] == "video_frame"]
            sheets = [i for i in items if i["kind"] == "video_sheet"]
            add(f"视频 {sid} 时长 {dur:.3f}s，共抽出 {len(frames)} 帧、拼成 {len(sheets)} 张 "
                f"{index.get('cols')}×{index.get('rows')} 接触表；时间戳已烧进像素，可逐格核对。",
                [rep["id"] if rep else None] + [s["id"] for s in sheets[:1]], "high", [sid],
                "ffprobe 时长 + ffmpeg 产物文件计数，均可机检")
            if frames:
                picks = [frames[0], frames[len(frames) // 2], frames[-1]]
                seen = set()
                for f in picks:
                    if f["id"] in seen:
                        continue
                    seen.add(f["id"])
                    add(f"视频帧 {f['id'].split(':')[-1]} 落在 {f['locator']}，帧文件为 "
                        f"{f['artifact']}（接触表 {f['sheet']} 第 {f.get('cell')} 格）。",
                        [f["id"]], "high", [sid], "帧文件 + index.json 里的时间映射，两者都可复核")
            add(f"关于 {sid} 的**画面语义**（画面里讲了什么）本笔记不作判断：mock 模式下没有读图能力，"
                f"任何语义结论都必须先补模型或人工看图。",
                [rep["id"] if rep else (frames[0]["id"] if frames else None)], "low", [sid],
                "显式标注能力边界，避免无证据断言")

        elif kind == "subtitle":
            checks = util.read_json(Path(index["out"]) / rep["artifact"], default={}) if rep else {}
            cues = [i for i in items if i["kind"] == "subtitle_cue"]
            add(f"字幕 {sid}（{checks.get('format', '?')}）共 {checks.get('cue_count')} 句，"
                f"首句 {checks.get('first')}，末句 {checks.get('last')}，"
                f"覆盖率 {checks.get('coverage_pct')}%"
                f"（{checks.get('covered_seconds')}s / {checks.get('duration')}s），"
                f"跳段 {len(checks.get('jumps') or [])} 处，可疑={checks.get('suspicious')}。",
                [rep["id"] if rep else None], "high", [sid],
                "物理体检脚本的机检输出，阈值写死在 ingest.check_subtitles 里")
            for c in cues[:4]:
                add(f"字幕 {sid} 在 {c['locator']} 的原文是：「{c['text']}」",
                    [c["id"]], "high", [sid], "逐字引用，可直接 diff 校验")
            if checks.get("suspicious"):
                add(f"字幕 {sid} 被判可疑：{'；'.join(checks.get('hard_reasons') or checks.get('reasons') or [])}。"
                    f"这份字幕不能直接用来记笔记，需先修时间轴或换源。",
                    [rep["id"] if rep else None], "high", [sid], "覆盖率/跳段为硬阈值判定")

        else:  # doc
            lines = [i for i in items if i["kind"] in ("doc_line", "doc_heading")]
            headings = [i for i in lines if i["kind"] == "doc_heading"]
            add(f"文档 {sid} 已提取文本，本次挑出 {len(lines)} 行作为可引用行"
                f"（其中标题 {len(headings)} 个）。",
                [rep["id"] if rep else None], "high", [sid],
                "行号来自 docs/<sid>.txt 的真实文本行")
            if headings:
                outline = "；".join(f"L{h['line']} {h['text'][:28]}" for h in headings[:4])
                add(f"文档 {sid} 的结构（按行号）：{outline}",
                    [h["id"] for h in headings[:4]], "high", [sid], "标题行逐字引用")
            for ln in lines[:4]:
                add(f"文档 {sid} 第 {ln['line']} 行原文：「{ln['text']}」",
                    [ln["id"]], "high", [sid], "逐字引用，带行号")

    return notes if max_notes is None else notes[:max_notes]


# --------------------------------------------------------------------- 真实模型生成

def _real_notes(index: dict, llm: LLM, max_notes: int | None = None) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    valid_ids = {it["id"] for it in index.get("items", [])}
    digest_lines = []
    for it in index.get("items", [])[:400]:
        text = (it.get("text") or "").replace("\n", " ")[:160]
        digest_lines.append(f"{it['id']} | {it['kind']} | {it['locator']} | {text}")
    user = (
        "下面是资料切出的证据条目（格式：证据ID | 类型 | 定位符 | 内容摘要）。\n"
        "请写学习笔记，最多 %d 条。每条结论都要给出 citations（证据 ID 列表，必须来自上面）。\n"
        "输出 JSON：{\"notes\":[{\"claim\":\"...\",\"confidence\":\"high|medium|low\","
        "\"citations\":[\"ev:...\"],\"rationale\":\"为什么这条可信\"}]}\n\n证据：\n%s"
        % ((max_notes or 20), "\n".join(digest_lines))
    )
    obj = llm.chat_json(SYSTEM_PROMPT, user, mock_fn=lambda _u: {"notes": []}, max_tokens=4000)
    raw_notes = obj.get("notes", []) if isinstance(obj, dict) else []
    notes: list[dict] = []
    dropped = 0
    for rn in raw_notes:
        cits = [c for c in (rn.get("citations") or []) if c in valid_ids]
        dropped += len(rn.get("citations") or []) - len(cits)
        srcs = sorted({c.split(":")[2] for c in cits if len(c.split(":")) > 2})
        notes.append({
            "id": f"n{len(notes) + 1:03d}",
            "claim": str(rn.get("claim", "")).strip(),
            "confidence": rn.get("confidence") if rn.get("confidence") in CONF_RANK else "low",
            "citations": cits, "sources": srcs,
            "rationale": str(rn.get("rationale", "")).strip(),
        })
    if dropped:
        warnings.append(f"模型给出了 {dropped} 个不存在的证据 ID，已丢弃（这些结论会因此被判缺少引用）")
    return notes if max_notes is None else notes[:max_notes], warnings


# --------------------------------------------------------------------- 渲染

def render_markdown(out: Path, index: dict, notes: list[dict], mode_desc: str) -> str:
    lines = ["# 学习笔记（learn-loop 生成，可核对）", ""]
    lines.append(f"- 生成时间：{now_iso()}")
    lines.append(f"- 模型：{mode_desc}")
    lines.append(f"- 资料：{', '.join(sorted(index.get('sources', {}))) or '（无）'}")
    lines.append(f"- 结论条数：{len(notes)}")
    lines.append("")
    lines.append("> 每条结论都带证据引用；引用格式 `证据ID @ 定位符`，"
                 "可直接与 `evidence/index.json` 对照复核。置信度是自评，不是事实强度。")
    lines.append("")
    for n in notes:
        lines.append(f"## 结论 {n['id']} — {n['claim'][:60]}{'…' if len(n['claim']) > 60 else ''}")
        lines.append("")
        lines.append(f"- **结论**：{n['claim']}")
        lines.append(f"- **置信度**：{n['confidence']}")
        if n.get("rationale"):
            lines.append(f"- **为什么**：{n['rationale']}")
        lines.append("- **证据引用**：")
        if not n["citations"]:
            lines.append("  - （无引用 —— 这条一定会被判据机检打回）")
        for cid in n["citations"]:
            it = next((i for i in index["items"] if i["id"] == cid), None)
            if it:
                artifact = it.get("artifact") or it.get("sheet") or ""
                lines.append(f"  - `{cid}` @ {it['locator']}"
                             f"{('  → ' + artifact) if artifact else ''}")
            else:
                lines.append(f"  - `{cid}` @ ？？（索引里不存在，判据机检会失败）")
        lines.append(f"- **来源**：{', '.join(n['sources']) or '（未标注）'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def round_robin_balanced(notes: list[dict], limit: int | None = None) -> list[dict]:
    """按来源轮转重排，并在 limit 约束下均衡截断（不让排最后的资料被整体砍掉）。

    结果形如 v,s,b,d,v,s,b,d…，这样后面按 N 条一组出题时，每组天然跨来源。
    """
    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for n in notes:
        key = (n.get("sources") or ["?"])[0]
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(n)
    out: list[dict] = []
    while True:
        progressed = False
        for k in order:
            if buckets[k]:
                out.append(buckets[k].pop(0))
                progressed = True
                if limit is not None and len(out) >= limit:
                    return out
        if not progressed:
            break
    return out


def build_notes(out: str | Path, mock: bool = False, max_notes: int = 20,
                model: str | None = None, verbose: bool = True) -> dict:
    out = Path(out)
    index = load_index(out)
    llm = LLM(model=model, mock=mock)
    warnings: list[str] = []
    if llm.mock:
        notes = _mock_notes(index, max_notes=None)
    else:
        notes, warnings = _real_notes(index, llm, max_notes=None)
    total_before = len(notes)
    notes = round_robin_balanced(notes, max_notes)
    if total_before > len(notes):
        warnings.append(f"共可生成 {total_before} 条结论，受 --max-notes={max_notes} 限制"
                        f"按来源均衡保留 {len(notes)} 条")

    md = render_markdown(out, index, notes, llm.describe())
    write_text(out / "notes.md", md)
    notes_obj = {
        "version": 1, "generated_at": now_iso(), "mode": "mock" if llm.mock else "model",
        "model": llm.describe(), "notes_md": "notes.md",
        "stats": {
            "count": len(notes),
            "with_citation": sum(1 for n in notes if n["citations"]),
            "without_citation": sum(1 for n in notes if not n["citations"]),
            "by_confidence": {k: sum(1 for n in notes if n["confidence"] == k)
                              for k in ("high", "medium", "low")},
        },
        "items": notes, "warnings": warnings,
    }
    write_json(out / "notes.json", notes_obj)
    util.append_jsonl(out / "ledger.jsonl", {
        "ts": now_iso(), "event": "notes", "mode": notes_obj["mode"],
        "count": len(notes), "without_citation": notes_obj["stats"]["without_citation"],
    })
    if verbose:
        print(f"[notes] 产出 {len(notes)} 条结论（带引用 {notes_obj['stats']['with_citation']} 条）"
              f" → notes.md / notes.json")
        print(f"[notes] 模型: {llm.describe()}")
        if llm.mock:
            print("[notes] 注意：mock 模式只产出结构性结论（时长/帧数/行号/逐字引用），"
                  "不含画面语义判断")
        for w in warnings:
            print(f"[notes] ! {w}")
    return {"exit_code": 0, "notes": notes_obj, "md_path": str(out / "notes.md"),
            "json_path": str(out / "notes.json"), "warnings": warnings}
