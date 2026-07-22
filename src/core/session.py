from pathlib import Path

SESSION_ROOT = Path(".mokioclaw") / "session"  # 会话存储根目录
SESSION_FILE = "session.json"  # 会话结构化数据文件
SESSION_SUMMARY_FILE = "SESSION_SUMMARY.md"  # 人可读会话摘要
MAX_RECENT_TURNS = 18  # 内存只保留最近18轮完整对话
MAX_TURN_CONTENT = 1800  # 单轮消息最大字符
MAX_SESSION_SUMMARY = 5000  # 全局压缩摘要上限
MAX_SESSION_CONTEXT = 7000  # 拼接给LLM的会话总上下文上限


# 这是会话持久化管理模块，专门管理单次完整对话会话：存储用户 / AI 交互轮次、会话元数据、生成会话摘要文件、压缩历史对话防上下文超限、提供会话生命周期事件，落地文件存放在 .mokioclaw/session/，和前面的 State、分层记忆、Checkpoint 快照体系配套，负责会话级上下文持久化。
def session_dir(workspace: Path) -> Path:
    return workspace / SESSION_ROOT


def session_file(workspace: Path) -> Path:
    return session_dir(workspace) / SESSION_FILE


def session_summary_file(workspace: Path) -> Path:
    return workspace / SESSION_SUMMARY_FILE
