---
name: skill-maintenance
description: 从已验收任务和获授权经验准备版本化技能候选，组织独立验证，交用户发布或兼容回退
version: "1.0.0"
triggers:
  - 技能维护
permissions:
  - A
  - B
required_tools:
  - read_file
  - read_artifact
needs_memory: true
enabled: true
---

S10 不因激活而获得写入、发布或执行权限。先读取已验收任务的真实证据和有效程序性经验；
经验须经既有 /remember 与 MemoryPolicy 获授权，procedure 必须有有效期。
不将聊天、模型建议、失败任务当作可靠经验。工具或记忆缺失时明确停止该步骤。

准备数据包：skill_md（现有极简七字段及可选 needs_memory）、resources（references/ 或 assets/
下的文本数据）、examples（每项 input/success/failure/disabled）、dependencies（技能名到精确hash）。
保留原父权限；scripts/、可执行资源、下载和依赖安装本批不支持。
概括方法而非复制原任务原文；去除姓名、联系方式、凭据和本机路径，列出需人工检查的私有词。
凭据拒绝入库，联系方式/路径/显式私有词在候选生成前脱敏；自动规则不能证明任意自然语言匿名化，
用户须审阅全部候选包，尤其是正文、资源与样例中的自由文本。

由用户执行 /skill release candidate，给 source_task、memory_key、bundle、private_terms。
读取返回的受管候选 artifact，核对完整内容和hash；它仍是candidate，不代表技能可用。
同名同版本不可覆盖，修改须新版本。候选定义依赖显式缺失时不可发布。

独立验证在不同会话、不同Task执行。用S06显式工作区把受管候选原字节保存为release.json；
锁定四项必要检查，rationale 分别为 input/success/failure/disabled，由验证方选择命令与断言。
验证者实际检验候选所述业务行为、拒绝路径和关闭回归；仅断言hash存在或退出0不是业务验收。
S06每条命令仍须真实批准、只读离线执行；候选自身不携带执行脚本。
四项真实进程完成且验证版本一致后，用户在验证会话执行 /skill release validate，传release_id/change_id。
独立性由不同会话/任务和外部真实检查落实，不声称证明验证者的人类身份；用户仍审核检查质量。

用户核对验证产物后执行 /skill release enable，明确release_id和expected_head（首次为null）。
模型没有此发布工具；不得借write_file/start_process修改技能目录、配置或发布账本。
candidate→validated→enabled；升级把旧版标retired。当前Task冻结版本，后续Run继续旧版；
新Task读取新版本，skills.enabled及工具开关仍独立生效。
退役用retire；兼容回退用rollback并给expected_head。验证快照/来源过期时需重新候选和验证，
本批不提供跨代次验证恢复、任意破坏性升级或自动数据迁移。

回退前停止或核实在途外部动作；代码/技能版本回退不撤销已发命令。
发布报告分别列S01–S10输入、成功证据、失败处理、关闭回归，不以S10通过代替其他能力验收。
来源撤回、删除、替换或过期后派生技能不可继续注入；保留明确不可用原因，不静默回落旧版本。
