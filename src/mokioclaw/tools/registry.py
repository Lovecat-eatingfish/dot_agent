from __future__ import annotations

from langchain_core.tools import StructuredTool

from mokioclaw.core.state import RuntimeState
from mokioclaw.tools.bash_tool import bash_tool_description, run_bash
from mokioclaw.tools.file_tools import edit_file, read_file, write_file
from mokioclaw.tools.grep_tool import grep
from mokioclaw.tools.notepad_tool import append_notepad, read_notepad
from mokioclaw.tools.web_search_tool import build_web_search_tool


def build_tools(state: RuntimeState) -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            name="FileReadTool",
            func=lambda file_path, offset=0, limit=2000: read_file(state, file_path, offset, limit),
            description="Read a UTF-8 text file inside the workspace. Supports offset and limit.",
        ),
        StructuredTool.from_function(
            name="FileWriteTool",
            func=lambda file_path, content: write_file(state, file_path, content),
            description="Create a new file or rewrite an existing file inside the workspace.",
        ),
        StructuredTool.from_function(
            name="FileEditTool",
            func=lambda file_path, old_text, new_text: edit_file(state, file_path, old_text, new_text),
            description="Edit an existing workspace file by replacing one unique old_text snippet.",
        ),
        StructuredTool.from_function(
            name="GrepTool",
            func=lambda pattern, path=".", glob=None, head_limit=50, ignore_case=False: grep(
                state, pattern, path, glob, head_limit, ignore_case
            ),
            description="Search workspace text files by regex pattern and return matching lines.",
        ),
        StructuredTool.from_function(
            name="BashTool",
            func=lambda command, timeout_seconds=None, run_in_background=False: run_bash(
                state, command, timeout_seconds, run_in_background
            ),
            description=bash_tool_description(),
        ),
        StructuredTool.from_function(
            name="NotepadReadTool",
            func=lambda: read_notepad(state),
            description="Read the durable workspace notepad from NOTEPAD.md.",
        ),
        StructuredTool.from_function(
            name="NotepadAppendTool",
            func=lambda heading, content: append_notepad(state, heading, content),
            description="Append a durable markdown note to NOTEPAD.md. Args: heading, content.",
        ),
    ]


def build_read_only_tools(state: RuntimeState) -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            name="FileReadTool",
            func=lambda file_path, offset=0, limit=2000: read_file(state, file_path, offset, limit),
            description="Read a UTF-8 text file inside the workspace. Supports offset and limit.",
        ),
        StructuredTool.from_function(
            name="GrepTool",
            func=lambda pattern, path=".", glob=None, head_limit=50, ignore_case=False: grep(
                state, pattern, path, glob, head_limit, ignore_case
            ),
            description="Search workspace text files by regex pattern and return matching lines.",
        ),
        StructuredTool.from_function(
            name="BashTool",
            func=lambda command, timeout_seconds=None, run_in_background=False: run_bash(
                state, command, timeout_seconds, run_in_background
            ),
            description=bash_tool_description(),
        ),
        StructuredTool.from_function(
            name="NotepadReadTool",
            func=lambda: read_notepad(state),
            description="Read the durable workspace notepad from NOTEPAD.md.",
        ),
        build_web_search_tool(),
    ]
