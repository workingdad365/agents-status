#Requires -Version 5.1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$SourceRepo = 'workingdad365/agents-status'
$ReleaseRepo = 'workingdad365/agents-status-releases'

function Invoke-ReleaseCommand([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "명령 실행 실패 (종료 코드 ${LASTEXITCODE}): $Command $($Arguments -join ' ')"
    }
}

$TempDir = $null
$BackedUp = $false
$Committed = $false
$Pushed = $false
$Success = $false
$ExitCode = 1
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($args.Count -ne 0) { throw '인자 없이 .\release.ps1 실행. 패치 버전은 자동 증가함.' }
    foreach ($Tool in @('git', 'gh', 'uv')) {
        if (-not (Get-Command $Tool -CommandType Application -ErrorAction SilentlyContinue)) {
            throw "$Tool 설치 필요"
        }
    }
    if ((Invoke-ReleaseCommand git @('branch', '--show-current')) -cne 'main') {
        throw 'main 브랜치에서 실행 필요'
    }
    if (Invoke-ReleaseCommand git @('status', '--porcelain')) {
        throw '미커밋 변경 사항이 있음. 기능 변경을 먼저 커밋한 뒤 실행 필요'
    }
    Invoke-ReleaseCommand gh @('auth', 'status') | Out-Null
    if ((Invoke-ReleaseCommand gh @('repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner')) -cne $SourceRepo) {
        throw '소스 저장소 확인 실패'
    }
    $Secrets = @(Invoke-ReleaseCommand gh @('secret', 'list', '--repo', $SourceRepo, '--json', 'name', '--jq', '.[].name'))
    if ($Secrets -cnotcontains 'RELEASE_TOKEN') { throw '최초 1회 RELEASE_TOKEN 등록 필요' }
    Invoke-ReleaseCommand git @('fetch', '--no-tags', 'origin', 'main')
    & git merge-base --is-ancestor FETCH_HEAD HEAD
    if ($LASTEXITCODE -ne 0) { throw '원격 main 변경을 먼저 반영해야 함' }

    Invoke-ReleaseCommand uv @('sync', '--locked')
    $VersionPath = Join-Path $PSScriptRoot 'agents_status/__init__.py'
    $LockPath = Join-Path $PSScriptRoot 'uv.lock'
    $Source = [IO.File]::ReadAllText($VersionPath, [Text.Encoding]::UTF8)
    $Pattern = [regex]'(?m)^__version__ = "([0-9]+)\.([0-9]+)\.([0-9]+)"(?=\r?$)'
    $VersionMatches = $Pattern.Matches($Source)
    if ($VersionMatches.Count -ne 1) { throw '안정 버전 정의를 정확히 하나 찾을 수 없음' }
    $Groups = $VersionMatches[0].Groups
    $Version = '{0}.{1}.{2}' -f [int]$Groups[1].Value, [int]$Groups[2].Value, (1 + [int]$Groups[3].Value)
    $Tag = "v$Version"
    if (Invoke-ReleaseCommand git @('tag', '--list', $Tag)) {
        throw "로컬 태그 $Tag 이미 존재. 기존 배포 상태 확인 필요"
    }
    if (Invoke-ReleaseCommand git @('ls-remote', '--tags', 'origin', "refs/tags/$Tag")) {
        throw "원격 태그 $Tag 이미 존재. 기존 태그는 덮어쓰지 않음"
    }

    $TempDir = Join-Path ([IO.Path]::GetTempPath()) ('agents-status-release-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $TempDir | Out-Null
    Copy-Item -LiteralPath $VersionPath -Destination (Join-Path $TempDir 'version.py')
    Copy-Item -LiteralPath $LockPath -Destination (Join-Path $TempDir 'uv.lock')
    $BackedUp = $true

    Write-Host "배포 준비: $Tag"
    $Updated = $Pattern.Replace($Source, "__version__ = `"$Version`"")
    [IO.File]::WriteAllText($VersionPath, $Updated, (New-Object Text.UTF8Encoding($false)))
    Invoke-ReleaseCommand uv @('lock')
    Invoke-ReleaseCommand uv @('sync', '--locked')
    Invoke-ReleaseCommand uv @('run', '--no-sync', 'python', 'packaging/build_release.py', '--check-tag', $Tag)
    Invoke-ReleaseCommand uv @('run', '--no-sync', 'python', '-m', 'unittest', 'discover', '-v')
    Invoke-ReleaseCommand uv @('run', '--no-sync', 'agents-status', '--version')
    Invoke-ReleaseCommand uv @('run', '--no-sync', 'agents-status', '--self-check')

    Invoke-ReleaseCommand git @('add', 'agents_status/__init__.py', 'uv.lock')
    Invoke-ReleaseCommand git @('commit', '-m', "release: $Tag")
    $Committed = $true
    Invoke-ReleaseCommand git @('tag', '-a', $Tag, '-m', "agents-status $Version")
    $Commit = Invoke-ReleaseCommand git @('rev-parse', 'HEAD')
    Invoke-ReleaseCommand git @('push', '--atomic', 'origin', 'main', $Tag)
    $Pushed = $true

    # GitHub에서 해당 태그의 실행이 등록될 때까지 최대 1분 대기함.
    $RunId = $null
    for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
        $RunId = Invoke-ReleaseCommand gh @('run', 'list', '--repo', $SourceRepo, '--workflow', 'release.yml', '--event', 'push', '--branch', $Tag, '--commit', $Commit, '--limit', '1', '--json', 'databaseId', '--jq', '.[0].databaseId // empty')
        if ($RunId) { break }
        Start-Sleep -Seconds 2
    }
    if (-not $RunId) { throw '태그 CI 실행을 찾지 못함. GitHub Actions에서 확인 필요' }
    Invoke-ReleaseCommand gh @('run', 'watch', $RunId, '--repo', $SourceRepo, '--exit-status', '--interval', '30')
    $Release = (Invoke-ReleaseCommand gh @('release', 'view', $Tag, '--repo', $ReleaseRepo, '--json', 'tagName,isDraft,isPrerelease,assets,url')) -join "`n" | ConvertFrom-Json
    $Expected = @('linux-x86_64', 'linux-aarch64', 'macos-x86_64', 'macos-aarch64', 'windows-x86_64') |
        ForEach-Object { "agents-status-$Version-$_.zip" }
    $Expected += @('install.sh', 'install.ps1', 'SHA256SUMS')
    $Actual = @($Release.assets | ForEach-Object { $_.name })
    if ($Release.tagName -cne $Tag -or $Release.isDraft -or $Release.isPrerelease -or
        $Actual.Count -ne $Expected.Count -or
        @(Compare-Object -ReferenceObject $Expected -DifferenceObject $Actual -CaseSensitive).Count -ne 0) {
        throw '공개 릴리스 또는 배포 파일 검증 실패'
    }
    Write-Host "배포 완료: $($Release.url)"
    $Success = $true
    $ExitCode = 0
} catch {
    [Console]::Error.WriteLine("배포 중단: $($_.Exception.Message)")
} finally {
    try {
        if (-not $Success) {
            if (-not $Committed -and $BackedUp) {
                Copy-Item -LiteralPath (Join-Path $TempDir 'version.py') -Destination $VersionPath -Force
                Copy-Item -LiteralPath (Join-Path $TempDir 'uv.lock') -Destination $LockPath -Force
                Invoke-ReleaseCommand git @('restore', '--staged', '--', 'agents_status/__init__.py', 'uv.lock')
                [Console]::Error.WriteLine('검증 실패: 버전 및 잠금 파일을 실행 전 상태로 복구함.')
            } elseif ($Committed) {
                [Console]::Error.WriteLine('릴리스 커밋은 보존됨. 현재 배포를 확인하기 전 스크립트 재실행 금지.')
                if (-not $Pushed) {
                    [Console]::Error.WriteLine("태그 확인: git tag -l $Tag`n푸시 재개: git push --atomic origin main $Tag")
                } else {
                    [Console]::Error.WriteLine("CI 확인: gh run list --repo $SourceRepo --branch $Tag")
                }
            }
        }
    } finally {
        try {
            if ($TempDir -and (Test-Path -LiteralPath $TempDir)) {
                Remove-Item -LiteralPath $TempDir -Recurse -Force
            }
        } finally { Pop-Location }
    }
}
exit $ExitCode
