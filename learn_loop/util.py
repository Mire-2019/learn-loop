"""通用工具：JSON/JSONL、哈希、时间格式、ffmpeg/ffprobe 调用、路径处理。

只用标准库。ffmpeg/ffprobe 通过 PATH 查找，找不到就如实报错而不是假装成功。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 首选等宽/无衬线字体，用于把时间戳烧进像素
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".flv", ".ts", ".mpg", ".mpeg"}
SRT_EXT = {".srt", ".vtt"}
DOC_EXT = {".md", ".markdown", ".txt", ".pdf", ".rst", ".text"}


# --------------------------------------------------------------------------- 基础 IO

def eprint(*args) -> None:
    print(*args, file=sys.stderr)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_text(path: str | os.PathLike) -> str:
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def write_text(path: str | os.PathLike, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def read_json(path: str | os.PathLike, default=None):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(read_text(p))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"[learn-loop] JSON 解析失败: {p}: {exc}") from exc


def write_json(path: str | os.PathLike, obj, indent: int = 2) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    write_text(p, json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=False) + "\n")


def read_jsonl(path: str | os.PathLike) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    for i, line in enumerate(read_text(p).splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            eprint(f"[learn-loop] 警告: 跳过损坏的 jsonl 行 {p}:{i}")
    return out


def append_jsonl(path: str | os.PathLike, obj) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def write_jsonl(path: str | os.PathLike, rows: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- 时间

def hms(seconds: float) -> str:
    """00:00:12.000"""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def srt_time(seconds: float) -> str:
    """00:00:12,000（SRT 风格）"""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_time_token(tok: str) -> float | None:
    """接受 00:00:12,500 / 00:00:12.500 / 00:12:30 / 02:05 / 7.5 等写法 → 秒。"""
    tok = (tok or "").strip().replace(",", ".")
    if not tok:
        return None
    parts = tok.split(":")
    if len(parts) > 3:
        return None
    nums: list[float] = []
    for p in parts:
        p = p.strip()
        if not re.fullmatch(r"\d+(?:\.\d+)?", p):
            return None
        nums.append(float(p))
    sec = 0.0
    for n in nums:
        sec = sec * 60.0 + n
    return sec


# --------------------------------------------------------------------------- 路径

def safe_name(name: str) -> str:
    """把来源文件名变成可用作目录名的安全 id。"""
    s = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+", "_", str(name)).strip("._")
    return s or "src"


def rel_to(path: str | os.PathLike, base: str | os.PathLike) -> str:
    try:
        return Path(path).resolve().relative_to(Path(base).resolve()).as_posix()
    except ValueError:
        return str(path)


def find_font() -> str | None:
    for c in _FONT_CANDIDATES:
        if Path(c).exists():
            return c
    exe = shutil.which("fc-match")
    if exe:
        try:
            out = subprocess.run([exe, "-f", "%{file}", "DejaVu Sans"],
                                 capture_output=True, text=True, timeout=10)
            p = (out.stdout or "").strip()
            if p and Path(p).exists():
                return p
        except Exception:
            pass
    return None


# --------------------------------------------------------------------------- 进程

def which(name: str) -> str | None:
    return shutil.which(name)


def run_cmd(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        return 127, "", f"{exc}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(cmd[:3])} ..."
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def ffprobe_duration(path: str | os.PathLike) -> float | None:
    exe = which("ffprobe")
    if not exe:
        return None
    rc, out, _ = run_cmd([exe, "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], timeout=60)
    if rc != 0:
        return None
    try:
        return float(out.strip())
    except ValueError:
        return None


def ffprobe_summary(path: str | os.PathLike) -> dict:
    exe = which("ffprobe")
    if not exe:
        return {"error": "ffprobe 不可用"}
    rc, out, err = run_cmd([exe, "-v", "error", "-print_format", "json",
                            "-show_format", "-show_streams", str(path)], timeout=60)
    if rc != 0:
        return {"error": err.strip()[:500]}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"error": "ffprobe 输出非 JSON"}
