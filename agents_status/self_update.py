"""동의한 관리형 설치만 갱신하며 실패 시 이전 실행 파일을 보존한다."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import __version__

REPOSITORY = "workingdad365/agents-status-releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_BASE = f"https://github.com/{REPOSITORY}/releases"
CACHE_SECONDS = 6 * 60 * 60
NETWORK_TIMEOUT = 20
DOWNLOAD_SECONDS = 300
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 20000
STARTUP_SECONDS = 30
SMOKE_SECONDS = 30
_READY_PATH = "AGENTS_STATUS_UPDATE_READY"
_READY_TOKEN = "AGENTS_STATUS_UPDATE_TOKEN"
_VERSION = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", re.ASCII)


class UpdateError(Exception):
    """현재 설치를 보존하는 업데이트 오류."""


class NetworkError(UpdateError):
    """오프라인, 연결 제한 또는 GitHub 응답 오류."""


class UnsupportedInstallError(UpdateError):
    """소스/uv 설치는 공식 설치 프로그램으로 먼저 이전해야 한다."""


class UpdateBusyError(UpdateError):
    """다른 설치 작업 또는 확인이 필요한 잠금이 존재한다."""


class _Interrupted(BaseException):
    def __init__(self, number: int):
        self.number = number


def _interrupt_update(number, frame):
    raise _Interrupted(number)


@dataclass(frozen=True)
class Release:
    version: str
    notes_url: str
    asset_url: str
    sha256_url: str


@dataclass
class PreparedUpdate:
    release: Release
    root: Path
    staging: Path
    previous_version: str
    token: str = field(repr=False)
    _lock_owned: bool = field(default=True, repr=False)
    _closed: bool = field(default=False, repr=False)


def _version_tuple(version: str) -> tuple[int, int, int]:
    if not isinstance(version, str) or len(version) > 32 or not _VERSION.fullmatch(version):
        raise UpdateError("릴리스 버전은 정확한 x.y.z 형식이어야 합니다.")
    return tuple(int(part) for part in version.split("."))


def _platform_asset(version: str) -> str:
    systems = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}
    machines = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
    system = systems.get(platform.system())
    machine = machines.get(platform.machine().lower())
    if system == "windows":
        # ARM 호스트의 x64 에뮬레이션에서도 실행 중인 배포판과 같은 아키텍처를 선택한다.
        machine = {"win-amd64": "x86_64", "win-arm64": "aarch64"}.get(sysconfig.get_platform().lower(), machine)
    if system is None or machine is None:
        raise UpdateError("이 운영체제/CPU의 배포 파일이 없습니다.")
    return f"agents-status-{version}-{system}-{machine}.zip"


def _default_root() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return base / "agents-status"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "agents-status"
    return Path.home() / ".local" / "share" / "agents-status"


def _executable_name() -> str:
    return "agents-status.exe" if sys.platform == "win32" else "agents-status"


def _read_current(root: Path) -> str:
    selector = root / "current.json"
    if selector.is_symlink() or selector.stat().st_size > 1024:
        raise UpdateError("설치 버전 선택 파일이 안전하지 않습니다.")
    try:
        value = json.loads(selector.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"version"}:
            raise ValueError
        _version_tuple(value["version"])
        return value["version"]
    except (ValueError, TypeError) as error:
        raise UpdateError("설치 버전 선택 파일이 올바르지 않습니다.") from error


def installation_root() -> Path | None:
    """공식 기본 경로의 frozen 설치만 반환한다. 환경 변수만으로 신뢰하지 않는다."""
    if not getattr(sys, "frozen", False):
        return None
    root = _default_root().absolute()
    executable = root / "versions" / __version__ / _executable_name()
    try:
        for candidate in (root, root / "versions", executable.parent, executable):
            if candidate.is_symlink():
                return None
        if not executable.is_file() or Path(sys.executable).absolute() != executable:
            return None
        _read_current(root)
        return root
    except (OSError, UpdateError):
        return None


def _cache_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Cache"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return base / "agents-status" / "release.json"


def _atomic_json(path: Path, value: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".agents-status-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _trusted_url(url: str, *, redirected: bool = False) -> None:
    if not isinstance(url, str):
        raise UpdateError("릴리스 주소가 올바르지 않습니다.")
    try:
        parsed = urllib.parse.urlsplit(url)
        allowed = parsed.scheme == "https" and not parsed.username and not parsed.password and parsed.port in (None, 443) and not parsed.fragment
    except ValueError as error:
        raise UpdateError("릴리스 주소가 올바르지 않습니다.") from error
    if not allowed:
        raise UpdateError("릴리스 다운로드는 신뢰한 HTTPS 주소만 허용합니다.")
    if parsed.hostname == "api.github.com" and parsed.path == f"/repos/{REPOSITORY}/releases/latest" and not parsed.query:
        return
    if parsed.hostname == "github.com" and parsed.path.startswith(f"/{REPOSITORY}/releases/download/") and not parsed.query:
        return
    if redirected and parsed.hostname in {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}:
        return
    raise UpdateError("공식 GitHub 저장소의 다운로드 주소가 아닙니다.")


class _TrustedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        _trusted_url(newurl, redirected=True)
        return super().redirect_request(request, response, code, message, headers, newurl)


def _download(url: str, destination: Path | None, limit: int) -> bytes:
    _trusted_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": f"agents-status/{__version__}", "Accept": "application/vnd.github+json" if url == API_URL else "application/octet-stream"})
    opener = urllib.request.build_opener(_TrustedRedirect())
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    data = bytearray()
    stream = None
    try:
        with opener.open(request, timeout=NETWORK_TIMEOUT) as response:
            _trusted_url(response.geturl(), redirected=True)
            length = response.headers.get("Content-Length")
            if length is not None and (len(length) > 20 or not length.isascii() or not length.isdecimal() or int(length) > limit):
                raise UpdateError("릴리스 다운로드 크기가 제한을 초과합니다.")
            if destination is not None:
                stream = destination.open("xb")
            size = 0
            while True:
                if time.monotonic() > deadline:
                    raise NetworkError("릴리스 다운로드 시간이 초과되었습니다.")
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise UpdateError("릴리스 다운로드 크기가 제한을 초과합니다.")
                if stream is not None:
                    stream.write(chunk)
                else:
                    data.extend(chunk)
        return bytes(data)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise NetworkError(f"릴리스에 연결할 수 없습니다: {error}") from error
    finally:
        if stream is not None:
            stream.close()


def _parse_release(metadata: object) -> Release:
    if not isinstance(metadata, dict) or metadata.get("draft") is not False or metadata.get("prerelease") is not False:
        raise UpdateError("안정 릴리스 메타데이터가 아닙니다.")
    tag = metadata.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError("릴리스 태그가 올바르지 않습니다.")
    version = tag[1:]
    _version_tuple(version)
    name = _platform_asset(version)
    notes = f"{RELEASE_BASE}/tag/v{version}"
    if metadata.get("html_url") != notes:
        raise UpdateError("릴리스 안내 주소가 공식 저장소와 다릅니다.")
    assets = metadata.get("assets")
    if not isinstance(assets, list):
        raise UpdateError("릴리스 배포 파일 목록이 없습니다.")
    urls = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            raise UpdateError("릴리스 배포 파일 정보가 올바르지 않습니다.")
        if asset["name"] in (name, "SHA256SUMS"):
            if asset["name"] in urls:
                raise UpdateError("릴리스 배포 파일 이름이 중복되었습니다.")
            expected = f"{RELEASE_BASE}/download/v{version}/{asset['name']}"
            if asset.get("browser_download_url") != expected:
                raise UpdateError("릴리스 배포 파일 주소가 공식 저장소와 다릅니다.")
            urls[asset["name"]] = expected
    if name not in urls or "SHA256SUMS" not in urls:
        raise UpdateError("현재 운영체제의 배포 파일 또는 SHA256SUMS가 없습니다.")
    return Release(version, notes, urls[name], urls["SHA256SUMS"])


def _validate_release(release: Release) -> None:
    _version_tuple(release.version)
    expected_base = f"{RELEASE_BASE}/download/v{release.version}/"
    if (release.notes_url != f"{RELEASE_BASE}/tag/v{release.version}"
            or release.asset_url != expected_base + _platform_asset(release.version)
            or release.sha256_url != expected_base + "SHA256SUMS"):
        raise UpdateError("공식 릴리스의 주소와 배포 파일만 설치할 수 있습니다.")


def check_for_update(force: bool = False) -> Release | None:
    """네트워크 실패는 NetworkError로 알리며 설치 파일은 변경하지 않는다."""
    cache = _cache_path()
    metadata = None
    if not force:
        try:
            if cache.stat().st_size <= 1024 * 1024:
                cached = json.loads(cache.read_text(encoding="utf-8"))
                age = time.time() - cached["checked_at"]
                if 0 <= age < CACHE_SECONDS:
                    _parse_release(cached["release"])
                    metadata = cached["release"]
        except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError, UpdateError):
            pass
    if metadata is None:
        try:
            metadata = json.loads(_download(API_URL, None, 1024 * 1024))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise UpdateError("GitHub 릴리스 응답이 JSON 형식이 아닙니다.") from error
        release = _parse_release(metadata)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(cache, {"checked_at": time.time(), "release": metadata})
        except OSError:
            pass
    else:
        release = _parse_release(metadata)
    return release if _version_tuple(release.version) > _version_tuple(__version__) else None


def _checksum(content: bytes, filename: str) -> str:
    try:
        text = content.decode("utf-8")
    except UnicodeError as error:
        raise UpdateError("SHA256SUMS 인코딩이 올바르지 않습니다.") from error
    found = []
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *]([^\r\n]+)", line)
        if not match:
            raise UpdateError("SHA256SUMS 형식이 올바르지 않습니다.")
        if match[2] == filename:
            found.append(match[1].lower())
    if len(found) != 1:
        raise UpdateError("배포 파일의 SHA256 값이 없거나 중복되었습니다.")
    return found[0]


def _extract_archive(archive: Path, destination: Path) -> Path:
    seen = set()
    total = 0
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_MEMBERS:
                raise UpdateError("배포 파일의 항목 수가 제한을 초과합니다.")
            for member in members:
                # Windows zipfile의 역슬래시 정규화 전 원본 이름을 검사함.
                name = member.orig_filename
                parts = PurePosixPath(name).parts
                mode = member.external_attr >> 16
                kind = stat.S_IFMT(mode)
                if (not name or "\x00" in member.orig_filename or "\\" in name or name.startswith("/")
                        or not parts or parts[0] != "agents-status"
                        or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))
                        or any(":" in part or part.rstrip(" .") != part for part in parts)
                        or any(part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))} for part in parts)
                        or kind not in (0, stat.S_IFREG, stat.S_IFDIR)
                        or member.flag_bits & 1):
                    raise UpdateError("배포 ZIP에 안전하지 않은 경로 또는 링크가 있습니다.")
                normalized = "/".join(parts).casefold()
                if normalized in seen:
                    raise UpdateError("배포 ZIP에 중복 경로가 있습니다.")
                seen.add(normalized)
                total += member.file_size
                if member.file_size > MAX_MEMBER_BYTES or total > MAX_EXPANDED_BYTES:
                    raise UpdateError("압축 해제 크기가 제한을 초과합니다.")
                target = destination.joinpath(*parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if len(parts) < 2:
                    raise UpdateError("배포 ZIP 최상위 폴더가 올바르지 않습니다.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("xb") as output:
                    copied = 0
                    while chunk := source.read(64 * 1024):
                        copied += len(chunk)
                        if copied > member.file_size:
                            raise UpdateError("ZIP 항목 크기가 선언된 값과 다릅니다.")
                        output.write(chunk)
                target.chmod(0o755 if mode & 0o111 else 0o644)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, OSError) as error:
        raise UpdateError(f"배포 ZIP을 안전하게 해제하지 못했습니다: {error}") from error
    payload = destination / "agents-status"
    executable = payload / _executable_name()
    if not executable.is_file() or not (payload / "_internal").is_dir():
        raise UpdateError("배포 ZIP에 실행 파일 또는 _internal 폴더가 없습니다.")
    if sys.platform != "win32":
        executable.chmod(0o755)
    return payload


def _smoke(executable: Path, version: str) -> None:
    environment = os.environ.copy()
    environment.pop(_READY_PATH, None)
    environment.pop(_READY_TOKEN, None)
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    try:
        for argument in ("--version", "--self-check"):
            # 파일로 받으면 손상된 바이너리의 무제한 stdout이 메모리를 소진하지 않는다.
            with tempfile.TemporaryFile() as output:
                result = subprocess.run([str(executable), argument], cwd=executable.parent,
                                        env=environment, stdout=output, stderr=subprocess.STDOUT,
                                        timeout=SMOKE_SECONDS, check=False)
                output.seek(0)
                text = output.read(4096).decode("utf-8", errors="replace").strip()
            if result.returncode != 0 or (argument == "--version" and text not in (version, f"agents-status {version}")):
                raise UpdateError(f"새 실행 파일의 {argument} 검사에 실패했습니다.")
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"새 실행 파일을 검사하지 못했습니다: {error}") from error


def _acquire_lock(root: Path) -> None:
    try:
        (root / ".update-lock").mkdir(mode=0o700)
    except FileExistsError as error:
        raise UpdateBusyError("다른 설치/업데이트 작업이 진행 중입니다. 중단된 작업이면 모든 agents-status를 종료한 뒤 설치 폴더의 .update-lock을 제거하세요.") from error


def _clean_prepared(prepared: PreparedUpdate) -> None:
    if prepared._closed:
        return
    shutil.rmtree(prepared.staging, ignore_errors=True)
    if prepared._lock_owned:
        try:
            (prepared.root / ".update-lock").rmdir()
        except FileNotFoundError:
            pass
        prepared._lock_owned = False
    prepared._closed = True


def discard_prepared_update(prepared: PreparedUpdate) -> None:
    """준비 뒤 사용자가 취소하거나 UI가 종료되면 잠금/임시 파일을 반환한다."""
    _clean_prepared(prepared)


def prepare_update(release: Release) -> PreparedUpdate:
    root = installation_root()
    if root is None:
        raise UnsupportedInstallError("소스/uv 설치는 직접 변경하지 않습니다. 공식 GitHub 릴리스의 설치 프로그램을 --migrate(Windows: -Migrate)로 실행해 관리형 설치로 이전하세요.")
    _validate_release(release)
    if _version_tuple(release.version) <= _version_tuple(__version__):
        raise UpdateError("현재 버전 이하의 릴리스는 설치하지 않습니다.")
    locked = False
    staging = None
    try:
        _acquire_lock(root)
        locked = True
        previous = _read_current(root)
        if previous != __version__:
            raise UpdateError("다른 버전이 이미 활성화되었습니다. 앱을 다시 시작하세요.")
        if (root / "versions" / release.version).exists() or (root / "versions" / release.version).is_symlink():
            raise UpdateError("대상 버전 폴더가 이미 존재합니다. 기존 폴더를 덮어쓰지 않습니다.")
        staging_root = root / ".staging"
        if staging_root.is_symlink():
            raise UpdateError("임시 설치 폴더가 심볼릭 링크입니다.")
        staging_root.mkdir(mode=0o700, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="update-", dir=staging_root))
        archive = staging / "release.zip"
        checksum = _checksum(_download(release.sha256_url, None, 1024 * 1024), _platform_asset(release.version))
        _download(release.asset_url, archive, MAX_ARCHIVE_BYTES)
        with archive.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if not secrets.compare_digest(actual, checksum):
            raise UpdateError("배포 파일의 SHA256 검증에 실패했습니다. 기존 설치를 유지합니다.")
        payload = _extract_archive(archive, staging / "unpacked")
        _smoke(payload / _executable_name(), release.version)
        archive.unlink()
        return PreparedUpdate(release, root, staging, previous, secrets.token_hex(32))
    except BaseException as error:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if locked:
            (root / ".update-lock").rmdir()
        if isinstance(error, OSError):
            raise UpdateError(f"업데이트를 준비하지 못했습니다: {error}") from error
        raise


def acknowledge_startup() -> None:
    """새 UI의 on_mount에서 호출하여 실행 준비를 이전 프로세스에 알린다."""
    path = os.environ.pop(_READY_PATH, None)
    token = os.environ.pop(_READY_TOKEN, None)
    if path is None and token is None:
        return
    root = installation_root()
    if root is None or not path or not token or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise UpdateError("업데이트 시작 확인 정보가 올바르지 않습니다.")
    marker = Path(path)
    if (marker.name != "ready.json" or marker.parent.parent != root / ".staging"
            or not marker.parent.name.startswith("update-") or marker.parent.is_symlink()
            or (root / ".staging").is_symlink() or not marker.parent.is_dir()):
        raise UpdateError("업데이트 시작 확인 경로가 올바르지 않습니다.")
    _atomic_json(marker, {"token": token, "version": __version__})


def _wait_ready(child: subprocess.Popen, prepared: PreparedUpdate) -> bool:
    deadline = time.monotonic() + STARTUP_SECONDS
    marker = prepared.staging / "ready.json"
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return False
        try:
            if marker.stat().st_size <= 1024:
                value = json.loads(marker.read_text(encoding="utf-8"))
                if value == {"token": prepared.token, "version": prepared.release.version}:
                    return child.poll() is None
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    return False


def _stop_child(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def apply_update(prepared: PreparedUpdate, argv: list[str]) -> int:
    """Textual 종료 뒤 실행한다. 새 UI 준비 실패 시 선택자를 복구하고 이전 앱을 재시작한다."""
    if prepared._closed or not prepared._lock_owned:
        raise UpdateError("이미 취소되거나 적용된 업데이트입니다.")
    root = installation_root()
    if root is None or root != prepared.root:
        discard_prepared_update(prepared)
        raise UnsupportedInstallError("관리형 설치 경로가 변경되었습니다.")
    destination = root / "versions" / prepared.release.version
    old_executable = root / "versions" / prepared.previous_version / _executable_name()
    child = None
    activated = False
    moved = False
    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.signal(signal.SIGTERM, _interrupt_update)
    try:
        _validate_release(prepared.release)
        if _read_current(root) != prepared.previous_version:
            raise UpdateError("활성 버전이 변경되어 업데이트를 중단했습니다.")
        if destination.exists() or destination.is_symlink():
            raise UpdateError("대상 버전 폴더가 이미 존재합니다.")
        cwd = os.getcwd()
        os.rename(prepared.staging / "unpacked" / "agents-status", destination)
        moved = True
        _atomic_json(root / "current.json", {"version": prepared.release.version})
        activated = True
        environment = os.environ.copy()
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        environment[_READY_PATH] = str(prepared.staging / "ready.json")
        environment[_READY_TOKEN] = prepared.token
        try:
            child = subprocess.Popen([str(destination / _executable_name()), *argv], cwd=cwd, env=environment)
            ready = _wait_ready(child, prepared)
        except OSError:
            ready = False
        if not ready:
            _atomic_json(root / "current.json", {"version": prepared.previous_version})
            activated = False
            if child is not None:
                _stop_child(child)
            shutil.rmtree(destination, ignore_errors=True)
            moved = False
            _clean_prepared(prepared)
            print("새 버전 시작 확인에 실패하여 이전 버전으로 복구했습니다.", file=sys.stderr)
            environment.pop(_READY_PATH, None)
            environment.pop(_READY_TOKEN, None)
            return subprocess.call([str(old_executable), *argv], cwd=cwd, env=environment)
        _clean_prepared(prepared)
        # 기존 파일은 Windows에서 실행 중일 수 있으므로 이전 버전을 삭제하지 않는다.
        return child.wait()
    except BaseException as error:
        if activated and not prepared._closed:
            _atomic_json(root / "current.json", {"version": prepared.previous_version})
            activated = False
        if child is not None:
            _stop_child(child)
        if moved and not activated:
            shutil.rmtree(destination, ignore_errors=True)
        if isinstance(error, KeyboardInterrupt):
            return 130
        if isinstance(error, _Interrupted):
            return 128 + error.number
        if isinstance(error, (OSError, subprocess.TimeoutExpired)):
            raise UpdateError(f"업데이트 적용에 실패했습니다: {error}") from error
        raise
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)
        _clean_prepared(prepared)
