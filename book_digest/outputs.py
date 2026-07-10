"""输出后端：五种形态 + 一个综合 HTML 报告。

1. 思维导图 —— markmap 兼容 Markdown + 可直接打开的 HTML
2. 交互式知识图谱 —— pyvis 力导向图
3. 对抗式文字总结 —— Markdown
4. 技能包 —— 借鉴 Cangjie Skill，结构化 YAML/JSON
5. 可视化卡片 —— 每个核心概念一张 HTML 卡片
6. 综合报告 —— 上述全部整合的单页 HTML
"""
from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Template

from .config import AppConfig, DigestResult, KnowledgeUnit
from .llm import LLMClient


# ════════════════════════════════════════════════════════════
# 1. 思维导图
# ════════════════════════════════════════════════════════════

def build_mindmap_md(result: DigestResult) -> str:
    """生成 markmap 兼容的 Markdown 思维导图。"""
    lines: list[str] = [f"# {result.book_title}"]
    # 按聚类（主题）组织
    cluster_of: dict[str, list[KnowledgeUnit]] = {}
    for n in result.graph.nodes:
        cluster_of.setdefault(n.group, []).append(
            next((u for u in result.units if u.id == n.id), None)
        )
    for n in result.graph.nodes:
        pass  # 占位避免未用警告

    # 主题层
    for theme, units in cluster_of.items():
        units = [u for u in units if u]
        if not units:
            continue
        lines.append(f"\n## {theme}")
        for u in units:
            lines.append(f"### {u.content[:50]}")
            if u.tags:
                lines.append(f"- 标签：{', '.join(u.tags)}")

    # 章节总结层
    if result.chapter_summaries:
        lines.append("\n## 章节要点")
        for ch, s in result.chapter_summaries.items():
            # 取总结首句作为节点
            first = s.split("。")[0][:50] if s else ""
            lines.append(f"### {ch}")
            lines.append(f"- {first}…")

    return "\n".join(lines)


def render_mindmap_html(md: str, title: str) -> str:
    """用 markmap 在线渲染为可交互 HTML。"""
    import html as _html
    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>{_html.escape(title)} · 思维导图</title>
<style>html,body{{margin:0;height:100%}}#mindmap{{width:100%;height:100vh}}</style>
<script src="https://cdn.jsdelivr.net/npm/markmap-autoloader@0.16"></script>
</head>
<body>
<svg id="mindmap"></svg>
<script type="text/template">
{_html.escape(md)}
</script>
</body>
</html>"""


# ════════════════════════════════════════════════════════════
# 2. 交互式知识图谱
# ════════════════════════════════════════════════════════════

def build_graph_html(result: DigestResult) -> str:
    """用 pyvis 生成力导向知识图谱 HTML。"""
    try:
        from pyvis.network import Network
    except Exception:
        return "<p>缺少 pyvis 依赖，无法渲染知识图谱</p>"

    net = Network(
        height="100vh", width="100%", bgcolor="#1a1a2e",
        font_color="white", notebook=False, directed=False,
        cdn_resources="in_line",
    )
    net.heading = f"{result.book_title} · 知识图谱"

    # 按聚类上色
    palette = [
        "#e74c3c", "#3498db", "#2ecc71", "#f39c12",
        "#9b59b6", "#1abc9c", "#e67e22", "#34495e",
    ]
    groups = sorted({n.group for n in result.graph.nodes})
    color_map = {g: palette[i % len(palette)] for i, g in enumerate(groups)}

    for n in result.graph.nodes:
        net.add_node(
            n.id, label=n.label, title=n.label,
            size=n.size, color=color_map.get(n.group, "#95a5a6"),
            group=n.group,
        )
    for e in result.graph.edges:
        net.add_edge(e.source, e.target, width=0.5 + e.weight * 2)

    # 物理布局
    net.set_options(json.dumps({
        "physics": {
            "barnesHut": {
                "gravitationalConstant": -8000,
                "centralGravity": 0.3,
                "springLength": 120,
                "springConstant": 0.04,
                "damping": 0.4,
            }
        },
        "interaction": {"hover": True, "tooltipDelay": 100},
    }))

    return net.generate_html(notebook=False)


# ════════════════════════════════════════════════════════════
# 3. 对抗式文字总结
# ════════════════════════════════════════════════════════════

def build_summary_md(result: DigestResult) -> str:
    lines = [f"# {result.book_title} · 拆书总结", ""]
    lines.append("## 全书总结")
    lines.append(result.overall_summary or "（暂无）")
    if result.chapter_summaries:
        lines.append("\n## 各章要点")
        for ch, s in result.chapter_summaries.items():
            lines.append(f"\n### {ch}")
            lines.append(s)
    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# 4. 技能包（借鉴 Cangjie Skill）
# ════════════════════════════════════════════════════════════

SKILL_SYSTEM = """你是一位方法论提炼专家。你的任务是把书中的知识单元提炼成 AI 智能体可执行的"技能包"。

