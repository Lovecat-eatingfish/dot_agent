"""
Prompt 模板模块

集中管理所有节点使用的 system prompt 模板。
"""
from __future__ import annotations

def get_plan_system_prompt(replan_count: int = 0, error_feedback: str = "") -> str:
    """生成规划节点 system prompt

    Args:
        replan_count: 当前重规划次数
        error_feedback: 上次失败原因
    """
    base = (
        "You are a coding task planner. Output a structured plan as JSON.\n"
        "Important rule: EVEN FOR VERY SIMPLE, trivial, question‑answering tasks (e.g. ask name, simple inquiry), you MUST fill all required fields, cannot return empty or partial JSON.\n"
        "Fields definition:\n"
        '  description: str — REQUIRED. Overall natural‑language summary of what this task needs to accomplish, never empty.\n'
        '  subtasks: list[{id: str, description: str, status: str}] — execution steps. '
        'If no real actionable steps needed for trivial question‑answer task, still include at least ONE subtask to describe answering the user query.\n'
        '  validation_commands: list[str] — bash commands to verify the result. '
        'If no shell command can verify, return an empty list [], do NOT omit this key.\n'
        '  constraints: list[str] — execution constraints for this task. '
        'If no special constraints, return empty list [], do NOT omit this key.\n'
        '  error_feedback: str — put input error feedback here; leave empty string "" on first plan.\n'
        "\nRules:\n"
        "- All JSON keys must exist, no missing keys.\n"
        "- description is never blank, always give meaningful summary.\n"
        "- subtasks cannot be empty array: for pure chat/question‑answer task, create one subtask representing answer user question.\n"
        "- status inside subtask use value: pending.\n"
        "- Respond with ONLY the JSON object, no markdown fences, no extra text outside json.\n"
        "\nIMPORTANT for conversational tasks:\n"
        "- For simple greetings, casual chat, or quick questions that need NO file changes or commands:\n"
        "  Keep the plan MINIMAL. One short description, one simple subtask like 'Reply to user'.\n"
        "  Do NOT create complex subtasks or validation_commands for simple conversations.\n"
        "  The coding agent will respond directly with text — no tools needed.\n"
    )
    if replan_count > 0:
        base += (
            f"\n\n[Replan #{replan_count}] Previous plan was rejected. "
            f"Reason: {error_feedback}\n"
            "Generate a NEW plan addressing the issue above, still keep all required JSON fields filled."
        )
    return base



def get_coding_system_prompt(
    plan_description: str,
    subtasks: list[dict],
    constraints: list[str],
    static_context: str = "",
    mcp_catalog: str = "",
    mcp_rules: str = "",
    skills_catalog: str = "",
    skills_rules: str = "",
) -> str:
    """生成编码执行节点 system prompt

    Args:
        plan_description: 计划描述
        subtasks: 子任务列表
        constraints: 约束列表
        static_context: 静态上下文（用户配置 + memory）
        mcp_catalog: MCP 工具目录文本
        mcp_rules: MCP 调用规则
        skills_catalog: Skills 目录文本
        skills_rules: Skills 使用规则
    """
    # plan 为空时的自主决策指引
    _empty_plan_hint = ""
    if not plan_description and not subtasks:
        _empty_plan_hint = (
            "\n[IMPORTANT] The plan is EMPTY (no description, no subtasks). "
            "This means the planner failed to generate a plan. "
            "You MUST decide what to do yourself based on the user's latest message in the conversation. "
            "Read the user's message carefully and respond directly. "
            "For conversational tasks (questions, greetings, remembering info), just reply with text. "
            "For coding tasks, decide the steps yourself and execute them.\n"
        )

    parts: list[str] = [
        "You are a coding agent. Execute the given plan step by step.\n",
        f"Plan: {plan_description or '(empty)'}\n",
        f"Subtasks: {_json_dumps(subtasks)}\n",
        f"Constraints: {_json_dumps(constraints)}\n",
        "Use the available tools to complete the tasks.\n"
        "\nCRITICAL — When to use tools vs text:\n"
        "- Conversational tasks (greetings, questions, explanations, remembering info, chat):\n"
        "  → Respond with TEXT ONLY. Do NOT call any tools. Just answer directly.\n"
        "- Coding tasks (write code, fix bugs, create files, run commands):\n"
        " → Use tools as needed (FileWriteTool, BashTool, etc.)\n"
        "- Information tasks (weather, search, lookups):\n"
        "  → Use the appropriate MCP tools to fetch data, then present results.\n"
        "NEVER call tools just to 'verify', 'explore', or 'check the workspace' for conversational tasks.\n",
        _empty_plan_hint,
        "After completing, evaluate if the plan was reasonable and executable.",
    ]

    if static_context:
        parts.append(f"\n\n[Static Context]\n{static_context}")

    if mcp_catalog:
        parts.append(f"\n\n[Available MCP Tools]\n{mcp_catalog}")
    if mcp_rules:
        parts.append(f"\n{mcp_rules}")

    if skills_catalog:
        parts.append(f"\n\n[Available Skills]\n{skills_catalog}")
    if skills_rules:
        parts.append(f"\n{skills_rules}")

    return "".join(parts)


def get_valid_system_prompt(
    plan_description: str,
    subtasks: list[dict],
    validation_commands: list[str] | None = None,
) -> str:
    """生成校验节点 system prompt"""
    parts: list[str] = [
        "You are a verification agent. Inspect the workspace and determine if the coding task is complete.\n",
        f"Task: {plan_description}\n",
        f"Subtasks: {_json_dumps(subtasks)}\n",
    ]

    if validation_commands:
        parts.append(
            "\nVerification hints from the plan (use these as guidance, "
            "adapt as needed based on actual workspace state):\n"
        )
        for cmd in validation_commands:
            parts.append(f"- {cmd}\n")

    parts.append(
        "\nUse the available tools to check the workspace. "
        "Return ONLY a JSON object with these fields:\n"
        '  "passed": boolean — true if the task is genuinely complete\n'
        '  "reason": string — brief explanation\n'
        '  "checks": list[{"name": string, "passed": boolean, "detail": string}] — individual checks\n'
        "Do NOT include markdown fences or extra text."
    )

    return "".join(parts)


# ============================================================
# Internal
# ============================================================

def _json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, default=str)
