"""三阶段流水线：集百家之所长的核心。

阶段一 知识单元提取 —— 借鉴 SpineDigest 分块提取
阶段二 知识图谱构建 —— 经典图算法语义聚类
阶段三 对抗式总结 —— 多智能体互批，反复修订

附加：技能包提炼（借鉴 Cangjie Skill）+ 可视化卡片。
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from .config import (
    AppConfig, BookType, DigestResult, KnowledgeUnit,
    KnowledgeGraph, GraphNode, GraphEdge,
)
from .chunker import chunk_book, Chunk
from .llm import LLMClient
from . import cache

logger = logging.getLogger("book_digest.pipeline")


# ──────────────────────────────────────────────────────────────
# 阶段一：知识单元提取
# ──────────────────────────────────────────────────────────────

UNIT_SYSTEM = """你是一位资深的拆书专家。你的任务是从给定的文本片段中，提取出离散的、自包含的"知识单元"。

知识单元 = 一个完整的事实、论点、概念、方法论或案例，脱离上下文也能被理解。

要求：
1. 每个知识单元用一句话精炼表达（不超过 80 字）
2. 为每个单元打 1-3 个语义标签（关键词），用于后续聚类
3. 最多提取 {max_units} 个最核心的单元
4. 忽略过渡句、客套话、纯叙事铺垫

