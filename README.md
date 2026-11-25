# Vastar

![Vastar](ui/assets/startup/Vastar-wallpaper.png)

Vastar 是本地优先的个人 Agent，可在终端中检索项目资料、操作文件和接续任务。运行数据保存在本机。

## 从安装包安装（Windows x64）

新版导出快照使用 `.gitattributes` 保留文件原始字节，可用默认 Git 换行配置克隆后按该版本的 `manifest.json` 校验。

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

**3. 检查并执行数据库 schema 迁移（跨版本必检）。** 旧库 schema 低于新版要求时，首次启动会明确提示需要显式迁移并拒绝启动（不静默升级）。在新版根目录先跑只读预览（不改变任何数据）：

~~~powershell
Set-Location -LiteralPath $new
.\runtime\python\python.exe -X utf8 -B main.py --migrate-storage "memory\history.db"
~~~

预览列出源版本、目标版本与待执行步（如 `[[11, 12]]`）；若无待执行步说明库已当前，可直接进入下一步启动。确认后执行迁移（自动先备份再原子切换）：

~~~powershell
.\runtime\python\python.exe -X utf8 -B main.py --migrate-storage "memory\history.db" --apply-storage-migration
~~~

执行前自动备份到 `memory\history.db.migrations\<id>\backup.db`，迁移日志 `memory\history.db.migration.json` 请保留备查。中断后重入同一命令会先自动核账恢复，不重复执行。回退数据的口径：停止实例后离线恢复该备份——回退代码不等于回退数据，勿用旧库覆盖已迁移的新库。本步骤验收基线：源库 schema 11 → 目标 schema 12，2026-11-22 于 866MB 生产库独立副本实测通过（预览只读 / 备份 / 启动 / 回退全链路）；目标提交以发布包 `manifest.json` 记录的来源提交为准。早期公开版 `v2026-11-19` 的升级验收维持既有记录，不在本次变更范围。

**4. 启动新版并核对。** 如旧配置含可选组件的绝对路径，请在新版重新安装相应组件并更新路径。任一步失败，请保持两份安装停机，从保留的旧快照向**另一空目录**重试。下列命令启动新版：在终端输入 `/sessions`，确认原会话可见。正常使用后退出、重启，再确认升级后新增的会话仍可读。

~~~powershell
Set-Location -LiteralPath $new
.\vastar.cmd
~~~

**5. 需要回退时，先保存新版新增数据，再切回旧版。** 先退出新版，继续使用上述 PowerShell 窗口。下面的 restore 将完整的新快照验证并恢复到独立目录。只有恢复成功后才启动保留的旧安装。旧版继续使用升级前的历史；升级后新增的记录保留在新版、安全快照和 `new-restored` 中，不会自动并入旧库。

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

在已初始化的项目中，运行 `vastar --show-paths` 可查看项目根、实际工作目录和 `instruction_files`。编码时会从执行根到当前目录定位项目约定，每层依次查找非空的 `AGENTS.override.md`、`AGENTS.md`、`agent.md`。指定 `--workspace` 时读取执行副本的约定。

要从脚本发起项目只读检查，使用 `.\vastar.cmd --project <已初始化项目> --exec "概述项目入口及其依据"`，或将多行问题通过管道传给 `--exec -`。命令调用已配置的模型，输出包含回答和运行号的 JSON。退出码 0 表示本轮及工具调用成功，1 表示未完成或工具出现问题，2 表示参数或启动出错。

需要实时进度时，加上 `--exec-events`，例如 `.\vastar.cmd --project <已初始化项目> --exec "检查入口" --exec-events`。每行 JSON 带序号、类型和运行号。开始、工具请求和工具结果实时输出，最后一条 `run.finished` 的 `receipt` 字段包含回答与持久化状态。参数或启动失败会写入标准错误并退出 2。

要让脚本读取固定字段，给 `--exec` 加 `--output-schema result-schema.json`。例如要返回组件数，可将 `{"type":"object","properties":{"count":{"type":"integer"}},"required":["count"],"additionalProperties":false}` 保存为 UTF-8 文件，再检查安装清单。schema 最多 64 KiB，支持 `type`、`properties`、`required`、`additionalProperties`、`items` 和 `enum`，会随问题发给模型。校验通过后从回执的 `structured_output` 读取值。重复键、非有限数和无法原值回传的小数会导致校验失败。回答无法通过时退出 1，`output_validation.reason` 说明原因，`answer` 和持久化状态保留。可与 `--exec-events` 合用。

## 交付工作文件和代码修改

首次使用某个工作目录时，用 `.\vastar.cmd --project <工作目录> --init-project` 建立项目身份。之后用 `.\vastar.cmd --project <工作目录>` 开始交互会话。

- **报告和表格**。用 `/task new` 加上目标、输入文件和验收条件，例如“汇总 sales.csv 的销售额，交付按地区汇总的结果，注明未计入的行”。核对计划和预算后输入 `/task ok`、`/task run`。运行结束后用 `/task deliver` 核验文件。报告回执列出可打开的 Word、PDF 路径和产物引用。表格回执列出各组总额、原始行号、未计入原因和读取失败的工作表。以后可以用 `/task outputs` 重查已验收文件。
- **编码**。在目标仓库的工作副本中说明可修改的文件、问题和验证命令。用 `/task new` 起草并确认计划后要求执行代码修改验证。工具会记录基线、逐项请求所需的执行确认，随后给出变更路径、差异和命令退出码。用 `/task status` 查看持久任务的状态。验证依赖当前 Windows 离线执行环境，项目需要的第三方工具或依赖若未装入验证副本，回执应保留该限制。 新初始化的 Git 仓库也可从第一份源文件开始，回执会标明尚无首提交的分支。

需要显式调用专门流程时，在下一条任务输入前使用 `/skill code-change-validation`、`/skill spreadsheet-analysis` 或 `/skill report-generation`。

开发者在仓库根目录用 Python 3.11+ 创建 `.venv`、安装 requirements.txt，按需从 config/settings.example.json 初始化个人配置；开发入口为 `.\vastar.cmd`，离线诊断为 `.venv/Scripts/python.exe -B scripts/doctor.py --json`。上述「从安装包安装」流程使用安装器提供的运行时。

诊断把开关、依赖、服务、权限和已验证行为分开显示。可选组件的状态取决于本机安装与配置。manifest.json 列出本次快照的来源提交和文件摘要。
