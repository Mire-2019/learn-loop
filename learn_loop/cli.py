"""单入口 CLI。

  learn-loop ingest <资料...> --out DIR [--cols 3 --rows 3 --frames 9] [--duration SEC]
  learn-loop notes  --out DIR [--max-notes 20] [--mock]
  learn-loop exam   --out DIR [--every 5] [--recheck] [--quiz] [--force] [--mock]
  learn-loop gate   --out DIR [--rules rules.json] [--add-forbidden 词1,词2]
  learn-loop evolve --target FILE (--append 文本 | --append-file FILE) [--inject-mismatch]
  learn-loop run    <资料...> --out DIR [--mock]

全局：--mock（无密钥也能跑通全流程）、--quiet、--model。

退出码：0 成功；ingest 判定可疑字幕=2；exam --recheck 出现失败/退化=1；
        gate 有规则失败=1；evolve 回读不一致回滚=3；模型调用失败=5（不产出伪造内容）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, evolve as evolve_mod, exam as exam_mod, gate as gate_mod, ingest as ingest_mod
from . import notes as notes_mod
from .llm import LLMError
from .util import eprint, now_iso

DESCRIPTION = "learn-loop —— 让 agent 学会一个领域的最小闭环（摄入 → 可核对笔记 → 综合考题 → 判据机检 → 写回技能）"


def _add_mock(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mock", action="store_true",
                   help="不调用任何真实模型，用确定性假响应跑通全流程")


def _add_common(p: argparse.ArgumentParser) -> None:
    """把 --mock / --quiet 也挂到每个子命令上（这样放前面或放后面都能用）。"""
    _add_mock(p)
    p.add_argument("--quiet", action="store_true", dest="quiet_sub", default=False,
                   help="少打印")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="learn-loop", description=DESCRIPTION,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="示例：\n"
                                        "  bin/learn-loop run tests/demo.mp4 tests/demo.srt --mock --out /tmp/ll-demo\n"
                                        "  bin/learn-loop gate --out /tmp/ll-demo\n")
    ap.add_argument("--version", action="version", version=f"learn-loop {__version__}")
    ap.add_argument("--model", default=os.environ.get("MODEL"), help="模型名（也可用环境变量 MODEL）")
    ap.add_argument("--quiet", action="store_true", help="少打印")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="摄入资料（视频/字幕/文档）")
    p.add_argument("paths", nargs="+", help="资料路径，可多个")
    p.add_argument("--out", required=True, help="产物目录")
    p.add_argument("--cols", type=int, default=3)
    p.add_argument("--rows", type=int, default=3)
    p.add_argument("--frames", type=int, default=None, help="抽帧总数，默认 cols×rows")
    p.add_argument("--duration", type=float, default=None, help="字幕覆盖率参考时长（秒）")
    p.add_argument("--sub-format", choices=["srt", "bracket"], default=None,
                   help="强制字幕格式（默认按扩展名/内容自动判断）")
    p.add_argument("--max-doc-lines", type=int, default=8)
    _add_common(p)

    p = sub.add_parser("notes", help="产出可核对笔记")
    p.add_argument("--out", required=True)
    p.add_argument("--max-notes", type=int, default=20)
    _add_common(p)

    p = sub.add_parser("exam", help="跨条目综合考题 / --recheck 防回归")
    p.add_argument("--out", required=True)
    p.add_argument("--every", type=int, default=5, help="每 N 条结论合成一道题（默认 5）")
    p.add_argument("--recheck", action="store_true", help="复跑旧题并把结果写进 ledger")
    p.add_argument("--quiz", action="store_true", help="只打印题目（不含答案）")
    p.add_argument("--force", action="store_true", help="已存在题目时强制重出")
    _add_common(p)

    p = sub.add_parser("gate", help="判据机检")
    p.add_argument("--out", required=True)
    p.add_argument("--rules", default=None, help="禁用词表 JSON（{'forbidden': [...]})")
    p.add_argument("--add-forbidden", default=None, help="追加禁用词，逗号分隔")
    p.add_argument("--json", dest="json_out", default=None, help="把报告另存一份到这个路径")
    p.add_argument("--quiet", action="store_true", dest="quiet_sub", default=False, help="少打印")

    p = sub.add_parser("evolve", help="把判据写回技能文件（写入后回读逐字节比对）")
    p.add_argument("--target", required=True, help="目标 .md（技能文件）")
    p.add_argument("--append", default=None, help="要追加的文本")
    p.add_argument("--append-file", default=None, help="要追加的文本文件")
    p.add_argument("--section", default=evolve_mod.DEFAULT_SECTION)
    p.add_argument("--inject-mismatch", action="store_true",
                   help="自检开关：故意让回读不一致，用于演练回滚")
    p.add_argument("--quiet", action="store_true", dest="quiet_sub", default=False, help="少打印")

    p = sub.add_parser("run", help="串起来跑一条资料（ingest → notes → exam → gate）")
    p.add_argument("paths", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--cols", type=int, default=3)
    p.add_argument("--rows", type=int, default=3)
    p.add_argument("--frames", type=int, default=None)
    p.add_argument("--every", type=int, default=5)
    p.add_argument("--max-notes", type=int, default=20)
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--max-doc-lines", type=int, default=8)
    p.add_argument("--skip-ingest", action="store_true", help="复用已有 evidence/index.json")
    _add_common(p)
    return ap


def cmd_run(args) -> int:
    out = Path(args.out)
    mock = getattr(args, "mock", False)
    codes: dict[str, int] = {}
    print("=" * 72)
    print(f"learn-loop run ｜ out={out} ｜ 模式={'mock' if mock else '真实模型'}")
    print("=" * 72)
    if args.skip_ingest and (out / "evidence" / "index.json").exists():
        print("[run] 跳过 ingest（--skip-ingest 且已有索引）")
        codes["ingest"] = 0
    else:
        res = ingest_mod.ingest(args.paths, out, cols=args.cols, rows=args.rows,
                                n_frames=args.frames, duration=args.duration,
                                max_doc_lines=args.max_doc_lines,
                                verbose=not args.quiet)
        codes["ingest"] = res["exit_code"]
        if res["exit_code"] != 0:
            print("[run] 摄入阶段判定字幕可疑：仍继续跑后续阶段，但最终退出码会带出来（这是有意的）")

    n = notes_mod.build_notes(out, mock=mock, max_notes=args.max_notes, model=args.model,
                              verbose=not args.quiet)
    codes["notes"] = n["exit_code"]

    e = exam_mod.generate(out, every=args.every, mock=mock, model=args.model,
                          verbose=not args.quiet)
    codes["exam"] = e["exit_code"]
    rc = exam_mod.recheck(out, verbose=not args.quiet)
    codes["exam_recheck"] = rc["exit_code"]

    g = gate_mod.run_gate(out, verbose=not args.quiet)
    codes["gate"] = g["exit_code"]

    print("-" * 72)
    for k, v in codes.items():
        print(f"  阶段 {k:12s} 退出码 {v}")
    final = max(codes.values()) if any(codes.values()) else 0
    if codes.get("gate"):
        final = 1
    print(f"[run] 产物目录: {out.resolve()}")
    print(f"[run] 最终退出码: {final}")
    return final


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    # --quiet 放在子命令前或后都要生效
    args.quiet = bool(getattr(args, "quiet", False) or getattr(args, "quiet_sub", False))
    try:
        return _dispatch(args, ap)
    except LLMError as exc:
        # 模型侧失败时绝不伪造输出：明确报错并提示可用 --mock 打通流程
        eprint(f"[learn-loop] 模型调用失败（未产出任何伪造内容）: {exc}")
        eprint("[learn-loop] 提示: 加 --mock 可在没有可用密钥/网络时跑通全流程；"
               "或检查 OPENAI_BASE_URL / OPENAI_API_KEY / MODEL")
        return 5
    except KeyboardInterrupt:
        eprint("[learn-loop] 已中断")
        return 130


def _dispatch(args, ap) -> int:
    cmd = args.cmd
    if cmd == "ingest":
        res = ingest_mod.ingest(args.paths, Path(args.out), cols=args.cols, rows=args.rows,
                                n_frames=args.frames, duration=args.duration,
                                sub_format=args.sub_format,
                                max_doc_lines=args.max_doc_lines, verbose=not args.quiet)
        return res["exit_code"]
    if cmd == "notes":
        return notes_mod.build_notes(Path(args.out), mock=args.mock,
                                     max_notes=args.max_notes, model=args.model,
                                     verbose=not args.quiet)["exit_code"]
    if cmd == "exam":
        out = Path(args.out)
        if args.quiz:
            return exam_mod.quiz(out)
        code = exam_mod.generate(out, every=args.every, mock=args.mock, model=args.model,
                                 verbose=not args.quiet, force=args.force)["exit_code"]
        if args.recheck:
            code = max(code, exam_mod.recheck(out, verbose=not args.quiet)["exit_code"])
        return code
    if cmd == "gate":
        extra = [w for w in (args.add_forbidden or "").split(",") if w.strip()]
        return gate_mod.run_gate(Path(args.out), rules_path=args.rules,
                                 extra_forbidden=extra, verbose=not args.quiet,
                                 json_out=args.json_out)["exit_code"]
    if cmd == "evolve":
        return evolve_mod.evolve(args.target, fragment=args.append,
                                 append_file=args.append_file, section=args.section,
                                 inject_mismatch=args.inject_mismatch)["exit_code"]
    if cmd == "run":
        return cmd_run(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
