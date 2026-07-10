"""总调度：把提取 → 流水线 → 输出 串成一条命令。

run() 是唯一对外入口，CLI 和 Web 共用。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .config import AppConfig, DigestResult, BookType, OutputFormat
from .extractor import extract
from .chunker import chunk_book
from .llm import LLMClient
from .pipeline import extract_units, build_graph, adversarial_summary
from .outputs import (
    build_skill_pack, build_cards, write_all,
)
from . import cache


def run(
    file_path: str | Path,
    cfg: AppConfig,
    outputs: set[OutputFormat] | None = None,
    on_progress: Callable[[str, int, int], None] | None = None,
) -> tuple[DigestResult, dict[str, Path]]:
    """拆书主流程。

    参数:
        file_path: 书的文件路径
        cfg: 应用配置
        outputs: 想要的输出形态集合；None 表示全部
        on_progress: 进度回调 (阶段名, 当前, 总数)

    返回:
        (拆书结果, {形态: 输出文件路径})
    """
    cfg.ensure_dirs()
    if outputs is None:
        outputs = set(OutputFormat)

    llm = LLMClient(cfg.llm)

    # ── 提取 ──
    if on_progress:
        on_progress("提取文本", 0, 1)
    title, chapters = extract(file_path, cfg.book_type)
    chapter_titles = [t for t, _ in chapters]

    # ── 分块 ──
    chunks = chunk_book(chapters, cfg.pipeline)

    # ── 阶段一：知识单元 ──
    units = extract_units(
        cfg, llm, chunks,
        on_progress=lambda i, t: on_progress("提取知识单元", i, t) if on_progress else None,
    )

    # ── 阶段二：知识图谱 ──
    if on_progress:
        on_progress("构建知识图谱", 0, 1)
    graph = build_graph(cfg, units)

    # ── 阶段三：对抗式总结 ──
    chapter_summaries, overall = adversarial_summary(
        cfg, llm, units, chapter_titles,
        on_progress=lambda stage, i, t: on_progress(stage, i, t) if on_progress else None,
    )

    # 组装中间结果
    result = DigestResult(
        book_title=title,
        book_type=cfg.book_type,
        chapters=chapter_titles,
        units=units,
        graph=graph,
        chapter_summaries=chapter_summaries,
        overall_summary=overall,
    )

    # ── 附加：技能包 ──
    if OutputFormat.SKILL in outputs:
        if on_progress:
            on_progress("提炼技能包", 0, 1)
        result.skill_pack = build_skill_pack(cfg, llm, result)

    # ── 附加：可视化卡片 ──
    if OutputFormat.CARDS in outputs:
        if on_progress:
            on_progress("生成概念卡片", 0, 1)
        result.cards = build_cards(cfg, llm, result)

    # ── 存档 + 输出 ──
    archive_path = cache.save_archive(cfg, result, title)
    paths = write_all(cfg, result)
    paths["存档(.bda)"] = archive_path

    return result, paths


def rerun_from_archive(
    archive_path: str | Path,
    cfg: AppConfig,
    outputs: set[OutputFormat] | None = None,
) -> dict[str, Path]:
    """从 .bda 存档二次导出，不重跑 LLM（对标 SpineDigest 的 --input .sdpub）。"""
    if outputs is None:
        outputs = set(OutputFormat)
    result = cache.load_archive(archive_path)
    cfg.ensure_dirs()
    return write_all(cfg, result)
