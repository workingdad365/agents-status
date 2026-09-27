#Requires -Version 5.1
# AI 코딩 에이전트별 설치 여부와 현재 설정된 LLM 프로바이더를 조사하여 출력한다. (Windows PowerShell 용)
# agents_status.sh 와 동일한 동작을 수행함. 아무 것도 변경하지 않으며 조사 결과만 보여줌.
#
# 출력 형식
#   [ok]    도구이름: 실행파일 경로 | provider: 프로바이더
#   [cfg]   도구이름: 실행파일 없음, 설정 디렉토리만 남음 | provider: 프로바이더
#   [none]  도구이름: 미설치
#
# 실행 예: powershell -ExecutionPolicy Bypass -File .\agents_status.ps1
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

# 항목: Name(도구이름), Bins(실행파일 후보), Root(최상위디렉토리), Probe(프로바이더 조사 함수명)
$Targets = @(
    @{ Name = 'claude';      Bins = @('claude');                       Root = "$HOME\.claude";          Probe = 'Probe-Claude' }
    @{ Name = 'codex cli';   Bins = @('codex');                        Root = "$HOME\.codex";           Probe = 'Probe-Codex' }
    @{ Name = 'commandcode'; Bins = @('commandcode');                  Root = "$HOME\.commandcode";     Probe = 'Probe-CommandCode' }
    @{ Name = 'copilot CLI'; Bins = @('copilot');                      Root = "$HOME\.copilot";         Probe = 'Probe-Copilot' }
    @{ Name = 'opencode';    Bins = @('opencode');                     Root = "$HOME\.config\opencode"; Probe = 'Probe-OpenCode' }
    @{ Name = 'omp';         Bins = @('omp');                          Root = "$HOME\.omp";             Probe = 'Probe-Omp' }
    @{ Name = 'pi';          Bins = @('pi');                           Root = "$HOME\.pi";              Probe = 'Probe-Pi' }
    @{ Name = 'antigravity'; Bins = @('agy', 'antigravity', 'gemini'); Root = "$HOME\.gemini";          Probe = 'Probe-Antigravity' }
    @{ Name = 'crush';       Bins = @('crush');                        Root = "$HOME\.config\crush";    Probe = 'Probe-Crush' }
    @{ Name = 'hermes';      Bins = @('hermes');                       Root = "$HOME\.hermes";          Probe = 'Probe-Hermes' }
    @{ Name = 'openclaw';    Bins = @('openclaw');                     Root = "$HOME\.openclaw";        Probe = 'Probe-OpenClaw' }
)

function Read-Text([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return [System.IO.File]::ReadAllText($Path)
}

# JSON 텍스트에서 "key": "value" 형태의 첫 문자열 값을 꺼냄 (중첩 무시, 단순 검색)
function Get-JsonStr([string]$Text, [string]$Key) {
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    $m = [regex]::Match($Text, '"' + [regex]::Escape($Key) + '"\s*:\s*"([^"]*)"')
    if ($m.Success) { return $m.Groups[1].Value }
    return ''
}

# JSON 텍스트에서 "key": { 형태로 등장하는 최상위급 키 목록을 콤마로 이어 반환함
function Get-JsonObjKeys([string]$Text) {
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    $ms = [regex]::Matches($Text, '(?m)^[ \t]{2,4}"([a-zA-Z0-9_-]+)"\s*:\s*\{')
    return (($ms | ForEach-Object { $_.Groups[1].Value }) -join ',')
}

# YAML 텍스트에서 최상위 섹션 아래 들여쓴 자식 키의 값을 꺼냄 (2단계 한정 단순 검색)
function Get-YamlChild([string]$Text, [string]$Section, [string]$Child) {
    if ([string]::IsNullOrEmpty($Text)) { return '' }
    $inSection = $false
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match ('^' + [regex]::Escape($Section) + ':\s*$')) { $inSection = $true; continue }
        if ($inSection -and $line -match '^\S') { $inSection = $false }
        if ($inSection -and $line -match ('^\s+' + [regex]::Escape($Child) + ':\s*(.*)$')) { return $Matches[1].Trim() }
    }
    return ''
}

# "provider/model" 문자열에서 프로바이더 부분만 꺼냄
function Get-ProviderOf([string]$Ref) {
    if ([string]::IsNullOrEmpty($Ref)) { return '' }
    $i = $Ref.IndexOf('/')
    if ($i -ge 0) { return $Ref.Substring(0, $i) }
    return $Ref
}

