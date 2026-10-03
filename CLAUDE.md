# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**dot-agent** (v2.0-dev) is a self-built workflow engine + coding agent system (similar to Claude Code). It features a custom node+conditional-edge graph workflow engine that does NOT use langgraph/langchain. Code comments, docs, and commit messages are written in Chinese (commit style: `feature：...`).

The system implements a dual-loop architecture:
- **Outer loop**: WorkflowGraph orchestrates phases (plan → code → validate)
- **Inner loop**: Agent loop handles LLM streaming + tool execution

## Quick Commands

### Installation & Setup

```bash
# Install dependencies (Python >= 3.13)
uv sync                    # Preferred method using uv
pip install -e .           # Alternative with pip

# Configure environment (NOTE: filename is .env_example, not .env.example)
cp .env_example .env
# Edit .env with your API_KEY, MODEL, BASE_URL
```

### Running the Application

```bash
agent                        # TUI interactive mode (textual)
agent run "任务描述"          # One-shot execution
agent console                # Console mode
python -m dot.rag             # RAG CLI/server (FastAPI + ChromaDB)
python -m dot.kg serve         # Knowledge-graph RAG server (port 8001)
```

### Testing

```bash
# Run all tests (IMPORTANT: always ignore legacy tests)
.venv/Scripts/python.exe -m pytest tests --ignore=tests/legacy -x -q

# Run specific test file
.venv/Scripts/python.exe -m pytest tests/test_workflow_engine.py -q
.venv/Scripts/python.exe -m pytest tests/test_coding_workflow.py -q

# Run with verbose output
.venv/Scripts/python.exe -m pytest tests/test_workflow_engine.py -v

# Run specific test
.venv/Scripts/python.exe -m pytest tests/test_workflow_engine.py::test_duplicate_node_rejected -v
```

**Environment**: Windows + Git Bash. The TUI is textual and has had mouse-rendering conflicts—test TUI changes in a real terminal, not WSL.

## Architecture

### Layer Structure (Strict One-Way Dependencies)

```
dot.coding → dot.workflow ─┐
dot.coding → dot.agent → dot.ai ─┼→ dot.core
dot.workflow ────────────────────┘
```

**Layer responsibilities**:

| Layer | Package | Purpose | Dependencies |
|-------|---------|---------|--------------|
| Core | `dot.core` | WireModel serialization base, CancellationToken Protocol | None |
| Provider | `dot.ai` | ModelProvider Protocol, streaming events, catalog | httpx only |
| Agent | `dot.agent` | Agent loop, AgentTool frozen dataclasses, harness, tool-history self-repair, loop protection | dot.ai + pydantic |
| Workflow | `dot.workflow` | Generic workflow engine (graph/runner/validate/export) | dot.core only (NO agent/LLM SDKs) |
| Coding | `dot.coding` | CLI, TUI, sessions, permissions, compression, extensions/MCP, skills, tracing | dot.workflow + dot.agent |

### Key Design Patterns

**1. Workflow Engine** (`src/dot/workflow/`)
- Node + conditional-edge graph with retry/timeout/compensation/cancel/interrupt
- Zero dependency on langgraph/langchain or any orchestration framework
- Full documentation: `src/dot/workflow/README.md`
- Core concepts:
  - `WorkflowGraph`: add_node / add_edge / add_conditional_edges
  - `WorkflowContext`: Node communication (data/results/cancel token)
  - `WorkflowEvent`: Discriminated union (NodeStart/NodeEnd/Error/Done/Interrupt)
  - Built-in nodes: `FunctionNode`, `ParallelNode`, `SubgraphNode`

**2. Tool System**
- `AgentTool` frozen dataclass (zero inheritance, `frozen=True` prevents mutation)
- Built-in tools: `read_file`, `write_file`, `edit_file`, `bash`, `glob_search`, `grep`
- Located in `src/dot/coding/tools/`

