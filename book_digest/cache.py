"""缓存层：断点续传 + .bda 存档。

借鉴 SpineDigest 的 .sdpub 思路：把完整知识结构序列化，
二次导出不同格式时无需重跑 LLM。
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from .config import DigestResult, AppConfig

logger = logging.getLogger("book_digest.cache")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def chunk_cache_path(cfg: AppConfig, chapter: str, chunk_text: str) -> Path:
    """单个 chunk 处理结果的缓存路径。"""
    key = _hash(chapter + "::" + chunk_text)
    return cfg.cache_dir / f"chunk_{key}.json"


def load_chunk(cfg: AppConfig, chapter: str, chunk_text: str) -> dict | None:
    p = chunk_cache_path(cfg, chapter, chunk_text)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Cache file corrupted, ignoring: {p} ({e})")
            return None
    return None


def save_chunk(cfg: AppConfig, chapter: str, chunk_text: str, data: dict) -> None:
    p = chunk_cache_path(cfg, chapter, chunk_text)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def save_archive(cfg: AppConfig, result: DigestResult, name: str) -> Path:
    """保存完整拆书结果为 .bda 存档（可二次导出）。"""
    cfg.ensure_dirs()
    path = cfg.output_dir / f"{name}.bda.json"
    path.write_text(
        result.model_dump_json(indent=2), "utf-8"
    )
    logger.info(f"Archive saved: {path}")
    return path


def load_archive(path: str | Path) -> DigestResult:
    """读取 .bda 存档，供二次导出。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"存档文件不存在: {p}")
    try:
        data = json.loads(p.read_text("utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"存档文件损坏或格式无效: {p} ({e})") from e
    return DigestResult.model_validate(data)
