![Vastar 星空封面](assets/Vastar-wallpaper.png)

# Vastar

**在终端里对话，在本地推进任务。**

Vastar 是一个本地优先的通用 AI Agent。它把模型对话、文件工具、持久化会话、技能和任务管理放在同一个终端里：从理解一份代码、整理项目资料，到分步骤执行和追踪任务，都可以沿着同一段上下文继续。

会话记录和任务状态保存在本机，模型推理由你配置的 API 供应商提供。请求中所需的对话和工具结果会发送给该供应商；“本地优先”指数据存储与运行方式，不代表离线推理。

[快速开始](#快速开始) · [日常使用](#日常使用) · [模型配置](#模型配置) · [扩展能力](#扩展能力)

## 能做什么

- **围绕本地文件工作**：读取和检索代码、定位文件、编辑内容、整理目录，通过工具结果继续下一步。
- **随时回到之前的对话**：会话保存在 SQLite 中，可以查看列表、切换会话、完整回放历史并接着聊。
- **把复杂工作拆成任务**：起草计划，确认后记录目标、步骤、验收要求与预算，查看进度和执行轨迹。
- **按任务加载技能**：内置代码库理解、文件整理、文档提取、电子表格分析等技能说明，按需组合模型与工具。
- **管理执行过程**：查看当前状态、切换模型、补充要求、取消回答，并在上下文较长时预览和批准压缩。
- **扩展到协作与外部工具**：包含多 Agent 编排、浏览器操作、联网检索和 MCP 接入模块；使用条件见下方扩展说明。

工具的可用性由配置、权限和本机依赖共同决定。可以先从对话、文件读取和会话续聊开始，再逐项启用其他能力。

## 快速开始

当前发布版面向 **Windows x64 + Python 3.11 或更新版本**。需要 Git 和一个模型供应商的 API Key。请在 PowerShell 或 Windows Terminal 中操作。

### 1. 下载与安装

```powershell
git clone --branch release https://github.com/Vange-wang/vastar.git
cd vastar
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config\settings.example.json config\settings.json
```

无需激活虚拟环境，启动器会优先使用项目中的 `.venv`。

### 2. 配置一个模型供应商

首次使用可以只配置 DeepSeek。打开 `config/settings.json`，在 `llm` 段中将 `fallback_provider` 改为 `null`：

```json
"fallback_provider": null
```

这是修改现有字段，不要用这一行替换整个配置文件。主供应商保留为 `deepseek`，并核对 `llm.model` 是否为你的账户可调用的模型。

在仓库根目录新建 `.env`，填入自己的密钥：

```dotenv
DEEPSEEK_API_KEY=在这里填写自己的密钥
```

`.env` 和本地 `config/settings.json` 已被 Git 忽略。模型调用的费用、配额与数据处理政策由所选供应商决定。

### 3. 启动

```powershell
.\vastar.cmd
```

进入界面后直接输入要求，例如：

```text
阅读这个项目的 README 和目录结构，说明它的用途与启动方式。先不要修改文件。
```

可以先用以下命令检查启动器和路径；这两条命令不会调用模型：

```powershell
.\vastar.cmd --help
.\vastar.cmd --show-paths
```

交互界面需要真实的 Windows 终端，请直接启动，不要通过管道或重定向运行。

## 日常使用

### 找回并继续会话

启动时继续最近一次会话：

```powershell
.\vastar.cmd --continue
```

在运行中的界面里选择其他会话：

```text
/sessions
/resume 2
```

`/resume 2` 会切换到列表中的第 2 个会话，从第一条消息开始完整回放。长会话也可以用 `/resume 2 --page 3` 定位，每页 20 条。回放时按 **Ctrl+C** 可以停止显示，随后继续输入。

### 常用命令

| 命令 | 用途 |
|---|---|
| `/help` | 查看命令与快捷键 |
| `/new` | 开始新会话，保留旧会话记录 |
| `/model` | 列出模型并选择切换 |
| `/status` | 查看运行状态 |
| `/steer <补充要求>` | 为当前任务补充要求 |
| `/retry` | 重问上一条输入 |
| `/skill` | 查看技能；`/skill <名称>` 安排下一条输入使用的技能 |
| `/task help` | 查看计划、步骤、验收和预算相关操作 |
| `/team` | 查看团队与任务总览 |
| `/trace` | 查看执行追踪入口 |
| `/compact` | 生成上下文压缩预览，确认后用 `/compact ok` 应用 |
| `/exit` | 退出程序 |

**Enter** 提交，**Ctrl+Enter** 换行。**Ctrl+C** 根据当前状态取消回答、停止历史回放、退出模型选择或清空草稿。

## 模型配置

供应商、模型、上下文和输出参数统一放在 `config/settings.json` 中。仓库提供配置模板，实际使用时应按自己的账户与服务端能力调整。

| 供应商 | API Key 环境变量 | 配置方式 |
|---|---|---|
| DeepSeek | `DEEPSEEK_API_KEY` | 模板中的主供应商 |
| GLM | `GLM_API_KEY` | 模板中的备用供应商 |
| MiMo | `MIMO_API_KEY` | 模板中提供 OpenAI 兼容协议配置 |
| 其他 OpenAI 兼容服务 | 自定义 | 在 `llm.providers` 中声明 `protocol: "openai_compatible"`，配置端点、模型与密钥环境变量 |

模板默认启用 DeepSeek 主供应商和 GLM 备用供应商，因此原样使用时，**启动就需要同时提供两者的密钥**。只使用一个供应商时，将 `llm.fallback_provider` 设为 `null` 即可。

新增模型还需填写相应的上下文窗口、输出限制等参数；仅填写 API Key 不会自动发现或验证服务端模型。

## 扩展能力

基础安装包含运行代码、终端界面、提示词、技能说明和必要静态资源。以下能力需要额外配置：

| 能力 | 使用条件 |
|---|---|
| 浏览器操作 | 配置 Node.js、Playwright、Chromium 路径与访问范围；模板默认关闭浏览器入口 |
| 联网检索 | 配置检索端点、凭据和允许访问的主机；模板默认关闭联网检索 |
| MCP 工具 | 在 `providers.servers` 中配置服务及其依赖；模板未预置服务 |
| 文档与表格处理 | 取决于具体格式及所需解析依赖，可先查看相应能力状态 |
| 报告导出 | 需要匹配资源清单的 Windows 渲染环境包；本仓库不附带该独立环境包 |
| 多 Agent 协作 | 通过任务与团队模块配置和管理，受模型、预算及工具权限约束 |

隔离执行中的网络权限由 `execution.network` 单独控制，模板值为 `false`。启用浏览器或检索入口不等于同时启用隔离进程联网。

## 文件与数据

```text
vastar/
├── main.py                 启动入口
├── vastar.cmd              Windows 启动器
├── config/                 配置模板与本地配置
├── core/                   模型、工具、任务与协作逻辑
├── ui/                     终端交互与输出
├── agents/                 Agent 定义与加载
├── memory/                 持久化实现与本地会话数据
├── prompts/                提示词
└── skills/                 技能说明与配套资源
```

默认配置下，会话数据写入 `memory/history.db`，运行日志写入 `logs/`。这些数据在本地生成，不随仓库分发。`memory/` 同时包含运行所需的 Python 模块，备份或迁移时请保留其中的代码文件。

Linux 和 macOS 尚未完成验证；当前的 Windows 隔离执行实现也需要相应的平台支持。

## 许可证

项目代码采用 [MIT License](LICENSE)。字体和其他第三方资源遵循各自随附的许可证。
