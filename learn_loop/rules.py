"""判据（可机检规则）的默认定义。

禁用词表：明确禁止出现在笔记里的“无证据断言”措辞。
"""
from __future__ import annotations

import json
from pathlib import Path

DEFAULT_FORBIDDEN = [
    "众所周知",
    "显而易见",
    "显然",
    "大家都知道",
    "据我所知",
    "据说",
    "应该是这样",
    "肯定没问题",
    "毫无疑问",
    "无需证实",
]

DEFAULT_MIN_CITATIONS_PER_NOTE = 1
DEFAULT_EXAM_EVERY = 5


def load_forbidden(path: str | None = None, extra: list[str] | None = None) -> list[str]:
    words = list(DEFAULT_FORBIDDEN)
    if path:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(obj, dict):
            words = list(obj.get("forbidden", words))
        elif isinstance(obj, list):
            words = list(obj)
    for w in (extra or []):
        w = w.strip()
        if w and w not in words:
            words.append(w)
    return words
