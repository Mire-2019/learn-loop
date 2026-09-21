"""把学到的判据写回一个技能文件（.md），并保证写入是可信的。

流程：
  1. 读原文件（字节）并算 sha256
  2. 备份成 <target>.bak
  3. 追加带标记的片段（默认带 <!-- learn-loop:criteria v1 --> 标记，已存在则跳过，避免重复写）
  4. **写入后回读，逐字节比对**：回读 sha256 == 预期 sha256 才算成功
  5. 不一致 → 从 .bak 回滚，并验证回滚后哈希与原文件一致，退出码 3

`--inject-mismatch` 是自检开关：写完故意把文件改坏一次，用来演练回滚路径。
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from . import util
from .util import now_iso

MARKER = "<!-- learn-loop:criteria v1 -->"
DEFAULT_SECTION = "## 机器判据（learn-loop 写回）"


def _first_diff(a: bytes, b: bytes) -> int | None:
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return i
    if len(a) != len(b):
        return min(len(a), len(b))
    return None


def evolve(target: str | Path, fragment: str | None = None, append_file: str | None = None,
           section: str = DEFAULT_SECTION, inject_mismatch: bool = False,
           verbose: bool = True) -> dict:
    target = Path(target).expanduser()
    if not target.exists():
        raise SystemExit(f"[evolve] 目标文件不存在: {target}")
    if fragment is None and append_file:
        fragment = util.read_text(append_file)
    if fragment is None:
        raise SystemExit("[evolve] 必须提供 --append 或 --append-file")
    fragment = fragment.rstrip("\n") + "\n"
    if section and section not in fragment:
        fragment = f"{section}\n\n{MARKER}\n{fragment}"
    else:
        fragment = f"{MARKER}\n{fragment}"

    orig = target.read_bytes()
    orig_hash = hashlib.sha256(orig).hexdigest()
    bak = Path(str(target) + ".bak")

    if MARKER.encode("utf-8") in orig:
        if verbose:
            print(f"[evolve] 目标里已存在标记 {MARKER}，不重复写回（幂等）")
            print(f"[evolve] 目标: {target}")
            print(f"[evolve] sha256: {orig_hash}（未改动）")
        util.append_jsonl(target.parent / "evolve_ledger.jsonl", {
            "ts": now_iso(), "event": "evolve_skip", "target": str(target.resolve()),
            "sha256": orig_hash,
        })
        return {"exit_code": 0, "status": "skipped_duplicate", "target": str(target),
                "sha256_before": orig_hash, "sha256_after": orig_hash}

    shutil.copyfile(target, bak)
    bak_hash = util.sha256_file(bak)
    separator = b"" if orig.endswith(b"\n") or not orig else b"\n"
    new_bytes = orig + separator + fragment.encode("utf-8")
    expected_hash = hashlib.sha256(new_bytes).hexdigest()

    with open(target, "wb") as fh:
        fh.write(new_bytes)

    if inject_mismatch:
        # 自检：故意把写出去的内容改坏，用于验证“回读不一致 → 回滚”路径
        with open(target, "ab") as fh:
            fh.write(b"\n<!-- injected-mismatch-for-self-test -->\n")
        if verbose:
            print("[evolve] ⚠ --inject-mismatch：已故意破坏写入内容，用于演练回滚")

    readback = target.read_bytes()
    readback_hash = hashlib.sha256(readback).hexdigest()
    ok = readback_hash == expected_hash

    if verbose:
        print(f"[evolve] 目标: {target}")
        print(f"[evolve] 备份: {bak}（sha256 {bak_hash[:16]}…）")
        print(f"[evolve] 追加片段 {len(fragment.encode('utf-8'))} 字节，断言 {len(new_bytes)} 字节")
        print(f"[evolve] 改前 sha256: {orig_hash}")
        print(f"[evolve] 改后 sha256(预期): {expected_hash}")
        print(f"[evolve] 回读 sha256(实际): {readback_hash}")

    if ok:
        if verbose:
            print(f"[evolve] ✓ 回读逐字节一致（{len(readback)} 字节）")
        util.append_jsonl(target.parent / "evolve_ledger.jsonl", {
            "ts": now_iso(), "event": "evolve_write", "target": str(target.resolve()),
            "sha256_before": orig_hash, "sha256_after": expected_hash,
            "bytes_before": len(orig), "bytes_after": len(new_bytes),
            "backup": str(bak),
        })
        return {"exit_code": 0, "status": "ok", "target": str(target),
                "sha256_before": orig_hash, "sha256_after": expected_hash,
                "backup": str(bak), "bytes_after": len(new_bytes)}

    # ---- 回读不一致：回滚
    diff_at = _first_diff(new_bytes, readback)
    with open(bak, "rb") as src_fh, open(target, "wb") as dst_fh:
        shutil.copyfileobj(src_fh, dst_fh)
    restored_hash = util.sha256_file(target)
    restored_ok = restored_hash == orig_hash
    if verbose:
        print(f"[evolve] ✗ 回读不一致！首个不同字节偏移 = {diff_at}")
        print(f"[evolve]   预期 {len(new_bytes)} 字节 / 实际 {len(readback)} 字节")
        print(f"[evolve] 已从 .bak 回滚 → 回滚后 sha256 = {restored_hash}"
              f"（与原文件{'一致 ✓' if restored_ok else '不一致 ✗'}）")
        print("[evolve] 退出码 3（写回失败，文件已还原）")
    util.append_jsonl(target.parent / "evolve_ledger.jsonl", {
        "ts": now_iso(), "event": "evolve_rollback", "target": str(target.resolve()),
        "expected_sha256": expected_hash, "readback_sha256": readback_hash,
        "diff_offset": diff_at, "restored_sha256": restored_hash,
        "restore_verified": restored_ok, "backup": str(bak),
    })
    return {"exit_code": 3, "status": "rollback", "target": str(target),
            "expected_sha256": expected_hash, "readback_sha256": readback_hash,
            "restored_sha256": restored_hash, "restore_verified": restored_ok,
            "diff_offset": diff_at, "backup": str(bak)}
