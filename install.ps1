﻿param(
    [string]$Version = '',
    [switch]$Migrate,
    [switch]$NoPath
)
# Windows PowerShell 5.1 지원. Python/uv 및 관리자 권한 불필요.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Repository = 'https://github.com/workingdad365/agents-status-releases'
$Api = 'https://api.github.com/repos/workingdad365/agents-status-releases'
$Root = Join-Path $env:LOCALAPPDATA 'agents-status'
$Bin = Join-Path $Root 'bin'
$Launcher = Join-Path $Bin 'agents-status.cmd'
$Utf8 = New-Object Text.UTF8Encoding($false)
if ($Version -and $Version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { throw '안정 버전 x.y.z만 허용됨' }
$Architecture = $env:PROCESSOR_ARCHITECTURE
if ($env:PROCESSOR_ARCHITEW6432) { $Architecture = $env:PROCESSOR_ARCHITEW6432 }
if ($Architecture -eq 'ARM64') {
    Write-Warning 'Windows ARM64는 x64 에뮬레이션으로 실행함. 네이티브 ARM64 산출물은 없음.'
} elseif ($Architecture -ne 'AMD64') { throw '64비트 Windows 필요' }
$Existing = Get-Command agents-status -ErrorAction SilentlyContinue
if ($Existing -and $Existing.Source -ne $Launcher -and -not $Migrate) {
    throw "기존 설치 감지: $($Existing.Source). uv tool uninstall agents-status 후 재시도하거나 -Migrate로 명시적 전환. 소스는 삭제하지 않음."
}
if ((Test-Path -LiteralPath $Launcher) -and -not $Migrate -and -not (Select-String -LiteralPath $Launcher -Pattern '^rem agents-status managed launcher$' -Quiet)) { throw '기존 실행기를 덮어쓰려면 -Migrate 필요' }
if ((Test-Path -LiteralPath $Bin) -and ((Get-Item -LiteralPath $Bin).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw '실행기 디렉터리 링크 거부' }
if ((Test-Path -LiteralPath $Root) -and ((Get-Item -LiteralPath $Root).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw '설치 루트 링크 거부' }
[IO.Directory]::CreateDirectory($Root) | Out-Null
$Versions = Join-Path $Root 'versions'
[IO.Directory]::CreateDirectory($Versions) | Out-Null
if ((Get-Item -LiteralPath $Versions).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'versions 링크 거부' }
$Lock = Join-Path $Root '.update-lock'
# New-Item은 기존 디렉터리가 있으면 실패하므로 상호 배제를 보장함.
try { New-Item -ItemType Directory -Path $Lock -ErrorAction Stop | Out-Null }
catch { throw "다른 설치/업데이트 진행 중. 모두 종료한 뒤 남은 잠금만 삭제: $Lock" }
$Stage = Join-Path $Root ('.install.' + [Guid]::NewGuid().ToString('N'))
$Activated = $false
$Success = $false
$Installed = $false
$HadCurrent = $false
$HadBin = Test-Path -LiteralPath $Bin
$OldPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$PathChanged = $false
$Current = Join-Path $Root 'current.json'
$Target = $null
function Fetch([string]$Address, [string]$Destination) {
    $Uri = [Uri]$Address
    for ($Redirect = 0; $Redirect -lt 8; $Redirect++) {
        if ($Uri.Scheme -ne 'https' -or $Uri.Host -notin @('github.com', 'api.github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com')) { throw '허용하지 않는 다운로드 주소' }
        $Request = [Net.HttpWebRequest]::Create($Uri)
        $Request.AllowAutoRedirect = $false
        $Request.UserAgent = 'agents-status-installer'
        $Request.Timeout = 60000
        $Request.ReadWriteTimeout = 600000
        $Response = $Request.GetResponse()
        try {
            $Status = [int]$Response.StatusCode
            if ($Status -in @(301, 302, 303, 307, 308)) {
                $Uri = New-Object Uri($Uri, $Response.Headers['Location'])
                continue
            }
            if ($Status -ne 200) { throw "다운로드 실패: $Status" }
            $InputStream = $Response.GetResponseStream()
            $OutputStream = [IO.File]::Create($Destination)
            try { $InputStream.CopyTo($OutputStream) }
            finally { $OutputStream.Dispose(); $InputStream.Dispose() }
            return
        } finally { $Response.Dispose() }
    }
    throw '다운로드 리디렉션 한도 초과'
}
function Smoke([string]$Executable, [string]$Expected) {
    $Output = & $Executable --version
    if ($LASTEXITCODE -ne 0 -or (($Output -join "`n").Trim() -split '\s+')[-1] -ne $Expected) { throw '실행 파일 버전 불일치' }
    & $Executable --self-check
    if ($LASTEXITCODE -ne 0) { throw '실행 파일 자체 점검 실패' }
}
function Activate([string]$Source, [string]$Destination) {
    if (Test-Path -LiteralPath $Destination) { [IO.File]::Replace($Source, $Destination, $null) }
    else { [IO.File]::Move($Source, $Destination) }
}
try {
    [IO.Directory]::CreateDirectory($Stage) | Out-Null
    if (-not $Version) {
        $Metadata = Join-Path $Stage 'release.json'
        Fetch "$Api/releases/latest" $Metadata
        $Release = Get-Content -LiteralPath $Metadata -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($Release.draft -or $Release.prerelease -or $Release.tag_name -notmatch '^v([0-9]+\.[0-9]+\.[0-9]+)$') { throw '최신 안정 릴리스 확인 실패' }
        $Version = $Matches[1]
    }
    $Asset = "agents-status-$Version-windows-x86_64.zip"
    $Base = "$Repository/releases/download/v$Version"
    $Checksums = Join-Path $Stage 'SHA256SUMS'
    $ArchivePath = Join-Path $Stage 'archive.zip'
    Fetch "$Base/SHA256SUMS" $Checksums
    Fetch "$Base/$Asset" $ArchivePath
    $MatchesFound = @(Get-Content -LiteralPath $Checksums | Where-Object { $_ -match ('^([a-fA-F0-9]{64})  ' + [regex]::Escape($Asset) + '$') })
    if ($MatchesFound.Count -ne 1) { throw '체크섬 항목이 없거나 중복됨' }
    $Expected = $MatchesFound[0].Substring(0, 64)
    if ((Get-FileHash -LiteralPath $ArchivePath -Algorithm SHA256).Hash -ne $Expected) { throw 'SHA256 불일치' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    $Extracted = Join-Path $Stage 'extracted'
    [IO.Directory]::CreateDirectory($Extracted) | Out-Null
    try {
        $Names = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
        $Total = [long]0
        if ($Archive.Entries.Count -eq 0 -or $Archive.Entries.Count -gt 50000) { throw '잘못된 아카이브 크기' }
        foreach ($Entry in $Archive.Entries) {
            $Name = $Entry.FullName
            if ($Name -notmatch '^agents-status/[A-Za-z0-9_./+@-]+$' -or $Name -match '(^|/)\.{1,2}(/|$)|//|/$' -or ($Name -ne 'agents-status/agents-status.exe' -and -not $Name.StartsWith('agents-status/_internal/'))) { throw "안전하지 않은 아카이브 경로: $Name" }
            foreach ($Part in $Name.Split('/')) {
                if ($Part.EndsWith('.') -or $Part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)') { throw '예약된 Windows 경로 거부' }
            }
            $Kind = ($Entry.ExternalAttributes -shr 16) -band 0xF000
            if ($Kind -ne 0 -and $Kind -ne 0x8000) { throw '일반 파일 이외 아카이브 항목 거부' }
            if (-not $Names.Add($Name)) { throw '중복 아카이브 경로' }
            $Total += $Entry.Length
            if ($Total -gt 2147483648) { throw '아카이브 압축 해제 크기 초과' }
        }
        foreach ($Entry in $Archive.Entries) {
            $Destination = Join-Path $Extracted ($Entry.FullName.Replace('/', [IO.Path]::DirectorySeparatorChar))
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Destination)) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($Entry, $Destination, $false)
        }
    } finally { $Archive.Dispose() }
    $Payload = Join-Path $Extracted 'agents-status'
    $Executable = Join-Path $Payload 'agents-status.exe'
    if (-not (Test-Path -LiteralPath (Join-Path $Payload '_internal') -PathType Container)) { throw '잘못된 onedir 구조' }
    Smoke $Executable $Version
    $Target = Join-Path $Versions $Version
    if (Test-Path -LiteralPath $Target) { throw "이미 설치된 버전: $Version (덮어쓰지 않음)" }
    [IO.Directory]::Move($Payload, $Target)
    $Installed = $true
    if (Test-Path -LiteralPath $Current) { Copy-Item -LiteralPath $Current -Destination (Join-Path $Stage 'old-current.json'); $HadCurrent = $true }
    if ($HadBin) { Copy-Item -LiteralPath $Bin -Destination (Join-Path $Stage 'old-bin') -Recurse }
    [IO.Directory]::CreateDirectory($Bin) | Out-Null
    $LaunchScript = @'
# agents-status managed launcher
$ErrorActionPreference = 'Stop'
try {
    $Root = Split-Path -Parent $PSScriptRoot
    $Selector = Get-Content -LiteralPath (Join-Path $Root 'current.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($Selector.version -notmatch '^[0-9]+\.[0-9]+\.[0-9]+$') { throw 'current.json 오류' }
    $env:AGENTS_STATUS_INSTALL_ROOT = $Root
    $Executable = Join-Path $Root ('versions\' + $Selector.version + '\agents-status.exe')
    & $Executable @args
    exit $LASTEXITCODE
} catch { Write-Error $_; exit 1 }
'@
    $CommandScript = "@echo off`r`nrem agents-status managed launcher`r`npowershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File `"%~dp0launch.ps1`" %*`r`nexit /b %errorlevel%`r`n"
    [IO.File]::WriteAllText((Join-Path $Bin 'launch.ps1'), $LaunchScript, (New-Object Text.UTF8Encoding($true)))
    [IO.File]::WriteAllText($Launcher, $CommandScript, [Text.Encoding]::ASCII)
    $Next = Join-Path $Stage 'current.json'
    [IO.File]::WriteAllText($Next, ('{"version":"' + $Version + '"}'), $Utf8)
    Activate $Next $Current
    $Activated = $true
    Smoke $Launcher $Version
    if (-not $NoPath) {
        $Parts = @($OldPath -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ine $Bin.TrimEnd('\') })
        $NewPath = (@($Bin) + $Parts) -join ';'
        [Environment]::SetEnvironmentVariable('Path', $NewPath, 'User')
        $PathChanged = $true
        $env:Path = $Bin + ';' + $env:Path
    }
    $Success = $true
    Write-Host "설치 완료: $Version`n실행: $Launcher`n새 터미널에서 agents-status 사용. 기존 터미널은 사용자 PATH를 다시 읽어야 함."
    if ($Migrate -and $Existing -and $Existing.Source -ne $Launcher) { Write-Warning "기존 설치는 보존됨: $($Existing.Source). PATH 우선순위를 확인하고 필요하면 uv tool uninstall agents-status 실행. 소스는 삭제하지 않음." }
} finally {
    if (-not $Success) {
        if ($Activated) {
            if ($HadCurrent) { Activate (Join-Path $Stage 'old-current.json') $Current }
            elseif (Test-Path -LiteralPath $Current) { Remove-Item -LiteralPath $Current -Force }
        }
        if (Test-Path -LiteralPath (Join-Path $Stage 'old-bin')) {
            Remove-Item -LiteralPath $Bin -Recurse -Force
            Move-Item -LiteralPath (Join-Path $Stage 'old-bin') -Destination $Bin
        } elseif (-not $HadBin -and (Test-Path -LiteralPath $Bin)) { Remove-Item -LiteralPath $Bin -Recurse -Force }
        if ($Installed -and $Target -and (Test-Path -LiteralPath $Target)) { Remove-Item -LiteralPath $Target -Recurse -Force }
        if ($PathChanged) { [Environment]::SetEnvironmentVariable('Path', $OldPath, 'User') }
    }
    if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
    Remove-Item -LiteralPath $Lock -Force
}
