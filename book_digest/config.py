"""配置与数据模型。

集百家之所长的拆书 Agent —— 配置中心。
支持 OpenAI 兼容接口（覆盖 OpenAI / DeepSeek / Gemini 兼容 / Moonshot 等）。
"""
from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class BookType(str, Enum):
    """书籍类型，影响提示词侧重点。"""
    NONFICTION = "nonfiction"   # 社科/非虚构
    FICTION = "fiction"         # 小说/虚构
    TECHNICAL = "technical"     # 技术/学术


class OutputFormat(str, Enum):
    """输出形态。可叠加选择。"""
    MINDMAP = "mindmap"         # 思维导图
    GRAPH = "graph"            # 交互式知识图谱
    SUMMARY = "summary"        # 对抗式文字总结
    SKILL = "skill"            # 技能包
    CARDS = "cards"            # 可视化卡片
    REPORT = "report"          # HTML 综合报告


class LLMConfig(BaseModel):
    """LLM 配置（OpenAI 兼容，默认硅基流动）。"""
    api_key: str = Field(default_factory=lambda: os.getenv("BD_API_KEY", ""))
    base_url: str = Field(
        default_factory=lambda: os.getenv(
            "BD_BASE_URL", "https://api.siliconflow.cn/v1"
        )
    )
    model: str = Field(default_factory=lambda: os.getenv("BD_MODEL", "Qwen/Qwen3.5-9B"))
    temperature: float = 0.3
    max_tokens: int = 4096
    # 审稿专家数量（对抗式总结用）
    reviewers: int = 2
    # 最大修订轮次
    max_revisions: int = 2
    # 是否启用思考型模型的 reasoning（None=不传，False=关闭思考）
    enable_thinking: bool | None = False


class PipelineConfig(BaseModel):
    """流水线参数。"""
    # 分块大小（token），兼顾上下文窗口与抽取粒度
    chunk_size: int = 2500
    chunk_overlap: int = 200
    # 知识图谱聚类阈值（0-1，越小簇越细）
    cluster_threshold: float = 0.35
    # 单章知识单元抽取上限
    max_units_per_chunk: int = 8


class AppConfig(BaseModel):
    """应用总配置。"""
    llm: LLMConfig = Field(default_factory=LLMConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    # 缓存目录（断点续传）
    cache_dir: Path = Path(os.getenv("BD_CACHE_DIR", ".book_digest_cache"))
    # 输出目录
    output_dir: Path = Path(os.getenv("BD_OUTPUT_DIR", "output"))
    # 书籍类型
    book_type: BookType = BookType.NONFICTION
    # 自定义提示词侧重点
    focus_prompt: Optional[str] = None
    # 输出语言
    output_lang: str = "中文"

    def ensure_dirs(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


# ── 流水线数据结构 ──────────────────────────────────────────

class KnowledgeUnit(BaseModel):
    """知识单元：自包含的事实/论点/概念。"""
    id: str
    content: str
    chapter: str = ""
    chunk_index: int = 0
    # 语义标签，供图谱聚类
    tags: list[str] = Field(default_factory=list)


class GraphNode(BaseModel):
    id: str
    label: str
    group: str = ""
    size: int = 20
    weight: float = 1.0


class GraphEdge(BaseModel):
    source: str
    target: str
    weight: float = 1.0
    relation: str = ""


class KnowledgeGraph(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    clusters: dict[str, list[str]] = Field(default_factory=dict)


class DigestResult(BaseModel):
    """拆书最终结果，可序列化为 .bda 存档（对标 SpineDigest 的 .sdpub）。"""
    book_title: str = ""
    book_type: BookType = BookType.NONFICTION
    chapters: list[str] = Field(default_factory=list)
    units: list[KnowledgeUnit] = Field(default_factory=list)
    graph: KnowledgeGraph = Field(default_factory=KnowledgeGraph)
    chapter_summaries: dict[str, str] = Field(default_factory=dict)
    overall_summary: str = ""
    skill_pack: dict = Field(default_factory=dict)
    cards: list[dict] = Field(default_factory=list)