function Probe-Claude {
    $text = Read-Text "$HOME\.claude\settings.json"
    # 환경변수 또는 settings.json 의 env 블록에서 대체 백엔드 지정 여부 확인
    $src = ((Get-ChildItem Env: | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join "`n") + "`n" + $text
    if ($src -match 'CLAUDE_CODE_USE_BEDROCK["=: ]+"?(1|true)') { return 'anthropic (AWS Bedrock)' }
    if ($src -match 'CLAUDE_CODE_USE_VERTEX["=: ]+"?(1|true)')  { return 'anthropic (Google Vertex AI)' }
    if ($src -match 'CLAUDE_CODE_USE_FOUNDRY["=: ]+"?(1|true)') { return 'anthropic (Azure AI Foundry)' }
    $m = [regex]::Match($src, 'ANTHROPIC_BASE_URL["=: ]+"?([^" ,\r\n]+)')
    if ($m.Success) { return "anthropic 호환 ($($m.Groups[1].Value))" }
    return 'anthropic'
}

function Probe-Codex {
    $cfg = Read-Text "$HOME\.codex\config.toml"
    if ($cfg) {
        $m = [regex]::Match($cfg, '(?m)^model_provider\s*=\s*"([^"]*)"')
        if ($m.Success) { return $m.Groups[1].Value }
    }
    $mode = Get-JsonStr (Read-Text "$HOME\.codex\auth.json") 'auth_mode'
    switch ($mode) {
        'chatgpt' { return 'openai (ChatGPT 로그인)' }
        'apikey'  { return 'openai (API key)' }
        ''        { return 'openai (미로그인)' }
        default   { return "openai ($mode)" }
    }
}

function Probe-CommandCode {
    $cfg = Read-Text "$HOME\.commandcode\config.json"
    $mp = Get-JsonStr $cfg 'provider'
    if (-not $mp) { $mp = Get-JsonStr $cfg 'modelProvider' }
    if (-not $mp) {
        $model = Get-JsonStr $cfg 'model'
        if ($model -match '^.+/.+$') { $mp = Get-ProviderOf $model }
    }
    if ($mp) { return $mp }
    return 'unknown'
}

function Probe-Copilot {
    return 'github-copilot'
}

function Probe-OpenCode {
    foreach ($f in @("$HOME\.config\opencode\opencode.jsonc", "$HOME\.config\opencode\opencode.json")) {
        $model = Get-JsonStr (Read-Text $f) 'model'
        if ($model) { return (Get-ProviderOf $model) }
    }
    # 설정에 model 이 없으면 최근 사용 모델(state/model.json)의 providerID 사용
    $stateHome = if ($env:XDG_STATE_HOME) { $env:XDG_STATE_HOME } else { "$HOME\.local\state" }
    foreach ($f in @("$stateHome\opencode\model.json", "$env:LOCALAPPDATA\opencode\model.json")) {
        $pid_ = Get-JsonStr (Read-Text $f) 'providerID'
        if ($pid_) { return "$pid_ (최근 사용)" }
    }
    return 'unknown'
}

function Probe-Omp {
    $ref = Get-YamlChild (Read-Text "$HOME\.omp\agent\config.yml") 'modelRoles' 'default'
    if ($ref) { return (Get-ProviderOf $ref) }
    return 'unknown'
}

function Probe-Pi {
    $p = Get-JsonStr (Read-Text "$HOME\.pi\agent\settings.json") 'defaultProvider'
    if ($p) { return $p }
    # 단일 인증은 자동 선택 후보로 추정하며 복수 인증은 확정하지 않음
    $keys = Get-JsonObjKeys (Read-Text "$HOME\.pi\agent\auth.json")
    if ($keys -like '*,*') { return "자동 선택 (인증 후보: $keys)" }
    if ($keys) { return "$keys (인증 기반 추정)" }
    return 'unknown'
}

function Probe-Antigravity {
    return 'google'
}

function Probe-Crush {
    $dataHome = if ($env:XDG_DATA_HOME) { $env:XDG_DATA_HOME } else { "$HOME\.local\share" }
    foreach ($f in @("$env:LOCALAPPDATA\crush\crush.json", "$dataHome\crush\crush.json")) {
        $text = Read-Text $f
        if (-not $text) { continue }
        # models.large.provider 를 우선 사용. 단순 검색이므로 "large" 블록 안의 첫 provider 를 취함
        $m = [regex]::Match(($text -replace '\r?\n', ''), '"large"\s*:\s*\{[^}]*\}')
        if ($m.Success) {
            $p = Get-JsonStr $m.Value 'provider'
            if ($p) { return $p }
        }
    }
    return 'unknown'
}

function Probe-Hermes {
    $p = Get-YamlChild (Read-Text "$HOME\.hermes\config.yaml") 'model' 'provider'
    if ($p) { return $p }
    return 'unknown'
}

function Probe-OpenClaw {
    $cfg = Read-Text "$HOME\.openclaw\openclaw.json"
    $ref = Get-JsonStr $cfg 'primary'
    # model 객체의 primary 가 없으면 문자열형 model 을 사용함.
    if (-not $ref) { $ref = Get-JsonStr $cfg 'model' }
    if ($ref) { return (Get-ProviderOf $ref) }
    return 'unknown'
}

$installed = 0
$cfgOnly = 0
$missing = 0

foreach ($t in $Targets) {
    $binPath = ''
    foreach ($b in $t.Bins) {
        $cmd = Get-Command $b -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cmd) { $binPath = $cmd.Source; break }
    }

    if ($binPath) {
        Write-Host "[ok]    $($t.Name): $binPath | provider: $(& $t.Probe)"
        $installed++
    }
    elseif (Test-Path -LiteralPath $t.Root -PathType Container) {
        Write-Host "[cfg]   $($t.Name): 실행파일 없음, 설정 디렉토리만 남음 ($($t.Root)) | provider: $(& $t.Probe)"
        $cfgOnly++
    }
    else {
        Write-Host "[none]  $($t.Name): 미설치"
        $missing++
    }
}

Write-Host ''
Write-Host "합계: 설치 ${installed}개, 설정만 남음 ${cfgOnly}개, 미설치 ${missing}개"
