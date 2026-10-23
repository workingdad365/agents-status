from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
textual_data, textual_binaries, textual_imports = collect_all("textual")
a = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root)],
    binaries=textual_binaries,
    datas=textual_data + collect_data_files("certifi") + copy_metadata("textual", recursive=True) + [
        (str(root / "GLOBAL_INSTRUCTIONS.md"), "agents_status"),
    ],
    hiddenimports=textual_imports + collect_submodules("rich._unicode_data"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="agents-status",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    contents_directory="_internal",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="agents-status")
