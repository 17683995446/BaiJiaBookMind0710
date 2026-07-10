"""导出工具集：书籍元数据提取、Anki 闪卡导出、Obsidian Vault 导出。

本模块为拆书 Agent 提供三个独立的导出/提取能力：

1. extract_metadata —— 用 isbnlib / ebooklib / PyMuPDF 提取书籍元数据与封面
2. export_anki      —— 用 genanki 把拆书结果导出为 Anki .apkg 牌组
3. export_obsidian  —— 把拆书结果导出为 Obsidian Vault（双链 Markdown）

所有第三方依赖均在文件顶部导入。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import isbnlib
import ebooklib
from ebooklib import epub

import fitz  # PyMuPDF

import genanki

from .config import DigestResult, KnowledgeUnit


# ════════════════════════════════════════════════════════════
# 通用小工具
# ════════════════════════════════════════════════════════════

# 文件名中不允许出现的字符（Windows / Unix 通用安全集合）
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def _safe_name(name: str, fallback: str = "book") -> str:
    """把任意字符串清洗为安全的文件/链接名。"""
    name = (name or fallback).strip()
    name = _UNSAFE_CHARS.sub("_", name)
    name = re.sub(r"\s+", "_", name)
    name = name.strip("._")
    return name[:80] or fallback


def _hash_id(text: str) -> int:
    """把字符串映射为稳定的正整数（供 genanki 的 model_id / deck_id 使用）。"""
    digest = hashlib.md5(text.encode("utf-8")).hexdigest()
    # 取前 12 位十六进制（48bit），足够避免碰撞又不超过 Anki 的整数限制
    return int(digest[:12], 16)


# ════════════════════════════════════════════════════════════
# 1. 书籍元数据提取
# ════════════════════════════════════════════════════════════

def _find_isbns(text: str) -> list[str]:
    """从文本中提取 ISBN-like 字符串，失败返回空列表。"""
    if not text:
        return []
    try:
        found = isbnlib.get_isbnlike(text)
        return found or []
    except Exception:
        return []


def _meta_get(info: Any, *keys: str, default: str = "") -> str:
    """从 isbnlib 返回的元信息字典中安全取值（兼容不同 key 命名）。"""
    if not isinstance(info, dict):
        return default
    for k in keys:
        val = info.get(k)
        if val:
            return val
    return default


def _fill_from_pdf(path: Path, meta: dict[str, Any], out_dir: Path) -> list[str]:
    """用 PyMuPDF 读取 PDF 元数据、页数并渲染首页为封面，返回 ISBN 候选列表。"""
    doc = fitz.open(path)
    isbn_candidates: list[str] = []
    try:
        m = doc.metadata or {}
        if m.get("title"):
            meta["title"] = m["title"].strip()
        if m.get("author"):
            meta["author"] = m["author"].strip()
        meta["pages"] = len(doc)

        # 渲染首页为封面，存为 JPG
        if len(doc) > 0:
            try:
                pix = doc[0].get_pixmap(dpi=150, alpha=False)
                # 非 RGB/灰度色彩空间（如 CMYK）先转 RGB 再存
                if pix.colorspace and pix.colorspace.n not in (1, 3):
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                cover_path = out_dir / f"cover_{_safe_name(meta['title'])}.jpg"
                pix.save(str(cover_path))
                meta["cover_path"] = str(cover_path)
            except Exception:
                # 封面渲染失败不影响整体元数据提取
                pass

        # 从前 5 页文本中找 ISBN
        snippet = "".join(doc[p].get_text("text") for p in range(min(5, len(doc))))
        isbn_candidates = _find_isbns(snippet)
    finally:
        doc.close()
    return isbn_candidates


def _fill_from_epub(path: Path, meta: dict[str, Any], out_dir: Path) -> list[str]:
    """用 ebooklib 读取 EPUB 元数据与内嵌封面，返回 ISBN 候选列表。"""
    book = epub.read_epub(path, options={"ignore_ncx": False})

    # DC 元数据：title / creator(identifier)
    title_meta = book.get_metadata("DC", "title") or []
    if title_meta and title_meta[0][0]:
        meta["title"] = str(title_meta[0][0]).strip()
    author_meta = book.get_metadata("DC", "creator") or []
    if author_meta:
        authors = [str(a[0]) for a in author_meta if a and a[0]]
        if authors:
            meta["author"] = ", ".join(authors)

    # EPUB 无固定页数概念
    meta["pages"] = 0

    # 提取封面图片：优先 ITEM_COVER，其次按 id / 属性名匹配 cover
    cover_bytes: bytes | None = None
    for item in book.get_items_of_type(ebooklib.ITEM_COVER):
        cover_bytes = item.get_content()
        break
    if not cover_bytes:
        for item in book.get_items():
            item_id = (item.get_id() or "").lower()
            media = getattr(item, "media_type", "") or ""
            if "cover" in item_id and "image" in media:
                cover_bytes = item.get_content()
                break
    if cover_bytes:
        try:
            cover_path = out_dir / f"cover_{_safe_name(meta['title'])}.jpg"
            cover_path.write_bytes(cover_bytes)
            meta["cover_path"] = str(cover_path)
        except Exception:
            pass

    # ISBN 候选：从 DC:identifier 与前几个文档项文本中提取
    candidates: list[str] = []
    for val, _attrs in (book.get_metadata("DC", "identifier") or []):
        candidates.append(str(val))
    snippet = ""
    count = 0
    for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
        try:
            snippet += item.get_content().decode("utf-8", "ignore")
        except Exception:
            continue
        count += 1
        if count >= 3:
            break
    candidates.extend(_find_isbns(snippet))
    return candidates


def _enrich_with_isbnlib(meta: dict[str, Any], candidates: list[str]) -> None:
    """用 isbnlib 补全出版社/年份/简介等信息；查询失败时优雅降级。"""
    isbn = ""

    # 1) 优先使用文档中直接找到的合法 ISBN
    for cand in candidates:
        if not cand:
            continue
        cleaned = isbnlib.clean(cand)
        if cleaned and (isbnlib.is_isbn13(cleaned) or isbnlib.is_isbn10(cleaned)):
            isbn = cleaned
            break

    # 2) 退而求其次：用“书名 + 作者”反查 ISBN
    if not isbn and (meta["title"] or meta["author"]):
        try:
            words = f"{meta['title']} {meta['author']}".strip()
            found = isbnlib.isbn_from_words(words)
            if found:
                # 反查结果可能带连字符，统一清洗并校验
                cleaned = isbnlib.clean(found)
                if cleaned and (isbnlib.is_isbn13(cleaned) or isbnlib.is_isbn10(cleaned)):
                    isbn = cleaned
        except Exception:
            pass

    if not isbn:
        # 没有 ISBN，无法补全外部信息，直接降级返回
        return

    meta["isbn"] = isbn

    # 3) 查询书籍元信息（出版社、年份等），网络/查询失败均不报错
    try:
        info = isbnlib.meta(isbn)
        if info:
            title = _meta_get(info, "Title")
            if title:
                meta["title"] = title
            authors = _meta_get(info, "Authors")
            if authors:
                meta["author"] = ", ".join(authors) if isinstance(authors, list) else str(authors)
            publisher = _meta_get(info, "Publisher")
            if publisher:
                meta["publisher"] = publisher
            year = _meta_get(info, "Year")
            if year:
                meta["year"] = str(year)
    except Exception:
        pass

    # 4) 查询书籍简介
    try:
        desc = isbnlib.desc(isbn)
        if desc:
            meta["description"] = desc
    except Exception:
        pass


def extract_metadata(file_path: str, output_dir: str | Path = "output") -> dict[str, Any]:
    """提取书籍元数据（标题、作者、ISBN、出版社、年份、封面、页数、简介）。

    - PDF 用 PyMuPDF(fitz) 读取元数据与页数，首页渲染为封面
    - EPUB 用 ebooklib 读取元数据与内嵌封面
    - 用 isbnlib 查询 ISBN 补全出版社/年份/简介，查询失败时优雅降级，不报错

    参数:
        file_path: 书籍文件路径（支持 .pdf / .epub；其它格式仅做 isbnlib 反查）
        output_dir: 封面图片输出目录，默认 "output"
    返回:
        dict: {title, author, isbn, publisher, year, cover_path, pages, description}
    """
    path = Path(file_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    suffix = path.suffix.lower()

    # 元数据默认值
    meta: dict[str, Any] = {
        "title": path.stem,
        "author": "",
        "isbn": "",
        "publisher": "",
        "year": "",
        "cover_path": "",
        "pages": 0,
        "description": "",
    }

    isbn_candidates: list[str] = []
    if suffix == ".pdf":
        isbn_candidates = _fill_from_pdf(path, meta, out_dir)
    elif suffix == ".epub":
        isbn_candidates = _fill_from_epub(path, meta, out_dir)
    # 其它格式（md/txt 等）：仅以文件名作为书名尝试 isbnlib 反查

    # 用 isbnlib 补全信息（任何失败均优雅降级）
    _enrich_with_isbnlib(meta, isbn_candidates)

    return meta


# ════════════════════════════════════════════════════════════
# 2. Anki 闪卡导出
# ════════════════════════════════════════════════════════════

# 深色主题 + 紫色强调色的卡片样式
_ANKI_CSS = """
.card {
  font-family: "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif;
  font-size: 18px;
  text-align: center;
  color: #e5e7eb;
  background-color: #1a1a2e;
  padding: 28px 24px;
  line-height: 1.8;
}
.type {
  display: inline-block;
  font-size: 12px;
  color: #a78bfa;
  background: #2d2d44;
  border: 1px solid #7c3aed;
  border-radius: 14px;
  padding: 3px 14px;
  margin-bottom: 18px;
  letter-spacing: 0.05em;
}
.front {
  color: #f5f3ff;
  font-size: 21px;
  font-weight: 600;
  margin-bottom: 14px;
}
.back {
  color: #c4b5fd;
  text-align: left;
  background: #16162a;
  border-radius: 10px;
  padding: 14px 18px;
  border-left: 3px solid #7c3aed;
}
hr {
  border: none;
  border-top: 1px dashed #7c3aed;
  margin: 18px 0;
}
""".strip()

# 固定的模型命名空间，保证同一套牌组复用同一个模型定义
_ANKI_MODEL_ID = _hash_id("bookdigest:anki:model:v1")


def _build_anki_model() -> genanki.Model:
    """创建自定义 Anki 笔记模型（深色主题 + 紫色强调色）。"""
    return genanki.Model(
        _ANKI_MODEL_ID,
        "Book Digest 拆书卡片",
        fields=[
            {"name": "Front"},   # 正面
            {"name": "Back"},    # 反面
            {"name": "Type"},    # 卡片类型标签
        ],
        templates=[
            {
                "name": "卡片1",
                "qfmt": '<span class="type">{{Type}}</span>'
                        '<div class="front">{{Front}}</div>',
                "afmt": '{{FrontSide}}<hr id="answer">'
                        '<div class="back">{{Back}}</div>',
            },
        ],
        css=_ANKI_CSS,
    )


def export_anki(result: DigestResult, output_path: Path) -> Path:
    """把拆书结果导出为 Anki .apkg 牌组。

    - 从 result.units 生成问答卡片：正面=知识单元 content，反面=chapter + tags
    - 从 result.cards 生成深度卡片：正面=title，反面=insight
    - 使用自定义 Anki 模型（深色主题、紫色强调色）

    参数:
        result: 拆书结果
        output_path: .apkg 输出路径
    返回:
        生成的 .apkg 文件路径
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    model = _build_anki_model()
    deck_name = f"{result.book_title or 'book'} · 拆书牌组"
    deck = genanki.Deck(_hash_id(f"bookdigest:deck:{result.book_title}"), deck_name)

    # —— 知识单元问答卡片 ——
    for u in result.units:
        front = u.content
        back_parts: list[str] = []
        if u.chapter:
            back_parts.append(f"章节：{u.chapter}")
        if u.tags:
            back_parts.append("标签：" + "、".join(u.tags))
        back = "\n".join(back_parts) if back_parts else "（暂无章节与标签信息）"
        deck.add_note(genanki.Note(
            model=model,
            fields=[front, back, "知识单元"],
            guid=genanki.guid_for(f"{result.book_title}|unit|{u.id}"),
        ))

    # —— 深度解读卡片 ——
    for c in result.cards:
        front = str(c.get("title", "")) if isinstance(c, dict) else ""
        back = str(c.get("insight", "")) if isinstance(c, dict) else ""
        if not front:
            continue
        card_id = ""
        if isinstance(c, dict):
            card_id = str(c.get("id", ""))
        deck.add_note(genanki.Note(
            model=model,
            fields=[front, back, "深度解读"],
            guid=genanki.guid_for(f"{result.book_title}|card|{card_id}"),
        ))

    package = genanki.Package(deck)
    package.write_to_file(str(output_path))
    return output_path


