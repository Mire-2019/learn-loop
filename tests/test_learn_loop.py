"""learn-loop 自测（只用标准库 unittest，不需要 pytest、不需要密钥）。

    python3 -m unittest discover -s tests -v

涉及 ffmpeg 的用例在没有 ffmpeg 或没有 tests/demo.mp4 时自动 skip。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from learn_loop import evolve as evolve_mod          # noqa: E402
from learn_loop import exam as exam_mod              # noqa: E402
from learn_loop import gate as gate_mod              # noqa: E402
from learn_loop import ingest as ingest_mod          # noqa: E402
from learn_loop import notes as notes_mod            # noqa: E402
from learn_loop import util                          # noqa: E402
from learn_loop.llm import LLM, LLMError             # noqa: E402

DEMO_VIDEO = ROOT / "tests" / "demo.mp4"
HAS_FFMPEG = bool(util.which("ffmpeg")) and bool(util.which("ffprobe"))


class TimeHelperTest(unittest.TestCase):
    def test_parse_time_token(self):
        self.assertEqual(util.parse_time_token("00:00:12,500"), 12.5)
        self.assertEqual(util.parse_time_token("02:05"), 125.0)
        self.assertEqual(util.parse_time_token("1:02:03"), 3723.0)
        self.assertIsNone(util.parse_time_token("abc"))

    def test_hms_and_srt(self):
        self.assertEqual(util.hms(3661.25), "01:01:01.250")
        self.assertEqual(util.srt_time(12.5), "00:00:12,500")


class SubtitleParseTest(unittest.TestCase):
    def test_parse_srt(self):
        cues = ingest_mod.parse_srt(util.read_text(ROOT / "tests" / "demo.srt"))
        self.assertEqual(len(cues), 10)
        self.assertEqual(cues[0]["start"], 0.0)
        self.assertEqual(cues[-1]["end"], 30.0)
        self.assertTrue(all(c["text"] for c in cues))

    def test_parse_bracket(self):
        cues = ingest_mod.parse_bracket(util.read_text(ROOT / "tests" / "demo_bracket.txt"))
        self.assertEqual(len(cues), 6)
        self.assertEqual([c["start"] for c in cues], [0.0, 5.0, 10.0, 15.0, 20.0, 25.0])
        self.assertEqual(cues[-1]["end"], 27.0)          # 末条 = 起点 + 2s 兜底
        self.assertTrue(all(c["end_inferred"] for c in cues))

    def test_good_subtitle_passes(self):
        cues = ingest_mod.parse_srt(util.read_text(ROOT / "tests" / "demo.srt"))
        rep = ingest_mod.check_subtitles(cues, 30.0)
        self.assertFalse(rep["suspicious"])
        self.assertAlmostEqual(rep["coverage"], 1.0, places=3)
        self.assertEqual(rep["jumps"], [])

    def test_low_coverage_and_jump_is_suspicious(self):
        cues = ingest_mod.parse_srt(util.read_text(ROOT / "tests" / "demo_bad.srt"))
        rep = ingest_mod.check_subtitles(cues, 30.0)
        self.assertTrue(rep["suspicious"])
        self.assertLess(rep["coverage"], ingest_mod.COVERAGE_LOW)
        self.assertGreaterEqual(len(rep["jumps"]), 1)
        self.assertGreater(rep["max_jump"], 20.0)
        self.assertTrue(any("50%" in r for r in rep["reasons"]))

    def test_over_105_percent_is_suspicious(self):
        cues = [{"index": i + 1, "start": float(i), "end": float(i) + 1.6,
                 "text": "x", "raw_line": i, "end_inferred": False} for i in range(20)]
        rep = ingest_mod.check_subtitles(cues, 30.0)
        self.assertTrue(rep["suspicious"])
        self.assertGreater(rep["coverage"], ingest_mod.COVERAGE_HIGH)
        self.assertTrue(any("105%" in r for r in rep["reasons"]))

    def test_bracket_file_detection(self):
        self.assertTrue(ingest_mod.looks_like_bracket_subtitle(ROOT / "tests" / "demo_bracket.txt"))
        self.assertFalse(ingest_mod.looks_like_bracket_subtitle(ROOT / "tests" / "doc_notes.md"))


class MockLLMTest(unittest.TestCase):
    def test_mock_is_deterministic_and_keyless(self):
        a = LLM(api_key="", mock=True)
        b = LLM(api_key="", mock=True)
        self.assertTrue(a.mock)
        self.assertEqual(a.chat("s", "同样的 prompt"), b.chat("s", "同样的 prompt"))
        self.assertNotEqual(a.chat("s", "同样的 prompt"), a.chat("s", "另一个 prompt"))

    def test_mock_auto_enabled_without_key(self):
        self.assertTrue(LLM(api_key="").mock)

    def test_extract_json_tolerant(self):
        from learn_loop.llm import extract_json
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('说点废话 {"a": [1,2]} 后面还有'), {"a": [1, 2]})
        self.assertIsNone(extract_json("没有 JSON"))


class NotesAndGateTest(unittest.TestCase):
    """用「文档 + [mm:ss] 字幕」两来源跑 ingest→notes→gate，不依赖 ffmpeg。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ll-notes-"))
        shutil.copy(ROOT / "tests" / "doc_notes.md", self.tmp / "doc_notes.md")
        shutil.copy(ROOT / "tests" / "demo_bracket.txt", self.tmp / "demo_bracket.txt")
        self.out = self.tmp / "out"
        ingest_mod.ingest([str(self.tmp / "doc_notes.md"), str(self.tmp / "demo_bracket.txt")],
                          self.out, duration=30.0, verbose=False)
        notes_mod.build_notes(self.out, mock=True, verbose=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_note_has_resolvable_citation(self):
        index = util.read_json(self.out / "evidence" / "index.json")
        valid = {i["id"] for i in index["items"]}
        notes = util.read_json(self.out / "notes.json")["items"]
        self.assertGreater(len(notes), 0)
        for n in notes:
            self.assertTrue(n["citations"], f"{n['id']} 没有引用")
            for c in n["citations"]:
                self.assertIn(c, valid)

    def test_notes_are_balanced_across_sources(self):
        notes = util.read_json(self.out / "notes.json")["items"]
        srcs = {s for n in notes for s in n["sources"]}
        self.assertEqual(len(srcs), 2)

    def test_gate_passes_on_healthy_output(self):
        res = gate_mod.run_gate(self.out, verbose=False)
        self.assertEqual(res["exit_code"], 0, res["report"]["results"])
        self.assertEqual(res["report"]["passed"], res["report"]["total"])
        self.assertTrue((self.out / "gate" / "report.md").exists())

    def test_gate_fails_on_broken_citation(self):
        obj = util.read_json(self.out / "notes.json")
        obj["items"][0]["citations"] = ["ev:does-not-exist:v1"]
        util.write_json(self.out / "notes.json", obj)
        res = gate_mod.run_gate(self.out, verbose=False)
        self.assertEqual(res["exit_code"], 1)
        self.assertIn("R3", res["failed"])

    def test_gate_fails_on_missing_artifact(self):
        idx = util.read_json(self.out / "evidence" / "index.json")
        frame_like = next(i for i in idx["items"] if i["kind"] == "doc_line")
        victim = self.out / frame_like["artifact"]
        backup = victim.read_bytes()
        victim.unlink()
        try:
            res = gate_mod.run_gate(self.out, verbose=False)
            self.assertEqual(res["exit_code"], 1)
            self.assertIn("R4", res["failed"])
        finally:
            victim.write_bytes(backup)

    def test_gate_fails_on_forbidden_word(self):
        md = self.out / "notes.md"
        md.write_text(util.read_text(md) + "\n众所周知的结论。\n", encoding="utf-8")
        res = gate_mod.run_gate(self.out, verbose=False)
        self.assertEqual(res["exit_code"], 1)
        self.assertIn("R5", res["failed"])


class ExamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ll-exam-"))
        for f in ("doc_notes.md", "demo_bracket.txt", "demo.srt"):
            shutil.copy(ROOT / "tests" / f, self.tmp / f)
        self.out = self.tmp / "out"
        ingest_mod.ingest([str(self.tmp / "doc_notes.md"), str(self.tmp / "demo_bracket.txt"),
                           str(self.tmp / "demo.srt")], self.out, duration=30.0, verbose=False)
        notes_mod.build_notes(self.out, mock=True, verbose=False)
        exam_mod.generate(self.out, every=5, mock=True, verbose=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_questions_are_cross_entry_and_cross_source(self):
        qs = util.read_jsonl(self.out / "exam.jsonl")
        self.assertGreater(len(qs), 0)
        for q in qs:
            self.assertGreaterEqual(len(q["covers"]), 2)
            self.assertGreaterEqual(len(q["sources"]), 2)
            self.assertNotIn("答案", q["question"])

    def test_answers_are_stored_separately(self):
        qs = util.read_jsonl(self.out / "exam.jsonl")
        keys = {k["id"]: k for k in util.read_jsonl(self.out / "exam_key.jsonl")}
        self.assertEqual({q["id"] for q in qs}, set(keys))
        for q in qs:
            self.assertNotIn("answer", q)          # 题目文件里没有答案
            self.assertIn("answer", keys[q["id"]])

    def test_key_hash_matches_answer(self):
        for k in util.read_jsonl(self.out / "exam_key.jsonl"):
            self.assertEqual(k["answer_sha256"],
                             hashlib.sha256(k["answer"].encode("utf-8")).hexdigest())

    def test_recheck_passes_then_reports_regression(self):
        first = exam_mod.recheck(self.out, verbose=False)
        self.assertEqual(first["exit_code"], 0)
        self.assertEqual(first["regressions"], [])

        # 破坏存档参考答案（模拟“上次通过、这次失败”）
        keys = util.read_jsonl(self.out / "exam_key.jsonl")
        keys[0]["answer"] = keys[0]["answer"] + "\n（被人手工改过一行）"
        util.write_jsonl(self.out / "exam_key.jsonl", keys)

        second = exam_mod.recheck(self.out, verbose=False)
        self.assertEqual(second["exit_code"], 1)
        self.assertIn(keys[0]["id"], second["failed"])
        self.assertIn(keys[0]["id"], second["regressions"])
        rows = [r for r in util.read_jsonl(self.out / "ledger.jsonl")
                if r.get("event") == "exam_recheck" and r.get("qid") == keys[0]["id"]]
        self.assertEqual(rows[-1]["prev_status"], "pass")
        self.assertTrue(rows[-1]["regression"])

    def test_quiz_prints_questions_without_answers(self):
        rc = exam_mod.quiz(self.out)
        self.assertEqual(rc, 0)


class EvolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ll-evolve-"))
        self.target = self.tmp / "SKILL.md"
        self.target.write_text("---\nname: demo\n---\n\n# 技能\n\n原有内容。\n", encoding="utf-8")
        self.原 = self.target.read_bytes()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_and_readback_identical(self):
        res = evolve_mod.evolve(self.target, fragment="- 判据：每条结论必须带引用\n", verbose=False)
        self.assertEqual(res["exit_code"], 0)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["sha256_after"], util.sha256_file(self.target))
        self.assertIn("每条结论必须带引用", util.read_text(self.target))
        # 备份 == 改前原文（逐字节）
        self.assertEqual(Path(res["backup"]).read_bytes(), self.原)
        self.assertTrue(self.target.read_bytes().startswith(self.原))

    def test_idempotent_second_write_is_skipped(self):
        evolve_mod.evolve(self.target, fragment="- 判据 A\n", verbose=False)
        h1 = util.sha256_file(self.target)
        res2 = evolve_mod.evolve(self.target, fragment="- 判据 A\n", verbose=False)
        self.assertEqual(res2["status"], "skipped_duplicate")
        self.assertEqual(h1, util.sha256_file(self.target))

    def test_readback_mismatch_rolls_back_byte_exact(self):
        res = evolve_mod.evolve(self.target, fragment="- 判据 B\n",
                               inject_mismatch=True, verbose=False)
        self.assertEqual(res["exit_code"], 3)
        self.assertEqual(res["status"], "rollback")
        self.assertTrue(res["restore_verified"])
        self.assertEqual(self.target.read_bytes(), self.原)      # 逐字节还原
        self.assertEqual(res["restored_sha256"], hashlib.sha256(self.原).hexdigest())
        self.assertNotEqual(res["readback_sha256"], res["expected_sha256"])


@unittest.skipUnless(HAS_FFMPEG and DEMO_VIDEO.exists(),
                     "需要 ffmpeg 与 tests/demo.mp4（先跑 tests/make_demo_video.sh）")
class VideoIngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ll-video-"))
        self.out = self.tmp / "out"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_frames_sheets_and_burned_timestamps(self):
        res = ingest_mod.ingest([str(DEMO_VIDEO)], self.out, n_frames=3, verbose=False)
        idx = res["index"]
        self.assertEqual(idx["stats"]["video_sources"], 1)
        src = next(iter(idx["sources"].values()))
        self.assertEqual(src["frame_count"], 3)
        self.assertEqual(src["sheet_count"], 1)
        self.assertTrue(src["duration"] and 29.0 < src["duration"] < 31.0)
        rep = util.read_json(self.out / "evidence" / "index.json")
        video_rep = next(i for i in rep["items"] if i["kind"] == "video_report")
        self.assertIn("时间戳烧入像素=True", video_rep["text"])
        for item in [i for i in rep["items"] if i["kind"] == "video_frame"]:
            self.assertTrue((self.out / item["artifact"]).exists(), item["artifact"])
            self.assertAlmostEqual(item["seconds"], util.parse_time_token(item["locator"]), places=3)
        sheet = next(i for i in rep["items"] if i["kind"] == "video_sheet")
        self.assertTrue((self.out / sheet["artifact"]).exists())
        self.assertGreater((self.out / sheet["artifact"]).stat().st_size, 1000)

    def test_suspicious_subtitle_makes_ingest_exit_nonzero(self):
        res = ingest_mod.ingest([str(DEMO_VIDEO), str(ROOT / "tests" / "demo_bad.srt")],
                                self.out, n_frames=2, verbose=False)
        self.assertEqual(res["exit_code"], 2)
        self.assertEqual(res["suspicious_subtitles"], ["demo_bad.srt"])


class CliTest(unittest.TestCase):
    def test_subcommands_present(self):
        from learn_loop.cli import build_parser
        ap = build_parser()
        self.assertEqual(sorted(ap._subparsers._group_actions[0].choices),
                         ["evolve", "exam", "gate", "ingest", "notes", "run"])

    def test_gate_cli_exit_code(self):
        from learn_loop.cli import main
        tmp = Path(tempfile.mkdtemp(prefix="ll-cli-"))
        try:
            shutil.copy(ROOT / "tests" / "doc_notes.md", tmp / "doc_notes.md")
            out = tmp / "out"
            self.assertEqual(main(["ingest", str(tmp / "doc_notes.md"), "--out", str(out),
                                   "--quiet"]), 0)
            self.assertEqual(main(["notes", "--out", str(out), "--mock", "--quiet"]), 0)
            self.assertEqual(main(["gate", "--out", str(out)]), 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RealModelPathTest(unittest.TestCase):
    """覆盖“真实模型分支”里不需要外网也能测的部分（为已修 bug 加回归测试）。"""

    def test_real_notes_prompt_builds_with_max_notes_none(self):
        # 回归：max_notes=None 时曾触发 "%d format: a real number is required, not NoneType"
        index = {"items": [{"id": "ev:d:x:00001", "kind": "doc_line", "locator": "L1", "text": "样例"}],
                 "sources": {}, "out": str(ROOT), "cols": 3, "rows": 3}
        notes, warnings = notes_mod._real_notes(index, LLM(mock=True), max_notes=None)
        self.assertEqual(notes, [])
        self.assertEqual(warnings, [])

    def test_llm_error_is_raised_not_swallowed(self):
        llm = LLM(api_key="sk-bogus", base_url="http://127.0.0.1:9/v1", timeout=2)
        self.assertFalse(llm.mock)               # 有密钥 ⇒ 不走 mock
        with self.assertRaises(LLMError):
            llm.chat("system", "user")

    def test_cli_exit_5_when_model_unreachable_and_no_fabrication(self):
        tmp = Path(tempfile.mkdtemp(prefix="ll-unreachable-"))
        out = tmp / "out"
        saved = {k: os.environ.get(k) for k in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "LEARN_LOOP_MOCK")}
        try:
            shutil.copy(ROOT / "tests" / "doc_notes.md", tmp / "doc_notes.md")
            ingest_mod.ingest([str(tmp / "doc_notes.md")], out, verbose=False)
            os.environ.pop("LEARN_LOOP_MOCK", None)
            os.environ["OPENAI_API_KEY"] = "sk-bogus"
            os.environ["OPENAI_BASE_URL"] = "http://127.0.0.1:9/v1"   # 必然连接失败，且不出网
            from learn_loop.cli import main as cli_main
            code = cli_main(["notes", "--out", str(out), "--quiet"])
            self.assertEqual(code, 5)             # 失败要显式返回 5，而不是伪造笔记
            self.assertFalse((out / "notes.md").exists() or (out / "notes.json").exists())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
