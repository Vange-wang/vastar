#requires -Version 7.0

param([string]$Destination,
      [string[]]$Component=@(),
      [switch]$DryRun)
$ErrorActionPreference='Stop'
if(!$Destination){throw 'Specify -Destination'}
if('--dry-run' -in $Component){$DryRun=$true;$Component=@($Component|Where-Object {$_ -ne '--dry-run'})}
foreach($arg in $args){if($arg -eq '--dry-run'){$DryRun=$true}else{throw "Unknown argument $arg"}}
foreach($option in $Component){if($option -notin 'browser','search','report'){throw "Unknown component $option"}}
$root=[IO.Path]::GetFullPath($Destination)
$source=Split-Path $PSScriptRoot
$lock=Get-Content (Join-Path $PSScriptRoot 'install-lock.json') -Raw -Encoding utf8 | ConvertFrom-Json
$packages=@($lock | Where-Object { $_.name -ne 'playwright' -or $Component -contains 'browser' })
Write-Output "Vastar install -> $root (Windows x64). No service or autostart."
foreach($pkg in $packages){Write-Output "Package $($pkg.name) $($pkg.version) -> runtime/$($pkg.name) sha256=$($pkg.sha256)"}
Write-Output "Copy tracked application files. Preserve config/settings.json. Options: $Component"
if($DryRun){return}
if(!$IsWindows -or ![Environment]::Is64BitOperatingSystem){throw 'Requires Windows x64'}
if($root -eq $source -or $root.StartsWith($source+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Choose a separate installation directory'}
$runtime=Join-Path $root 'runtime'
[IO.Directory]::CreateDirectory($runtime) | Out-Null
$lease=[IO.File]::Open((Join-Path $runtime 'install.lock'),'OpenOrCreate','ReadWrite','None')
try {
foreach($pkg in $packages){
    $dir=Join-Path $runtime $pkg.name
    $marker=Join-Path $dir '.package-sha256'
    if((Test-Path $marker) -and (Get-Content $marker -Raw).Trim() -eq $pkg.sha256){Write-Output "Satisfied $($pkg.name)";continue}
    if(Test-Path $dir){throw "Unexpected or damaged $dir. Inspect and rename it before retrying."}
    $scratch=Join-Path $runtime ([guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($scratch)|Out-Null
    try{
        $zip=Join-Path $scratch 'download.zip'
        Invoke-WebRequest -Uri $pkg.url -OutFile $zip -TimeoutSec 180
        if((Get-FileHash $zip).Hash -ne $pkg.sha256){throw 'SHA256 mismatch'}
        $stage=Join-Path $scratch 'unpacked'
        [IO.Compression.ZipFile]::ExtractToDirectory($zip,$stage)
        if($pkg.name -eq 'python'){
            [IO.File]::WriteAllLines((Join-Path $stage 'python311._pth'),@('python311.zip','.','../prompt_toolkit','../wcwidth',$root))
        }
        [IO.File]::WriteAllText((Join-Path $stage '.package-sha256'),$pkg.sha256)
        [IO.Directory]::Move($stage,$dir)
        Write-Output "Installed $($pkg.name)"
    }catch{throw "Missing dependency $($pkg.name) $($pkg.version). Retry scripts/install.ps1 -Destination '$root'. $($_.Exception.Message)"}
    finally{if([IO.Path]::GetDirectoryName($scratch) -eq $runtime){Remove-Item -LiteralPath $scratch -Recurse -Force}}
}
$releaseManifest=Join-Path $source 'manifest.json'
if(Test-Path -LiteralPath $releaseManifest){
    $release=Get-Content -Raw -LiteralPath $releaseManifest -Encoding utf8 | ConvertFrom-Json -AsHashtable
    if($release.format -ne 1 -or !$release.files.Contains('main.py')){throw 'Invalid public snapshot manifest'}
    $files=@($release.files.Keys | Where-Object {
        $_ -cmatch '^(?:agents|core|ui|memory|prompts|skills|deploy/searxng)/[A-Za-z0-9_./-]+$' -or
        $_ -cin @('main.py','vastar.cmd','scripts/start_vastar.py','pyproject.toml','scripts/maintain.py','config/settings.example.json') -or
        $_ -cmatch '^scripts/doctor(?:_[a-z]+)?[.]py$'
    })
    foreach($file in $files){
        if($file -match '(?:^|/)[.]|[.]db(?:$|[.-])' -or
            (Get-FileHash -LiteralPath (Join-Path $source $file)).Hash -ne $release.files[$file]){
            throw "Invalid release file: $file"
        }
    }
}else{
$files=@(git -C $source ls-files -- agents core memory ui tools prompts skills main.py vastar.cmd scripts/start_vastar.py pyproject.toml 'scripts/doctor*.py' scripts/maintain.py config/settings.example.json deploy/searxng)
if($LASTEXITCODE -ne 0 -or !$files){throw 'Run installer from a Git checkout with Git available'}
}
foreach($rel in $files){
    if($rel.StartsWith('skills/report-generation/assets/') -and $Component -notcontains 'report'){continue}
    if($rel.StartsWith('deploy/searxng/') -and $Component -notcontains 'search'){continue}
    $from=Join-Path $source $rel
    $to=Join-Path $root $rel
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($to))|Out-Null
    if(Test-Path $to){if((Get-FileHash $to).Hash -ne (Get-FileHash $from).Hash){throw "Existing file differs: $to"};continue}
    $tmp=$to+'.install-'+[guid]::NewGuid().ToString('N')
    [IO.File]::Copy($from,$tmp);[IO.File]::Move($tmp,$to)
}
$configFile=Join-Path $root 'config/settings.json'
$config=Get-Content (Join-Path $root 'config/settings.example.json') -Raw -Encoding utf8 | ConvertFrom-Json
if($Component -contains 'search'){
    $docker=Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if(!$docker){throw 'Missing optional Docker CLI/engine. Install Docker Desktop and retry -Component search.'}
    $oldSecret=$env:SEARXNG_SECRET
    try {
        if(!$env:SEARXNG_SECRET){$env:SEARXNG_SECRET='compose-pull-only'}
        & $docker.Source compose -f (Join-Path $root 'deploy/searxng/compose.yaml') pull
    } finally {$env:SEARXNG_SECRET=$oldSecret}
    if($LASTEXITCODE){throw 'Pinned SearXNG/gateway image pull failed; check Docker engine and retry -Component search.'}
    $config.network.enabled=$true
    $config.network.authorization='Installation explicitly selected local search'
    $config.network.declared_cost='Self-hosted loopback service; no paid search API configured'
    Write-Output 'Search images downloaded. To start manually, see deploy/searxng/README.md. Existing configs need network.enabled and local network.authorization / network.declared_cost.'
}
if($Component -contains 'browser'){
    $driver=Join-Path $runtime 'playwright/playwright/driver'
    $browserRoot=Join-Path $runtime 'browsers'
    $previous=$env:PLAYWRIGHT_BROWSERS_PATH
    try{$env:PLAYWRIGHT_BROWSERS_PATH=$browserRoot; & "$driver/node.exe" "$driver/package/cli.js" install chromium --only-shell; if($LASTEXITCODE){throw 'Chromium download failed; retry -Component browser'}}
    finally{$env:PLAYWRIGHT_BROWSERS_PATH=$previous}
    $engine=Get-ChildItem $browserRoot -Filter chrome-headless-shell.exe -File -Recurse | Select-Object -First 1
    if(!$engine){throw 'Missing Chromium headless shell'}
    $config.browser.node_executable="$driver/node.exe";$config.browser.playwright_package="$driver/package";$config.browser.chromium_directory=$engine.DirectoryName;$config.browser.enabled=$true
    Write-Output "Browser configuration for existing installations: $($config.browser | ConvertTo-Json -Compress)"
}
if(Test-Path $configFile){Write-Output 'Preserved existing config/settings.json'}
else{$tmp=$configFile+'.'+[guid]::NewGuid().ToString('N');$config|ConvertTo-Json -Depth 64|Set-Content $tmp -Encoding utf8;[IO.File]::Move($tmp,$configFile)}
$python=Join-Path $runtime 'python/python.exe'
& $python -B -c 'import prompt_toolkit,wcwidth;print("Core dependencies OK:",prompt_toolkit.__version__,wcwidth.__version__)'
if($LASTEXITCODE){throw "Core import failed. Inspect runtime/prompt_toolkit and runtime/wcwidth; rename damaged package directory and retry -Destination '$root'."}
$manifest=@($lock|Where-Object {Test-Path (Join-Path $runtime "$($_.name)/.package-sha256")} |ForEach-Object {[ordered]@{name=$_.name;version=$_.version;source=$_.url;sha256=$_.sha256;path=(Join-Path $runtime $_.name)}})
$tmp=Join-Path $runtime 'manifest.tmp';ConvertTo-Json -InputObject $manifest|Set-Content $tmp -Encoding utf8;[IO.File]::Move($tmp,(Join-Path $runtime 'manifest.json'),$true)
$assets=@()
foreach($rel in @('runtime/playwright/playwright/driver/node.exe','skills/report-generation/assets/manifest.json','deploy/searxng/compose.yaml')){
    $asset=Join-Path $root $rel
    if(Test-Path $asset){$assets+=@{path=$rel;sha256=(Get-FileHash $asset).Hash}}
}
$browserDir=Join-Path $runtime 'browsers'
if(Test-Path $browserDir){foreach($file in Get-ChildItem $browserDir -File -Recurse -Filter chrome-headless-shell.exe){
    $assets+=@{path=$file.FullName;version=$file.VersionInfo.ProductVersion;sha256=(Get-FileHash $file.FullName).Hash}
}}
$tmp=Join-Path $runtime 'assets.tmp';ConvertTo-Json -InputObject $assets|Set-Content $tmp -Encoding utf8;[IO.File]::Move($tmp,(Join-Path $runtime 'optional-assets.json'),$true)
$launcher=(Join-Path $root 'vastar.cmd').Replace("'", "''")
Write-Output "Ready: & '$launcher'"
}finally{$lease.Dispose()}