**3. Event System** (3 levels, Pydantic discriminated unions)
- `ProviderEvent`: TextDelta/ToolCallStart/End/Done (LLM streaming)
- `AgentEvent`: TurnStart/End, MessageStart/End, ToolExecutionStart/End
- `CodingEvent`: Compaction, SessionInfoChanged

**4. Permission System** (3-level interception)
1. System blacklist (dangerous commands, path traversal)
2. Project blacklist (`.agent-security.json`)
3. Pattern rules (plan/edit/auto modes)
- Decision states: ALLOW / ASK / DENY (ASK auto-degrades to DENY without UI)

**5. Context Compression** (3-tier)
- L1 (≥50%): Remove recoverable tool results (read_file), keep paths (no LLM)
- L2 (≥70%): Delete old tool calls (bash/grep output) (no LLM)
- L3 (≥85%): LLM generates structured summary (requires LLM)

**6. RAG Module** (optional, uses langchain exception)
- Parent-child chunking strategy
- Hybrid retrieval (vector + BM25 + RRF fusion)
- See `docs/RAG_MODULE_GUIDE.md` for complete details
- Backends: ChromaDB (vectors) + JSON files (parent text)

## Critical Rules & Gotchas

### Dependency Direction
- **NEVER** introduce reverse dependencies (e.g., agent → coding)
- Permission contracts live as Protocols in `dot.agent.permission`, implemented in `dot.coding.permission`
- Workflow core must stay framework-free

