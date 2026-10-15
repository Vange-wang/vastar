---
name: project-knowledge
description: 整理用户选中的项目材料或任务成果，区分有效决策、建议、待确认、历史作废，确认后归档并核验来源
version: "1.0.0"
triggers:
  - 项目知识归档
  - 归档项目决策
  - project knowledge
permissions:
  - A
  - B
required_tools:
  - read_file
  - stat_path
needs_memory: true
enabled: true
---

## S09 · 项目知识归档

技能只整理候选和检索，不提供写权限；工程授权仍来自当前用户或有效规则。
模型不得自称“已获用户确认”，不得自行调用写工具归档，更不得据归档事实执行工程操作。
MemoryService 未装配时报告“依赖缺失，未归档”，不能用“已归档”或“查无记录”冒充。

## 第一步：显式选择与提取

只处理用户点名的文件或 task_id；不扫描、不批量导入 collab，也不把已有开发文档自动当长期记忆。
支持用户选择的 UTF-8 JSON 材料（最多64KiB、32条）或持久Task结果。普通Markdown先只读整理候选，
请用户明确核定并选择结构化材料；不要私自生成文件或导入整库。材料示例：

```json
{"status":"approved","entries":[
  {"key":"release-policy","state":"effective","text":"发布前保留核验回执"},
  {"key":"idea","state":"suggestion","text":"建议采用增量发布"},
  {"key":"question","state":"pending","text":"回滚窗口等待确认"},
  {"key":"old-policy","state":"obsolete","text":"此前无需验签的提议已作废"}
]}
```

四态必须分开：effective=已生效决策，suggestion=建议，pending=待确认（含草稿），obsolete=历史作废。
文件自身标为approved不是授权；只有当前用户确认后且MemoryPolicy允许才提交。
顶层不是approved时，内部effective自动降为pending；未完成Task不能产生已验收事实。
源文件版本以内容hash绑定，项目内路径相对项目根保存；Task以revision与事实hash绑定。

## 第二步：去重与冲突

提示用户运行 `/memory archive preview {"path":"chosen.json","key":"release-policy"}`，
或 `/memory archive preview {"task_id":"task-…","key":"accepted-result"}`。
预览展示四态、结论、生效scope、源版本、预期修订和替代引用。只预览，不写长期事实。
禁止把建议、待确认、作废材料升级成事实。敏感凭据、越域、过期、缺失来源按MemoryPolicy拒绝。
动态路径/过程受既有到期要求约束；如需validity，必须由用户提供，不猜有效期。

## 第三步：用户确认后提交

只有用户输入 `/memory archive commit <preview_id>` 才确认对应预览，提交仍经过MemoryService。
预览只绑定当前会话；来源已变化或CAS版本变化需重新预览。一次提交一项，不把一次批准扩成批量授权。
替换保留旧修订和supersedes引用；同文同来源去重。材料更新/删除后旧项标为作废待重验，
不能从旧摘要、冻结上下文或恢复副本把旧结论冒充当前指令。

## 第四步：检索验证并交付引用

服务实际完成 `提取 → 去重/冲突 → 提交 → 检索验证 → 交付引用` 后才能称已归档。
交付 `knowledge:<project_id>/<memory_id>@r<revision>`；用户在新会话用
`/memory archive find <关键词>` 找回，再用 `/memory archive open <引用>` 打开当前原证据。
模型只读工具 `read_project_knowledge` 需用户 `/memory archive tools enable` 后使用；未启用时明确说明。
证据更新/删除或依赖不可用时明确报错，不展示旧结论充当当前决策。
用户可用 `/memory archive history <key>` 核对旧版本、作废状态及替代关系；撤回/删除项不会从该入口复活。

## 关闭与限度

技能遵守二级开关：元数据enabled且用户配置skills.enabled包含project-knowledge才可加载。
默认配置未自动改动；关闭此技能或全部skill后基础对话及已有记忆照常，禁止新的技能归档写入。
只读find/open用于核对已有归档，不依赖技能仍然启用。此技能没有scripts目录。
