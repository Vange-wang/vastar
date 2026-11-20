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

恢复目录根部的 `history.db` 是数据库，`artifacts` 和随附 JSON 是数据。安装副本的 `memory` **还包含新版 Python 程序**，包括 `store.py` 和 `migrations`。保留整个新版程序树；直接用恢复目录替换 `memory` 会导致启动失败。

## 跨版本升级与回退（Windows 安装副本）

以下步骤适用于默认单用户布局：配置在 `config/settings.json`，库在 `memory/history.db`。用 **PowerShell 7** 在同一窗口按顺序执行。先在旧版输入 `/sessions` 记下一条要保留的会话，再用 `/exit` 关闭所有实例。解压目标版本，进入其含 `manifest.json` 的根目录。新旧安装使用独立目录；若设置了指向共享配置或共享库的路径覆盖，请先确认实际路径再迁移。升级后需重新提供模型服务的环境变量。

**1. 备份旧版，在空目录安装新版。** 输入两个绝对路径，新目录须尚未存在，并与旧安装同级。另建的 `vastar-safety-…` 目录保存个人配置、历史快照与恢复演练，请妥善保管。旧版安装始终原位保留。

~~~powershell
$ErrorActionPreference = 'Stop'
$package = (Get-Location).Path
$old = (Resolve-Path -LiteralPath (Read-Host '旧安装目录')).Path
$new = [IO.Path]::GetFullPath((Read-Host '同级的新安装目录'))
if ((Split-Path $old -Parent) -ne (Split-Path $new -Parent) -or (Test-Path -LiteralPath $new)) { throw '请选择同级且尚未存在的新目录' }
$safety = Join-Path (Split-Path $old -Parent) ('vastar-safety-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $safety | Out-Null
function Invoke-Maintenance($root, [string[]]$arguments) {
    $priorEncoding = [Console]::OutputEncoding
    Push-Location -LiteralPath $root
    try {
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        $json = & .\runtime\python\python.exe -X utf8 -B scripts/maintain.py @arguments
        if ($LASTEXITCODE -ne 0) { throw "维护命令失败：$($arguments[0])" }
        $json | ConvertFrom-Json
    } finally { [Console]::OutputEncoding = $priorEncoding; Pop-Location }
}
function Save-LocalState($root, $label) {
    $savedConfig = Join-Path $safety "$label-settings.json"
    if (Test-Path -LiteralPath $savedConfig) { throw "配置备份已存在：$savedConfig；请使用新的安全目录" }
    Copy-Item -LiteralPath "$root/config/settings.json" -Destination $savedConfig -ErrorAction Stop
    (Invoke-Maintenance $root @('backup','--backups',$safety,'--confirm')).snapshot
}
$oldZip = Save-LocalState $old 'old'
pwsh -NoProfile -File "$package/scripts/install.ps1" -Destination $new
if ($LASTEXITCODE -ne 0) { throw '新版安装失败，请保留旧版与备份，解决安装错误后重新选择空目录' }
~~~

**2. 恢复旧快照，只导入数据和兼容的配置。** 保持新版未启动。下面先用新版恢复工具验证 SQLite、产物索引和摘要，再检查所有目标是否空闲。只复制快照中的数据库、受支持的附属 JSON 及产物目录；新版 `memory` 内的程序文件全部保留。恢复工具读取数据库时可能产生的临时 WAL/SHM 不属于快照数据。

~~~powershell
$stage = Join-Path $safety 'old-restored'
Invoke-Maintenance $new @('restore','--archive',$oldZip,'--dest',$stage,'--confirm')
$data = Join-Path $new 'memory'
$names = @('history.db','history.db.revocations.json','history.db.migration.json','owner.json','migration-result.json','artifacts')
$entries = @(Get-ChildItem -LiteralPath $stage -Force | Where-Object Name -NotIn 'history.db-wal','history.db-shm')
if (!(Test-Path -LiteralPath "$stage/history.db" -PathType Leaf) -or @($entries | Where-Object Name -NotIn $names).Count) { throw '快照布局不受此流程支持，停止导入' }
foreach ($name in ($names + @('history.db-wal','history.db-shm','history.db.instance.lock'))) {
    if (Test-Path -LiteralPath (Join-Path $data $name)) { throw "新版数据位置已有 $name，请保留该安装并改用新的空目录" }
}
$oldCfg = Join-Path $safety 'old-settings.json'
$cfg = Get-Content -Raw -Encoding utf8 -LiteralPath $oldCfg | ConvertFrom-Json
if (($cfg.schema_version -and $cfg.schema_version -ne 1) -or $cfg.memory.path -ne 'memory/history.db') { throw '旧配置使用其他数据布局，请停止并核对实际数据路径' }
if ((Get-FileHash "$old/config/settings.example.json").Hash -ne (Get-FileHash "$new/config/settings.example.json").Hash) { throw '两版配置模板不同，请先逐项迁移兼容设置，勿整份覆盖' }
Copy-Item -LiteralPath "$new/config/settings.json" -Destination "$safety/new-default-settings.json"
foreach ($item in $entries) {
    Copy-Item -LiteralPath $item.FullName -Destination (Join-Path $data $item.Name) -Recurse
}
Copy-Item -LiteralPath $oldCfg -Destination "$new/config/settings.json"
if ((Get-FileHash $oldCfg).Hash -ne (Get-FileHash "$new/config/settings.json").Hash) { throw '配置复制校验失败' }
Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object { $_.FullName -notin @("$stage\history.db-wal","$stage\history.db-shm") } | ForEach-Object {
    $target = Join-Path $data ([IO.Path]::GetRelativePath($stage, $_.FullName))
    if ((Get-FileHash -LiteralPath $_.FullName).Hash -ne (Get-FileHash -LiteralPath $target).Hash) { throw "数据复制校验失败：$target" }
}
~~~

如旧配置含可选组件的绝对路径，请在新版重新安装相应组件并更新路径。任一步失败，请保持两份安装停机，从保留的旧快照向**另一空目录**重试。下列命令启动新版：在终端输入 `/sessions`，确认原会话可见。正常使用后退出、重启，再确认升级后新增的会话仍可读。

~~~powershell
Set-Location -LiteralPath $new
.\vastar.cmd
~~~

**3. 需要回退时，先保存新版新增数据，再切回旧版。** 先退出新版，继续使用上述 PowerShell 窗口。下面的 restore 将完整的新快照验证并恢复到独立目录。只有恢复成功后才启动保留的旧安装。旧版继续使用升级前的历史；升级后新增的记录保留在新版、安全快照和 `new-restored` 中，不会自动并入旧库。

~~~powershell
$newZip = Save-LocalState $new 'before-rollback'
$rollbackProof = Join-Path $safety 'new-restored'
Invoke-Maintenance $new @('restore','--archive',$newZip,'--dest',$rollbackProof,'--confirm')
Set-Location -LiteralPath $old
if (Test-Path -LiteralPath '.\vastar.cmd') {
    .\vastar.cmd
} else {
    .\runtime\python\python.exe -X utf8 -B main.py
}
~~~

回到旧终端后用 `/sessions` 核对原会话，再用 `/exit` 退出。早期公开版 `v2026-11-19` 需要同时配置 DeepSeek 和 GLM 的凭据才能启动；请沿用升级前可用的环境。今后切回新版时使用保留的新版安装，避免用旧库覆盖新版历史。

## 从 Git clone 开发（开发目录）

开发者在仓库根目录用 Python 3.11+ 创建 `.venv`、安装 requirements.txt，按需从 config/settings.example.json 初始化个人配置；开发入口为 `.\vastar.cmd`，离线诊断为 `.venv/Scripts/python.exe -B scripts/doctor.py --json`。上述「从安装包安装」流程使用安装器提供的运行时。

诊断把开关、依赖、服务、权限和已验证行为分开显示。可选组件的状态取决于本机安装与配置。manifest.json 列出本次快照的来源提交和文件摘要。
