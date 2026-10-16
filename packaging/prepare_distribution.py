"""빈 공개 배포 저장소에만 안내 파일을 생성함. 비공개 소스는 전송하지 않음."""
import base64
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPOSITORY = "workingdad365/agents-status-releases"
BASE = f"https://api.github.com/repos/{REPOSITORY}"
TOKEN = os.environ["GH_TOKEN"]
if not TOKEN:
    raise SystemExit("RELEASE_TOKEN secret 필요")


def request(path: str, payload: dict | None = None):
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agents-status-release",
    }
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    with urlopen(Request(BASE + path, data=data, headers=headers, method="GET" if data is None else "PUT"), timeout=60) as response:
        return json.load(response)


repository = request("")
if repository["private"] or repository["full_name"] != REPOSITORY:
    raise SystemExit("공개 배포 저장소 확인 실패")
try:
    commits = request("/commits?per_page=1")
except HTTPError as error:
    detail = json.loads(error.read())
    if error.code != 409 or detail.get("message") != "Git Repository is empty.":
        raise SystemExit(f"배포 저장소 확인 실패: HTTP {error.code}") from None
    content = (
        "# agents-status 배포\n\n"
        "공개 실행 파일과 설치 스크립트는 Releases에서 내려받으세요.\n"
        "이 저장소는 배포 전용이며 애플리케이션 소스는 포함하지 않습니다.\n"
    ).encode("utf-8")
    request("/contents/README.md", {
        "message": "Initialize public distribution repository",
        "content": base64.b64encode(content).decode("ascii"),
    })
else:
    if not commits:
        raise SystemExit("빈 저장소 상태를 확인할 수 없음")
