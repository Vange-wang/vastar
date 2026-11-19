# Vastar

![Vastar](ui/assets/startup/Vastar-wallpaper.png)

Vastar 是本地优先的个人 Agent，可在终端中检索项目资料、操作文件和接续任务。运行数据保存在本机。

## Windows 快速开始

在快照根目录使用 Python 3.11 及以上版本，先创建虚拟环境并安装运行依赖。在新安装中将配置样例复制为个人配置。

~~~powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install prompt_toolkit==3.0.53
if (!(Test-Path config/settings.json)) { Copy-Item config/settings.example.json config/settings.json }
.\vastar.cmd
~~~

按配置中所选服务的环境变量提供凭据。首次启动会创建本地数据库。退出后可运行离线诊断。

~~~powershell
.venv/Scripts/python.exe -B scripts/doctor.py --json
~~~

诊断把开关、依赖、服务、权限和已验证行为分开显示。可选组件的状态取决于本机安装与配置。manifest.json 列出本次快照的来源提交和文件摘要。
