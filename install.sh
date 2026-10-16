#!/bin/sh
# Python/uv 없이 사용자 영역에 설치함. 기존 버전은 롤백용으로 보존.
set -eu
umask 077
REPOSITORY=https://github.com/workingdad365/agents-status-releases
version=
migrate=0
register_path=1
while [ "$#" -gt 0 ]; do
    case "$1" in
        --version) [ "$#" -ge 2 ] || { echo '--version 값 필요' >&2; exit 1; }; version=$2; shift 2 ;;
        --migrate) migrate=1; shift ;;
        --no-path) register_path=0; shift ;;
        *) echo '사용법: sh install.sh [--version 1.0.12] [--migrate] [--no-path]' >&2; exit 1 ;;
    esac
done
fail() { printf '%s\n' "$*" >&2; exit 1; }
valid_version() { printf '%s\n' "$1" | LC_ALL=C grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; }
[ -z "$version" ] || valid_version "$version" || fail '안정 버전 x.y.z만 허용됨'
case $(uname -s) in
    Linux) platform=linux; root=$HOME/.local/share/agents-status ;;
    Darwin) platform=macos; root=$HOME/Library/Application\ Support/agents-status ;;
    *) fail 'Linux/WSL/macOS만 지원함. Windows는 install.ps1 사용' ;;
esac
case $(uname -m) in
    x86_64|amd64) arch=x86_64 ;;
    arm64|aarch64) arch=aarch64 ;;
    *) fail '지원하지 않는 CPU 아키텍처' ;;
esac
for utility in curl unzip zipinfo awk grep sort uniq mktemp; do
    command -v "$utility" >/dev/null 2>&1 || fail "$utility 설치 필요"
done
if command -v sha256sum >/dev/null 2>&1; then hash_command=sha256sum
elif command -v shasum >/dev/null 2>&1; then hash_command=shasum
else fail 'sha256sum 또는 shasum 설치 필요'; fi
launcher=$HOME/.local/bin/agents-status
existing=$(command -v agents-status 2>/dev/null || true)
uv_tool=
remove_uv=0
if command -v uv >/dev/null 2>&1; then
    uv_tools=$(uv tool dir 2>/dev/null || true)
    [ -z "$uv_tools" ] || uv_tool=$uv_tools/agents-status
fi
is_uv_launcher() {
    [ -n "$uv_tool" ] && [ ! -L "$uv_tool" ] &&
        [ -f "$uv_tool/uv-receipt.toml" ] &&
        [ "$1" -ef "$uv_tool/bin/agents-status" ]
}
check_existing() {
    if is_uv_launcher "$1"; then
        remove_uv=1
    elif [ "$migrate" -ne 1 ]; then
        fail "기존 실행기 감지: $1. uv 설치로 확인되지 않아 자동 제거하지 않음. --migrate로 명시적 전환 가능."
    fi
}
if [ -n "$existing" ] && [ "$existing" != "$launcher" ]; then
    check_existing "$existing"
fi
if [ -e "$launcher" ] || [ -L "$launcher" ]; then
    if ! grep -q '^# agents-status managed launcher$' "$launcher" 2>/dev/null; then
        check_existing "$launcher"
    fi
