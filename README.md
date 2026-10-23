# Vastar

本地优先的 Python 通用 Agent，包含工具调用、持久化会话与任务记录、可组合技能及多 Agent 协作。

## 2026-10-23 更新

- 新增 `/sessions` 会话列表与 `/resume <序号>` 完整历史回放；可用 `--page <页码>` 分页定位，Ctrl+C 可取消回放。
- 更新终端欢迎界面、状态栏与输入容器显示。
- 修复长篇、多段正文滚屏时段首被覆盖的问题，实时输出与历史回放均保留完整正文。

## 运行

当前发布版面向 **Windows x64、Python 3.11+**，请使用 PowerShell 或 Windows Terminal。Linux/macOS 未完成验证；Windows 原生隔离执行能力不能直接移植使用。

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
Copy-Item config/settings.example.json config/settings.json
.\vastar.cmd --help
.\vastar.cmd --show-paths
```

编辑本地 `config/settings.json`，选择自己的供应商、模型和参数。通过配置中的 `api_key_env` 对应的环境变量提供自己的 API Key；可在本机自行创建 `.env`，该文件被忽略且不随发布包提供。不要把密钥写进配置或提交到 Git。启动命令：`.\vastar.cmd`，续聊：`.\vastar.cmd --continue`。

默认示例使用 DeepSeek 主供应商和 GLM 备用供应商，**启动时就需要 `DEEPSEEK_API_KEY` 与 `GLM_API_KEY`**。如果只使用 DeepSeek，把本地 `config/settings.json` 中的 `llm.fallback_provider` 改为 `null`，只配置 `DEEPSEEK_API_KEY` 即可。可在仓库根目录自行创建 `.env`，每行按 `变量名=自己的密钥` 填写。MiMo 仅在切换到该供应商时需要配置。`--help` 和 `--show-paths` 不调用模型。使用者须自行核对所用供应商的模型可用性、配额和费用。

请在真正的 Windows 终端中交互运行；不要通过重定向标准输入/输出或管道启动交互界面，否则可能出现 `NoConsoleScreenBufferError`。

## 发布范围

仅包含 Agent 运行代码、启动器、提示词、技能说明、必要静态资源、依赖清单及中性配置模板。开发测试、协作材料、历史日志、数据库、个人配置、凭据、本地虚拟环境均不发布。

`memory/` 中的 Python 文件是持久化实现代码，不能删除；聊天记录和数据库由运行时在本地生成。`scripts/start_vastar.py` 是启动器依赖，并非测试脚本。

浏览器与联网检索的示例开关默认关闭，MCP 未配置服务，需使用者配置自己的依赖、端点和授权后启用。`execution.network=false` 保持不变。未配置外部能力时会显示不可用，不代表已经完成实际浏览器或联网验证。

报告导出所需的独立 Windows Python/渲染环境包不随本次纯 Agent 版本发布；相关能力在资源缺失时会显式不可用。字体、资源清单和第三方许可保留；安装外部资源时须匹配清单中的版本和哈希。PDF解析、浏览器等其他外部程序也须按各能力要求另行配置。

## 许可

Agent 项目代码采用 MIT，见 LICENSE。第三方字体及其他资源遵循其随附许可，不因项目 MIT 许可而变更授权。
