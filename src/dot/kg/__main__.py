"""
dot.kg — Knowledge Graph RAG 服务入口

Usage:
  python -m dot.kg serve
  python -m dot.kg ingest <file>
  python -m dot.kg stats
"""
from __future__ import annotations

import sys


USAGE = """Usage: python -m dot.kg <command>
Commands:
  serve        Start KG RAG server on port 8001
  ingest <f>   Index a file
  stats        Show graph statistics
  query <q>    Ask the knowledge graph (prints answer)
"""


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0 if len(sys.argv) > 1 else 1)

    cmd = sys.argv[1]

    if cmd == "serve":
        from .server import main as serve_main
        serve_main()

    elif cmd == "ingest":
        if len(sys.argv) < 3:
            print("Usage: python -m dot.kg ingest <file>")
            sys.exit(1)
        from .pipeline import KGPipeline
        from .config import KGSettings

        pipeline = KGPipeline(KGSettings())
        result = pipeline.ingest_file(sys.argv[2])
        print(f"Indexed: {result.chunks_processed} chunks, "
              f"{result.entities_count} entities, {result.relations_count} relations")

    elif cmd == "stats":
        from .pipeline import KGPipeline
        from .config import KGSettings

        pipeline = KGPipeline(KGSettings())
        stats = pipeline.stats()
        print(f"Graph Statistics:")
        print(f"  Nodes: {stats['node_count']}")
        print(f"  Edges: {stats['edge_count']}")
        print("  Type distribution:")
        for t, count in stats.get("type_distribution", {}).items():
            print(f"    {t}: {count}")

    elif cmd == "query":
        if len(sys.argv) < 3:
            print("Usage: python -m dot.kg query <question>")
            sys.exit(1)
        import asyncio

        from .pipeline import KGPipeline
        from .config import KGSettings

        pipeline = KGPipeline(KGSettings())

        async def _ask() -> str:
            async for event in pipeline.astream(" ".join(sys.argv[2:])):
                if event["type"] == "done":
                    return event["answer"]
                if event["type"] == "error":
                    return f"error: {event['message']}"
            return ""

        print(asyncio.run(_ask()))

    else:
        print(f"Unknown command: {cmd}")
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
