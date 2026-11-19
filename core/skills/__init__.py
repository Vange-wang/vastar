"""文件式 Skill 加载底座（TASK-2026-10-02-46 / B11；skills-decisions v1.2 D1–D12）。

子模块分工（D10：发现/注册/选择/注入/审计各司其职）：
- frontmatter：SKILL.md 极简 YAML frontmatter 解析（项目内实现，零第三方依赖）；
- registry：启动时递归一层发现 + 校验（失败 = 显式不可用，禁静默）；
- activation：三通道触发与 max_active 超额处置（显式 > 关键词 > 模型）；
- context：SkillsContext 装配与运行期桥（清单常驻 / 正文选中才注入）；
- tool_spec：use_skill 模型通道工具（走 tool 底座同一闸门，D7）；
- session_hook：/skill 显式通道 Mixin（SessionCoordinator 外挂，不进类体）。
"""
