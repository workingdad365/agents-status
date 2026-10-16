#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
SOURCE_REPO=workingdad365/agents-status
RELEASE_REPO=workingdad365/agents-status-releases

fail() { printf '배포 중단: %s\n' "$*" >&2; exit 1; }
[[ $# -eq 0 ]] || fail '인자 없이 ./release.sh 실행. 패치 버전은 자동 증가함.'
for tool in git gh uv; do
    command -v "$tool" >/dev/null || fail "$tool 설치 필요"
done
[[ "$(git branch --show-current)" == main ]] || fail 'main 브랜치에서 실행 필요'
[[ -z "$(git status --porcelain)" ]] || fail '미커밋 변경 사항이 있음. 기능 변경을 먼저 커밋한 뒤 실행 필요'
gh auth status >/dev/null 2>&1 || fail 'gh auth login 필요'
[[ "$(gh repo view --json nameWithOwner --jq .nameWithOwner)" == "$SOURCE_REPO" ]] || fail '소스 저장소 확인 실패'
[[ "$(gh secret list --repo "$SOURCE_REPO" --json name --jq 'any(.[]; .name == "RELEASE_TOKEN")')" == true ]] || fail '최초 1회 RELEASE_TOKEN 등록 필요'
git fetch --no-tags origin main
git merge-base --is-ancestor FETCH_HEAD HEAD || fail '원격 main 변경을 먼저 반영해야 함'

uv sync --locked
VERSION=$(uv run --no-sync python -c 'import re; from agents_status import __version__; assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", __version__), "안정 버전 형식 필요"; major, minor, patch = map(int, __version__.split(".")); print(f"{major}.{minor}.{patch + 1}")')
TAG="v$VERSION"
if git show-ref --verify --quiet "refs/tags/$TAG"; then
    fail "로컬 태그 $TAG 이미 존재. 기존 배포 상태 확인 필요"
fi
[[ -z "$(git ls-remote --tags origin "refs/tags/$TAG")" ]] || fail "원격 태그 $TAG 이미 존재. 기존 태그는 덮어쓰지 않음"

TEMP_DIR=$(mktemp -d)
COMMITTED=0
PUSHED=0
BACKED_UP=0
cleanup() {
    status=$?
    trap - EXIT
    if [[ $status -ne 0 ]]; then
        if [[ $COMMITTED -eq 0 && $BACKED_UP -eq 1 ]]; then
            cp "$TEMP_DIR/version.py" agents_status/__init__.py
            cp "$TEMP_DIR/uv.lock" uv.lock
            git restore --staged -- agents_status/__init__.py uv.lock
            printf '검증 실패: 버전 및 잠금 파일을 실행 전 상태로 복구함.\n' >&2
        elif [[ $COMMITTED -eq 1 ]]; then
            printf '릴리스 커밋은 보존됨. 현재 배포를 확인하기 전 스크립트 재실행 금지.\n' >&2
            if [[ $PUSHED -eq 0 ]]; then
                printf '태그 확인: git tag -l %s\n푸시 재개: git push --atomic origin main %s\n' "$TAG" "$TAG" >&2
            else
                printf 'CI 확인: gh run list --repo %s --branch %s\n' "$SOURCE_REPO" "$TAG" >&2
            fi
        fi
    fi
    rm -rf -- "$TEMP_DIR"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
cp agents_status/__init__.py "$TEMP_DIR/version.py"
cp uv.lock "$TEMP_DIR/uv.lock"
BACKED_UP=1

printf '배포 준비: %s\n' "$TAG"
uv run --no-sync python - "$VERSION" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path("agents_status/__init__.py")
source = path.read_text(encoding="utf-8")
updated, count = re.subn(
    r'^__version__ = "[0-9]+\.[0-9]+\.[0-9]+"$',
    f'__version__ = "{sys.argv[1]}"', source, flags=re.MULTILINE,
)
if count != 1:
    raise SystemExit("버전 정의를 정확히 하나 찾을 수 없음")
path.write_text(updated, encoding="utf-8")
PY
uv lock
uv sync --locked
uv run --no-sync python packaging/build_release.py --check-tag "$TAG"
uv run --no-sync python -m unittest discover -v
uv run --no-sync agents-status --version
uv run --no-sync agents-status --self-check

git add agents_status/__init__.py uv.lock
git commit -m "release: $TAG"
COMMITTED=1
git tag -a "$TAG" -m "agents-status $VERSION"
COMMIT=$(git rev-parse HEAD)
git push --atomic origin main "$TAG"
PUSHED=1

# GitHub에서 해당 태그의 실행이 등록될 때까지 최대 1분 대기함.
RUN_ID=''
for attempt in {1..30}; do
    RUN_ID=$(gh run list --repo "$SOURCE_REPO" --workflow release.yml --event push --branch "$TAG" --commit "$COMMIT" --limit 1 --json databaseId --jq '.[0].databaseId // empty')
    [[ -z "$RUN_ID" ]] || break
    sleep 2
done
[[ -n "$RUN_ID" ]] || fail '태그 CI 실행을 찾지 못함. GitHub Actions에서 확인 필요'
gh run watch "$RUN_ID" --repo "$SOURCE_REPO" --exit-status --interval 30
gh release view "$TAG" --repo "$RELEASE_REPO" --json tagName,isDraft,isPrerelease,assets,url > "$TEMP_DIR/release.json"
uv run --no-sync python - "$VERSION" "$TEMP_DIR/release.json" <<'PY'
import json
import pathlib
import sys

version = sys.argv[1]
release = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
expected = {f"agents-status-{version}-{target}.zip" for target in (
    "linux-x86_64", "linux-aarch64", "macos-x86_64", "macos-aarch64", "windows-x86_64",
)} | {"install.sh", "install.ps1", "SHA256SUMS"}
if (release["tagName"] != f"v{version}" or release["isDraft"] or release["isPrerelease"]
        or {asset["name"] for asset in release["assets"]} != expected):
    raise SystemExit("공개 릴리스 또는 배포 파일 검증 실패")
print(f"배포 완료: {release['url']}")
PY
