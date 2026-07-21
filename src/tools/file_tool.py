from pathlib import Path
from core import RuntimeState

MAX_READ_LINES = 2000
TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gbk")


# 获取文件的绝对路径 （相比于工作区的）
def resolve_workspace_path(state: RuntimeState, file_path: str) -> Path:
    raw = Path(_strip_workspace_prefix(file_path))
    if not raw.is_absolute():
        raw = state.workspace / raw
    return state.assert_workspace_path(raw)


# 移除文件路径中的工作区目录前缀，将绝对路径或带前缀的路径转换为简洁的相对路径。
# 将类似 /workspace/src/main.py 或 workspace/data/file.txt 这样的路径转换为 src/main.py 或 data/file.txt，即去掉工作区根目录的标识。
def _strip_workspace_prefix(file_path: str) -> str:
    # 将 Windows 反斜杠 \ 替换为 Unix 风格的正斜杠 / ， 去除首尾空白字符
    normalized = file_path.replace("\\", "/").strip()

    # 匹配：匹配类型	示例
    # 正好是 "workspace"	workspace
    # 正好是 "./workspace"	./workspace
    # 以 "workspace/" 开头	workspace/src/main.py
    # 以 "./workspace/" 开头	./workspace/data/file.txt
    while normalized in {"workspace", "./workspace"} or normalized.startswith(("workspace/", "./workspace/")):
        if normalized in {"workspace", "./workspace"}:
            normalized = "."
        elif normalized.startswith("./workspace/"):
            normalized = normalized[len("./workspace/"):]
        else:
            normalized = normalized[len("workspace/"):]
    return normalized


# 以"尽力而为"的方式读取文本文件，即使遇到编码错误也不会崩溃，而是尝试多种编码方案，最后用"替换"策略处理无法解码的字符。
def read_text_lossy(path: Path) -> str:
    last_error: UnicodeDecodeError | None = None
    for encoding in TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as e:
            last_error = e

    if last_error is not None:
        return path.read_text(encoding="utf-8", errors="replace")
    return path.read_text(encoding="utf-8")
