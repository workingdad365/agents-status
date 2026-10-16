"""모든 플랫폼 산출물이 완성된 경우에만 공개 체크섬 목록 작성."""
from pathlib import Path
import hashlib
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents_status import __version__

TARGETS = (
    ("linux", "x86_64"), ("linux", "aarch64"),
    ("windows", "x86_64"), ("macos", "x86_64"), ("macos", "aarch64"),
)
lines = []
for system, arch in TARGETS:
    name = f"agents-status-{__version__}-{system}-{arch}.zip"
    artifact = ROOT / "release" / name
    with artifact.open("rb") as stream:
        checksum = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    line = f"{checksum.hexdigest()}  {name}\n"
    if artifact.with_suffix(".zip.sha256").read_text(encoding="ascii") != line:
        raise SystemExit(f"체크섬 불일치: {name}")
    lines.append(line)
for name in ("install.sh", "install.ps1"):
    checksum = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    lines.append(f"{checksum}  {name}\n")
(ROOT / "release" / "SHA256SUMS").write_text("".join(lines), encoding="ascii")
