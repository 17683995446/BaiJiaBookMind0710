"""CLI 入口：一条命令完成拆书。

用法:
    book-digest digest book.pdf
    book-digest digest book.epub --model deepseek-chat --base-url https://api.deepseek.com/v1
    book-digest rerun output/book.bda.json            # 二次导出，不重跑 LLM
    book-digest serve                                  # 启动 Web 服务
"""
from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

from .config import AppConfig, LLMConfig, PipelineConfig, BookType, OutputFormat
from .orchestrator import run, rerun_from_archive

app = typer.Typer(
    name="book-digest",
    help="集百家之所长的拆书 Agent：思维导图 + 知识图谱 + 对抗式总结 + 技能包 + 可视化卡片",
    no_args_is_help=True,
)
console = Console()


@app.command()
def digest(
    file: Path = typer.Argument(..., help="待拆解的书（PDF/EPUB/MD/TXT）"),
    api_key: str = typer.Option("", "--api-key", "-k", envvar="BD_API_KEY", help="LLM API Key"),
    base_url: str = typer.Option("https://api.siliconflow.cn/v1", "--base-url", envvar="BD_BASE_URL"),
    model: str = typer.Option("Qwen/Qwen3.5-9B", "--model", "-m", envvar="BD_MODEL"),
    book_type: BookType = typer.Option(BookType.NONFICTION, "--type", "-t", help="书籍类型"),
    focus: str = typer.Option("", "--focus", help="自定义侧重点提示词"),
    outputs: str = typer.Option("all", "--outputs", "-o", help="输出形态，逗号分隔，或 all"),
    chunk_size: int = typer.Option(2500, "--chunk-size", help="分块 token 大小"),
    reviewers: int = typer.Option(2, "--reviewers", help="对抗式总结审稿人数"),
    max_revisions: int = typer.Option(2, "--max-revisions", help="最大修订轮次"),
):
    """拆解一本书，生成多种形态的知识输出。"""
    if not file.exists():
        console.print(f"[red]文件不存在：{file}[/red]")
        raise typer.Exit(1)
    if not api_key:
        console.print("[red]缺少 API Key，请用 --api-key 或设置环境变量 BD_API_KEY[/red]")
        raise typer.Exit(1)

    cfg = AppConfig(
        llm=LLMConfig(api_key=api_key, base_url=base_url, model=model,
                      reviewers=reviewers, max_revisions=max_revisions),
        pipeline=PipelineConfig(chunk_size=chunk_size),
        book_type=book_type,
        focus_prompt=focus or None,
    )

    out_set = _parse_outputs(outputs)

    console.print(f"[cyan]开始拆书：[/cyan]{file.name}")
    console.print(f"  模型: {model} @ {base_url}")
    console.print(f"  输出: {', '.join(o.value for o in out_set)}")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as prog:
        task_id = prog.add_task("初始化", total=10)

        def on_progress(stage: str, cur: int, total: int):
            if total > 0:
                pct = int(cur / total * 100)
                prog.update(task_id, description=f"{stage} ({cur}/{total})", completed=pct / 10)

        result, paths = run(file, cfg, out_set, on_progress=on_progress)
        prog.update(task_id, description="完成", completed=10)

    console.print(f"\n[green]✓ 拆书完成！[/green]")
    console.print(f"  知识单元: {len(result.units)} 个")
    console.print(f"  主题聚类: {len(result.graph.clusters)} 个")
    console.print(f"  概念卡片: {len(result.cards)} 张")
    console.print(f"\n[cyan]输出文件：[/cyan]")
    for kind, p in paths.items():
        console.print(f"  • {kind}: {p}")


@app.command()
def rerun(
    archive: Path = typer.Argument(..., help=".bda.json 存档路径"),
    outputs: str = typer.Option("all", "--outputs", "-o", help="输出形态"),
    api_key: str = typer.Option("", "--api-key", "-k", envvar="BD_API_KEY"),
    base_url: str = typer.Option("https://api.siliconflow.cn/v1", "--base-url", envvar="BD_BASE_URL"),
    model: str = typer.Option("Qwen/Qwen3.5-9B", "--model", "-m", envvar="BD_MODEL"),
):
    """从存档二次导出，不重跑 LLM。"""
    if not archive.exists():
        console.print(f"[red]存档不存在：{archive}[/red]")
        raise typer.Exit(1)
    cfg = AppConfig(llm=LLMConfig(api_key=api_key, base_url=base_url, model=model))
    out_set = _parse_outputs(outputs)
    paths = rerun_from_archive(archive, cfg, out_set)
    console.print("[green]✓ 二次导出完成[/green]")
    for kind, p in paths.items():
        console.print(f"  • {kind}: {p}")


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", "--host"),
    port: int = typer.Option(8000, "--port", "-p"),
):
    """启动 Web 服务。"""
    import uvicorn
    console.print(f"[cyan]启动拆书 Web 服务：[/cyan]http://{host}:{port}")
    uvicorn.run("book_digest.server:app", host=host, port=port, reload=False)


def _parse_outputs(s: str) -> set[OutputFormat]:
    if s.strip().lower() == "all":
        return set(OutputFormat)
    result = set()
    for part in s.split(","):
        part = part.strip().lower()
        try:
            result.add(OutputFormat(part))
        except ValueError:
            console.print(f"[yellow]未知输出形态：{part}，忽略[/yellow]")
    return result or set(OutputFormat)


if __name__ == "__main__":
    app()
