"""LLM 适配层：统一封装 OpenAI 兼容接口。

覆盖 OpenAI / DeepSeek / Gemini(兼容) / Moonshot 等，只需配置 base_url + key。
借鉴 ebook-to-mindmap 的多模型支持思路。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from .config import LLMConfig

logger = logging.getLogger("book_digest.llm")

# 可重试的异常类型（网络/超时/临时服务不可用）
_RETRYABLE_ERRORS = (
    ConnectionError,
    TimeoutError,
)


class LLMClient:
    """对 openai SDK 的薄封装，带重试与 JSON 解析容错。"""

    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self.cfg.api_key,
                base_url=self.cfg.base_url,
                timeout=120.0,  # 2 分钟超时
                max_retries=0,   # 我们自己管理重试
            )
            logger.info(f"LLM client initialized: base_url={self.cfg.base_url}, model={self.cfg.model}")
        return self._client

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        """同步对话。json_mode=True 时尝试返回合法 JSON。"""
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        # 关闭思考型模型的 reasoning（如 Qwen3.5），通过 extra_body 传递
        if self.cfg.enable_thinking is False:
            kwargs["extra_body"] = {"enable_thinking": False}

        for attempt in range(3):
            try:
                resp = self.client.chat.completions.create(**kwargs)
                msg = resp.choices[0].message
                content = msg.content or ""
                # 兜底：thinking 模型有时 content 为空，取 reasoning_content
                if not content and getattr(msg, "reasoning_content", None):
                    content = msg.reasoning_content
                return content
            except _RETRYABLE_ERRORS as e:
                if attempt == 2:
                    logger.error(f"LLM request failed after 3 retries (network): {e}")
                    raise
                wait = 2 ** attempt
                logger.warning(f"LLM retry {attempt+1}/3 after {wait}s (network error: {e})")
                time.sleep(wait)
            except Exception as e:
                # 不可重试的错误（认证失败、参数错误等）直接抛出
                err_str = str(e).lower()
                if any(k in err_str for k in ["auth", "api key", "invalid", "forbidden", "unauthorized"]):
                    logger.error(f"LLM auth/param error (not retryable): {e}")
                    raise
                if attempt == 2:
                    logger.error(f"LLM request failed after 3 retries: {e}")
                    raise
                wait = 2 ** attempt
                logger.warning(f"LLM retry {attempt+1}/3 after {wait}s: {e}")
                time.sleep(wait)
        return ""

    def chat_json(self, system: str, user: str) -> Any:
        """对话并解析为 JSON（带容错：剥离 ```json 围栏、补全截断）。"""
        raw = self.chat(system, user, json_mode=True)
        # 即使指定了 json_object，部分模型仍可能带围栏
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 尝试定位首个 { 到末尾 }
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    pass
            # 最后兜底：返回原始文本包裹
            return {"_raw": raw}
