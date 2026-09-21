"""资料摄入。

三类资料：
  视频 → ffprobe 取时长；按时间点抽帧，用 drawtext 把 **真实 PTS 烧进像素**（%{pts\\:hms}），
         再按 cols×rows 拼成接触表（sheet）。产物：frames/<sid>/、sheets/
  字幕 → 解析 .srt 与 `[mm:ss] 文本` 两种格式，做物理体检：句数 / 首末点 / 覆盖率 / 跳段>20s。
         覆盖率 >105% 或 <50% 或存在跳段 ⇒ 判可疑，ingest 整体非零退出。
  文档 → .md/.txt 直接读文本；.pdf 用 pdftotext（不存在就跳过并如实提示）。

产物：<out>/evidence/index.json（证据索引，notes/exam/gate 都从这里取引用锚点）
"""
from __future__ import annotations

import re
from pathlib import Path

from . import util
from .util import (
    DOC_EXT,
    SRT_EXT,
    VIDEO_EXT,
    eprint,
    ffprobe_duration,
    ffprobe_summary,
    find_font,
    hms,
    now_iso,
    parse_time_token,
    read_text,
    rel_to,
    run_cmd,
    safe_name,
    srt_time,
    write_json,
    write_text,
)

# 物理体检阈值
COVERAGE_HIGH = 1.05      # >105% 判可疑
COVERAGE_LOW = 0.50       # <50%  判可疑
JUMP_SECONDS = 20.0       # 相邻字幕间隔 >20s 判为跳段

_SRT_ARROW = re.compile(r"(-->|->)")
_BRACKET_LINE = re.compile(
    r"^\s*[\[\u3010(](?P<t>\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)[\]\u3011)]\s*(?P<txt>.*)$")
_ANY_TIME = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}:\d{2}|\d{1,2}:\d{2})")


# =============================================================== 视频

def extract_frame(video: Path, t: float, dest: Path, font: str | None,
                  width: int = 480, height: int = 270) -> tuple[bool, str]:
    """抽一帧并把真实 PTS 烧进像素。返回 (ok, err)。"""
    if not util.which("ffmpeg"):
        return False, "ffmpeg 不可用"
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf_parts = []
    if font:
        vf_parts.append(
            "drawtext=fontfile=" + font +
            ":text='%{pts\\:hms}':x=14:y=14:fontsize=34:fontcolor=white:"
            "box=1:boxcolor=black@0.65:boxborderw=8")
    else:
        # 找不到字体时不静默跳过：明确报一次错，但不阻断流程（帧仍会抽出）
        eprint("[learn-loop] 警告: 未找到可用字体，帧上无法烧入时间戳")
    vf_parts.append(f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-copyts", "-ss", f"{t:.3f}", "-i", str(video),
           "-frames:v", "1", "-an", "-vf", ",".join(vf_parts), "-q:v", "3", str(dest)]
    rc, _, err = run_cmd(cmd, timeout=120)
    if rc != 0 or not dest.exists() or dest.stat().st_size == 0:
        return False, (err.strip().splitlines()[-1] if err.strip() else f"ffmpeg rc={rc}")
    return True, ""


def build_sheet(frames_dir: Path, start_number: int, out_sheet: Path,
                cols: int, rows: int) -> tuple[bool, str]:
    """把连续 cols*rows 张帧拼成一张接触表。"""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-start_number", str(start_number),
           "-i", str(frames_dir / "f-%06d.jpg"),
           "-vf", f"tile={cols}x{rows}:padding=6:margin=8:color=0x101010",
           "-frames:v", "1", "-q:v", "3", str(out_sheet)]
    out_sheet.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = run_cmd(cmd, timeout=180)
    if rc != 0 or not out_sheet.exists() or out_sheet.stat().st_size == 0:
        return False, (err.strip().splitlines()[-1] if err.strip() else f"ffmpeg rc={rc}")
    return True, ""