输出 JSON：
{{
  "units": [
    {{"content": "知识单元内容", "tags": ["标签1", "标签2"]}}
  ]
}}"""


def _focus_hint(cfg: AppConfig) -> str:
    if cfg.focus_prompt:
        return f"\n\n用户特别关注的侧重点：{cfg.focus_prompt}，请优先提取相关单元。"
    return ""


def extract_units(
    cfg: AppConfig,
    llm: LLMClient,
    chunks: list[Chunk],
    on_progress: Callable[[int, int], None] | None = None,
) -> list[KnowledgeUnit]:
    """逐块提取知识单元，带缓存实现断点续传。"""
    units: list[KnowledgeUnit] = []
    total = len(chunks)
    for i, ck in enumerate(chunks):
        on_progress(i, total) if on_progress else None
        # 查缓存
        cached = cache.load_chunk(cfg, ck.chapter, ck.text)
        if cached and "units" in cached:
            for u in cached["units"]:
                units.append(KnowledgeUnit(
                    id=f"u{len(units):04d}",
                    content=u["content"],
                    chapter=ck.chapter,
                    chunk_index=ck.index,
                    tags=u.get("tags", []),
                ))
            continue

        system = UNIT_SYSTEM.format(max_units=cfg.pipeline.max_units_per_chunk)
        user = f"章节：{ck.chapter}\n\n文本：\n{ck.text}{_focus_hint(cfg)}"
        data = llm.chat_json(system, user)

        block_units = data.get("units", []) if isinstance(data, dict) else []
        # 兜底：模型有时返回纯列表
        if not block_units and isinstance(data, list):
            block_units = data

        clean_units = []
        for u in block_units:
            content = u.get("content", "") if isinstance(u, dict) else str(u)
            tags = u.get("tags", []) if isinstance(u, dict) else []
            if content:
                units.append(KnowledgeUnit(
                    id=f"u{len(units):04d}",
                    content=content,
                    chapter=ck.chapter,
                    chunk_index=ck.index,
                    tags=tags,
                ))
                clean_units.append({"content": content, "tags": tags})

        cache.save_chunk(cfg, ck.chapter, ck.text, {"units": clean_units})

    on_progress(total, total) if on_progress else None
    return units


# ──────────────────────────────────────────────────────────────
# 阶段二：知识图谱构建（经典图算法，非 LLM）
# ──────────────────────────────────────────────────────────────

def _tokenize_zh(text: str) -> list[str]:
    """简易中文分词：按字 + 标签词。"""
    import re
    chars = [c for c in text if "\u4e00" <= c <= "\u9fff" or c.isalpha()]
    return chars


def build_graph(cfg: AppConfig, units: list[KnowledgeUnit]) -> KnowledgeGraph:
    """用 TF-IDF + 余弦相似度聚类，构建知识图谱。"""
    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import networkx as nx

    if not units:
        return KnowledgeGraph()

    # 构造文档：标签 + 内容
    docs = [" ".join(u.tags + _tokenize_zh(u.content)) for u in units]

    try:
        vec = TfidfVectorizer(analyzer="word", token_pattern=r"(?u)\b\w+\b")
        mat = vec.fit_transform(docs)
    except ValueError:
        # 全是单字时退化为字符级
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(1, 2))
        mat = vec.fit_transform(docs)

    sim = cosine_similarity(mat)
    np.fill_diagonal(sim, 0)

    # 构图：相似度高于阈值连边
    G = nx.Graph()
    for u in units:
        G.add_node(u.id, label=u.content[:30], chapter=u.chapter)

    threshold = cfg.pipeline.cluster_threshold
    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            if sim[i][j] >= threshold:
                G.add_edge(units[i].id, units[j].id, weight=float(sim[i][j]))

    # 社区发现聚类
    try:
        communities = nx.community.greedy_modularity_communities(G)
    except Exception as e:
        logger.warning(f"Community detection failed, using single community: {e}")
        communities = [set(G.nodes())]

    clusters: dict[str, list[str]] = {}
    node_group: dict[str, str] = {}
    for ci, comm in enumerate(communities):
        name = f"主题{ci + 1}"
        clusters[name] = list(comm)
        for nid in comm:
            node_group[nid] = name

    # 为每个聚类生成主题名（取最高频标签）
    tag_freq: dict[str, dict[str, int]] = {}
    for name in clusters:
        tag_freq[name] = {}
    for u in units:
        g = node_group.get(u.id, "其它")
        for t in u.tags:
            tag_freq[g][t] = tag_freq[g].get(t, 0) + 1
    cluster_names: dict[str, str] = {}
    for name, freq in tag_freq.items():
        if freq:
            top = max(freq, key=freq.get)
            cluster_names[name] = f"{name}·{top}"
        else:
            cluster_names[name] = name

    # 节点大小 = 度数
    nodes = []
    for u in units:
        g = node_group.get(u.id, "其它")
        degree = G.degree(u.id) if u.id in G else 0
        nodes.append(GraphNode(
            id=u.id,
            label=u.content[:40],
            group=cluster_names.get(g, g),
            size=20 + degree * 6,
            weight=float(degree),
        ))

    edges = []
    for s, t, d in G.edges(data=True):
        edges.append(GraphEdge(
            source=s, target=t,
            weight=round(d["weight"], 3),
        ))

    # clusters: 用重命名后的主题名做键，节点ID列表做值
    renamed_clusters: dict[str, list[str]] = {}
    for name, members in clusters.items():
        display = cluster_names.get(name, name)
        renamed_clusters[display] = members

    return KnowledgeGraph(nodes=nodes, edges=edges, clusters=renamed_clusters)


# ──────────────────────────────────────────────────────────────
# 阶段三：对抗式总结（多智能体互批 + 修订）
# ──────────────────────────────────────────────────────────────

WRITER_SYSTEM = """你是一位资深拆书人，擅长把一本书的核心内容提炼成高质量总结。
请基于给定的知识单元，撰写一份结构清晰、信息密度高的总结。
要求：分点论述、避免空话套话、每个论点要有知识单元支撑。"""

REVIEWER_SYSTEM = """你是一位严苛的学术审稿人。你的任务是审查拆书总结的质量。
审查维度：
1. 准确性：是否有与知识单元矛盾或臆造的内容
2. 完整性：是否遗漏了重要的核心观点
3. 冗余度：是否有重复或注水
请指出具体问题，并给出修改建议。输出 JSON：
{"issues": ["问题1", "问题2"], "suggestions": ["建议1", "建议2"], "verdict": "pass|revise"}"""

FINAL_SYSTEM = """你是拆书总结的终稿编辑。根据审稿意见修订总结，输出最终版本。
要求：保留原总结的优点，修正指出的问题，使总结既准确又精炼。"""


def _units_to_text(units: list[KnowledgeUnit], chapter: str = "") -> str:
    if chapter:
        us = [u for u in units if u.chapter == chapter]
    else:
        us = units
    return "\n".join(f"- [{u.id}] {u.content}（标签：{'/'.join(u.tags)}）" for u in us)


def adversarial_summary(
    cfg: AppConfig,
    llm: LLMClient,
    units: list[KnowledgeUnit],
    chapters: list[str],
    on_progress: Callable[[str, int, int], None] | None = None,
) -> tuple[dict[str, str], str]:
    """对抗式总结：每章一稿 + 全书一稿，审稿人轮番挑刺后修订。"""
    chapter_summaries: dict[str, str] = {}
    total = len(chapters) + 1

    # 逐章总结
    for i, ch in enumerate(chapters):
        if on_progress:
            on_progress(f"章节总结：{ch}", i, total)
        ctx = _units_to_text(units, ch)
        if not ctx:
            chapter_summaries[ch] = "（本章无可提取知识单元）"
            continue
        draft = llm.chat(WRITER_SYSTEM, f"章节《{ch}》的知识单元：\n{ctx}")
        chapter_summaries[ch] = _revise_loop(llm, ctx, draft, cfg)

    # 全书总结
    if on_progress:
        on_progress("全书总结", len(chapters), total)
    all_ctx = _units_to_text(units)
    all_draft = llm.chat(WRITER_SYSTEM, f"全书所有知识单元：\n{all_ctx}")
    overall = _revise_loop(llm, all_ctx, all_draft, cfg)

    return chapter_summaries, overall


def _revise_loop(llm: LLMClient, ctx: str, draft: str, cfg: AppConfig) -> str:
    """一稿 → N 个审稿人挑刺 → 修订，直到 verdict=pass 或达到上限。"""
    current = draft
    for _ in range(cfg.llm.max_revisions):
        # 多个审稿人各出一份意见
        all_issues: list[str] = []
        verdicts: list[str] = []
        for _ in range(cfg.llm.reviewers):
            review = llm.chat_json(REVIEWER_SYSTEM, f"知识单元原文：\n{ctx}\n\n待审总结：\n{current}")
            issues = review.get("issues", []) if isinstance(review, dict) else []
            all_issues.extend(issues)
            verdicts.append(review.get("verdict", "revise") if isinstance(review, dict) else "revise")

        if "pass" in verdicts and all_issues == []:
            break
        if not all_issues:
            break
        # 修订
        current = llm.chat(
            FINAL_SYSTEM,
            f"原总结：\n{current}\n\n审稿意见：\n" + "\n".join(f"- {i}" for i in all_issues),
        )
    return current
