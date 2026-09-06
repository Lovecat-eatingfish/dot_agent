"""
dot.rag 命令行入口

  uv run python -m dot.rag ingest <文档目录>     # 建索引（默认 data/rag/docs）
  uv run python -m dot.rag query "问题"          # 检索 + 问答
  uv run python -m dot.rag search "关键词"       # 只检索，看召回片段
"""
from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.panel import Panel

from .config import RagSettings
from .pipeline import RAGPipeline

app = typer.Typer(help="dot.rag — RAG 命令行", no_args_is_help=True)
console = Console()


@app.command()
def ingest(docs_dir: str = typer.Argument(None, help="文档目录（默认取配置 docs_dir）")):
    """加载文档并建立向量索引"""
    pipeline = RAGPipeline()
    target = docs_dir or pipeline.settings.docs_dir
    with console.status(f"正在加载并索引 {target} ..."):
        count = pipeline.ingest_dir(target)
    console.print(f"[green]完成[/green]：写入 {count} 个 chunk 到 {pipeline.settings.vector_db_dir}")


@app.command()
def search(question: str):
    """只做检索，打印召回的片段（调试检索质量用）"""
    pipeline = RAGPipeline()
    docs = pipeline.retrieve(question)
    if not docs:
        console.print("[yellow]没有召回任何片段，先跑 ingest 建索引[/yellow]")
        return
    for i, doc in enumerate(docs, 1):
        console.print(Panel(
            doc.page_content,
            title=f"片段 {i} — {doc.metadata.get('source', 'unknown')}",
        ))


@app.command()
def query(question: str):
    """检索增强问答"""
    pipeline = RAGPipeline()

    async def run():
        docs = pipeline.retrieve(question)
        console.print(f"[dim]召回 {len(docs)} 个片段[/dim]")
        result = await pipeline.aquery(question)
        return result

    result = asyncio.run(run())
    console.print(Panel(result.answer, title="回答"))
    for doc in result.source_documents:
        console.print(f"[dim]来源: {doc.metadata.get('source', 'unknown')}[/dim]")


@app.command()
def serve(
    host: str = typer.Option(None, help="监听地址（默认取配置 server_host）"),
    port: int = typer.Option(None, help="监听端口（默认取配置 server_port）"),
):
    """启动 FastAPI Web 服务（生产模式自动托管 web/dist）"""
    import uvicorn

    settings = RagSettings()
    uvicorn.run(
        "dot.rag.server:app",
        host=host or settings.server_host,
        port=port or settings.server_port,
        log_level="info",
    )


if __name__ == "__main__":
    app()
