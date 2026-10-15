---
name: report-generation
description: 从有出处的结构化原文生成可分享的中英文 PDF 和 Word 报告，核对表格引用与实际页图并交真人看版式
version: "1.0.0"
triggers:
  - 生成报告
  - 报告生成
  - report generation
permissions:
  - A
  - B
required_tools:
  - report_capabilities
  - generate_report
  - read_file
  - read_artifact
needs_memory: false
enabled: true
---

## S03 · 受控路径

先 report_capabilities 核对能力及三个版本。唯一生成路径：结构化来源 JSON → 受控 A4 模板 → PDF 与 OOXML DOCX → PDF 实际页图检查 → 验证 → 发布。
模板、中文字体与完整渲染资源随 assets 落库，manifest.json 记录版本与 SHA256；任一缺失或不匹配则不生成。
渲染组件为外部依赖、非内置，使用 CPython/ReportLab/PDFium/Pillow 固定资源包；不借系统字体或 PATH 中碰巧安装的程序。
执行沿既有 B27/B28 离线只读 AppContainer，仍需开放 D 并逐次明确批准；技能的只读权限不授予命令执行许可。
工具只读来源，生成件只进受管独立目录，不接受用户输出路径，不覆盖用户原件。
原始渲染输出先在私有内存暂存，不提前发布进程 artifact；全部验证通过才发布文档及页图。

## 来源准备

用户提供的 JSON 字段：title、header、paragraphs、tables、references，可选 images。
paragraphs 每条为 text 原文和 source 来源编号；references 每条为 id 与 citation 原样引用。
tables 每条含 headers 文本列名、rows 原样文本单元格、source、unit 原始单位。
所有事实须来自当前已核对的来源；不得根据常识补数字、换单位、改引用或编造来源。
信息不足时保留缺口，不从标题猜正文；空正文显示“信息不足”。
图片只接受有界 PNG：png_base64 原图字节、caption 原说明、source、license 授权原文、allow_report=true 明确允许报告使用。最多 6 图，每图 512 KiB/400 万像素；不重新绘制原图、不扩大授权。
图片缺失或无授权时列明剩余需求，不把失败当作已完成的图文报告。
源文件上限 1 MiB；最多 60 段、8 表、每表 150 行/5 列、30 条来源、40 页。
若需写入准备好的源文件，必须退出只读技能并经既有文件写入许可，不能借技能扩权。

## 生成与检查

generate_report(path) 读取现有来源 JSON；原文件 SHA256 前后不一致则禁止发布。
结构证据检查 DOCX 可打开、表格逐格与原数据一致、重复表头、页眉、段落与引用。
视觉证据单列：PDFium 对实际 PDF 每页渲染 PNG，检查空页和边缘裁切风险，保留页图供查看。
这些机器检查不能证明所有主观排版质量，DOCX 在不同阅读器中可能重新分页。
正文与文档元数据不得包含内部模块、工具名、系统路径、异常堆栈和调试痕迹；发现即拒绝，勿偷偷删改来源事实。
交付短说明、文件位置/类型、页图及检查清单、未解决问题；不要把内部实现和调试信息写给文档读者。
真人版式结论由 用户 填写；实施方和审核方不得替他说“版式合格”。

## 失败与关闭

失败保留 source.json 可编辑源，STATUS 标“未完成输出”；没有可打开文件与结构/视觉证据不能报完成。
修正缺口后可明确重试，每次独立目录；同名源不会覆盖旧输出。unknown 操作先 reconcile，不自动重试。
工具/技能各自可关闭；skills.enabled 与本文件 enabled 为二级开关，tools.enabled 控制各工具，不默认自动开启。
不自动写长期记忆；回退先停止或核实在途 Job，按 Operation 回执说明已发动作，代码回退不能撤销已发生执行。