### Testing
- Always pass `--ignore=tests/legacy` to pytest (those tests import removed packages)
- `tests/conftest.py` puts `src/` first on `sys.path` (don't bypass this)
- `tests/mcp_server.py` is a fixture server, NOT a test file
- `tests/conftest.py` deletes `RAG_API_TOKEN` from env (don't re-add it in tests)

### Environment Configuration
- Config file is `.env` (copy from `.env_example`)
- Required vars: `API_KEY`, `MODEL`, `BASE_URL`
- Optional: `MODEL_FALLBACKS`, `DOT_REQUEST_TIMEOUT`, `DOT_MAX_RETRIES`, `TAVILY_API_KEY`, `DOT_LOG_LEVEL`, `DOT_TRACE_ENABLED`
- **Note**: The example file is named `.env_example` (with underscore), not `.env.example`

### Code Style
- Comments, docs, and commit messages are in Chinese
- Commit style: `feature：...` (Chinese colon)
- Events are Pydantic discriminated unions
- Tools are frozen dataclasses
- Extension points are Protocols with zero inheritance

### TUI Development
- Uses Textual library
- Has had mouse-rendering conflicts in the past
- Test in a real terminal, not WSL or VS Code integrated terminal

### File Organization
- `src/dot/` - Main Python package
- `tests/` - Test suite (legacy/ subdirectory is broken, always ignore)
- `web/` - Vite+TS frontend (auxiliary, not part of Python package)
- `springboot-demo/`, `pi_src/`, `scripts/` - Auxiliary sandboxes
- `specs/` - Architecture specifications (001-004 are numbered in order)
- `doc/` - Per-feature design docs
- `docs/` - Additional documentation (RAG guide, etc.)
- `evals/` - Evaluation framework (new)

## Key Files to Read Before Making Changes

### Core Documentation
1. `README.md` - Architecture overview and module table
2. `AGENTS.md` - Project-specific instructions (this is like a per-project AGENTS.md)
3. `src/dot/workflow/README.md` - Workflow engine usage guide (REQUIRED before touching workflow)
4. `docs/RAG_MODULE_GUIDE.md` - RAG pipeline documentation

### Architecture Specs
5. `specs/004-agent-architecture-overhaul/spec.md` - Latest architecture spec
6. `specs/003-arch-smells-refactor/spec.md` - Previous refactor spec
7. `specs/002-cli-terminal-rewrite/spec.md` - CLI/TUI rewrite spec
8. `specs/001-architecture-refactor/spec.md` - Initial refactor spec

### Design Docs
9. `doc/architecture-design.md` - Architecture design doc
10. `doc/tool-calling-design.md` - Tool calling design
11. `doc/trace-design.md` - Tracing system design
12. `doc/extension-design.md` - Extension system design
13. `doc/eval-design.md` - Evaluation framework design (new)

## Package Structure

```
src/dot/
├── ai/                          # Layer 1: Provider abstraction
│   ├── types.py                 # Message types (AgentMessage, AssistantMessage...)
│   ├── provider.py              # ModelProvider Protocol
│   ├── events.py                # ProviderEvent discriminated union
│   ├── stream.py                # Stream canonicalization
│   ├── catalog.py               # catalog.toml config loader
│   ├── limits.py                # Context window estimation
│   └── providers/openai.py      # OpenAI provider implementation
│
├── agent/                       # Layer 2: Agent core
│   ├── tools.py                 # AgentTool frozen dataclass
│   ├── events.py                # AgentEvent discriminated union
│   ├── harness.py               # AgentHarness (message history, event subscription, dual queues)
│   ├── loop.py                  # run_agent_loop (inner loop)
│   ├── executor.py              # execute_tool_safely unified error handling
│   ├── history.py               # repair_tool_history self-healing
│   ├── loop_detector.py         # Loop detection and protection
│   ├── cancel.py                # CancellationToken Protocol
│   ├── permission.py            # Permission Protocol (for dot.coding to implement)
│   └── workflow.py              # AgentNode adapter (wraps agent turn as workflow node)
│
├── workflow/                    # Layer 3: Generic workflow engine
│   ├── graph.py                 # WorkflowGraph (add_node/add_edge/add_conditional_edges)
│   ├── graph_runner.py          # GraphRunner (execution loop with retry/backoff/compensation/cancel)
│   ├── graph_validate.py        # GraphValidator (static validation: entry/reachable/END/cycles)
│   ├── graph_export.py          # to_dict / to_mermaid serialization
│   ├── node.py                  # WorkflowNode Protocol + FunctionNode / ParallelNode
│   ├── subgraph.py              # SubgraphNode (nested graphs with shared data)
│   ├── interaction.py           # run_with_interaction (interrupt ↔ human handler bridge)
│   ├── context.py               # WorkflowContext (data/results + cancel token + resume)
│   ├── events.py                # WorkflowEvent discriminated union
│   └── cancel.py                # WorkflowCancellationToken (read-only Protocol)
│
├── coding/                      # Layer 4: Coding application
│   ├── state.py                 # CodingWorkflowState + ValidationResult
│   ├── workflow.py              # Built-in coding workflow (build_coding_workflow)
│   ├── permission.py            # PermissionManager (3-level interception)
│   ├── modes.py                 # AgentMode (plan/edit/auto)
│   ├── commands.py              # CommandRegistry (slash commands)
│   ├── host.py                  # CodingHost (assembly layer)
│   ├── tools/                   # Built-in tools
│   │   ├── file_tools.py        # read_file / write_file / edit_file
│   │   ├── bash_tool.py         # bash (with dangerous command detection)
│   │   ├── glob_tool.py         # glob_search
│   │   └── grep_tool.py         # grep
│   ├── session/                 # Session management
│   │   ├── session.py           # Session (messages + file snapshots)
│   │   ├── manager.py           # SessionManager lifecycle
│   │   ├── storage.py           # SessionStorage (JSONL)
│   │   └── tree.py              # SessionTree branch management
│   ├── compress/                # 3-tier compression
│   │   ├── planner.py           # Compression planning
│   │   ├── l1_extract.py        # L1 remove recoverable tool results
│   │   ├── l2_summarize.py      # L2 delete old tool calls
│   │   └── l3_semantic.py       # L3 LLM summary
│   ├── trace/                   # Event-driven tracing
│   │   ├── collector.py         # TraceCollector
│   │   └── exporter.py          # JSONL export
│   ├── skills/                  # Skills (prompt injection)
│   │   ├── skill.py             # SKILL.md parser
│   │   └── manager.py           # SkillManager
│   ├── extensions/              # Extension system
│   │   ├── api.py               # ExtensionAPI
│   │   ├── generation.py        # ExtensionGeneration liveness token
│   │   └── builtins/mcp/        # MCP built-in extension
│   ├── ui/                      # UI abstraction
│   │   └── bridge.py            # UiBridge + NullUiBridge
│   └── cli/                     # CLI entry
│       ├── app.py               # typer commands
│       ├── config.py            # CLIConfig
│       ├── console_app.py       # Console mode
│       └── tui/                 # Textual TUI
│
├── rag/                         # RAG service (MAY use langchain)
├── kg/                          # Knowledge graph RAG server
└── __init__.py                  # Top-level exports
```

## Python Version Note

The `pyproject.toml` specifies `requires-python = ">=3.13"`, but the current environment shows Python 3.11.15. Be aware of potential compatibility issues when developing or testing.

## Common Development Tasks

### Adding a New Built-in Tool

1. Implement in `src/dot/coding/tools/` (follow existing patterns in `file_tools.py`, `bash_tool.py`)
2. Register in `src/dot/coding/host.py` or appropriate registry
3. Add tests following existing patterns in `tests/`

### Modifying Workflow Engine

**REQUIRED**: Read `src/dot/workflow/README.md` first.
- Core engine files: `graph.py`, `graph_runner.py`, `graph_validate.py`, `graph_export.py`
- Never import agent, model SDKs, or orchestration frameworks in workflow core
- Events use Pydantic discriminated unions

### Working with the Agent Loop

Key file: `src/dot/agent/loop.py` (run_agent_loop)
- Handles LLM streaming → tool execution → message appending
- Uses dual queues (steering/follow-up) for message injection
- Tool execution via `execute_tool_safely` in `executor.py`

### Permission System

Permission Protocol: `src/dot/agent/permission.py`
Permission Implementation: `src/dot/coding/permission.py`
- 3-level interception: system blacklist → project blacklist → pattern rules
- Decision states: ALLOW / ASK / DENY

### Adding a New Feature

1. Check relevant design doc in `doc/` first
2. Follow layer dependency rules strictly
3. Write tests (pytest is the only configured dev tool)
4. Update relevant documentation

## Important Behavioral Guidelines

From the project's AGENTS.md and your own instructions:

1. **Think Before Coding**: State assumptions explicitly. If multiple interpretations exist, present them. Surface tradeoffs.

2. **Simplicity First**: Minimum code that solves the problem. No speculative features. If you write 200 lines and it could be 50, rewrite it.

3. **Surgical Changes**: Touch only what you must. Match existing style. Don't refactor working code. Clean up only your own mess.

4. **Goal-Driven Execution**: Define verifiable success criteria before implementing. For multi-step tasks, state a brief plan with verification checks.

5. **Verify Before Delivery**: All deliverables must be self-verified. For API calls, test with curl/Invoke-RestMethod first. For scripts, run syntax check or dry-run before delivery.

## Commit Message Style

- Format: `feature：...` (Chinese colon)
- Written in Chinese
- End with: `Co-Authored-By: Claude Code <noreply@anthropic.com>`

## Additional Resources

- `PROJECT_SUMMARY.md` - Comprehensive project overview
- `specs/` - Numbered architecture specifications (read in order: 001 → 004)
- `doc/` - Feature-specific design documents
- `docs/` - User-facing documentation (RAG guide, etc.)
- `.dot/` - Runtime data (sessions, logs, traces, MCP config)
- `evals/` - Evaluation framework and results (new)

## Not Part of This Package

These directories are auxiliary/untracked sandboxes:
- `web/` - Vite+TypeScript frontend
- `springboot-demo/` - Java demo
- `pi_src/` - Raspberry Pi source
- `scripts/` - Utility scripts
- `deepeval/` - Deep evaluation tools
- `求职/` - Chinese for "job hunting" (personal directory)
