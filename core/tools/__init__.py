"""tool 能力包（TASK-15，Spec v1.4 §3/§5/§10）：注册表 / 权限闸门 / 执行器 / 审计。

依赖方向（Spec §5 写死）：executor **必须先调 permissions 判定**才允许调用工具实现；
工具实现文件内不得出现权限级别判定（防双重实现与旁路）；读取范围口径 = R20 整机只读
+ 凭据类黑名单（.env 例外见 R25 补充⑫）+ 写操作三值判定（R25），全部由执行层统一持有；
max_bytes/depth/limit 等工具私有参数校验留在工具内。
"""

from __future__ import annotations

from core.tools.registry import ToolParamError, ToolRegistry, ToolsContext, ToolSpec

__all__ = ["ToolParamError", "ToolRegistry", "ToolSpec", "ToolsContext"]