# ════════════════════════════════════════════════════════════
# 3. Obsidian Vault 导出
# ════════════════════════════════════════════════════════════

def _build_same_tag_links(units: list[KnowledgeUnit]) -> dict[str, list[str]]:
    """为每个知识单元找出与之共享至少一个标签的其它单元 id（用于双链）。"""
    links: dict[str, list[str]] = {u.id: [] for u in units}
    by_tag: dict[str, list[str]] = {}
    for u in units:
        for t in u.tags:
            by_tag.setdefault(t, []).append(u.id)
    for u in units:
        seen: set[str] = set()
        for t in u.tags:
            for other_id in by_tag.get(t, []):
                if other_id != u.id and other_id not in seen:
                    seen.add(other_id)
                    links[u.id].append(other_id)
    return links


def _write_readme(vault: Path, result: DigestResult) -> None:
    """写书籍概览 README.md，含元数据与导航链接。"""
    chapter_count = len(result.chapters) or len(result.chapter_summaries)
    lines = [
        f"# {result.book_title}",
        "",
        "## 书籍概览",
        f"- 书名：{result.book_title}",
        f"- 类型：{result.book_type.value}",
        f"- 章节数：{chapter_count}",
        f"- 知识单元数：{len(result.units)}",
        f"- 主题聚类数：{len(result.graph.clusters)}",
        f"- 概念卡片数：{len(result.cards)}",
        "",
        "## 导航",
        "- [[总结]] —— 全书总结",
        "- 章节/ —— 各章要点",
        "- 知识单元/ —— 知识单元（双链互相关联）",
        "- [[技能包]] —— 可执行技能列表",
        "- [[知识图谱]] —— Mermaid 知识图谱",
    ]
    # 章节索引
    chapters = result.chapters or list(result.chapter_summaries.keys())
    if chapters:
        lines.append("")
        lines.append("## 章节索引")
        for ch in chapters:
            lines.append(f"- [[章节/{_safe_name(ch)}|{ch}]]")
    (vault / "README.md").write_text("\n".join(lines), "utf-8")


