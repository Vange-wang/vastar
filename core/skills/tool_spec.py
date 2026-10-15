"""use_skill：模型自主触发通道（V3③；D7——走 tool 底座同一闸门，不另开旁路）。

模型在系统提示的常驻清单里看到 name+description（D5），需要时调用本工具；
技能正文作为工具结果回填（与读文件同一回灌路径，token 走既有计量）。
权限 A：只注入文字、不碰系统（不执行脚本——scripts/ 在发现期已显式拒绝，D9）。
"""

from __future__ import annotations

from core.tools.registry import ToolSpec

USE_SKILL_TOOL = "use_skill"


def build_use_skill_spec(skills) -> ToolSpec:
    """模型通道工具 spec（run 闭包持有 SkillsContext；注册≠启用，开关在 tools.enabled）。"""

    def run(args, *, resolved_path=None, resolved_extra=None, project_root=None,
            skip=None, op_context=None):
        return skills.use_skill(str(args.get("name", "")).strip())

    return ToolSpec(
        name=USE_SKILL_TOOL,
        description=("加载指定技能的完整正文（可用技能见系统提示中的清单，含名称与用途说明）。"
                     "仅当用户任务明显匹配某技能用途时调用；每轮可加载数量有限，"
                     "被拒绝时请直接用已有信息回答。"),
        parameters={"type": "object",
                    "properties": {"name": {"type": "string",
                                            "description": "技能名（见可用技能清单）"}},
                    "required": ["name"]},
        level="A", run=run,
        effect="read", resource_scope="file", retryable=True,
        cancel_phase="any", reconcile="none", preserve_full_result=True)