def ingest_video(src: Path, out: Path, cols: int = 3, rows: int = 3,
                 n_frames: int | None = None, verbose: bool = True) -> dict:
    """返回 {source, items, report, warnings}。"""
    sid = safe_name(src.name)
    warnings: list[str] = []
    duration = ffprobe_duration(src)
    if duration is None:
        raise SystemExit(f"[learn-loop] 无法读取视频时长（ffprobe 失败）: {src}")
    per_sheet = max(1, cols * rows)
    if n_frames is None:
        n_frames = per_sheet
    n_frames = max(1, int(n_frames))
    # 均匀取样点：第 i 帧取 i*duration/n_frames（保证落在时长内）
    times = [round(i * duration / n_frames, 3) for i in range(n_frames)]
    while len(times) < per_sheet:      # 保证至少能拼出 1 张完整接触表（补采样点）
        times.append(round(min(duration - 0.001, times[-1] + duration / n_frames), 3))
    frames_dir = out / "frames" / sid
    sheets_dir = out / "sheets"
    frames_dir.mkdir(parents=True, exist_ok=True)
    sheets_dir.mkdir(parents=True, exist_ok=True)
    font = find_font()
    if not font:
        warnings.append("未找到字体文件，时间戳未烧入像素（帧仍已抽出）")

    frames: list[dict] = []
    for i, t in enumerate(times, 1):
        name = f"f-{i:06d}.jpg"
        dest = frames_dir / name
        ok, err = extract_frame(src, t, dest, font)
        if not ok:
            warnings.append(f"第 {i} 帧（t={t:.3f}s）抽取失败: {err}")
            continue
        sheet_index = (i - 1) // per_sheet
        frames.append({
            "n": i, "t": t, "locator": hms(t),
            "file": rel_to(dest, out),
            "sheet": rel_to(sheets_dir / f"{sid}-sheet-{sheet_index + 1:02d}.jpg", out),
            "cell": (i - 1) % per_sheet + 1,
        })

    sheets: list[dict] = []
    total_cells = len(frames)
    for s in range((total_cells + per_sheet - 1) // per_sheet):
        group = frames[s * per_sheet:(s + 1) * per_sheet]
        # 补齐不足一格的部分（复制末帧），保证 tile 输入张数足够
        pad_from = group[-1]["n"] if group else 1
        for k in range(len(group), per_sheet):
            pad_name = f"f-{pad_from + (k - len(group) + 1):06d}.jpg"
            pad_src = frames_dir / f"f-{pad_from:06d}.jpg"
            pad_dst = frames_dir / pad_name
            if pad_src.exists() and not pad_dst.exists():
                pad_dst.write_bytes(pad_src.read_bytes())
                warnings.append(f"接触表 {s + 1} 末格不足，用重复帧 {pad_dst.name} 补齐")
        sheet_path = sheets_dir / f"{sid}-sheet-{s + 1:02d}.jpg"
        start_number = group[0]["n"] if group else 1
        ok, err = build_sheet(frames_dir, start_number, sheet_path, cols, rows)
        if ok:
            sheets.append({
                "index": s + 1, "file": rel_to(sheet_path, out),
                "cols": cols, "rows": rows, "cell_count": per_sheet,
                "from": group[0]["locator"] if group else None,
                "to": group[-1]["locator"] if group else None,
                "frame_numbers": [f["n"] for f in group],
                "complete": len(group) == per_sheet,
            })
        else:
            warnings.append(f"接触表 {s + 1} 拼接失败: {err}")

    index_path = frames_dir / "index.json"
    index_obj = {
        "source_id": sid, "path": str(src), "duration": duration,
        "cols": cols, "rows": rows, "frame_count": len(frames),
        "sheet_count": len(sheets), "sheets": sheets, "frames": frames,
        "timestamp_burned_into_pixels": bool(font),
        "generated_at": now_iso(),
    }
    write_json(index_path, index_obj)

    items: list[dict] = [{
        "id": f"ev:r:{sid}:frames",
        "source_id": sid, "kind": "video_report",
        "locator": f"{len(frames)} frames / {len(sheets)} sheet(s)",
        "artifact": rel_to(index_path, out),
        "text": f"视频 {src.name} 时长 {duration:.3f}s，抽帧 {len(frames)} 张，"
                f"接触表 {len(sheets)} 张（{cols}x{rows}），时间戳烧入像素={bool(font)}",
    }]
    for f in frames:
        items.append({
            "id": f"ev:v:{sid}:{f['n']:06d}",
            "source_id": sid, "kind": "video_frame", "locator": f["locator"],
            "seconds": f["t"], "artifact": f["file"], "sheet": f["sheet"],
            "cell": f["cell"],
            "text": f"视频帧 #{f['n']} @ {f['locator']}（接触表 {f['sheet']} 第 {f['cell']} 格）",
        })
    for s in sheets:
        items.append({
            "id": f"ev:sh:{sid}:{s['index']:03d}",
            "source_id": sid, "kind": "video_sheet",
            "locator": f"sheet {s['index']} {s['from']}→{s['to']}",
            "artifact": s["file"],
            "seconds": next((f["t"] for f in frames if f["n"] == s["frame_numbers"][0]), None),
            "text": f"接触表 {s['index']}：{s['from']}→{s['to']}，含 "
                    f"{len(s['frame_numbers'])} 帧（{cols}x{rows} 宫格）",
        })

    report = {
        "source_id": sid, "path": str(src), "kind": "video",
        "duration": duration, "frame_count": len(frames), "sheet_count": len(sheets),
        "cols": cols, "rows": rows, "timestamp_burned_into_pixels": bool(font),
        "warnings": warnings,
    }
    if verbose:
        print(f"  · 视频 {src.name}: 时长 {duration:.3f}s → 抽帧 {len(frames)} 张 / "
              f"接触表 {len(sheets)} 张" + (f"（{len(warnings)} 条提示）" if warnings else ""))
    return {"source": {"source_id": sid, "path": str(src), "kind": "video",
                       "duration": duration, "frame_count": len(frames),
                       "sheet_count": len(sheets)},
            "items": items, "report": report, "warnings": warnings}


# =============================================================== 字幕解析

def parse_srt(text: str) -> list[dict]:
    lines = text.replace("\ufeff", "").replace("\r\n", "\n").split("\n")
    cues: list[dict] = []
    i = 0
    while i < len(lines):
        raw = lines[i].strip()
        if not raw:
            i += 1
            continue
        # 跳过纯序号行
        if re.fullmatch(r"\d+", raw) and i + 1 < len(lines) and _SRT_ARROW.search(lines[i + 1]):
            i += 1
            raw = lines[i].strip()
        if _SRT_ARROW.search(raw):
            block_line = i + 1
            left, _, right = _SRT_ARROW.split(raw, maxsplit=1)  # type: ignore[arg-type]
            st = _first_time(left)
            et = _first_time(right)
            i += 1
            buf: list[str] = []
            while i < len(lines):
                nxt = lines[i].strip()
                if not nxt:
                    break
                if _SRT_ARROW.search(nxt):
                    break
                if re.fullmatch(r"\d+", nxt) and i + 1 < len(lines) and _SRT_ARROW.search(lines[i + 1]):
                    break
                buf.append(nxt)
                i += 1
            if st is None:
                continue
            if et is None or et < st:
                et = st + 2.0
            cues.append({
                "index": len(cues) + 1, "start": st, "end": et,
                "text": " ".join(buf).strip(), "raw_line": block_line,
                "end_inferred": False,
            })
            continue
        i += 1
    return cues


def parse_bracket(text: str) -> list[dict]:
    """解析 `[mm:ss] 文本` / `[mm:ss.mmm] 文本` / `[hh:mm:ss] 文本`，末点由下一条起点推断。"""
    cues: list[dict] = []
    for ln, raw in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        m = _BRACKET_LINE.match(raw)
        if not m:
            continue
        st = parse_time_token(m.group("t"))
        if st is None:
            continue
        cues.append({"index": len(cues) + 1, "start": st, "end": None,
                     "text": m.group("txt").strip(), "raw_line": ln,
                     "end_inferred": True})
    for i, c in enumerate(cues):
        if c["end"] is None:
            nxt = cues[i + 1]["start"] if i + 1 < len(cues) else None
            c["end"] = nxt if nxt is not None else c["start"] + 2.0
        if c["end"] < c["start"]:
            c["end"] = c["start"]
    return cues


def _first_time(s: str) -> float | None:
    m = _ANY_TIME.search(s or "")
    if not m:
        return None
    return parse_time_token(m.group(1))


def looks_like_bracket_subtitle(path: Path, sample_lines: int = 40) -> bool:
    if path.suffix.lower() not in {".txt", ".md", ".text"}:
        return False
    try:
        lines = read_text(path).splitlines()[:sample_lines]
    except OSError:
        return False
    hits = sum(1 for ln in lines if _BRACKET_LINE.match(ln))
    return hits >= 2


def check_subtitles(cues: list[dict], duration: float | None) -> dict:
    """物理体检：句数 / 首末点 / 覆盖率 / 跳段。"""
    reasons: list[str] = []
    cue_count = len(cues)
    if cue_count == 0:
        reasons.append("未解析到任何字幕条目")
    first = cues[0]["start"] if cues else None
    last = cues[-1]["end"] if cues else None
    last_start = cues[-1]["start"] if cues else None

    covered = 0.0
    overlaps = 0
    prev_end: float | None = None
    jumps: list[dict] = []
    for c in cues:
        s, e = max(0.0, c["start"]), max(0.0, c["end"])
        if duration is not None:
            s, e = min(s, duration), min(e, duration)
        covered += max(0.0, e - s)
        if prev_end is not None:
            if c["start"] < prev_end - 0.001:
                overlaps += 1
            if c["start"] - prev_end > JUMP_SECONDS:
                jumps.append({"after_index": c["index"] - 1, "gap": round(c["start"] - prev_end, 3),
                              "at": srt_time(c["start"]), "prev_end": srt_time(prev_end)})
            prev_end = max(prev_end, c["end"])
        else:
            prev_end = c["end"]

    coverage = (covered / duration) if duration else None
    if duration is None:
        reasons.append("缺少参考时长（未提供视频/--duration），覆盖率无法计算")
    else:
        if coverage is not None and coverage > COVERAGE_HIGH:
            reasons.append(f"覆盖率 {coverage * 100:.1f}% > 105%：字幕时长总和超过视频时长，"
                           f"疑似时间轴错配或叠句")
        if coverage is not None and coverage < COVERAGE_LOW:
            reasons.append(f"覆盖率 {coverage * 100:.1f}% < 50%：大量时间无字幕，"
                           f"疑似字幕残缺或与视频错配")
    if jumps:
        worst = max(jumps, key=lambda j: j["gap"])
        reasons.append(f"存在 {len(jumps)} 处 >{JUMP_SECONDS:.0f}s 跳段"
                       f"（最大 {worst['gap']:.1f}s，位于 {worst['at']}）")
    if overlaps:
        reasons.append(f"存在 {overlaps} 处时间轴重叠（后一条在前一条结束前开始）")
    if cue_count and first is not None and first > 5.0:
        reasons.append(f"首条字幕起点偏晚（{srt_time(first)}）")
    if cue_count and duration and last_start is not None and (duration - last_start) > 60.0:
        reasons.append(f"末条字幕起点距视频结尾 {(duration - last_start):.1f}s，尾部大段无字幕")

    hard = [r for r in reasons if ("覆盖率" in r and ("105%" in r or "50%" in r)) or "跳段" in r
            or "未解析到" in r]
    return {
        "cue_count": cue_count,
        "first": srt_time(first) if first is not None else None,
        "first_seconds": first,
        "last": srt_time(last) if last is not None else None,
        "last_seconds": last,
        "last_start": srt_time(last_start) if last_start is not None else None,
        "duration": duration,
        "covered_seconds": round(covered, 3),
        "coverage": round(coverage, 4) if coverage is not None else None,
        "coverage_pct": round(coverage * 100, 1) if coverage is not None else None,
        "overlaps": overlaps,
        "jumps": jumps,
        "max_jump": max((j["gap"] for j in jumps), default=0.0),
        "reasons": reasons,
        "hard_reasons": hard,
        "suspicious": bool(hard),
        "thresholds": {"coverage_high": COVERAGE_HIGH, "coverage_low": COVERAGE_LOW,
                       "jump_seconds": JUMP_SECONDS},
    }


def ingest_subtitles(src: Path, out: Path, duration: float | None,
                     fmt: str | None = None, verbose: bool = True,
                     max_quotes: int = 4) -> dict:
    sid = safe_name(src.name)
    text = read_text(src)
    if fmt is None:
        fmt = "srt" if src.suffix.lower() in SRT_EXT else "bracket"
    cues = parse_srt(text) if fmt == "srt" else parse_bracket(text)
    report = check_subtitles(cues, duration)
    report.update({"source_id": sid, "path": str(src), "kind": "subtitle", "format": fmt,
                   "generated_at": now_iso()})
    sub_dir = out / "subtitles"
    sub_dir.mkdir(parents=True, exist_ok=True)
    checks_path = sub_dir / f"{sid}.checks.json"
    write_json(checks_path, report)
    write_text(sub_dir / f"{sid}.cues.tsv",
               "index\tstart\tend\ttext\n" + "\n".join(
                   f"{c['index']}\t{srt_time(c['start'])}\t{srt_time(c['end'])}\t{c['text']}"
                   for c in cues) + "\n")

    items: list[dict] = [{
        "id": f"ev:r:{sid}:checks",
        "source_id": sid, "kind": "subtitle_report",
        "locator": f"coverage={report['coverage_pct']}% cues={report['cue_count']}",
        "artifact": rel_to(checks_path, out),
        "text": (f"字幕物理体检 {src.name}: 句数 {report['cue_count']}，首 {report['first']}，"
                 f"末 {report['last']}，覆盖率 {report['coverage_pct']}%，"
                 f"跳段 {len(report['jumps'])} 处，可疑={report['suspicious']}"),
    }]
    picks = list(cues[:max_quotes])
    if len(cues) > max_quotes:
        mid = len(cues) // 2
        picks += [cues[mid]]
        picks += list(cues[-1:])
    seen = set()
    for c in picks:
        if c["index"] in seen:
            continue
        seen.add(c["index"])
        items.append({
            "id": f"ev:s:{sid}:{c['index']:04d}",
            "source_id": sid, "kind": "subtitle_cue",
            "locator": f"{srt_time(c['start'])}–{srt_time(c['end'])}",
            "span": [c["start"], c["end"]],
            "line": c.get("raw_line"),
            "artifact": rel_to(checks_path, out),
            "text": c["text"],
            "end_inferred": c.get("end_inferred", False),
        })
    if verbose:
        flag = "⚠ 可疑" if report["suspicious"] else "OK"
        print(f"  · 字幕 {src.name}（{fmt}）: 句数 {report['cue_count']}，"
              f"覆盖 {report['coverage_pct']}% ，跳段 {len(report['jumps'])} → {flag}")
        for r in report["reasons"]:
            print(f"      ! {r}")
    return {"source": {"source_id": sid, "path": str(src), "kind": "subtitle",
                       "duration": duration, "cue_count": report["cue_count"],
                       "coverage_pct": report["coverage_pct"],
                       "suspicious": report["suspicious"]},
            "items": items, "report": report, "warnings": []}


# =============================================================== 文档

def extract_doc_text(src: Path, out: Path) -> tuple[str | None, str | None, str]:
    """返回 (text, artifact_rel_path, note)。docx 等不支持则明确返回 None。"""
    sid = safe_name(src.name)
    docs_dir = out / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower()
    if suffix == ".pdf":
        exe = util.which("pdftotext")
        if not exe:
            return None, None, "跳过：系统未安装 pdftotext，本工具不做 PDF 解析（不猜内容）"
        dest = docs_dir / f"{sid}.txt"
        rc, _, err = run_cmd([exe, "-layout", "-enc", "UTF-8", str(src), str(dest)], timeout=120)
        if rc != 0 or not dest.exists():
            return None, None, f"pdftotext 失败: {err.strip()[:200]}"
        return read_text(dest), rel_to(dest, out), f"pdftotext 提取 {len(read_text(dest))} 字符"
    if suffix in DOC_EXT:
        text = read_text(src)
        dest = docs_dir / f"{sid}.txt"
        write_text(dest, text)
        return text, rel_to(dest, out), f"直接读取 {len(text)} 字符"
    return None, None, f"跳过：不支持的文档类型 {suffix}"


def _select_doc_lines(lines: list[str], max_items: int) -> list[tuple[int, str, bool]]:
    """挑出可引用的行：标题优先，其次是长度足够的正文行。返回 (行号, 文本, 是否标题)。"""
    headings: list[tuple[int, str, bool]] = []
    body: list[tuple[int, str, bool]] = []
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or re.fullmatch(r"[-=*_#\s]{3,}", s):
            continue
        if s.startswith("#"):
            headings.append((i, s, True))
        elif len(s) >= 12:
            body.append((i, s, False))
    picked = headings[:4]
    remain = max(0, max_items - len(picked))
    if remain and body:
        # 均匀取样正文行
        step = max(1, len(body) // remain)
        picked += body[::step][:remain]
    return sorted(picked, key=lambda x: x[0])


def ingest_doc(src: Path, out: Path, max_lines: int = 8, verbose: bool = True) -> dict:
    sid = safe_name(src.name)
    text, artifact, note = extract_doc_text(src, out)
    warnings: list[str] = []
    items: list[dict] = []
    if text is None:
        warnings.append(f"{src.name}: {note}")
        if verbose:
            print(f"  · 文档 {src.name}: {note}")
        return {"source": {"source_id": sid, "path": str(src), "kind": "doc",
                           "line_count": 0, "skipped": True, "note": note},
                "items": [], "report": {"source_id": sid, "path": str(src), "kind": "doc",
                                        "skipped": True, "note": note},
                "warnings": warnings}

    lines = text.replace("\r\n", "\n").split("\n")
    index_path = out / "docs" / f"{sid}.index.json"
    picked = _select_doc_lines(lines, max_lines)
    write_json(index_path, {"source_id": sid, "path": str(src), "line_count": len(lines),
                            "picked_lines": [p[0] for p in picked],
                            "headings": [p[0] for p in picked if p[2]],
                            "generated_at": now_iso()})
    items.append({
        "id": f"ev:r:{sid}:doc",
        "source_id": sid, "kind": "doc_report",
        "locator": f"{len(lines)} lines",
        "artifact": artifact or rel_to(index_path, out),
        "text": f"文档 {src.name} 共 {len(lines)} 行；{note}",
    })
    for (ln, s, is_heading) in picked:
        items.append({
            "id": f"ev:d:{sid}:{ln:05d}",
            "source_id": sid, "kind": "doc_heading" if is_heading else "doc_line",
            "locator": f"L{ln}", "line": ln, "artifact": artifact,
            "text": s[:400],
        })
    report = {"source_id": sid, "path": str(src), "kind": "doc", "line_count": len(lines),
              "picked": len(picked), "note": note, "artifact": artifact}
    if verbose:
        print(f"  · 文档 {src.name}: {len(lines)} 行 → 可引用行 {len(picked)} 条（{note}）")
    return {"source": {"source_id": sid, "path": str(src), "kind": "doc",
                       "line_count": len(lines)},
            "items": items, "report": report, "warnings": warnings}


# =============================================================== 入口

def classify(src: Path) -> str:
    suffix = src.suffix.lower()
    if suffix in VIDEO_EXT:
        return "video"
    if suffix in SRT_EXT:
        return "subtitle"
    if suffix in DOC_EXT:
        return "subtitle" if looks_like_bracket_subtitle(src) else "doc"
    return "unknown"


def ingest(paths: list[str], out: Path, cols: int = 3, rows: int = 3,
           n_frames: int | None = None, duration: float | None = None,
           sub_format: str | None = None, max_doc_lines: int = 8,
           verbose: bool = True) -> dict:
    """摄入一批资料，写 evidence/index.json。返回 {'exit_code': int, ...}"""
    out = Path(out)
    (out / "evidence").mkdir(parents=True, exist_ok=True)
    sources: dict[str, dict] = {}
    items: list[dict] = []
    warnings: list[str] = []
    suspicious_subs: list[str] = []
    duration_by_stem: dict[str, float] = {}

    print(f"[ingest] 目标目录: {out.resolve()}")
    for p in paths:
        src = Path(p).expanduser()
        if not src.exists():
            warnings.append(f"资料不存在，已跳过: {src}")
            eprint(f"[learn-loop] 警告: 资料不存在，已跳过: {src}")
            continue
        kind = classify(src)
        if kind == "video":
            res = ingest_video(src, out, cols=cols, rows=rows, n_frames=n_frames, verbose=verbose)
            duration_by_stem[src.stem] = res["report"]["duration"]
        elif kind == "subtitle":
            ref = duration
            if ref is None:
                ref = duration_by_stem.get(src.stem) or duration_by_stem.get(
                    next(iter(duration_by_stem), ""), None)
            res = ingest_subtitles(src, out, ref, fmt=sub_format, verbose=verbose)
            if res["report"]["suspicious"]:
                suspicious_subs.append(src.name)
        elif kind == "doc":
            res = ingest_doc(src, out, max_lines=max_doc_lines, verbose=verbose)
        else:
            warnings.append(f"不支持的类型，已跳过: {src}")
            res = {"source": {"source_id": safe_name(src.name), "path": str(src),
                              "kind": "unknown", "skipped": True},
                   "items": [], "report": {"skipped": True, "note": "不支持的类型"},
                   "warnings": []}
        sources[res["source"]["source_id"]] = res["source"]
        items.extend(res["items"])
        warnings.extend(res["warnings"])

    index_path = out / "evidence" / "index.json"
    prev = util.read_json(index_path, default=None) or {}
    index_obj = {
        "version": 1, "generated_at": now_iso(), "out": str(out.resolve()),
        "cols": cols, "rows": rows,
        "sources": sources,
        "items": items,
        "stats": {"source_count": len(sources), "item_count": len(items),
                  "video_sources": sum(1 for s in sources.values() if s.get("kind") == "video"),
                  "subtitle_sources": sum(1 for s in sources.values() if s.get("kind") == "subtitle"),
                  "doc_sources": sum(1 for s in sources.values() if s.get("kind") == "doc")},
        "suspicious_subtitles": suspicious_subs,
        "warnings": warnings,
        "previous_generated_at": prev.get("generated_at"),
    }
    write_json(index_path, index_obj)

    # ledger
    util.append_jsonl(out / "ledger.jsonl", {
        "ts": now_iso(), "event": "ingest", "out": str(out.resolve()),
        "sources": sorted(sources), "item_count": len(items),
        "exit_code": 2 if suspicious_subs else 0,
    })

    print(f"[ingest] 来源 {len(sources)} 个，证据条目 {len(items)} 条 → {rel_to(index_path, out)}")
    code = 0
    if suspicious_subs:
        print(f"[ingest] ✗ 可疑字幕 {len(suspicious_subs)} 个: {', '.join(suspicious_subs)}")
        print("[ingest]   覆盖率 >105% / <50% / 存在 >20s 跳段 ⇒ 判可疑（退出码 2）")
        code = 2
    else:
        print("[ingest] ✓ 字幕物理体检通过")
    return {"exit_code": code, "index": index_obj, "index_path": str(index_path),
            "suspicious_subtitles": suspicious_subs, "warnings": warnings}