def _build_overall_md(result: DigestResult) -> str:
    """生成全书总结 Markdown。"""
    lines = [f"# {result.book_title} · 全书总结", ""]
    lines.append(result.overall_summary or "（暂无总结）")
    return "\n".join(lines)


def _write_chapters(chapters_dir: Path, result: DigestResult) -> None:
    """每章一个 Markdown 文件，含章节总结与本章知识单元链接。"""
    summaries = result.chapter_summaries
    chapters = result.chapters or list(summaries.keys())
    for ch in chapters:
        safe = _safe_name(ch)
        summary = summaries.get(ch, "（本章暂无总结）")
        md = [f"# {ch}", "", "## 章节总结", summary, ""]
        # 本章知识单元
        unit_lines = [
            f"- [[{u.id}]] {u.content}" for u in result.units if u.chapter == ch
        ]
        if unit_lines:
            md.append("## 本章知识单元")
            md.extend(unit_lines)
            md.append("")
        (chapters_dir / f"{safe}.md").write_text("\n".join(md), "utf-8")


def _write_units(units_dir: Path, result: DigestResult,
                 same_tag_links: dict[str, list[str]]) -> None:
    """每个知识单元一个 Markdown 文件，用 [[unit_id]] 双链关联同标签单元。"""
    for u in result.units:
        md = [f"# {u.id}", ""]
        if u.chapter:
            md.append(f"**所属章节**：[[章节/{_safe_name(u.chapter)}|{u.chapter}]]")
        if u.tags:
            md.append("**标签**：" + " ".join(f"#{t}" for t in u.tags))
        md.append("")
        md.append("## 内容")
        md.append(u.content)
        links = same_tag_links.get(u.id, [])
        if links:
            md.append("")
            md.append("## 相关知识单元（同标签）")
            md.append(" ".join(f"[[{lid}]]" for lid in links))
        (units_dir / f"{u.id}.md").write_text("\n".join(md), "utf-8")