fi
[ ! -L "$root" ] || fail '설치 루트 심볼릭 링크 거부'
mkdir -p "$root/versions" "$HOME/.local/bin"
[ ! -L "$root/versions" ] || fail 'versions 심볼릭 링크 거부'
mkdir "$root/.update-lock" 2>/dev/null || fail "다른 설치/업데이트 진행 중. 모두 종료한 뒤 남은 잠금만 삭제: $root/.update-lock"
stage=
activated=0
had_current=0
had_launcher=0
success=0
installed=0
restore_uv=0
uv_external=
cleanup() {
    result=$?
    trap - EXIT HUP INT TERM
    if [ "$success" -ne 1 ] && [ -n "$stage" ]; then
        if [ "$restore_uv" -eq 1 ]; then
            mkdir -p "$uv_tool"
            cp -RP "$stage/old-uv-tool/." "$uv_tool/"
            if [ -n "$uv_external" ]; then cp -P "$stage/old-uv-launcher" "$uv_external"; fi
        fi
        if [ "$activated" -eq 1 ]; then
            if [ "$had_current" -eq 1 ]; then mv -f "$stage/old-current.json" "$root/current.json"; else rm -f "$root/current.json"; fi
        fi
        if [ "$had_launcher" -eq 1 ]; then
            mv -f "$stage/old-launcher" "$launcher"
        elif [ -f "$stage/launcher-written" ]; then rm -f "$launcher"; fi
        if [ "$installed" -eq 1 ]; then rm -rf "$root/versions/$version"; fi
    fi
    [ -z "$stage" ] || rm -rf "$stage"
    rmdir "$root/.update-lock" 2>/dev/null || true
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM
stage=$(mktemp -d "$root/.install.XXXXXXXX")
fetch() { curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 --retry 2 --connect-timeout 20 --max-time 600 "$1" -o "$2"; }
if [ -z "$version" ]; then
    resolved=$(curl --fail --silent --show-error --head --location --proto '=https' --proto-redir '=https' --tlsv1.2 --connect-timeout 20 --max-time 60 -o /dev/null -w '%{url_effective}' "$REPOSITORY/releases/latest")
    case "$resolved" in "$REPOSITORY/releases/tag/v"*) version=${resolved#"$REPOSITORY/releases/tag/v"} ;; *) fail '최신 안정 릴리스 주소 확인 실패' ;; esac
    valid_version "$version" || fail '최신 릴리스 버전 형식 오류'
fi
asset=agents-status-$version-$platform-$arch.zip
base=$REPOSITORY/releases/download/v$version
fetch "$base/SHA256SUMS" "$stage/SHA256SUMS"
fetch "$base/$asset" "$stage/archive.zip"
expected=$(awk -v name="$asset" '$2 == name {print $1}' "$stage/SHA256SUMS")
[ "${#expected}" -eq 64 ] || fail '체크섬 항목이 없거나 중복됨'
case "$expected" in *[!0-9a-fA-F]*) fail '잘못된 체크섬' ;; esac
if [ "$hash_command" = sha256sum ]; then actual=$(sha256sum "$stage/archive.zip"); else actual=$(shasum -a 256 "$stage/archive.zip"); fi
actual=${actual%% *}
[ "$actual" = "$expected" ] || fail 'SHA256 불일치'
zipinfo -1 "$stage/archive.zip" > "$stage/names"
[ -s "$stage/names" ] || fail '빈 아카이브'
# 빌드가 허용하는 경로 문자만 허용하며 링크/장치/중복 경로는 거부.
LC_ALL=C awk '
    $0 !~ /^agents-status\/[A-Za-z0-9_.\/+@-]+$/ {exit 1}
    /(^|\/)\.\.?($|\/)/ || /\/\// {exit 1}
    $0 != "agents-status/agents-status" && $0 !~ /^agents-status\/_internal\// {exit 1}
' "$stage/names" || fail '안전하지 않은 아카이브 경로'
[ -z "$(LC_ALL=C sort "$stage/names" | uniq -d)" ] || fail '중복 아카이브 경로'
zipinfo -l "$stage/archive.zip" > "$stage/details"
LC_ALL=C awk '/^[bcdlps-][rwxstST-]/ {if (substr($0,1,1) != "-") exit 1; count++} END {if (count == 0) exit 1}' "$stage/details" || fail '일반 파일 이외 아카이브 항목 거부'
# ZIP 목록의 줄 수와 중앙 디렉터리 항목 수를 비교해 개행 이름도 거부.
listed=$(wc -l < "$stage/names" | tr -d ' ')
entries=$(LC_ALL=C awk '/^Zip file size:/ {print $NF}' "$stage/details")
[ "$listed" = "$entries" ] || fail '아카이브 항목 수 불일치'
[ "$entries" -le 50000 ] || fail '아카이브 항목 수 초과'
LC_ALL=C awk '/^[0-9]+ files?,/ {if ($3 > 2147483648) exit 1; found=1} END {if (!found) exit 1}' "$stage/details" || fail '아카이브 압축 해제 크기 초과'
unzip -t -P '' "$stage/archive.zip" >/dev/null || fail '손상되거나 암호화된 아카이브'
mkdir "$stage/extracted"
unzip -q -P '' "$stage/archive.zip" -d "$stage/extracted"
payload=$stage/extracted/agents-status
[ -f "$payload/agents-status" ] && [ -d "$payload/_internal" ] || fail '잘못된 onedir 구조'
chmod 755 "$payload/agents-status"
reported=$("$payload/agents-status" --version)
[ "$(printf '%s\n' "$reported" | awk '{print $NF}')" = "$version" ] || fail '실행 파일 버전 불일치'
"$payload/agents-status" --self-check
[ ! -e "$root/versions/$version" ] && [ ! -L "$root/versions/$version" ] || fail "이미 설치된 버전: $version (덮어쓰지 않음)"
if [ -e "$root/current.json" ]; then cp "$root/current.json" "$stage/old-current.json"; had_current=1; fi
if [ -e "$launcher" ] || [ -L "$launcher" ]; then cp -P "$launcher" "$stage/old-launcher"; had_launcher=1; fi
if [ "$remove_uv" -eq 1 ]; then
    # 새 패키지 검증 후 제거하며, 전환 실패 시 uv 환경과 실행기를 복구함.
    cp -RP "$uv_tool" "$stage/old-uv-tool"
    if [ -n "$existing" ] && [ "$existing" != "$launcher" ] && is_uv_launcher "$existing"; then
        cp -P "$existing" "$stage/old-uv-launcher"
        uv_external=$existing
    fi
    restore_uv=1
    printf '기존 uv 설치 제거 후 릴리스 설치로 전환함.\n'
    uv tool uninstall agents-status || fail '기존 uv 설치 제거 실패'
    [ ! -f "$uv_tool/uv-receipt.toml" ] || fail 'uv 설치 등록이 남아 있어 전환 중단'
    if [ -n "$uv_external" ] && { [ -e "$uv_external" ] || [ -L "$uv_external" ]; }; then
        fail '기존 uv 실행기가 남아 있어 전환 중단'
    fi
