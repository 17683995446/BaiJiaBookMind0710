"""分块器：按 token 把章节切成可喂给 LLM 的块，带重叠。

借鉴 SpineDigest 的分块思路：逐段处理绕开上下文窗口，产出更干净。
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import PipelineConfig


@dataclass
class Chunk:
    chapter: str
    index: int       # 该章节内的块序号
    text: str
    start_token: int = 0


def _count_tokens(text: str) -> int:
    """近似 token 计数：中文按字数，英文按词数，取较大值兜底。"""
    # 优先用 tiktoken，失败则退化为字符估算
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        cn = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        en = len(text.split())
        return max(cn, en)


def chunk_chapter(
    chapter_title: str,
    text: str,
    cfg: PipelineConfig,
    global_index: int = 0,
) -> list[Chunk]:
    """把单个章节切成带重叠的块。"""
    total = _count_tokens(text)
    if total <= cfg.chunk_size:
        return [Chunk(chapter=chapter_title, index=global_index, text=text)]

    chunks: list[Chunk] = []
    # 用句子边界切分，尽量不在句中断裂
    import re
    sentences = re.split(r"(?<=[。！？!?\.])\s+", text)

    buf: list[str] = []
    buf_tokens = 0
    idx = global_index
    for s in sentences:
        s_tokens = _count_tokens(s)
        if buf_tokens + s_tokens > cfg.chunk_size and buf:
            chunks.append(Chunk(chapter=chapter_title, index=idx, text="".join(buf)))
            idx += 1
            # 保留重叠：留最后部分
            if cfg.chunk_overlap > 0:
                overlap: list[str] = []
                overlap_tokens = 0
                for piece in reversed(buf):
                    p_tokens = _count_tokens(piece)
                    if overlap_tokens + p_tokens > cfg.chunk_overlap:
                        break
                    overlap.insert(0, piece)
                    overlap_tokens += p_tokens
                buf = overlap
                buf_tokens = overlap_tokens
            else:
                buf = []
                buf_tokens = 0
        buf.append(s)
        buf_tokens += s_tokens

    if buf:
        chunks.append(Chunk(chapter=chapter_title, index=idx, text="".join(buf)))
    return chunks


def chunk_book(
    chapters: list[tuple[str, str]],
    cfg: PipelineConfig,
) -> list[Chunk]:
    """把整本书的所有章节切成块。"""
    all_chunks: list[Chunk] = []
    gidx = 0
    for title, text in chapters:
        cks = chunk_chapter(title, text, cfg, global_index=gidx)
        all_chunks.extend(cks)
        gidx += len(cks)
    return all_chunks