def _build_skill_md(result: DigestResult) -> str:
    """生成技能包 Markdown。"""
    lines = [f"# {result.book_title} · 技能包", ""]
    skills = []
    if isinstance(result.skill_pack, dict):
        skills = result.skill_pack.get("skills", []) or []
    if not skills:
        lines.append("（暂无技能）")
        return "\n".join(lines)
    for i, s in enumerate(skills, 1):
        if not isinstance(s, dict):
            continue
        lines.append(f"## {i}. {s.get('name', '')}")
        lines.append(f"- **触发场景**：{s.get('trigger', '')}")
        lines.append(f"- **核心原理**：{s.get('principle', '')}")
        steps = s.get("steps", []) or []
        if steps:
            lines.append("- **执行步骤**：")
            for idx, st in enumerate(steps, 1):
                lines.append(f"  {idx}. {st}")
        if s.get("example"):
            lines.append(f"- **应用示例**：{s.get('example')}")
        lines.append("")
    return "\n".join(lines)


def _build_mermaid_graph(result: DigestResult) -> str:
    """用 mermaid 格式输出知识图谱。"""
    lines = [f"# {result.book_title} · 知识图谱", "", "```mermaid", "graph LR"]

    # 节点：清理会破坏 mermaid 语法的字符
    for n in result.graph.nodes:
        label = re.sub(r'["\[\]()]', "", n.label)[:30]
        group = re.sub(r'["\[\]()]', "", n.group or "")[:20]
        lines.append(f'    {n.id}["{label}<br/><small>{group}</small>"]')

    # 边
    for e in result.graph.edges:
        relation = re.sub(r"[|]", "", e.relation) if e.relation else ""
        if relation:
            lines.append(f"    {e.source} ---|{relation}| {e.target}")
        else:
            lines.append(f"    {e.source} --- {e.target}")

    lines.append("```")

    # 聚类图例
    if result.graph.clusters:
        lines.append("")
        lines.append("## 主题聚类")
        for name, members in result.graph.clusters.items():
            safe_name = re.sub(r'["\[\]()]', "", str(name))
            links = ", ".join(f"[[{m}]]" for m in members)
            lines.append(f"- **{safe_name}**：{links}")

    return "\n".join(lines)


