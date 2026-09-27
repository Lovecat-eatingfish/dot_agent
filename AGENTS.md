# AGENTS.md — dot_agent

## What this is

`dot-agent` (v2.0-dev): a self-built workflow engine + coding agent (a Claude Code-style CLI/TUI). The workflow engine does NOT use langgraph/langchain — it's an original node+conditional-edge graph. Code comments, docs, and commit messages are written in Chinese (commit style: `feature：...`).

## Commands

```bash
uv sync                                  # install (Python >=3.13, uv; alternatively pip install -e .)
agent                                    # TUI (textual) interactive mode
agent run "任务"                          # one-shot; agent console = console mode
python -m dot.rag                        # RAG CLI/server (FastAPI + ChromaDB)
python -m dot.kg serve                   # knowledge-graph RAG server, port 8001

.venv/Scripts/python.exe -m pytest tests --ignore=tests/legacy -x -q   # tests (Git Bash on Windows)
```

- **Always pass `--ignore=tests/legacy`.** Those tests import packages that no longer exist (`tau_agent`, `tau_coding`, `mokioclaw`, `dot.cli`) and break collection otherwise.
- Focused test: `.venv/Scripts/python.exe -m pytest tests/test_workflow_engine.py -q`
- No lint/typecheck is configured (pytest is the only dev tool installed; `.ruff_cache` is stale).
- Runtime config comes from `.env` (copy from `.env_example` — note the filename): `API_KEY`, `MODEL`, `BASE_URL`.

## Architecture & layer rules

Package is `src/dot/` with strict one-way dependencies:

```
dot.coding → dot.workflow ─┐
dot.coding → dot.agent → dot.ai ─┼→ dot.core
dot.workflow ────────────────────┘
```

- `dot.core` — shared kernel (WireModel serialization base, cancel-token Protocol). Depends on nothing.
- `dot.ai` — provider abstraction (`ModelProvider` Protocol, streaming events). Depends only on httpx.
- `dot.agent` — agent loop, frozen-dataclass `AgentTool`, harness, tool-history self-repair, loop protection (`loop_detector.py`).
- `dot.workflow` — generic engine (graph/runner/validate/export). Must NOT import agent, model SDKs, or any orchestration framework. Read `src/dot/workflow/README.md` before touching it.
- `dot.coding` — the app: CLI (`cli/app.py`, entry point `agent`), textual TUI (`cli/tui/`), sessions (JSONL), 3-level permission, 3-tier context compression, extensions/MCP, skills, trace.
- `dot.rag` / `dot.kg` — RAG services; these MAY use langchain (embeddings/splitters/vector stores). This exception does not extend to the layers above.

Non-negotiables when editing:
- Never introduce a reverse dependency (e.g., agent → coding). The permission contract lives as Protocols in `dot.agent.permission` and is implemented in `dot.coding.permission` precisely for this.
- Keep the `workflow` core framework-free.
- Events are Pydantic discriminated unions at three levels (`ProviderEvent` → `AgentEvent` → `CodingEvent`); tools are frozen dataclasses; extension points are `Protocol`s with zero inheritance.

## Gotchas

- Windows + Git Bash environment; TUI is textual and has had mouse-rendering conflicts — test TUI changes in a real terminal.
- `tests/conftest.py` puts `src/` first on `sys.path` (so tests don't hit a stale site-packages copy) and deletes `RAG_API_TOKEN` from the env. Don't bypass or weaken it.
- `tests/mcp_server.py` is a fixture server used by MCP tests, not a test file.
- `web/` (Vite+TS frontend), `springboot-demo/`, `pi_src/`, and `scripts/` are auxiliary/untracked sandboxes, not part of the Python package.

## Docs worth reading before big changes

- `README.md` — architecture map and module table
- `src/dot/workflow/README.md` — engine usage (nodes, conditional edges, subgraphs, interrupts)
- `docs/RAG_MODULE_GUIDE.md` — RAG pipeline (parent-child chunking, hybrid retrieval)
- `specs/004-agent-architecture-overhaul/spec.md` — latest architecture spec (specs 001–004 are numbered in order)
- `doc/` — per-feature design docs (compression, tool-calling, tracing, MCP fixes)
