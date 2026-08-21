"""
Skill 渐进披露 Host

对标 Claude Code Skill 机制：目录常驻 system prompt，完整本体延迟加载。
Skill 是纯文本指令集，无外部进程 RPC，不走 tools 调用执行业务逻辑。

核心概念：
- 初始化时加载全部 Skill 元数据到内存，full_content 也全量缓存
- 构造 Skill 目录文本（仅 name + description）注入 system prompt
- 仅注册 1 个元工具 invoke_skill 进入 LLM tools 数组
- LLM 调用 invoke_skill 后，Host 把 full_content 追加到 system prompt 上下文
"""
from __future__ import annotations

from typing import Any, Optional


# ============================================================
# Skill Host
# ============================================================

class SkillHost:
    """Skill 渐进披露 Host

    管理 Skill 发现、缓存、目录生成和延迟加载。
    """

    def __init__(self, skills_manager: Any) -> None:
        """初始化 Skill Host

        Args:
            skills_manager: SkillsManager 实例，用于发现和加载 Skills
        """
        self._skills_manager = skills_manager
        # skill_name → Skill 对象（含 full_content）
        self._skill_cache: dict[str, Any] = {}
        # 已加载到上下文的 skill_name 集合
        self._loaded_skills: set[str] = set()
        # 目录纯文本（system prompt 注入用）
        self._catalog_text: str = ""
        # 当前 system prompt 附加内容（已加载 skill 的 full_content 拼接）
        self._active_content: str = ""

    def discover_skills(self) -> list[str]:
        """发现并缓存全部 Skill

        Returns:
            skill_name 列表
        """
        self._skill_cache.clear()
        self._loaded_skills.clear()
        self._active_content = ""

        # 从 SkillsManager 获取全部 Skills
        try:
            skills = self._skills_manager.get_all_skills()
        except Exception:
            skills = []

        for skill in skills:
            name = getattr(skill, "name", "") or ""
            if not name:
                continue
            self._skill_cache[name] = skill

        self._rebuild_catalog()
        return list(self._skill_cache.keys())

    def get_loaded_skills(self) -> list[str]:
        """获取已加载的 skill 名称列表"""
        return sorted(self._loaded_skills)

    def get_all_skill_names(self) -> list[str]:
        """获取所有可用 skill 名称列表"""
        return sorted(self._skill_cache.keys())

    def get_catalog_text(self) -> str:
        """获取 Skill 目录纯文本（注入 system prompt）"""
        return self._catalog_text

    def get_active_content(self) -> str:
        """获取已加载 Skill 的完整内容拼接（追加到 system prompt）"""
        return self._active_content

    def invoke_skill(self, skill_name: str) -> dict[str, Any]:
        """加载指定 Skill 完整内容，追加到系统上下文

        Args:
            skill_name: Skill 名称

        Returns:
            执行结果

        Raises:
            ValueError: Skill 不存在
        """
        if skill_name not in self._skill_cache:
            return {
                "ok": False,
                "error": f"skill {skill_name} 不存在，请查看 skill 目录",
            }

        skill = self._skill_cache[skill_name]
        full_content = getattr(skill, "_raw", {}).get("_body", "") or ""
        if not full_content:
            # 尝试从 path 读取
            try:
                full_content = skill.path.read_text(encoding="utf-8")
            except Exception:
                full_content = f"# {skill.name}\n{skill.description}"

        self._loaded_skills.add(skill_name)
        self._active_content += f"\n\n--- Skill: {skill_name} ---\n{full_content}\n--- End of {skill_name} ---\n"

        return {
            "ok": True,
            "skill_name": skill_name,
            "message": f"Skill {skill_name} 已加载，完整指令已注入系统上下文。",
        }

    def get_meta_tool(self) -> dict[str, Any]:
        """获取元工具定义（仅注册到 LLM tools 数组的 1 个工具）

        Returns:
            元工具 schema
        """
        return {
            "name": "invoke_skill",
            "description": "加载指定 Skill 的完整指令文本，合并到系统上下文。参数 skill_name 必须是目录中列出的 skill 名称。",
            "input_schema": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "要加载的 Skill 名称，如 frontend_builder",
                    }
                },
                "required": ["skill_name"],
            },
        }

    def get_system_prompt_rules(self) -> str:
        """获取 Skill 使用规则文本（注入 system prompt）"""
        return """Skill 使用规则
Skill 是预定义的指令模板，完整内容不会默认加载到上下文，不能直接生效。想要使用某一个 Skill，必须调用元工具 invoke_skill (skill_name="your_skill_name")。invoke_skill 执行成功之后，该 Skill 的全部完整指令会追加进系统上下文，之后你才可以按照 Skill 内的规则执行任务。禁止在未调用 invoke_skill 的前提下，假设拥有该 Skill 的能力。
元工具：
- invoke_skill (skill_name): 加载指定 Skill 完整指令文本，合并到 system prompt
"""

    # ----------------------------------------------------------
    # Internal helpers
    # ----------------------------------------------------------

    def _rebuild_catalog(self) -> None:
        """重建 Skill 目录纯文本"""
        lines = ["可用 Skills 目录：", ""]
        for name in sorted(self._skill_cache.keys()):
            skill = self._skill_cache[name]
            desc = getattr(skill, "description", "") or "无描述"
            if len(desc) > 80:
                desc = desc[:77] + "..."
            loaded_mark = " [已加载]" if name in self._loaded_skills else ""
            lines.append(f"- {name}: {desc}{loaded_mark}")
        lines.append("")
        lines.append(self.get_system_prompt_rules())
        self._catalog_text = "\n".join(lines)
