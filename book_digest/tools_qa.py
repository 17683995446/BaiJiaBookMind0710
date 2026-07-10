"""书籍问答工具：基于拆书结果的轻量 RAG。

提供三类能力：
1. SimpleRetriever —— TF-IDF + 余弦相似度的内存检索器，无需外部向量数据库；
2. answer_question —— 检索相关知识单元后做引用式问答；
3. generate_qa_pairs —— 基于知识单元生成适合自测理解的问答对。
"""
from __future__ import annotations

import re
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from .config import AppConfig, DigestResult, KnowledgeUnit
from .llm import LLMClient

# 问答检索时返回的知识单元数量
RETRIEVE_TOP_K = 5
# 生成问答对时喂给 LLM 的素材单元上限（避免超出上下文窗口）
MAX_QA_MATERIAL_UNITS = 40


class SimpleRetriever:
    """简单向量化检索器：TF-IDF + 余弦相似度。

    全部在内存中完成，不依赖外部向量数据库，
    适合中小规模（数百到数千条）的知识单元集合。
    """

    def __init__(self, units: list[KnowledgeUnit]):
        """构建检索索引。

        将每个知识单元的 content 与 tags 拼接为可检索文本，
        用 sklearn TfidfVectorizer 向量化，并预计算文档 TF-IDF 矩阵
        （余弦相似度基底），供检索时计算 query-doc 相似度。

        Args:
            units: 知识单元列表。
        """
        self._units: list[KnowledgeUnit] = list(units)
        # 拼接 content 与 tags，让语义标签也参与匹配
        self._texts: list[str] = [
            f"{u.content or ''} {' '.join(u.tags or [])}".strip()
            for u in self._units
        ]

        # 字符 n-gram 分词：对中文友好，且无需额外分词库（如 jieba）
        self._vectorizer = TfidfVectorizer(
            analyzer="char",
            ngram_range=(1, 2),
            lowercase=False,
            min_df=1,
        )

        non_empty = [t for t in self._texts if t]
        if non_empty:
            # 预计算文档向量矩阵（余弦相似度基底）
            self._doc_matrix = self._vectorizer.fit_transform(self._texts)
        else:
            # 无可索引内容
            self._doc_matrix = None

    def retrieve(self, query: str, top_k: int = 5) -> list[KnowledgeUnit]:
        """检索与 query 最相关的 top_k 个知识单元。

        Args:
            query: 用户问题或检索词。
            top_k: 返回数量的上限。

        Returns:
            按相关度从高到低排序的知识单元列表；
            无可用索引或查询为空时返回空列表。
        """
        if self._doc_matrix is None:
            return []
        if not query or not query.strip():
            return []

        # 将查询向量化（基于已拟合的词表）
        query_vec = self._vectorizer.transform([query])
        # 计算 query 与每个文档的余弦相似度（1 x N）
        sims = cosine_similarity(query_vec, self._doc_matrix)[0]

        k = min(top_k, len(self._units))
        # argsort 升序，取末尾 k 个并反转为降序
        order = np.argsort(sims)[::-1][:k]
        return [self._units[i] for i in order]


