"""模型调用层：只用标准库 urllib，配置来自环境变量。

约定：
  OPENAI_BASE_URL   例如 https://api.openai.com/v1 （或任何 OpenAI 兼容网关）
  OPENAI_API_KEY    密钥；**没有密钥时自动进入 mock 模式**，不报错
  MODEL             模型名，例如 gpt-4o-mini

mock 模式：确定性假响应。同一条 prompt 永远得到同一段文本，方便回归比对。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from .util import eprint


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None, mock: bool = False, timeout: int = 90):
        self.model = model or os.environ.get("MODEL") or os.environ.get("LEARN_LOOP_MODEL") or "gpt-4o-mini"
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key if api_key is not None else (os.environ.get("OPENAI_API_KEY") or "")
        env_mock = os.environ.get("LEARN_LOOP_MOCK", "").strip().lower() in {"1", "true", "yes", "on"}
        if mock or env_mock:
            self.mock, self.mock_reason = True, "--mock 指定" if mock else "LEARN_LOOP_MOCK=1"
        elif not self.api_key:
            self.mock, self.mock_reason = True, "未设置 OPENAI_API_KEY"
        else:
            self.mock, self.mock_reason = False, ""
        self.timeout = timeout
        self.calls = 0

    # ------------------------------------------------------------------ 真实调用
    def _post(self, payload: dict) -> dict:
        url = f"{self.base_url}/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        })
        last = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:400]
                except Exception:
                    pass
                last = f"HTTP {exc.code}: {body}"
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LLMError(last) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = f"网络错误: {exc}"
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LLMError(last) from exc
        raise LLMError(last or "未知错误")

    # ------------------------------------------------------------------ 公共接口
    def chat(self, system: str, user: str, mock_fn=None, temperature: float = 0.2,
             max_tokens: int = 1500) -> str:
        self.calls += 1
        if self.mock:
            if mock_fn is not None:
                return mock_fn(user)
            return self._deterministic_stub(system, user)
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        resp = self._post(payload)
        try:
            return resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"响应结构异常: {json.dumps(resp, ensure_ascii=False)[:300]}") from exc

    def chat_json(self, system: str, user: str, mock_fn, temperature: float = 0.1,
                  max_tokens: int = 2500):
        """要求模型输出 JSON；解析失败抛 LLMError。mock 模式下 mock_fn 直接返回对象。"""
        self.calls += 1
        if self.mock:
            return mock_fn(user)
        raw = self.chat(system, user, temperature=temperature, max_tokens=max_tokens)
        obj = extract_json(raw)
        if obj is None:
            raise LLMError(f"模型未返回可解析 JSON（前 300 字）: {raw[:300]}")
        return obj

    def _deterministic_stub(self, system: str, user: str) -> str:
        digest = hashlib.sha256((system + "\x00" + user).encode("utf-8")).hexdigest()[:12]
        return (f"[mock:{digest}] 确定性占位响应。未配置模型密钥，"
                f"本段内容不代表对资料的理解，仅用于打通流程。")

    def describe(self) -> str:
        if self.mock:
            return f"mock（{self.mock_reason}）"
        return f"{self.model} @ {self.base_url}"


def extract_json(text: str):
    """从自由文本里抠出第一个 JSON 对象/数组（容忍 ```json 代码块与前后废话）。"""
    if text is None:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if len(s.split("```")) > 1 else s
        s = s[4:] if s.lower().startswith("json") else s
    starts = [i for i in (s.find("{"), s.find("[")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[start:])
        return obj
    except json.JSONDecodeError:
        # 退一步：截到最后一个闭合括号
        for end in range(len(s), start, -1):
            try:
                return json.loads(s[start:end])
            except json.JSONDecodeError:
                continue
    return None
