"""文本转语音（TTS）听书工具。

基于微软 Edge 免费 TTS（edge-tts 库）实现，无需 GPU、支持中文。
把拆书总结（DigestResult）转为可收听的 mp3 音频。

提供三个能力：
1. text_to_speech    —— 单段文本转 mp3（核心合成函数）
2. generate_audiobook —— 把拆书总结转为听书音频（summary / chapters / full 三种模式）
3. list_voices       —— 常用中文音色列表

注意：edge-tts 是异步库（基于 aiohttp / WebSocket），但本模块对外暴露的是
**同步**接口，内部用 asyncio.run() 包装异步协程，调用方无需关心 async/await。

依赖：pip install edge-tts
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

try:
    import edge_tts
except ImportError as _e:  # 给出更友好的安装提示
    raise ImportError(
        "缺少 edge-tts 依赖，请先安装：pip install edge-tts"
    ) from _e

from .config import DigestResult


# ════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════

# 中文 TTS 正常语速下大约每秒朗读字数（用于估算时长，4 字/秒较接近实际）
_CHARS_PER_SECOND = 4

# edge-tts 单次合成请求的安全字数上限（留足余量，避免超限报错）
_MAX_CHARS_PER_REQUEST = 1800


# 常用中文音色（硬编码，避免每次都去查在线列表）
_CN_VOICES: list[dict] = [
    {
        "id": "zh-CN-YunxiNeural",
        "name": "云希",
        "gender": "男",
        "description": "青年男声，自然亲切，适合知识解读 / 有声书",
    },
    {
        "id": "zh-CN-XiaoxiaoNeural",
        "name": "晓晓",
        "gender": "女",
        "description": "成年女声，温柔清晰，通用性最强",
    },
    {
        "id": "zh-CN-YunyangNeural",
        "name": "云扬",
        "gender": "男",
        "description": "专业男播音腔，沉稳大气，适合新闻 / 正式解说",
    },
    {
        "id": "zh-CN-XiaoyiNeural",
        "name": "晓伊",
        "gender": "女",
        "description": "年轻女声，活泼亲和，适合轻松内容",
    },
    {
        "id": "zh-CN-YunjianNeural",
        "name": "云健",
        "gender": "男",
        "description": "成熟男声，浑厚有力，适合历史 / 体育题材",
    },
    {
        "id": "zh-CN-XiaohanNeural",
        "name": "晓涵",
        "gender": "女",
        "description": "温暖女声，富有感情，适合文学 / 情感内容",
    },
]


# ════════════════════════════════════════════════════════════
# 文本工具
# ════════════════════════════════════════════════════════════

def _sanitize_filename(name: str) -> str:
    """把书名 / 章节名清洗为合法的文件名片段。"""
    if not name:
        return "book"
    # 去掉文件系统不允许的字符
    cleaned = re.sub(r'[\\/:*?"<>|\n\r\t]', "", str(name)).strip()
    # 折叠多余空白为下划线
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "book"


def _clean_text_for_tts(text: str) -> str:
    """清理 Markdown 等排版标记，避免 TTS 把 `#`、`*` 等朗读出来。

    只做轻度清理：去标题井号、加粗 / 斜体星号、行内代码反引号、
    列表符号、链接与图片语法，并折叠多余空行。
    """
    if not text:
        return ""
    s = text
    # 图片 ![alt](url) → alt
    s = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", s)
    # 链接 [text](url) → text
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    # 行内代码 `code` → code
    s = re.sub(r"`([^`]+)`", r"\1", s)
    # 加粗 / 斜体 **x** / __x__ / *x* / _x_ → x
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", s)
    s = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", s)
    # 标题井号
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.MULTILINE)
    # 列表符号 - / * / + / 数字. 开头
    s = re.sub(r"^\s{0,3}[-*+]\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s{0,3}\d+\.\s+", "", s, flags=re.MULTILINE)
    # 引用 >
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.MULTILINE)
    # 水平分隔线
    s = re.sub(r"^\s{0,3}([-*_])\1{2,}\s*$", "", s, flags=re.MULTILINE)
    # 折叠多余空行
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _split_long_paragraph(text: str, max_chars: int) -> list[str]:
    """对单段超长文本按句末标点切分；句子仍超长则按逗号切，最后硬切。"""
    # 按句末标点切分，保留标点
    sentences = re.split(r"(?<=[。！？!?；;\n])", text)
    sentences = [s for s in sentences if s.strip()]
    if not sentences:
        return []

    chunks: list[str] = []
    buf = ""
    for s in sentences:
        if len(s) > max_chars:
            # 单句超长，先把缓冲冲掉，再按逗号细分
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_split_by_comma(s, max_chars))
            continue
        if buf and len(buf) + len(s) <= max_chars:
            buf += s
        else:
            if buf:
                chunks.append(buf)
            buf = s
    if buf:
        chunks.append(buf)
    return chunks


def _split_by_comma(text: str, max_chars: int) -> list[str]:
    """按逗号 / 顿号 / 冒号切分；仍超长则按 max_chars 硬切。"""
    parts = re.split(r"(?<=[，,、：:])", text)
    parts = [p for p in parts if p.strip()]

    chunks: list[str] = []
    buf = ""
    for p in parts:
        if len(p) > max_chars:
            if buf:
                chunks.append(buf)
                buf = ""
            # 硬切
            for i in range(0, len(p), max_chars):
                chunks.append(p[i:i + max_chars])
            continue
        if buf and len(buf) + len(p) <= max_chars:
            buf += p
        else:
            if buf:
                chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)
    return chunks


def _split_text(text: str, max_chars: int = _MAX_CHARS_PER_REQUEST) -> list[str]:
    """把长文本切成不超过 max_chars 的小段，尽量在句子边界切分。

    切分优先级：段落(换行) → 句末标点 → 逗号 → 硬切。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    buf = ""

    def _flush() -> None:
        nonlocal buf
        if buf:
            chunks.append(buf)
            buf = ""

    for paragraph in re.split(r"\n+", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        # 单段超长，单独细分
        if len(paragraph) > max_chars:
            _flush()
            chunks.extend(_split_long_paragraph(paragraph, max_chars))
            continue
        # 尝试并入当前缓冲段
        if buf and len(buf) + 1 + len(paragraph) <= max_chars:
            buf += "\n" + paragraph
        else:
            _flush()
            buf = paragraph
    _flush()
    return chunks


def _format_duration(seconds: int) -> str:
    """把秒数格式化为 HH:MM:SS 或 MM:SS。"""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _estimate_duration(text: str) -> int:
    """根据字数估算朗读秒数（中文约 4 字/秒，仅统计非空白字符）。"""
    chars = len(re.sub(r"\s", "", text or ""))
    return int(chars / _CHARS_PER_SECOND)


# ════════════════════════════════════════════════════════════
# 异步合成内核
# ════════════════════════════════════════════════════════════

async def _async_synthesize(text: str, voice: str, output_path: Path) -> None:
    """异步：把一段文本合成并以**追加**方式写入 output_path。

    采用 stream() 逐块写入，便于把多个分段拼接到同一个 mp3 文件。
    edge-tts 返回的是 mp3 帧数据，直接二进制拼接即可被播放器识别为单个 mp3。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    communicate = edge_tts.Communicate(text, voice)
    with open(output_path, "ab") as f:
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio" and chunk.get("data"):
                f.write(chunk["data"])


async def _async_text_to_speech(
    text: str, output_path: Path, voice: str
) -> None:
    """异步：长文本分段合成，拼接为完整 mp3。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # 先清空目标文件，随后逐段追加
    output_path.write_bytes(b"")

    for segment in _split_text(text, _MAX_CHARS_PER_REQUEST):
        await _async_synthesize(segment, voice, output_path)


def _run_async(coro):
    """在同步上下文中运行异步协程。

    正常情况下调用方是同步的，直接用 asyncio.run()。
    若当前已存在运行中的事件循环（例如在 Jupyter / 已有 async 环境中调用），
    则退到新线程里用 asyncio.run() 运行，避免 "loop already running" 报错。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的循环 —— 标准情况
        return asyncio.run(coro)
    # 已有循环：开新线程跑，主线程阻塞等待结果
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ════════════════════════════════════════════════════════════
# 1. 文本转音频
# ════════════════════════════════════════════════════════════

def text_to_speech(
    text: str,
    output_path: Path,
    voice: str = "zh-CN-YunxiNeural",
) -> Path:
    """把一段文本合成为 mp3 音频文件。

    参数:
        text: 待合成的文本（支持长文本，内部自动分段处理）。
        output_path: 输出 mp3 文件路径。
        voice: edge-tts 音色 ID，默认 ``zh-CN-YunxiNeural``（云希，男声）。
            其他常用中文音色见 :func:`list_voices`。

    返回:
        输出文件路径 ``output_path``。
    """
    output_path = Path(output_path)
    _run_async(_async_text_to_speech(text, output_path, voice))
    return output_path


# ════════════════════════════════════════════════════════════
# 2. 生成听书音频
# ════════════════════════════════════════════════════════════

def _build_intro(book_title: str) -> str:
    """生成开篇引导语。"""
    return f"欢迎收听《{book_title or '本书'}》的拆书解读。"


async def _async_generate_audiobook(
    result: DigestResult,
    output_dir: Path,
    voice: str,
    mode: str,
) -> tuple[list[Path], int]:
    """异步：根据 mode 生成对应音频文件。

    返回 ``(文件路径列表, 估算总秒数)``。
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    title = _sanitize_filename(result.book_title)
    intro = _build_intro(result.book_title)
    files: list[Path] = []
    total_seconds = 0

    if mode == "summary":
        # 只朗读全书总结
        text = intro + "\n" + _clean_text_for_tts(result.overall_summary or "")
        out = output_dir / f"{title}_听书.mp3"
        await _async_text_to_speech(text, out, voice)
        files.append(out)
        total_seconds += _estimate_duration(text)

    elif mode == "chapters":
        # 各章总结各产一个 mp3
        chapters = result.chapter_summaries or {}
        if not chapters:
            # 没有章节总结时，退化为朗读全书总结
            text = intro + "\n" + _clean_text_for_tts(result.overall_summary or "")
            out = output_dir / f"{title}_听书.mp3"
            await _async_text_to_speech(text, out, voice)
            files.append(out)
            total_seconds += _estimate_duration(text)
        for chapter, summary in chapters.items():
            ch_name = _sanitize_filename(chapter)
            text = (
                intro
                + f"\n下面进入章节：{chapter}。\n"
                + _clean_text_for_tts(summary or "")
            )
            out = output_dir / f"{title}_听书_{ch_name}.mp3"
            await _async_text_to_speech(text, out, voice)
            files.append(out)
            total_seconds += _estimate_duration(text)

    elif mode == "full":
        # 总结 + 所有章节，合并为一个 mp3
        parts: list[str] = [intro]
        if result.overall_summary:
            parts.append("首先，是全书总结。\n" + _clean_text_for_tts(result.overall_summary))
        if result.chapter_summaries:
            parts.append("接下来，逐章为您解读。")
            for chapter, summary in result.chapter_summaries.items():
                parts.append(f"章节：{chapter}。" + _clean_text_for_tts(summary or ""))
        text = "\n".join(parts)
        out = output_dir / f"{title}_听书完整版.mp3"
        await _async_text_to_speech(text, out, voice)
        files.append(out)
        total_seconds += _estimate_duration(text)

    else:
        raise ValueError(
            f"不支持的 mode: {mode!r}，可选 summary / chapters / full"
        )

    return files, total_seconds


def generate_audiobook(
    result: DigestResult,
    output_dir: Path,
    voice: str = "zh-CN-YunxiNeural",
    mode: str = "summary",
) -> dict:
    """把拆书总结转为听书音频。

    参数:
        result: 拆书结果（:class:`DigestResult`）。
        output_dir: 输出目录（通常为 ``AppConfig.output_dir``）。
        voice: edge-tts 音色 ID，默认 ``zh-CN-YunxiNeural``（云希，男声）。
        mode: 听书模式，三选一：
            - ``"summary"``: 只朗读全书总结 → ``{书名}_听书.mp3``
            - ``"chapters"``: 各章总结各产一个 mp3 → ``{书名}_听书_{章节}.mp3``
            - ``"full"``: 总结 + 所有章节合并 → ``{书名}_听书完整版.mp3``

    返回:
        ``{"mode": str, "files": list[Path], "total_duration_est": str}``，
        其中 ``total_duration_est`` 为估算朗读时长（如 ``"12:34"``）。
    """
    files, total_seconds = _run_async(
        _async_generate_audiobook(result, Path(output_dir), voice, mode)
    )
    return {
        "mode": mode,
        "files": files,
        "total_duration_est": _format_duration(total_seconds),
    }


# ════════════════════════════════════════════════════════════
# 3. 音色列表
# ════════════════════════════════════════════════════════════

def list_voices() -> list[dict]:
    """返回常用中文音色列表（硬编码，离线可用）。

    返回:
        形如 ``[{"id", "name", "gender", "description"}, ...]`` 的列表。
    """
    return [dict(v) for v in _CN_VOICES]


__all__ = ["text_to_speech", "generate_audiobook", "list_voices"]
