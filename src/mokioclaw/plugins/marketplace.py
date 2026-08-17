"""
本地 Plugin Marketplace（对齐 Claude Code 插件扩展面）

布局：
- 目录（catalog）：包内 builtin + ~/.mokioclaw/marketplace/catalog.json
- 安装位置：~/.mokioclaw/plugins/<name>/ 或 <workspace>/.mokioclaw/plugins/<name>/
- 启用状态：~/.mokioclaw/plugins.json / <workspace>/.mokioclaw/plugins.json

plugin.json：
{
  "name": "code-review-kit",
  "version": "0.1.0",
  "description": "...",
  "skills": true,
  "commands": true,
  "hooks": true
}
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mokioclaw.core.log import get_logger

logger = get_logger(__name__)

_GLOBAL_ROOT = Path.home() / ".mokioclaw"
_BUILTIN_ROOT = Path(__file__).resolve().parent / "builtin"

# 合法插件名：字母/数字/点/下划线/连字符，禁止路径分隔符与 ..
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_plugin_name(name: str) -> str | None:
    """校验插件名为安全标识符，杜绝路径遍历（../、绝对路径、空段）"""
    name = (name or "").strip()
    if not name or not _PLUGIN_NAME_RE.match(name):
        return None
    return name


def _safe_child(root: Path, name: str) -> Path | None:
    """返回 root/name 并保证解析后仍在 root 之内，否则 None"""
    child = (root / name)
    try:
        resolved = child.resolve()
        if resolved == root.resolve() or root.resolve() in resolved.parents:
            return resolved
    except OSError:
        pass
    return None


@dataclass
class PluginInfo:
    name: str
    version: str = "0.0.0"
    description: str = ""
    source: str = ""  # builtin | path | installed
    path: Path | None = None
    enabled: bool = False
    installed: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


def list_catalog(workspace: Path | None = None) -> list[PluginInfo]:
    """合并 builtin + 用户 marketplace catalog + 已安装插件"""
    by_name: dict[str, PluginInfo] = {}

    for info in _scan_builtin():
        by_name[info.name] = info

    for catalog_path, trusted in (
        (_GLOBAL_ROOT / "marketplace" / "catalog.json", True),
        *( [(workspace / ".mokioclaw" / "marketplace" / "catalog.json", False)] if workspace else [] ),
    ):
        for entry in _read_catalog_file(catalog_path):
            entry_path = Path(entry["path"]).expanduser() if entry.get("path") else None
            source = str(entry.get("source") or "")
            if source.startswith("path:"):
                entry_path = Path(source.split(":", 1)[1]).expanduser()
            # 不受信 catalog（随仓库分发）不允许把安装/加载指向任意目录：
            # 恶意仓库可借此把 ~/.ssh 拷进项目、或让其 markdown 进入 agent 提示词
            if not trusted and not _is_allowed_plugin_source(entry_path, workspace):
                logger.warning(
                    "marketplace catalog entry '%s' points outside workspace (%s), ignored",
                    entry.get("name", "?"), entry_path,
                )
                continue
            info = PluginInfo(
                name=str(entry.get("name", "")),
                version=str(entry.get("version", "0.0.0")),
                description=str(entry.get("description", "")),
                source=source or "catalog",
                path=entry_path,
                meta=entry,
            )
            if info.name:
                by_name[info.name] = info

    enabled = _load_enabled(workspace)
    for info in list_installed(workspace):
        prev = by_name.get(info.name)
        if prev is None:
            by_name[info.name] = info
        else:
            prev.installed = True
            prev.path = info.path
            prev.version = info.version or prev.version
            prev.description = info.description or prev.description
            prev.source = info.source

    for name, info in by_name.items():
        info.enabled = name in enabled
        if info.path and info.path.exists():
            info.installed = True
    return sorted(by_name.values(), key=lambda p: p.name)


def list_installed(workspace: Path | None = None) -> list[PluginInfo]:
    found: list[PluginInfo] = []
    roots = [_GLOBAL_ROOT / "plugins"]
    if workspace is not None:
        roots.append(workspace / ".mokioclaw" / "plugins")
    enabled = _load_enabled(workspace)
    for root in roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            meta = _read_plugin_json(child)
            name = str(meta.get("name") or child.name)
            found.append(
                PluginInfo(
                    name=name,
                    version=str(meta.get("version", "0.0.0")),
                    description=str(meta.get("description", "")),
                    source="installed",
                    path=child,
                    installed=True,
                    enabled=name in enabled,
                    meta=meta,
                )
            )
    return found


def install_plugin(
    name: str,
    *,
    workspace: Path | None = None,
    scope: str = "user",
) -> dict[str, Any]:
    """从 catalog / builtin 安装到 user 或 project plugins 目录"""
    name = _validate_plugin_name(name) or ""
    if not name:
        return {"ok": False, "error": "invalid plugin name"}

    catalog = {p.name: p for p in list_catalog(workspace)}
    info = catalog.get(name)
    if info is None:
        return {"ok": False, "error": f"unknown plugin: {name}", "available": sorted(catalog.keys())}

    source_dir = _resolve_source_dir(info, workspace)
    if source_dir is None or not source_dir.exists():
        return {"ok": False, "error": f"plugin source not found for '{name}'"}

    dest_root = _install_root(workspace, scope=scope)
    dest = _safe_child(dest_root, name)
    if dest is None:
        return {"ok": False, "error": f"invalid plugin name: {name}"}
    dest_root.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source_dir, dest)

    # 默认安装后启用
    enabled = _load_enabled(workspace)
    enabled.add(name)
    _save_enabled(enabled, workspace if scope == "project" else None)

    return {
        "ok": True,
        "name": name,
        "path": str(dest),
        "enabled": True,
        "scope": scope,
    }


def enable_plugin(name: str, *, workspace: Path | None = None) -> dict[str, Any]:
    installed = {p.name: p for p in list_installed(workspace)}
    if name not in installed:
        # catalog / builtin 可自动安装后启用
        inst = install_plugin(name, workspace=workspace, scope="user")
        if not inst.get("ok"):
            return inst
        return {"ok": True, "name": name, "enabled": True, "installed": True}
    # 写到插件所在 scope 的 state 文件
    scope_ws = _scope_workspace_for_plugin(name, workspace)
    enabled = _read_enabled_file(_enabled_state_path(scope_ws))
    enabled.add(name)
    _save_enabled(enabled, scope_ws)
    return {"ok": True, "name": name, "enabled": True}


def disable_plugin(name: str, *, workspace: Path | None = None) -> dict[str, Any]:
    # 从全局与项目 state 同时移除，避免 merge 后仍显示 enabled
    scopes: list[Path | None] = [None]
    if workspace is not None:
        scopes.append(workspace)
    for scope_ws in scopes:
        names = _read_enabled_file(_enabled_state_path(scope_ws))
        if name in names:
            names.remove(name)
            _save_enabled(names, scope_ws)
    return {"ok": True, "name": name, "enabled": False}


def uninstall_plugin(name: str, *, workspace: Path | None = None) -> dict[str, Any]:
    """从全局 / 项目 plugins 目录移除插件目录，并清掉 enabled 标记

    只删由 install_plugin 安装的副本，不动 builtin / 用户手写的源目录。
    """
    name = _validate_plugin_name(name) or ""
    if not name:
        return {"ok": False, "error": "invalid plugin name"}

    removed: list[str] = []
    roots: list[Path] = [_GLOBAL_ROOT / "plugins"]
    if workspace is not None:
        roots.append(workspace / ".mokioclaw" / "plugins")

    failed = False
    for root in roots:
        dest = _safe_child(root, name)
        if dest is None or not dest.exists():
            continue
        try:
            shutil.rmtree(dest)
            removed.append(str(dest))
        except OSError as exc:
            logger.warning("uninstall rmtree failed %s: %s", dest, exc)
            failed = True

    # 无论目录删除是否成功，都清理 enabled 标记，避免残留指向已残缺目录
    disable_plugin(name, workspace=workspace)

    if not removed and not failed:
        return {"ok": False, "error": f"plugin '{name}' not installed", "name": name}
    if failed and not removed:
        return {"ok": False, "error": "failed to remove plugin directory", "name": name}
    return {"ok": True, "name": name, "removed": removed}


def enabled_plugin_paths(workspace: Path | None = None) -> list[Path]:
    """返回已启用且已安装的插件根目录"""
    enabled = _load_enabled(workspace)
    paths: list[Path] = []
    for info in list_installed(workspace):
        if info.name in enabled and info.path and info.path.exists():
            paths.append(info.path)
    return paths


def _scan_builtin() -> list[PluginInfo]:
    items: list[PluginInfo] = []
    if not _BUILTIN_ROOT.exists():
        return items
    for child in sorted(_BUILTIN_ROOT.iterdir()):
        if not child.is_dir():
            continue
        meta = _read_plugin_json(child)
        name = str(meta.get("name") or child.name)
        items.append(
            PluginInfo(
                name=name,
                version=str(meta.get("version", "0.1.0")),
                description=str(meta.get("description", "")),
                source="builtin",
                path=child,
                meta=meta,
            )
        )
    return items


def _resolve_source_dir(info: PluginInfo, workspace: Path | None = None) -> Path | None:
    if info.path and info.path.exists() and _is_allowed_plugin_source(info.path, workspace):
        return info.path
    builtin = _BUILTIN_ROOT / info.name
    if builtin.exists():
        return builtin
    source = str(info.meta.get("source") or "")
    if source.startswith("builtin:"):
        return _BUILTIN_ROOT / source.split(":", 1)[1]
    if source.startswith("path:"):
        candidate = Path(source.split(":", 1)[1]).expanduser()
        if not _is_allowed_plugin_source(candidate, workspace):
            logger.warning("plugin source path outside allowed roots, ignored: %s", candidate)
            return None
        return candidate
    return None


def _is_allowed_plugin_source(candidate: Path | None, workspace: Path | None) -> bool:
    """插件源目录边界：只允许 workspace 内 / 全局 plugins / builtin 目录

    全局 catalog（用户手写）的路径视为受信（history 上用户可能装在任意位置），
    该函数只用于拦截 workspace 级不受信 catalog 与 path: source。
    """
    if candidate is None:
        return True  # 无路径字段（纯 builtin 条目）
    resolved = candidate.resolve()
    allowed_roots = [(_GLOBAL_ROOT / "plugins").resolve(), _BUILTIN_ROOT.resolve()]
    if workspace is not None:
        allowed_roots.append(workspace.resolve())
    return any(resolved == root or root in resolved.parents for root in allowed_roots)


def _install_root(workspace: Path | None, *, scope: str) -> Path:
    if scope == "project" and workspace is not None:
        return workspace / ".mokioclaw" / "plugins"
    return _GLOBAL_ROOT / "plugins"


def _load_enabled(workspace: Path | None) -> set[str]:
    names: set[str] = set()
    for path in (_GLOBAL_ROOT / "plugins.json",):
        names.update(_read_enabled_file(path))
    if workspace is not None:
        names.update(_read_enabled_file(workspace / ".mokioclaw" / "plugins.json"))
    return names


def _enabled_state_path(workspace: Path | None) -> Path:
    if workspace is not None:
        return workspace / ".mokioclaw" / "plugins.json"
    return _GLOBAL_ROOT / "plugins.json"


def _scope_workspace_for_plugin(name: str, workspace: Path | None) -> Path | None:
    """若插件装在项目目录则返回 workspace，否则返回 None（用户全局）"""
    if workspace is not None:
        project_dir = workspace / ".mokioclaw" / "plugins" / name
        if project_dir.exists():
            return workspace
    return None


def _save_enabled(names: set[str], workspace: Path | None) -> None:
    path = _enabled_state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"enabled": sorted(names)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_enabled_file(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(data, dict):
        raw = data.get("enabled") or []
    elif isinstance(data, list):
        raw = data
    else:
        return set()
    return {str(x) for x in raw if str(x).strip()}


def _read_catalog_file(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("catalog read failed %s: %s", path, exc)
        return []
    if isinstance(data, dict):
        plugins = data.get("plugins") or []
    elif isinstance(data, list):
        plugins = data
    else:
        return []
    return [p for p in plugins if isinstance(p, dict)]


def _read_plugin_json(plugin_dir: Path) -> dict[str, Any]:
    path = plugin_dir / "plugin.json"
    if not path.exists():
        return {"name": plugin_dir.name}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"name": plugin_dir.name}
    except (OSError, json.JSONDecodeError):
        return {"name": plugin_dir.name}