fi
mv "$payload" "$root/versions/$version"
installed=1
cat > "$stage/launcher" <<'LAUNCHER'
#!/bin/sh
# agents-status managed launcher
set -eu
case $(uname -s) in
    Darwin) root=$HOME/Library/Application\ Support/agents-status ;;
    Linux) root=$HOME/.local/share/agents-status ;;
    *) echo '지원하지 않는 운영체제' >&2; exit 1 ;;
esac
# 선택자는 실행 문자열이 아니라 숫자 버전만 추출함.
version=$(LC_ALL=C awk 'BEGIN {RS="\0"} /^[[:space:]]*\{[[:space:]]*"version"[[:space:]]*:[[:space:]]*"[0-9]+\.[0-9]+\.[0-9]+"[[:space:]]*\}[[:space:]]*$/ {gsub(/[^0-9.]/, ""); print}' "$root/current.json")
[ -n "$version" ] || { echo 'current.json 오류: 재설치 필요' >&2; exit 1; }
export AGENTS_STATUS_INSTALL_ROOT="$root"
exec "$root/versions/$version/agents-status" "$@"
LAUNCHER
chmod 755 "$stage/launcher"
printf '{"version":"%s"}\n' "$version" > "$stage/current.json"
mv -f "$stage/launcher" "$launcher"
: > "$stage/launcher-written"
activated=1
mv -f "$stage/current.json" "$root/current.json"
reported=$("$launcher" --version)
[ "$(printf '%s\n' "$reported" | awk '{print $NF}')" = "$version" ] || fail '실행기 버전 확인 실패'
"$launcher" --self-check
if [ "$migrate" -eq 1 ] && [ "$had_launcher" -eq 1 ] && [ "$remove_uv" -eq 0 ]; then
    backup=$root/legacy-launcher-$(date +%s)
    cp -P "$stage/old-launcher" "$backup"
    printf '기존 실행기 보존: %s (소스/uv 환경은 유지됨)\n' "$backup"
fi
if [ "$register_path" -eq 1 ]; then
    for profile in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zprofile" "$HOME/.zshrc"; do
        if ! grep -q '^# agents-status PATH$' "$profile" 2>/dev/null; then
            cat >> "$profile" <<'PROFILE'

# agents-status PATH
case "$PATH" in
    "$HOME/.local/bin"|"$HOME/.local/bin":*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
# agents-status PATH end
PROFILE
        fi
    done
fi
success=1
printf '설치 완료: %s\n실행: "%s"\n새 터미널에서 agents-status 사용. fish 등은 ~/.local/bin을 PATH에 추가.\n' "$version" "$launcher"
if [ "$migrate" -eq 1 ] && [ "$remove_uv" -eq 0 ]; then printf '기존 uv 도구를 나중에 제거하면 새 실행기도 지워질 수 있음. 제거 후 설치 스크립트 재실행 대신 실행기를 먼저 보존/복원 필요.\n'; fi
