# Vastar

![Vastar](ui/assets/startup/Vastar-wallpaper.png)

Vastar 是本地优先的个人 Agent，可在终端中检索项目资料、操作文件和接续任务。运行数据保存在本机。

## 从安装包安装（Windows x64）

解压发布包，在解压目录打开 **PowerShell 7（pwsh）或更高版本**。下面的命令先让你输入新的安装目录，再安装核心运行时。选择解压目录以外的空路径。

~~~powershell
$install = Read-Host '新的安装目录'
pwsh -NoProfile -File .\scripts\install.ps1 -Destination $install
Set-Location -LiteralPath $install
~~~

在该终端设置你已有的一家模型服务的密钥。下面以 DeepSeek 为例；已通过环境变量配置的用户可直接启动。输入时密钥隐藏。

~~~powershell
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new('', (Read-Host 'DeepSeek API key' -AsSecureString)).Password
.\vastar.cmd
~~~

安装器的 Ready 行也给出可直接执行的绝对路径。首次输入称呼后进入对话；输入 `/status` 查看其他提供方的配置状态，`/exit` 退出。安装副本自带 runtime。以下离线诊断命令在**安装目录**执行；退出码 1 表示有警告，2 表示存在阻断。

~~~powershell
.\runtime\python\python.exe -B scripts/doctor.py --json
~~~

**可选组件。** 在解压目录的 PowerShell 7 中直接调用安装脚本，`-Component report` 添加报告资源，`-Component browser` 添加浏览器，`-Component search` 需要已启动的 Docker。多项可传数组，如 `-Component @('report','browser')`。仅当需要相应组件时选择，网络下载及服务前提各自独立。

**备份与恢复。** 退出 Vastar 后在安装目录运行。下面演练恢复到新的 restore-check 目录，目标须尚未存在。

~~~powershell
$snapshot = (.\runtime\python\python.exe -B scripts/maintain.py backup --confirm | ConvertFrom-Json).snapshot
.\runtime\python\python.exe -B scripts/maintain.py restore --archive $snapshot --dest restore-check --confirm
~~~

恢复目录的根就是数据目录。默认库在 `restore-check/history.db`，同级还有快照中的附属数据。正常安装使用 `memory/history.db`。先在独立副本核验整个恢复目录；如需切换，停机、备份当前 memory 目录并保留升级后的数据，再将恢复目录作为新副本的数据目录。

## 从 Git clone 开发（开发目录）

开发者在仓库根目录用 Python 3.11+ 创建 `.venv`、安装 requirements.txt，按需从 config/settings.example.json 初始化个人配置；开发入口为 `.\vastar.cmd`，离线诊断为 `.venv/Scripts/python.exe -B scripts/doctor.py --json`。上述「从安装包安装」流程使用安装器提供的运行时。

诊断把开关、依赖、服务、权限和已验证行为分开显示。可选组件的状态取决于本机安装与配置。manifest.json 列出本次快照的来源提交和文件摘要。
