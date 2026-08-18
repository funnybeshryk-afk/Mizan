# -*- mode: python ; coding: utf-8 -*-
#
# Reproducible PyInstaller build spec for Mizan.
#
# Build with:
#   pip install -r requirements-build.txt
#   pyinstaller mizan.spec
#
# Output: dist/Mizan.exe (single file - see "onefile vs onedir" note below).
#
# IMPORTANT - no secrets in the bundle: this spec never adds .env to `datas`,
# so PyInstaller has no way to embed it - the app reads .env at *runtime*
# from the directory next to the running .exe (see app/core/paths.py),
# never from anything baked into the executable. Do not add .env here.
#
# onefile vs onedir: onefile was chosen per spec. It self-extracts PySide6's
# (sizeable) Qt binaries into a fresh temp dir on every launch, which costs a
# few hundred ms to a couple of seconds of extra startup time compared to
# onedir - that's the one concrete downside observed. Nothing about PySide6
# plugin loading requires onedir: PyInstaller's built-in PySide6 hooks
# (hook-PySide6*.py, rthooks/pyi_rth_pyside6.py) collect the Qt "platforms"/
# "styles" plugins and point QT_PLUGIN_PATH at the extracted copy
# automatically, regardless of onefile/onedir. If startup latency ever
# becomes a real complaint, switching to onedir is a one-line change
# (EXE(..., exclude_binaries=True, ...) + adding a COLLECT(...) at the end).

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden_imports = (
    collect_submodules("app")
    + collect_submodules("alpaca")
)

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests"],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Mizan",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # windowed: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # no icon asset in the repo yet; default PyInstaller icon
)
