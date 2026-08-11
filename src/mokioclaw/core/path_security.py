"""
路径安全控制模块

实现路径白名单/黑名单机制，防止 Agent 访问不安全的目录。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mokioclaw.core.state import RuntimeState


# ========== 路径安全配置 ==========

# 默认黑名单（禁止访问）
DEFAULT_BLACKLISTED_DIRS = {
    ".git", ".gitignore", ".gitmodules",
    ".venv", "venv", "env", ".env",
    "node_modules", "__pycache__", ".pytest_cache",
    ".idea", ".vscode", ".vs",
    "dist", "build", ".build",
    ".mokioclaw",  # 保护自己的元数据目录
}

# 默认白名单（允许写入）
DEFAULT_ALLOWED_WRITE_DIRS = {
    "src", "source",
    "tests", "test", "__tests__",
    "docs", "documentation",
    "examples", "samples",
    "scripts",
    "app", "apps",
    "lib", "libs",
}

# 总是允许访问的文件（无论在哪个目录）
ALWAYS_ALLOWED_FILES = {
    "README.md", "README.rst", "README.txt",
    "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "tsconfig.json",
    "Makefile", "Dockerfile", "docker-compose.yml",
    ".env.example", ".env.template",
    "requirements.txt", "requirements-dev.txt",
    "Pipfile", "poetry.lock",
}

# 敏感文件模式（即使存在也不应该被修改）
SENSITIVE_FILE_PATTERNS = [
    r".*\.env$",           # 环境变量文件
    r".*\.pem$",           # 私钥文件
    r".*\.key$",           # 密钥文件
    r"id_rsa.*",           # SSH 私钥
    r".*\.p12$",           # PKCS#12 证书
    r".*\.pfx$",           # PFX 证书
    r"credentials\.json$", # 凭据文件
    r"\.secrets.*",        # 秘密文件
]


class PathSecurityError(Exception):
    """路径安全异常"""
    pass


class PathAccessDeniedError(PathSecurityError):
    """路径访问被拒绝"""
    pass


class PathTraversalError(PathSecurityError):
    """路径遍历攻击检测"""
    pass


def validate_path_access(
    state: "RuntimeState",
    path: Path,
    operation: str = "read",
    *,
    allow_write_outside_workspace: bool = False,
) -> Path:
    """验证路径访问权限

    Args:
        state: 运行时状态
        path: 要访问的路径（绝对路径）
        operation: 操作类型（"read" / "write" / "delete"）
        allow_write_outside_workspace: 是否允许写入工作区外（用于特殊场景）

    Returns:
        验证后的路径

    Raises:
        PathTraversalError: 路径遍历攻击
        PathAccessDeniedError: 访问被拒绝
        ValueError: 参数错误
    """
    # 1. 基本工作区检查
    resolved = path.resolve()
    workspace = state.workspace.resolve()

    # 检查是否在工作区内
    if resolved != workspace and workspace not in resolved.parents:
        raise PathTraversalError(
            f"Path traversal attempt: {path} is outside workspace {workspace}"
        )

    # 2. 黑名单检查
    _check_blacklisted(resolved, workspace, operation)

    # 3. 写操作额外检查
    if operation in ("write", "delete"):
        _check_write_permission(resolved, workspace)

    return resolved


def _check_blacklisted(path: Path, workspace: Path, operation: str) -> None:
    """检查路径是否在黑名单中"""
    try:
        rel_path = path.relative_to(workspace)
    except ValueError:
        # 不在工作区内，前面已经检查过
        return

    parts = set(rel_path.parts)
    path_str = str(rel_path).lower()

    # 检查黑名单目录
    blacklisted = DEFAULT_BLACKLISTED_DIRS & parts
    if blacklisted:
        raise PathAccessDeniedError(
            f"Access denied: {operation} operation on blacklisted directory: {blacklisted.pop()}"
        )

    # 检查敏感文件（使用 search 而不是 match，以匹配子目录中的文件）
    for pattern in SENSITIVE_FILE_PATTERNS:
        if re.search(pattern, path_str, re.IGNORECASE):
            raise PathAccessDeniedError(
                f"Access denied: {operation} operation on sensitive file matching {pattern}"
            )


def _check_write_permission(path: Path, workspace: Path) -> None:
    """检查写权限"""
    try:
        rel_path = path.relative_to(workspace)
    except ValueError:
        return

    # 检查是否在白名单目录
    parts = set(rel_path.parts)
    in_whitelist = bool(DEFAULT_ALLOWED_WRITE_DIRS & parts)

    # 如果不在白名单，检查是否是根目录下的文件（如 README.md）
    if not in_whitelist and len(rel_path.parts) > 1:
        # 检查第一级目录是否在白名单
        top_dir = rel_path.parts[0]
        if top_dir not in DEFAULT_ALLOWED_WRITE_DIRS and top_dir not in ALWAYS_ALLOWED_FILES:
            raise PathAccessDeniedError(
                f"Write access denied: {rel_path} is not in an allowed directory. "
                f"Allowed directories: {', '.join(sorted(DEFAULT_ALLOWED_WRITE_DIRS))}"
            )


def is_path_safe_for_read(path: Path, workspace: Path) -> bool:
    """快速检查路径是否可读（不抛出异常）"""
    try:
        validate_path_access(RuntimeState(workspace=workspace), path, "read")
        return True
    except PathSecurityError:
        return False


def is_path_safe_for_write(path: Path, workspace: Path) -> bool:
    """快速检查路径是否可写（不抛出异常）"""
    try:
        validate_path_access(RuntimeState(workspace=workspace), path, "write")
        return True
    except PathSecurityError:
        return False


def get_security_config() -> dict[str, Any]:
    """获取当前安全配置"""
    return {
        "blacklisted_dirs": sorted(DEFAULT_BLACKLISTED_DIRS),
        "allowed_write_dirs": sorted(DEFAULT_ALLOWED_WRITE_DIRS),
        "always_allowed_files": sorted(ALWAYS_ALLOWED_FILES),
        "sensitive_patterns": SENSITIVE_FILE_PATTERNS,
    }
