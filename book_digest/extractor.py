"""文本提取层：把各种格式的书解析成统一的章节结构。

支持：PDF（含基础扫描版）、EPUB、Markdown、TXT。
输出：[(章节标题, 纯文本), ...]
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .config import BookType


def _clean(text: str) -> str:
    """清洗文本：去多余空白、修整断行。"""
    text = text.replace("\r\n", "\n").replace("\u00a0", " ")
    # 阶段7：移除 null 字符（\u0000）及其他不可见控制字符（保留 \n \t），
    # PDF/EPUB 提取时常带入 null 字节，会导致闪卡内容渲染为 "u0000"
    text = text.replace("\u0000", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    # 合并被错误断开的行（行尾非标点且下一行小写开头）
    text = re.sub(r"(?<=[^\n。！？!?\.])\n(?=[a-z])", " ", text)
    # 压缩多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_by_headings(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题或『第X章』模式切分章节。"""
    chapters: list[tuple[str, str]] = []
    # 优先匹配 Markdown 标题
    pattern_md = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
    # 中文章节模式
    pattern_cn = re.compile(
        r"^(第[一二三四五六七八九十百千零\d]+[章节回卷篇部话].*)$", re.MULTILINE
    )
    # 英文 Chapter X
    pattern_en = re.compile(
        r"^(Chapter\s+\d+|CHAPTER\s+\d+|Part\s+\d+)\b.*$", re.MULTILINE
    )

    matches = []
    for m in pattern_md.finditer(text):
        matches.append((m.start(), m.group(2).strip()))
    if not matches:
        for m in pattern_cn.finditer(text):
            matches.append((m.start(), m.group(1).strip()))
    if not matches:
        for m in pattern_en.finditer(text):
            matches.append((m.start(), m.group(1).strip()))

    if not matches:
        return [("全文", text)]

    matches.sort()
    for i, (pos, title) in enumerate(matches):
        end = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        body = text[pos:end].split("\n", 1)[-1] if i == 0 else text[pos:end]
        # 去掉标题行本身
        body = re.sub(r"^#{1,3}\s+.+\n", "", body, count=1)
        body = _clean(body)
        if body:
            chapters.append((title, body))

    # 标题前的内容归入前言
    if matches and matches[0][0] > 0:
        preface = _clean(text[: matches[0][0]])
        if preface:
            chapters.insert(0, ("前言", preface))

    return chapters or [("全文", _clean(text))]


def extract_pdf(path: Path) -> list[tuple[str, str]]:
    """提取 PDF：优先按书签/目录切分，无目录则按页聚合后统一切章。"""
    import fitz  # PyMuPDF

    doc = fitz.open(path)
    try:
        toc = doc.get_toc(simple=True)  # [[level, title, page], ...]

        if toc:
            chapters: list[tuple[str, str]] = []
            for i, (level, title, pno) in enumerate(toc):
                if level > 2:  # 只取一级二级标题
                    continue
                start = pno - 1
                end = toc[i + 1][2] - 1 if i + 1 < len(toc) else len(doc)
                text = "".join(doc[p].get_text("text") for p in range(start, end))
                if text.strip():
                    chapters.append((title.strip(), _clean(text)))
            if chapters:
                return chapters

        # 无目录：按页聚合，每 ~20 页合一段，再切章
        full = "".join(doc[p].get_text("text") for p in range(len(doc)))
        return _split_by_headings(full) if full.strip() else [("全文", "")]
    finally:
        doc.close()


def extract_epub(path: Path) -> list[tuple[str, str]]:
    """提取 EPUB：按 spine 里的文档项切分，提取标题。"""
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(path, options={"ignore_ncx": False})
    chapters: list[tuple[str, str]] = []

    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        title_tag = soup.find(["h1", "h2"])
        title = title_tag.get_text(strip=True) if title_tag else ""
        text = soup.get_text("\n", strip=True)
        if text:
            if not title:
                # 尝试从文本首行取标题
                first_line = text.split("\n", 1)[0][:40]
                title = first_line if first_line else "未命名章节"
            chapters.append((title, _clean(text)))

    if not chapters:
        return [("全文", "")]

    # 合并过短的片段
    merged: list[tuple[str, str]] = []
    for title, body in chapters:
        if merged and len(body) < 200:
            t, b = merged[-1]
            merged[-1] = (t, b + "\n\n" + body)
        else:
            merged.append((title, body))
    return merged


def extract_markdown(path: Path) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    return _split_by_headings(text)


def extract_txt(path: Path) -> list[tuple[str, str]]:
    text = path.read_text(encoding="utf-8")
    return _split_by_headings(text)


def extract(path: str | Path, book_type: Optional[BookType] = None) -> tuple[str, list[tuple[str, str]]]:
    """统一入口：根据扩展名分发，返回 (书名, [(章节, 文本), ...])。"""
    path = Path(path)
    title = path.stem
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        chapters = extract_pdf(path)
    elif suffix == ".epub":
        chapters = extract_epub(path)
    elif suffix in (".md", ".markdown"):
        chapters = extract_markdown(path)
    elif suffix == ".txt":
        chapters = extract_txt(path)
    else:
        raise ValueError(f"不支持的格式: {suffix}")

    # 过滤空章节
    chapters = [(t, b) for t, b in chapters if b]
    if not chapters:
        raise ValueError("未能从文件中提取到任何文本，可能是扫描版且无 OCR 文本层。")
    return title, chapters
