from core import RuntimeState
from tools.file_tool import read_text_lossy
from typing import Any
from datetime import datetime

# 关键事件记录的文件名， 记录agent 运行的关键数据
NOTEPAD_FILE = "NOTEPAD.md"




def read_notepad(state: RuntimeState) -> dict[str, Any]:
    path = state.assert_workspace_path(state.workspace / NOTEPAD_FILE)
    if not path.exists():
        return {"ok": True, "path": NOTEPAD_FILE, "content": "", "exists": False}
    content = read_text_lossy(path)
    state.record_read(path, complete=True)
    return {"ok": True, "path": NOTEPAD_FILE, "content": content, "exists": True}


def append_notepad(state: RuntimeState, heading: str, content: str) -> dict[str, Any]:
    if not content.strip():
        return {"ok": False, "error": "content must not be empty"}
    path = state.assert_workspace_path(state.workspace / NOTEPAD_FILE)

    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_text_lossy(path) if path.exists() else "# MokioClaw Notepad\n"
    title = heading.strip() or "Note"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n## {title}\n\n_Recorded: {timestamp}_\n\n{content.strip()}\n"
    # existing.rstrip()：移除 existing 末尾的所有空白字符（包括换行符 \n、空格等）
    updated = existing.rstrip() + "\n" + entry
    path.write_text(updated, encoding="utf-8")
    state.record_read(path, complete=True)
    return {"ok": True, "path": NOTEPAD_FILE, "heading": title, "lines": len(updated.splitlines())}
