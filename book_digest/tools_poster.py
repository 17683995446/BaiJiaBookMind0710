"""知识海报模块：从拆书结果提取金句，配 AI 生成图片，制作知识卡片。

流程：
1. extract_quotes: LLM 从知识单元中提炼经典金句（10 条）
2. generate_image: 调用硅基流动文生图 API 为每条金句生成配图
3. 组合成知识海报卡片

修复记录：
- 修复 material 切片逻辑（问题5）
- 使用 LLM 生成的 image_prompt 而非中文金句拼提示词（问题6）
- 删除死代码 generate_poster_cards（问题8）
- response_format 兼容降级（问题10）
- 删除未使用导入（问题17）
"""
from __future__ import annotations

import base64
import logging
import re
import time
import urllib.request
from pathlib import Path

from .config import AppConfig
from .llm import LLMClient
from .cache import DigestResult

logger = logging.getLogger("book_digest.poster")

# 文生图模型
IMAGE_MODEL = "Kwai-Kolors/Kolors"

# 图片过期天数
IMAGE_TTL_DAYS = 7


def extract_quotes(cfg: AppConfig, llm: LLMClient, result: DigestResult, n: int = 10) -> list[dict]:
    """用 LLM 从拆书结果中提炼经典金句，并为每条生成图片提示词。

    返回: [{"quote", "chapter", "interpretation", "image_prompt"}, ...]
    """
    # 收集所有知识单元内容作为素材
    unit_texts = []
    for u in result.units[:60]:
        unit_texts.append(f"[{u.chapter}] {u.content}")

    # 修复问题5: 正确的字符串截断
    material = "\n\n".join(unit_texts)[:8000]

    sys = (
        "你是书籍金句提炼大师。从给定的知识单元中，提炼出本书最精彩的金句/核心观点。"
        "要求：\n"
        "1. 提取或改写出 n 条金句，每条 15-40 字，朗朗上口、富有洞察\n"
        "2. 为每条金句标注出处章节\n"
        "3. 为每条金句写一句 15-30 字的解读\n"
        "4. 为每条金句设计一个英文图片提示词（image_prompt），"
        "风格统一为 minimalist artistic illustration, soft colors, inspirational，"
        "描述与金句意境相符的画面\n"
        "返回 JSON: {\"quotes\": [{\"quote\", \"chapter\", \"interpretation\", \"image_prompt\"}, ...]}"
    )
    user = f"书名：{result.book_title}\n金句数量：{n}\n\n知识单元素材：\n{material}"

    data = llm.chat_json(sys, user)
    quotes = data.get("quotes", []) if isinstance(data, dict) else []

    # 兜底：如果 LLM 返回不足，从 units 中补充
    if len(quotes) < n:
        for u in result.units:
            if len(quotes) >= n:
                break
            if any(u.content == q.get("quote") for q in quotes):
                continue
            quotes.append({
                "quote": u.content[:40],
                "chapter": u.chapter,
                "interpretation": u.content,
                "image_prompt": "minimalist illustration, book knowledge, soft gradient",
            })

    # 截断到 n 条
    return quotes[:n]


def generate_image(
    api_key: str,
    base_url: str,
    prompt: str,
    output_dir: Path,
    filename: str,
    size: str = "1024x1024",
) -> str | None:
    """调用硅基流动文生图 API 生成图片，保存到本地，返回文件名。

    失败时返回 None。
    修复问题10: response_format 兼容降级
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=base_url, timeout=120.0, max_retries=0)
    output_path = output_dir / f"{filename}.png"

    # 第一轮：尝试 b64_json
    for fmt in ("b64_json", "url"):
        try:
            resp = client.images.generate(
                model=IMAGE_MODEL,
                prompt=prompt,
                n=1,
                size=size,
                response_format=fmt,
            )
            if not resp.data:
                continue
            data0 = resp.data[0]
            b64 = getattr(data0, "b64_json", None)
            if b64:
                output_path.write_bytes(base64.b64decode(b64))
                logger.info(f"Image generated (b64): {filename}.png")
                return f"{filename}.png"
            # 尝试 url
            url = getattr(data0, "url", None)
            if url:
                urllib.request.urlretrieve(url, str(output_path))
                logger.info(f"Image generated (url): {filename}.png")
                return f"{filename}.png"
        except Exception as e:
            logger.warning(f"Image generation failed for {filename} (fmt={fmt}): {e}")
            continue

    # 最后降级：不指定 response_format
    try:
        resp = client.images.generate(
            model=IMAGE_MODEL,
            prompt=prompt,
            n=1,
            size=size,
        )
        if resp.data:
            url = getattr(resp.data[0], "url", None)
            if url:
                urllib.request.urlretrieve(url, str(output_path))
                logger.info(f"Image generated (fallback url): {filename}.png")
                return f"{filename}.png"
    except Exception as e:
        logger.warning(f"Image generation fallback failed for {filename}: {e}")

    return None


def cleanup_old_images(output_dir: Path, ttl_days: int = IMAGE_TTL_DAYS) -> int:
    """清理过期图片文件，返回删除数量。"""
    import time as _time
    now = _time.time()
    cutoff = now - ttl_days * 86400
    count = 0
    for f in output_dir.glob("poster_*.png"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                count += 1
        except OSError:
            pass
    if count:
        logger.info(f"Cleaned up {count} expired poster images")
    return count
