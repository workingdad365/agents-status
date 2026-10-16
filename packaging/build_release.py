"""고정 환경에서 onedir 생성, 네이티브 점검 후 재현 가능한 ZIP 작성."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents_status import __version__


def target() -> tuple[str, str]:
    systems = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}
    machines = {"x86_64": "x86_64", "AMD64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}
    return systems[platform.system()], machines[platform.machine()]


def smoke(executable: Path) -> None:
    result = subprocess.run([str(executable), "--version"], check=True, text=True, capture_output=True, timeout=30)
    if result.stdout.strip().split()[-1:] != [__version__]:
        raise RuntimeError(f"버전 불일치: {result.stdout!r}")
    subprocess.run([str(executable), "--self-check"], check=True, timeout=60)


def bundled_files(directory: Path, ancestors: frozenset[Path] = frozenset()):
    resolved = directory.resolve()
    if resolved in ancestors:
        raise RuntimeError(f"순환 디렉터리 링크: {directory}")
    ancestors = ancestors | {resolved}
    for source in sorted(directory.iterdir()):
        if source.is_dir():
            yield from bundled_files(source, ancestors)
        elif source.is_file():
            yield source
        else:
            raise RuntimeError(f"지원하지 않는 번들 항목: {source}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-tag")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", __version__):
        raise SystemExit("안정 버전만 배포할 수 있음")
    if args.check_tag is not None:
        if args.check_tag != f"v{__version__}":
            raise SystemExit("태그와 __version__ 불일치")
        return
    if sys.version_info[:2] != (3, 13):
        raise SystemExit("Python 3.13 필요")
    system, arch = target()
    os.environ.setdefault("PYTHONHASHSEED", "0")
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
        str(ROOT / "packaging" / "agents-status.spec"),
    ], cwd=ROOT, check=True)
    directory = ROOT / "dist" / "agents-status"
    executable = directory / ("agents-status.exe" if system == "windows" else "agents-status")
    smoke(executable)
    destination = ROOT / "release"
    destination.mkdir(exist_ok=True)
    artifact = destination / f"agents-status-{__version__}-{system}-{arch}.zip"
    with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in bundled_files(directory):
            # macOS 프레임워크의 디렉터리 링크까지 일반 파일로 펼침.
            relative = source.relative_to(directory.parent).as_posix()
            if not re.fullmatch(r"[A-Za-z0-9_./+@-]+", relative):
                raise RuntimeError(f"지원하지 않는 ZIP 경로: {relative}")
            info = zipfile.ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if source.stat().st_mode & 0o111 else 0o644
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes(), compresslevel=9)
    # 아카이브 자체를 풀어 실제 배포 구조도 점검함.
    import tempfile
    with tempfile.TemporaryDirectory() as temporary:
        with zipfile.ZipFile(artifact) as archive:
            archive.extractall(temporary)
            for member in archive.infolist():
                Path(temporary, member.filename).chmod(member.external_attr >> 16 & 0o777)
        smoke(Path(temporary, "agents-status", executable.name))
    with artifact.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    artifact.with_suffix(".zip.sha256").write_text(f"{checksum}  {artifact.name}\n", encoding="ascii")
    print(artifact)


if __name__ == "__main__":
    main()