技能包 = 一组结构化的、可被 AI 调用以解决特定问题的指令集。

输出 JSON：
{
  "skills": [
    {
      "name": "技能名（动宾结构，如『复利思维决策』）",
      "trigger": "什么场景下应启用该技能",
      "steps": ["执行步骤1", "执行步骤2"],
      "principle": "背后的核心原理（一句话）",
      "example": "一个简短应用示例"
    }
  ]
}"""


def build_skill_pack(
    cfg: AppConfig, llm: LLMClient, result: DigestResult
) -> dict:
    """让 LLM 从知识单元提炼可执行技能包。"""
    units_text = "\n".join(f"- {u.content}（标签：{'/'.join(u.tags)}）" for u in result.units[:60])
    user = f"书名：{result.book_title}\n\n知识单元：\n{units_text}"
    data = llm.chat_json(SKILL_SYSTEM, user)
    skills = data.get("skills", []) if isinstance(data, dict) else []
    if not isinstance(skills, list):
        skills = []
    return {"book": result.book_title, "skills": skills}


def render_skill_yaml(skill_pack: dict) -> str:
    """渲染为可读 YAML。"""
    import yaml
    return yaml.dump(skill_pack, allow_unicode=True, sort_keys=False)


# ════════════════════════════════════════════════════════════
# 5. 可视化卡片
# ════════════════════════════════════════════════════════════

def build_cards(
    cfg: AppConfig, llm: LLMClient, result: DigestResult, top_n: int = 12
) -> list[dict]:
    """为每个核心概念生成一张可视化卡片数据。"""
    # 选取度数最高的 top_n 个知识单元
    ranked = sorted(result.graph.nodes, key=lambda n: n.weight, reverse=True)[:top_n]
    cards: list[dict] = []
    for n in ranked:
        unit = next((u for u in result.units if u.id == n.id), None)
        if not unit:
            continue
        # 让 LLM 为每个核心概念生成解读
        sys = "你是拆书解读员。为给定的知识单元生成一段 2-3 句的深度解读，说明它为何重要、如何应用。直接输出解读文本，不要标题。"
        insight = llm.chat(sys, f"知识单元：{unit.content}\n标签：{', '.join(unit.tags)}\n章节：{unit.chapter}")
        cards.append({
            "id": unit.id,
            "title": unit.content[:30],
            "content": unit.content,
            "chapter": unit.chapter,
            "tags": unit.tags,
            "theme": n.group,
            "weight": n.weight,
            "insight": insight.strip(),
        })
    return cards


# ════════════════════════════════════════════════════════════
# 6. 综合报告（单页 HTML，整合所有形态）
# ════════════════════════════════════════════════════════════

def render_report_html(result: DigestResult, cfg: AppConfig) -> str:
    """把所有输出整合成一个可视化的单页 HTML 报告。"""
    import html as _html

    # 卡片 HTML
    card_tmpl = Template("""
    <div class="card" data-theme="{{ c.theme }}">
      <div class="card-theme">{{ c.theme }}</div>
      <div class="card-title">{{ c.title }}</div>
      <div class="card-content">{{ c.content }}</div>
      <div class="card-insight">{{ c.insight }}</div>
      <div class="card-meta">
        <span class="chapter">{{ c.chapter }}</span>
        {% for t in c.tags %}<span class="tag">{{ t }}</span>{% endfor %}
      </div>
    </div>""")
    cards_html = "\n".join(card_tmpl.render(c=c) for c in result.cards) if result.cards else "<p>暂无卡片</p>"

    # 知识单元列表
    units_html = "\n".join(
        f'<li><span class="uid">{u.id}</span> <span class="uchap">[{_html.escape(u.chapter)}]</span> {_html.escape(u.content)} '
        + "".join(f'<span class="tag">{_html.escape(t)}</span>' for t in u.tags) + "</li>"
        for u in result.units[:80]
    )

    # 章节总结
    chap_html = "\n".join(
        f"<h3>{_html.escape(ch)}</h3><p>{_html.escape(s).replace(chr(10),'<br>')}</p>"
        for ch, s in result.chapter_summaries.items()
    )

    # 技能包
    skills = result.skill_pack.get("skills", [])
    skill_html = "\n".join(
        f"""<div class="skill"><h3>{_html.escape(s.get('name',''))}</h3>
        <p class="trigger">触发：{_html.escape(s.get('trigger',''))}</p>
        <p class="principle">原理：{_html.escape(s.get('principle',''))}</p>
        <ol>{''.join(f'<li>{_html.escape(st)}</li>' for st in s.get('steps',[]))}</ol>
        <p class="example">示例：{_html.escape(s.get('example',''))}</p></div>"""
        for s in skills
    ) if skills else "<p>暂无技能包</p>"

    # 图谱数据嵌入（用 vis-network 前端渲染）
    graph_nodes = [{"id": n.id, "label": n.label, "group": n.group, "size": n.size} for n in result.graph.nodes]
    graph_edges = [{"from": e.source, "to": e.target, "width": 0.5 + e.weight * 2} for e in result.graph.edges]

    return REPORT_TPL.render(
        title=result.book_title, cards=cards_html, units=units_html,
        chapters=chap_html, skills=skill_html,
        overall=_html.escape(result.overall_summary).replace("\n", "<br>"),
        graph_nodes=json.dumps(graph_nodes, ensure_ascii=False),
        graph_edges=json.dumps(graph_edges, ensure_ascii=False),
        output_lang=cfg.output_lang,
        graph_clusters=len({n.group for n in result.graph.nodes}),
        skills_count=len(skills),
    )


REPORT_TPL = Template("""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>{{ title }} · 拆书报告</title>
<script src="https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js"></script>
<style>
  :root{--bg:#0f0f1a;--card:#1e1e2e;--accent:#7c3aed;--text:#e5e7eb;--muted:#9ca3af}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:"Noto Sans CJK SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif;line-height:1.7}
  .wrap{max-width:1100px;margin:0 auto;padding:32px 20px}
  h1{font-size:2.2em;background:linear-gradient(90deg,#a78bfa,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:8px}
  h2{border-left:4px solid var(--accent);padding-left:12px;margin-top:48px;font-size:1.4em}
  h3{color:#c4b5fd;margin:20px 0 8px}
  .overview{background:var(--card);border-radius:12px;padding:20px 24px;border:1px solid #2d2d44}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;margin-top:16px}
  .card{background:var(--card);border-radius:14px;padding:18px;border:1px solid #2d2d44;transition:transform .2s,border-color .2s}
  .card:hover{transform:translateY(-4px);border-color:var(--accent)}
  .card-theme{font-size:.75em;color:var(--accent);text-transform:uppercase;letter-spacing:.05em}
  .card-title{font-weight:700;font-size:1.05em;margin:6px 0}
  .card-content{color:var(--text);font-size:.92em}
  .card-insight{color:#fbbf24;font-size:.88em;margin-top:8px;border-top:1px dashed #3d3d55;padding-top:8px}
  .card-meta{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
  .chapter{color:var(--muted);font-size:.78em}
  .tag{background:#2d2d44;color:#a5b4fc;padding:1px 8px;border-radius:10px;font-size:.72em}
  .uid{color:var(--accent);font-family:monospace;font-size:.8em}
  .uchap{color:var(--muted);font-size:.8em}
  ul{list-style:none;padding-left:0}
  li{padding:5px 0;border-bottom:1px solid #1a1a2e}
  .skill{background:var(--card);border-radius:12px;padding:16px 20px;margin:12px 0;border:1px solid #2d2d44}
  .skill .trigger{color:#60a5fa;font-size:.88em}
  .skill .principle{color:#34d399;font-size:.88em}
  .skill .example{color:#f59e0b;font-size:.85em;font-style:italic}
  ol{padding-left:20px}
  #graph{height:600px;background:#16162a;border-radius:12px;border:1px solid #2d2d44;margin-top:12px}
  .stat{display:inline-block;background:var(--card);padding:8px 16px;border-radius:10px;margin:4px;border:1px solid #2d2d44}
  .stat b{color:var(--accent);font-size:1.3em}
</style>
</head>
<body>
<div class="wrap">
  <h1>{{ title }}</h1>
  <p style="color:var(--muted)">集百家之所长的 AI 拆书报告 · {{ output_lang }}</p>
  <div>
    <span class="stat">知识单元 <b>{{ units | length }}</b></span>
    <span class="stat">主题聚类 <b>{{ graph_clusters }}</b></span>
    <span class="stat">技能 <b>{{ skills_count }}</b></span>
  </div>

  <h2>全书总结</h2>
  <div class="overview">{{ overall }}</div>

  <h2>知识图谱</h2>
  <div id="graph"></div>

  <h2>核心概念卡片</h2>
  <div class="cards">{{ cards }}</div>

  <h2>各章要点</h2>
  {{ chapters }}

  <h2>可执行技能包</h2>
  {{ skills }}

  <h2>知识单元全表</h2>
  <ul>{{ units }}</ul>
</div>
<script>
  const nodes = new vis.DataSet({{ graph_nodes }});
  const edges = new vis.DataSet({{ graph_edges }});
  new vis.Network(document.getElementById('graph'), {nodes, edges}, {
    physics:{barnesHut:{gravitationalConstant:-8000,centralGravity:.3,springLength:120,damping:.4}},
    nodes:{shape:'dot',font:{color:'#e5e7eb',face:'sans-serif'}},
    edges:{color:'#3d3d55'},
    interaction:{hover:true}
  });
</script>
</body>
</html>""")


# ════════════════════════════════════════════════════════════
# 统一写出
# ════════════════════════════════════════════════════════════

def write_all(cfg: AppConfig, result: DigestResult) -> dict[str, Path]:
    """把所有输出写到 output_dir，返回 {形态: 路径}。"""
    cfg.ensure_dirs()
    out = cfg.output_dir
    name = result.book_title or "book"
    paths: dict[str, Path] = {}

    # 1. 思维导图
    md = build_mindmap_md(result)
    (out / f"{name}.mindmap.md").write_text(md, "utf-8")
    (out / f"{name}.mindmap.html").write_text(render_mindmap_html(md, name), "utf-8")
    paths["思维导图(md)"] = out / f"{name}.mindmap.md"
    paths["思维导图(html)"] = out / f"{name}.mindmap.html"

    # 2. 知识图谱
    (out / f"{name}.graph.html").write_text(build_graph_html(result), "utf-8")
    paths["知识图谱"] = out / f"{name}.graph.html"

    # 3. 总结
    (out / f"{name}.summary.md").write_text(build_summary_md(result), "utf-8")
    paths["总结"] = out / f"{name}.summary.md"

    # 4. 技能包
    try:
        (out / f"{name}.skill.yaml").write_text(render_skill_yaml(result.skill_pack), "utf-8")
        paths["技能包"] = out / f"{name}.skill.yaml"
    except Exception:
        (out / f"{name}.skill.json").write_text(
            json.dumps(result.skill_pack, ensure_ascii=False, indent=2), "utf-8")
        paths["技能包"] = out / f"{name}.skill.json"

    # 5. 综合报告
    (out / f"{name}.report.html").write_text(render_report_html(result, cfg), "utf-8")
    paths["综合报告"] = out / f"{name}.report.html"

    return paths
