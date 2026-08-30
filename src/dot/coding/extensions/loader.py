"""
dot.coding.extensions.loader — ExtensionLoader

扫描 ~/.dot/extensions/ 和 .dot/extensions/ 目录，发现扩展模块。
每个扩展文件必须定义 setup(ext: ExtensionAPI) 入口函数。
"""
from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

USER_EXTENSIONS_DIR = Path.home() / ".dot" / "extensions"
PROJECT_EXTENSIONS_DIR = Path(".dot") / "extensions"


@dataclass
class ExtensionModule:
    """一个已发现的扩展模块"""
    name: str
    path: Path
    setup_fn: Callable[..., Any] | None = None
    teardown_fn: Callable[..., Any] | None = None  # 可选：teardown(ctx)
    load_error: str | None = None

    @property
    def is_loadable(self) -> bool:
        """是否可加载： 判断是否有 setup 函数且没有加载错误"""
        return self.setup_fn is not None and self.load_error is None


class ExtensionLoader:
    """扫描并发现扩展模块"""

    def __init__(
        self,
        *,
        extra_dirs: list[Path] | None = None,
    ) -> None:
        self._extra_dirs = extra_dirs or []
        self._modules: dict[str, ExtensionModule] = {}

    @property
    def modules(self) -> dict[str, ExtensionModule]:
        return dict(self._modules)

    def scan(self) -> list[ExtensionModule]:
        """扫描所有扩展目录，发现扩展模块"""
        self._modules.clear()

        dirs = [USER_EXTENSIONS_DIR, PROJECT_EXTENSIONS_DIR] + self._extra_dirs
        for directory in dirs:
            if not directory.is_dir():
                continue
            self._scan_directory(directory)

        logger.info(
            "[extensions] Scanned %d extensions (%d loadable)",
            len(self._modules),
            sum(1 for m in self._modules.values() if m.is_loadable),
        )
        return list(self._modules.values())

    def _scan_directory(self, directory: Path) -> None:
        for path in directory.iterdir():
            if path.suffix != ".py" or path.name.startswith("_"):
                continue
            name = path.stem
            if name in self._modules:
                continue
            # key: 模块name value: ExtensionModule
            self._modules[name] = self._load_module_metadata(name, path)

    @staticmethod
    def _load_module_metadata(name: str, path: Path) -> ExtensionModule:
        """加载扩展模块元数据: ExtensionModule"""
        try:
            spec = importlib.util.spec_from_file_location(f"dot_ext_{name}", path)
            if spec is None or spec.loader is None:
                return ExtensionModule(name=name, path=path, load_error="Failed to create module spec")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            setup_fn = getattr(module, "setup", None)
            if not callable(setup_fn):
                return ExtensionModule(name=name, path=path, load_error="No setup() function found")
            teardown_fn = getattr(module, "teardown", None)
            teardown_fn = teardown_fn if callable(teardown_fn) else None
            return ExtensionModule(name=name, path=path, setup_fn=setup_fn, teardown_fn=teardown_fn)
        except Exception as exc:
            logger.warning("[extensions] Failed to load %s: %s", name, exc)
            return ExtensionModule(name=name, path=path, load_error=str(exc))
