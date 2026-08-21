"""
Coding Agent LangGraph 骨架

职责：图编排、状态定义、条件路由、Session 内存管理。
节点包含核心业务逻辑：上下文压缩、规划、编码执行、校验、人工介入、终止。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Dict, Generator, Optional, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph, add_messages
from langchain_core.runnables import RunnableConfig

from mokioclaw.core.log import get_logger
from mokioclaw.core.hook_loader import load_hooks_into_runner
from mokioclaw.core.hooks import HookRunner
from mokioclaw.core.utils import execute_tool_by_name, last_ai_content
from mokioclaw.memory.tiered_compression import compress_messages_by_tier
from mokioclaw.orchestration.agent_authorizer import AgentAuthorizer, AutoModeClassifier
from mokioclaw.orchestration.mcp_manager import MCPManager
from mokioclaw.orchestration.session_persistence import (
    AGENT_SESSIONS_DIR,
    SessionPersistence,
    diff_messages,
    persist_turn,
)
from mokioclaw.orchestration.skills_manager import SkillsManager
from mokioclaw.providers.openai_provider import create_model
from mokioclaw.reliability.cost import record_llm_usage
from mokioclaw.state.runtime import RuntimeState
from mokioclaw.tools import build_tools

logger = get_logger(__name__)


def _get_writer():
    """获取事件写入器（与 nodes.py 中的 _get_writer 行为一致）"""
    try:
        from langgraph.config import get_stream_writer
        langgraph_writer = get_stream_writer()
    except RuntimeError:
        langgraph_writer = None

    bus = get_event_bus()

    def writer(event: dict[str, Any]) -> None:
        if langgraph_writer is not None:
            try:
                langgraph_writer(event)
            except Exception:
                pass
        try:
            bus.emit(event)
        except Exception:
            pass

    return writer
# Constants
# ============================================================

REPLAN_THRESHOLD = 3
MAX_ATTEMPT_DEFAULT = 3


# ============================================================
# Graph State
# ============================================================

class CodingAgentState(TypedDict, total=False):
    """Coding Agent 图的运行时状态（内存为权威源）"""

    # LLM 工作上下文
    messages: Annotated[list[BaseMessage], add_messages]

    # 运行时状态（工作区、审批、工具等）
    runtime: RuntimeState

    # plan_node 输出：
    #     plan_description = task_plan.get("description", "")
    #     subtasks = task_plan.get("subtasks", [])
    #     constraints = task_plan.get("constraints", [])
    #     todo： plan节点返回值添加 校验的计划，或者 command
    task_plan: dict

    # 重规划计数（coding 触发 replan 时 +1）
    replan_count: int

    # 编码重试计数（仅编码失败累加，replan 不消耗）
    attempt_count: int

    # 最大编码重试次数
    max_attempt: int

    # valid_node 输出
    validate_result: dict

    # 工具调用缓存，coding 节点标记 plan_invalid 放在这里
    tool_artifacts: dict

    # 是否需要人工介入
    need_human_intervene: bool

    # 外部 resume 传入的值：continue / stop（human_intervene_marker 读取）
    resume_action: str

    # 当前轮次的原始用户需求
    task: str


# ============================================================
# Nodes（核心业务逻辑）
# ============================================================

def context_compress_node(state: CodingAgentState) -> dict[str, Any]:
    """上下文压缩节点

    检测 messages token 是否超限，超限则调用 compress_messages_by_tier 压缩。
    只修改 messages 字段，不碰其他 state 字段。
    仅在入口执行一次，replan 回跳 plan_node 不会再次触发此节点。
    """
    messages: list[Any] = list(state.get("messages", []))
    token_limit = _get_context_token_limit()
    estimated = _estimate_message_tokens(messages)

    if estimated <= token_limit:
        return {}

    logger.info("context_compress_node: %d tokens > limit %d, compressing", estimated, token_limit)
    context_summary = state.get("_context_summary", "")

    compressed: list[Any] = compress_messages_by_tier(
        messages,
        context_summary=context_summary,
    )

    return {"messages": compressed}


def plan_node(state: CodingAgentState) -> dict[str, Any]:
    """规划节点

    读取 messages，输出 task_plan。
    可以看到历史 messages，包含上一次 plan 失败的原因。
    """
    writer = _get_writer()
    messages: list[Any] = list(state.get("messages", []))
    existing_plan: dict = state.get("task_plan", {})
    error_feedback = existing_plan.get("error_feedback", "")
    replan_count = int(state.get("replan_count", 0))

    # 构建 system prompt
    replan_hint = ""
    if replan_count > 0:
        replan_hint = (
            f"\n\n[Replan #{replan_count}] Previous plan was rejected. "
            f"Reason: {error_feedback}\n"
            "Generate a NEW plan addressing the issue above."
        )

    # todo： 提示词抽取 + 完善
    system_prompt = (
        "You are a coding task planner. Output a structured plan as JSON.\n"
        "Fields:\n"
        '  description: str — overall plan description\n'
        '  subtasks: list[{id: str, description: str, status: str}] — execution steps\n'
        '  validation_commands: list[str] — bash commands to verify the result\n'
        '  constraints: list[str] — execution constraints\n'
        '  error_feedback: str — leave empty on first plan\n'
        "Respond with ONLY the JSON object, no markdown fences."
    )
    if replan_count > 0:
        system_prompt += replan_hint

    try:
        # todo： config 参数错误
        # todo：可以重试三次生成这个 plan计划，每次如果生成错误 把error 信息 再次拼接 给llm 重新生成
        config = RunnableConfig(**{"response_format": {"type": "json_object"}})
        response = create_model().invoke(
            [SystemMessage(content=system_prompt), *messages],
            config={"response_format": {"type": "json_object"}},
        )
        text = str(getattr(response, "content", "") or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        plan: dict[str, Any] = json.loads(text) if text else {}
    except Exception as exc:
        logger.warning("plan_node: LLM plan generation failed: %s", exc, exc_info=True)
        plan = {
            "description": "",
            "subtasks": [],
            "validation_commands": [],
            "constraints": [],
            "error_feedback": "",
        }

    plan.setdefault("description", "")
    plan.setdefault("subtasks", [])
    plan.setdefault("validation_commands", [])
    plan.setdefault("constraints", [])
    plan.setdefault("error_feedback", "")

    # todo： plan写入到 state
    state['task_plan'] = plan

    writer({"type": "plan_created", "plan": plan, "replan_count": replan_count})

    return {"task_plan": plan}


def coding_agent_node(state: CodingAgentState) -> dict[str, Any]:
    """编码执行节点

    根据 task_plan 执行编码相关工作。
    执行过程中识别 task_plan 是否不合理不可执行。
    """
    writer = _get_writer()
    runtime: RuntimeState | None = state.get("runtime")
    task_plan: dict = state.get("task_plan", {})

    # Build tools from runtime
    tools: list[Any] = []
    try:
        tools = build_tools(runtime) if runtime else []
    except Exception as exc:
        logger.warning("coding_agent_node: tool build failed: %s", exc, exc_info=True)

    plan_description = task_plan.get("description", "")
    subtasks = task_plan.get("subtasks", [])
    constraints = task_plan.get("constraints", [])

    # Build agent prompt
    system_prompt = (
        "You are a coding agent. Execute the given plan step by step.\n"
        f"Plan: {plan_description}\n"
        f"Subtasks: {json.dumps(subtasks, ensure_ascii=False)}\n"
        f"Constraints: {json.dumps(constraints, ensure_ascii=False)}\n"
        "Use the available tools to complete the tasks.\n"
        "After completing, evaluate if the plan was reasonable and executable."
    )

    messages: list[Any] = list(state.get("messages", []))
    agent_messages: list[Any] = [SystemMessage(content=system_prompt), *messages]

    # Execute tool-calling loop
    max_loops = 10
    loop_count = 0
    tool_call_count = 0

    while loop_count < max_loops:
        loop_count += 1
        try:
            response = create_model().bind_tools(tools).invoke(agent_messages)
        except Exception as exc:
            logger.warning("coding_agent_node: model invoke failed: %s", exc, exc_info=True)
            break

        record_llm_usage(response)

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break

        tool_call_count += len(tool_calls)
        writer({"type": "tool_calls", "count": len(tool_calls), "loop": loop_count})

        for call in tool_calls:
            try:
                tool_msg = execute_tool_by_name(
                    tools=tools,
                    call=call,
                    runtime=runtime,
                )
                agent_messages.append(tool_msg)
            except Exception as exc:
                logger.warning("coding_agent_node: tool execution failed: %s", exc, exc_info=True)
                agent_messages.append(
                    ToolMessage(
                        content=f"Error: {exc}",
                        tool_call_id=call.get("id", f"call-{tool_call_count}"),
                    )
                )

    summary = last_ai_content(agent_messages) or f"Executed {tool_call_count} tool calls in {loop_count} loops"

    # Evaluate plan quality
    plan_reasonable, plan_issue = _evaluate_plan_quality(
        plan_description=plan_description,
        subtasks=subtasks,
        tool_call_count=tool_call_count,
        loop_count=loop_count,
        summary=summary,
    )

    replan_count = int(state.get("replan_count", 0))

    if not plan_reasonable:
        messages.append(AIMessage(content=f"[Plan Issue] {plan_issue}"))
        if replan_count < REPLAN_THRESHOLD:
            new_replan = replan_count + 1
            updated_plan = dict(task_plan)
            updated_plan["error_feedback"] = plan_issue
            return {
                "messages": [AIMessage(content=f"[Plan Issue] {plan_issue}")],
                "task_plan": updated_plan,
                "replan_count": new_replan,
                "need_human_intervene": False,
            }
        else:
            return {
                "messages": [AIMessage(content=f"[Plan Issue] {plan_issue}")],
                "need_human_intervene": True,
                "replan_count": replan_count,
            }

    return {
        "messages": [AIMessage(content=summary)],
        "need_human_intervene": False,
    }


def valid_node(state: CodingAgentState) -> dict[str, Any]:
    """校验节点

    执行 plan 预先定义的校验逻辑（编译、单元测试等），
    判断编码结果是否达标。只处理编码实现层面的失败。
    """
    writer = _get_writer()
    runtime: RuntimeState | None = state.get("runtime")
    task_plan: dict = state.get("task_plan", {})
    validation_commands: list[str] = task_plan.get("validation_commands", [])

    results: list[dict[str, Any]] = []
    all_passed = True
    error_msg = ""

    for cmd in validation_commands:
        try:
            from mokioclaw.tools.bash_tool import run_bash
            exit_code, stdout, stderr = run_bash(runtime, cmd)
            passed = exit_code == 0
            results.append({
                "command": cmd,
                "passed": passed,
                "exit_code": exit_code,
                "stdout": (stdout or "")[:2000],
                "stderr": (stderr or "")[:2000],
            })
            if not passed:
                all_passed = False
                error_msg = f"Command failed: {cmd}\nExit code: {exit_code}\nStderr: {(stderr or '')[:500]}"
                break
        except Exception as exc:
            all_passed = False
            error_msg = f"Validation error: {exc}"
            results.append({
                "command": cmd,
                "passed": False,
                "error": str(exc),
            })
            break

    fail_reason = error_msg if not all_passed else ""
    attempt_count = int(state.get("attempt_count", 0)) + 1

    validate_result = {
        "passed": all_passed,
        "error_msg": error_msg,
        "fail_reason": fail_reason,
        "checks": results,
    }

    writer({"type": "validation_result", "passed": all_passed, "error_msg": error_msg})

    return {
        "validate_result": validate_result,
        "attempt_count": attempt_count,
    }


def human_intervene_marker_node(state: CodingAgentState) -> dict[str, Any]:
    """人工介入标记节点

    调用 interrupt() 暂停图执行，等待外部代码传入 resume 值。
    外部 resume 时读取 state["resume_action"]：
      - "continue" → 路由到 plan_node（重置 replan_count）
      - "stop" / 无值 → 路由到 finally_node
    """
    writer = _get_writer()
    need_intervene = state.get("need_human_intervene", False)
    replan_count = state.get("replan_count", 0)
    attempt_count = state.get("attempt_count", 0)

    interrupt_value = {
        "reason": "replan_or_attempt_exhausted",
        "replan_count": replan_count,
        "attempt_count": attempt_count,
        "need_human_intervene": need_intervene,
    }
    writer({"type": "human_intervene", "value": interrupt_value})

    # First call raises Interrupt; resume returns the resume value
    resume_data = interrupt(interrupt_value)

    resume_action = "stop"
    if isinstance(resume_data, dict):
        action = resume_data.get("resume_action", "stop")
        if action in ("continue", "stop"):
            resume_action = action

    if resume_action == "continue":
        return {
            "need_human_intervene": False,
            "replan_count": 0,
            "attempt_count": 0,
            "resume_action": "continue",
        }

    return {
        "need_human_intervene": False,
        "resume_action": "stop",
    }


def finally_node(state: CodingAgentState) -> dict[str, Any]:
    """终止节点

    统一收尾，无业务输出。
    图运行到此结束，所有持久化/git commit/turn 快照全部交给外部业务代码处理。
    """
    writer = _get_writer()
    validate_result: dict = state.get("validate_result", {})
    passed = validate_result.get("passed", False)
    task_plan: dict = state.get("task_plan", {})

    summary = task_plan.get("description", "")
    final_answer = f"Task completed. Passed: {passed}. Plan: {summary}"

    writer({"type": "final", "passed": passed, "summary": summary})

    return {"final_answer": final_answer}


# ============================================================
# Helpers
# ============================================================

def _evaluate_plan_quality(
    *,
    plan_description: str,
    subtasks: list[dict],
    tool_call_count: int,
    loop_count: int,
    summary: str,
) -> tuple[bool, str]:
    """评估 plan 是否合理可执行"""
    if not plan_description and not subtasks:
        return False, "Plan is empty — no description or subtasks provided."
    if not subtasks:
        return False, "Plan has no subtasks — nothing to execute."
    if loop_count >= 10 and tool_call_count == 0:
        return False, "Agent exhausted all loops without making any tool calls."
    if not summary or summary == "(no result)":
        return False, "Agent produced no output after execution."
    return True, ""


# ============================================================
# Routers
# ============================================================

def route_coding_agent(state: CodingAgentState) -> str:
    """coding_agent_node 之后的条件路由

    - tool_artifacts.plan_invalid=True & replan_count < replan_max → "plan_node"
    - tool_artifacts.plan_invalid=True & replan_count >= replan_max → "human_intervene_marker"
    - 其他 → "valid_node"
    """
    tool_artifacts: dict = state.get("tool_artifacts", {})
    if tool_artifacts.get("plan_invalid"):
        replan_count = int(state.get("replan_count", 0))
        if replan_count < REPLAN_THRESHOLD:
            return "plan_node"
        return "human_intervene_marker"
    return "valid_node"


def route_valid_node(state: CodingAgentState) -> str:
    """valid_node 之后的条件路由

    - passed == true → "finally_node"
    - passed == false & attempt_count < max_attempt → "coding_agent_node"
    - else → "human_intervene_marker"
    """
    validate_result: dict = state.get("validate_result", {})
    if validate_result.get("passed"):
        return "finally_node"

    attempt_count = int(state.get("attempt_count", 0))
    max_attempt = int(state.get("max_attempt", MAX_ATTEMPT_DEFAULT))

    if attempt_count < max_attempt:
        return "coding_agent_node"
    return "human_intervene_marker"


def route_human_intervene(state: CodingAgentState) -> str:
    """human_intervene_marker 之后的条件路由

    - resume_action == "continue" → "plan_node"
    - 其他（stop / 无值）→ "finally_node"
    """
    resume_action = state.get("resume_action", "stop")
    return "plan_node" if resume_action == "continue" else "finally_node"


# ============================================================
# Graph Builder（每个 session 只 compile 一次）
# ============================================================

def build_graph() -> StateGraph:
    """构建并返回未编译的图。外部调用 .compile() 拿到 CompiledGraph。

    图结构：
        START → context_compress → plan_node → coding_agent_node
                                                       │
                                           ┌───────────┼───────────────────┐
                                           │           │                   │
                                     plan_invalid   plan_valid          plan_invalid
                                     replan<3         │                replan>=3
                                           │           │                   │
                                           ▼           ▼                   ▼
                                       plan_node   valid_node     human_intervene_marker
                                                                           │
                                                                           ▼
                                                                       finally_node → END
    """
    graph = StateGraph(CodingAgentState)

    graph.add_node("context_compress", context_compress_node)
    graph.add_node("plan_node", plan_node)
    graph.add_node("coding_agent_node", coding_agent_node)
    graph.add_node("valid_node", valid_node)
    graph.add_node("human_intervene_marker", human_intervene_marker_node)
    graph.add_node("finally_node", finally_node)

    # 固定链路
    graph.add_edge(START, "context_compress")
    graph.add_edge("plan_node", "coding_agent_node")
    graph.add_edge("human_intervene_marker", "finally_node")
    graph.add_edge("finally_node", END)

    # 条件路由
    graph.add_conditional_edges(
        "context_compress",
        lambda _state: "plan_node",
        {"plan_node": "plan_node"},
    )

    graph.add_conditional_edges(
        "coding_agent_node",
        route_coding_agent,
        {
            "plan_node": "plan_node",
            "valid_node": "valid_node",
            "human_intervene_marker": "human_intervene_marker",
        },
    )

    graph.add_conditional_edges(
        "valid_node",
        route_valid_node,
        {
            "finally_node": "finally_node",
            "coding_agent_node": "coding_agent_node",
            "human_intervene_marker": "human_intervene_marker",
        },
    )

    graph.add_conditional_edges(
        "human_intervene_marker",
        route_human_intervene,
        {
            "plan_node": "plan_node",
            "finally_node": "finally_node",
        },
    )

    return graph


# ============================================================
# Session 数据结构
# ============================================================

@dataclass
class Session:
    """单个会话的内存数据"""

    session_id: str
    compiled_graph: Any  # CompiledGraph — 图只编译一次
    current_state: CodingAgentState  # 内存常驻 state，内存为权威源，就是agent运行的时候产生的交互数据
    is_running: bool = False # 是不是在运行
    replan_max: int = REPLAN_THRESHOLD # plan agent最大交互次数
    max_attempt: int = MAX_ATTEMPT_DEFAULT # 最大 校验和code agent交互次数
    persistence: Optional[SessionPersistence] = None # 会话和state持久化
    workspace: Optional[Path] = None # 工作空间
    current_turn_id: int = 0 # 当前会话的轮数
    mcp_host: Optional[Any] = None  # MCPHost（渐进披露，有状态）
    skill_host: Optional[Any] = None  # SkillHost（渐进披露，有状态）
    authorizer: Optional[AgentAuthorizer] = None # 审批引擎
    hook_runner: Optional[HookRunner] = None # Hook 执行引擎


# ============================================================
# SessionManager
# ============================================================

class SessionManager:
    """Session 内存管理器

    管理 session 的内存状态和持久化。
    内部集成了 SessionPersistence 处理磁盘 / git 操作。
    持久化策略：每个 session 对应一个 workspace 目录，graph 只在外部层（stream 结束后）落盘。
    """

    def __init__(self, sessions_root: Optional[Path] = None) -> None:
        self._sessions_root = sessions_root or Path(AGENT_SESSIONS_DIR)
        self._sessions: Dict[str, Session] = {}

    def create_session(self, session_id: Optional[str] = None) -> Session:
        """创建新 session

        - 编译图一次
        - 初始化 state
        - 初始化持久化目录（git init）
        - 存入内存字典
        """
        sid = session_id or str(uuid.uuid4())
        if sid in self._sessions:
            raise ValueError(f"Session {sid} already exists")

        compiled_graph = build_graph().compile()
        initial_state = build_initial_state()

        # 初始化持久化层
        persistence = SessionPersistence(sessions_root=self._sessions_root)
        persistence.save_session_meta(sid, persistence._empty_session_meta(sid))
        git_init(persistence.session_dir(sid))

        # 初始化 MCP / Skills Host（渐进披露）
        mcp_mgr = MCPManager(workspace=persistence.session_dir(sid))
        mcp_host = MCPHost(mcp_mgr)
        skills_mgr = SkillsManager(workspace=persistence.session_dir(sid))
        skill_host = SkillHost(skills_mgr)

        # 初始化 Auto 模式分类器（使用独立模型实例，与主 Agent 模型隔离）
        try:
            classifier_model = create_model()
        except Exception:
            classifier_model = None
        classifier = AutoModeClassifier(model=classifier_model)

        # 初始化 Agent 授权器
        authorizer = AgentAuthorizer(classifier=classifier)

        session = Session(
            session_id=sid,
            compiled_graph=compiled_graph,
            current_state=initial_state,
            persistence=persistence,
            workspace=persistence.session_dir(sid),
            mcp_host=mcp_host,
            skill_host=skill_host,
            authorizer=authorizer,
        )
        self._sessions[sid] = session
        return session

    def get_session(self, session_id: str) -> Session:
        """获取 session，不存在则抛异常"""
        if session_id not in self._sessions:
            raise KeyError(f"Session {session_id} not found")
        return self._sessions[session_id]

    def destroy_session(self, session_id: str) -> None:
        """清理内存 session"""
        self._sessions.pop(session_id, None)

    def load_session_meta(self, session_id: str) -> dict[str, Any]:
        """读取 session.json"""
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")
        return session.persistence.load_session_meta(session_id)

    def save_session_meta(self, session_id: str, meta: dict[str, Any]) -> None:
        """写回 session.json"""
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")
        session.persistence.save_session_meta(session_id, meta)

    def write_turn_snapshot(
        self,
        session_id: str,
        turn_id: int,
        git_commit_hash: str,
        final_state: dict[str, Any],
        full_messages: list[Any],
    ) -> None:
        """写入 turn 快照"""
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")
        session.persistence.write_turn_snapshot(
            session_id, turn_id, git_commit_hash, final_state, full_messages
        )

    def read_turn_snapshot(
        self, session_id: str, turn_id: int
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """读取 turn 快照，返回 (graph_state, snapshot_raw)"""
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")
        return session.persistence.read_turn_snapshot(session_id, turn_id)

    def list_available_turns(self, session_id: str) -> list[int]:
        """扫描 turns 目录，返回所有合法 turn_id 列表（升序）"""
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")
        return session.persistence.list_available_turns(session_id)

    def rewind_to_turn(self, session_id: str, target_turn_id: int) -> dict[str, Any]:
        """回滚到指定 turn

        步骤：
        1. 校验 turn 存在，读取快照
        2. git reset --hard
        3. 恢复 session.json["messages"]
        4. 更新 session.json 元信息
        5. 返回 graph_state 供内存恢复
        """
        session = self.get_session(session_id)
        if session.persistence is None:
            raise RuntimeError(f"Session {session_id} has no persistence layer")

        graph_state = session.persistence.rewind_to_turn(session_id, target_turn_id)

        # 恢复内存 state
        session.current_state = self._restore_state(graph_state, session.current_state)
        session.current_turn_id = target_turn_id

        return graph_state

    def stream_session_events(
        self, session_id: str, new_user_input: Optional[str] = None
    ) -> Generator[dict[str, Any], None, None]:
        """流式驱动图执行，yield 每个事件

        流程：
        1. 校验 is_running 锁
        2. 追加用户输入到 current_state["messages"]
        3. 从持久层加载完整 messages 覆盖（确保内存与磁盘一致）
        4. 调用 compiled_graph.stream 迭代事件 yield
        5. 最终 state 回写 session.current_state
        6. 持久化：diff 新消息 → 追加 session.json → git commit → 写 turn 快照
        7. finally 重置 is_running

        持久化约束：仅在 finally_node 结束后执行，graph 内部禁止磁盘 IO。
        """
        session = self.get_session(session_id)

        if session.is_running:
            raise RuntimeError(f"Session {session_id} is already running")

        # 加载磁盘上的完整消息作为事实源
        if session.persistence is not None:
            disk_meta = session.persistence.load_session_meta(session_id)
            disk_messages = disk_meta.get("messages", [])
            if disk_messages:
                session.current_state["messages"] = disk_messages

        # 追加本轮用户输入
        if new_user_input:
            messages = list(session.current_state.get("messages", []))
            from langchain_core.messages import HumanMessage
            messages.append(HumanMessage(content=new_user_input))
            session.current_state["messages"] = messages

        # 记录 graph 启动前的完整消息（用于 diff）
        old_full_messages = list(session.current_state.get("messages", []))
        session.is_running = True

        try:
            config = {"configurable": {"thread_id": session_id}, "recursion_limit": 50}

            for event in session.compiled_graph.stream(
                session.current_state,
            ):
                yield event

            # graph 结束后，在外部层执行持久化
            if session.persistence is not None and session.workspace is not None:
                final_state = dict(session.current_state)
                turn_id = session.current_turn_id + 1
                session.current_turn_id = turn_id

                persist_turn(
                    persistence=session.persistence,
                    session_id=session_id,
                    turn_id=turn_id,
                    workspace=session.workspace,
                    old_full_messages=old_full_messages,
                    final_state=final_state,
                )

        finally:
            session.is_running = False

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _restore_state(
        self, graph_state: dict[str, Any], current: CodingAgentState
    ) -> CodingAgentState:
        """从快照 graph_state 恢复内存 state，保留类型兼容性"""
        restored = dict(current)
        for key in (
            "messages",
            "task_plan",
            "replan_count",
            "attempt_count",
            "max_attempt",
            "validate_result",
            "tool_artifacts",
            "need_human_intervene",
        ):
            if key in graph_state:
                restored[key] = graph_state[key]
        return restored  # type: ignore[return-value]


# ============================================================
# 全局 SessionManager 单例
# ============================================================

_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """获取全局 SessionManager 单例"""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager


# ============================================================
# State 初始化
# ============================================================

def build_initial_state(
    messages: Optional[list[BaseMessage]] = None,
    replan_max: int = REPLAN_THRESHOLD,
    max_attempt: int = MAX_ATTEMPT_DEFAULT,
    task: str = "",
) -> CodingAgentState:
    """构建初始 GraphState

    Args:
        messages: 初始消息列表，None 则空列表
        replan_max: replan 阈值
        max_attempt: 编码重试阈值
        task: 当前轮次原始用户需求

    Returns:
        完整初始化的 CodingAgentState 字典
    """
    return {
        "messages": list(messages or []),
        "runtime": None,
        "task_plan": {},
        "replan_count": 0,
        "attempt_count": 0,
        "max_attempt": max_attempt,
        "validate_result": {},
        "tool_artifacts": {},
        "need_human_intervene": False,
        "resume_action": "",
        "task": task,
    }