def answer_question(
    cfg: AppConfig,
    llm: LLMClient,
    result: DigestResult,
    question: str,
) -> dict[str, Any]:
    """基于拆书结果回答用户问题（轻量 RAG）。

    流程：
      1. 用 SimpleRetriever 检索相关知识单元；
      2. 构造 system prompt（要求标注引用的知识单元编号）；
      3. 把检索到的知识单元内容作为上下文交给 LLM；
      4. 解析回答中的引用编号，返回答案、来源与上下文。

    Args:
        cfg: 应用配置。
        llm: LLM 客户端。
        result: 拆书结果。
        question: 用户问题。

    Returns:
        dict: {
            "answer": str,            # LLM 回答
            "sources": list[str],     # 回答中引用的知识单元 id
            "context": list[str],     # 参与回答的知识单元内容
        }
    """
    retriever = SimpleRetriever(result.units)
    top_units = retriever.retrieve(question, top_k=RETRIEVE_TOP_K)

    # 构造带编号的上下文
    if top_units:
        context_blocks = [
            f"[{u.id}]（{u.chapter or '未分类'}）\n{u.content}" for u in top_units
        ]
        context_text = "\n\n".join(context_blocks)
    else:
        context_text = "（未检索到相关知识单元）"

    system_prompt = (
        "你是拆书助手，基于以下知识单元回答问题。"
        "回答时标注引用的知识单元编号 [u0001]。"
        "如果知识单元中没有相关信息，坦诚说明。"
    )
    user_prompt = (
        f"以下是相关知识单元：\n\n{context_text}\n\n"
        f"问题：{question}"
    )

    answer = llm.chat(system_prompt, user_prompt)

    # 解析回答中 [xxx] 形式的引用，仅保留确实参与检索的来源，去重保序
    cited = re.findall(r"\[\s*([A-Za-z0-9_-]+)\s*\]", answer)
    valid_ids = {u.id for u in top_units}
    sources: list[str] = []
    for cid in cited:
        if cid in valid_ids and cid not in sources:
            sources.append(cid)

    return {
        "answer": answer,
        "sources": sources,
        "context": [u.content for u in top_units],
    }


def generate_qa_pairs(
    cfg: AppConfig,
    llm: LLMClient,
    result: DigestResult,
    num: int = 10,
) -> list[dict[str, Any]]:
    """基于拆书知识单元生成自测问答对。

    让 LLM 依据知识单元生成 num 个问答对，供读者自测对书本的理解。

    Args:
        cfg: 应用配置。
        llm: LLM 客户端。
        result: 拆书结果。
        num: 需要生成的问答对数量。

    Returns:
        list[dict]，每项形如 {"question": ..., "answer": ..., "unit_id": ...}。
    """
    units = result.units
    if not units:
        return []

    # 单元过多时均匀采样，兼顾全书覆盖度与上下文长度
    if len(units) > MAX_QA_MATERIAL_UNITS:
        idx = np.linspace(0, len(units) - 1, MAX_QA_MATERIAL_UNITS).round().astype(int)
        sampled = [units[i] for i in idx]
    else:
        sampled = units

    material = "\n\n".join(
        f"[{u.id}]（{u.chapter or '未分类'}）\n{u.content}" for u in sampled
    )
    valid_ids = {u.id for u in sampled}

    system_prompt = (
        "你是拆书助手，请基于提供的知识单元生成适合自测理解的问答对。"
        "每个问答对需标注其依据的知识单元编号 unit_id（须来自给定编号）。"
    )
    user_prompt = (
        f"以下是知识单元：\n\n{material}\n\n"
        f"请基于上述内容生成 {num} 个问答对，用于检验读者对书本的理解。"
        "要求：问题具体、答案准确简练，并如实标注 unit_id。\n"
        '仅返回 JSON：{"qa_pairs": [{"question": "...", "answer": "...", "unit_id": "u0001"}, ...]}'
    )

    data = llm.chat_json(system_prompt, user_prompt)

    # 兼容多种返回结构
    if isinstance(data, dict):
        pairs = data.get("qa_pairs") or data.get("pairs") or data.get("data") or []
    elif isinstance(data, list):
        pairs = data
    else:
        pairs = []

    # 规整字段，仅保留合法的 unit_id（避免 LLM 杜撰编号）
    results: list[dict[str, Any]] = []
    for item in pairs:
        if not isinstance(item, dict):
            continue
        question = (item.get("question") or item.get("q") or "").strip()
        if not question:
            continue
        answer = (item.get("answer") or item.get("a") or "").strip()
        uid = (
            item.get("unit_id") or item.get("unitId") or item.get("id") or ""
        ).strip()
        if uid not in valid_ids:
            # 杜撰编号：置空以提示无效来源
            uid = ""
        results.append({"question": question, "answer": answer, "unit_id": uid})

    return results[:num]