def export_obsidian(result: DigestResult, output_dir: Path) -> Path:
    """把拆书结果导出为 Obsidian Vault（文件夹 + 双链 Markdown）。

    结构:
      {book_title}/
        README.md          书籍概览，含元数据与导航链接
        总结.md            overall_summary
        章节/{chapter}.md  每章一个文件，含 chapter_summaries
        知识单元/          每个单元一个文件，用 [[unit_id]] 双链互相关联
        技能包.md          技能列表
        知识图谱.md        mermaid 格式知识图谱

    参数:
        result: 拆书结果
        output_dir: Vault 输出目录
    返回:
        Vault 根目录路径
    """
    output_dir = Path(output_dir)
    vault = output_dir / _safe_name(result.book_title or "book")
    chapters_dir = vault / "章节"
    units_dir = vault / "知识单元"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    units_dir.mkdir(parents=True, exist_ok=True)

    # 预计算同标签关联，供知识单元双链使用
    same_tag_links = _build_same_tag_links(result.units)

    # 1. README.md —— 书籍概览
    _write_readme(vault, result)
    # 2. 总结.md —— 全书总结
    (vault / "总结.md").write_text(_build_overall_md(result), "utf-8")
    # 3. 章节/ —— 每章一个文件
    _write_chapters(chapters_dir, result)
    # 4. 知识单元/ —— 每个单元一个文件（双链互相关联）
    _write_units(units_dir, result, same_tag_links)
    # 5. 技能包.md —— 技能列表
    (vault / "技能包.md").write_text(_build_skill_md(result), "utf-8")
    # 6. 知识图谱.md —— mermaid 图谱
    (vault / "知识图谱.md").write_text(_build_mermaid_graph(result), "utf-8")

    return vault
