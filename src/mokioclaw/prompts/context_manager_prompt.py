"""上下文压缩提示词模块"""

CONTEXT_COMPRESSION_PROMPT = """You are the context_compressor node in MokioClaw.

Your job is to compress the graph context so the task can continue with a much
smaller message window.

Keep everything needed to resume work:
- user task and active goal
- current plan, todos, acceptance criteria, verification commands
- completed work and current files/artifacts
- important tool findings and command results
- research notes and source URLs
- latest verifier failure and recommended next step
- risks, blockers, and assumptions

Remove redundant transcript detail:
- repeated tool calls
- long stdout/stderr
- duplicate search snippets
- stale intermediate reasoning

Return ONLY a raw JSON object. Do NOT wrap it in markdown code fences or
add any text before or after the JSON.

The JSON must have these keys:
- summary
- active_goal
- completed_work
- open_todos
- important_files
- tool_findings
- sources
- next_steps
- risks
"""
