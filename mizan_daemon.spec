# -*- mode: python ; coding: utf-8 -*-
#
# Reproducible PyInstaller build spec for the Mizan automation daemon
# (Stage 8c - Windows autostart).
#
# Build with:
#   pip install -r requirements-build.txt
#   pyinstaller mizan_daemon.spec
#
# Output: dist/MizanDaemon.exe
#
# Same no-secrets-in-the-bundle rule as mizan.spec: .env is never added to
# `datas` here either - it's read at runtime from next to the running .exe
# (app/core/paths.py:get_base_dir(), which resolves to Path(sys.executable)
# .parent for a frozen build), never baked into the bundle. data/ and
# logs/ resolve the same way, right next to MizanDaemon.exe.
#
# Deliberately a SEPARATE, lean build from Mizan.exe: this entry point
# (daemon.py) never imports main.py or anything PySide6-based, so this
# bundle excludes PySide6/shiboken6 entirely - a windowed GUI toolkit has
# no business in a background service exe, and leaving it out keeps this
# build meaningfully smaller and its startup faster. (Nothing under app/
# imports PySide6 either - only main.py does - so collect_submodules("app")
# below is safe without pulling Qt in by accident.)
#
# console=False: not "minimized", genuinely absent. Task Scheduler can run
# this with "Run whether user is logged on or not" and no desktop session
# at all, so there may be no console to attach to in the first place. All
# daemon output goes to logs/daemon.log - see
# app.automation.daemon.build_daemon_logger(), which skips attaching a
# stderr console handler entirely when sys.stderr is None (exactly the
# case in a console=False build), so logging never crashes for lack of a
# console to write to.

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden_imports = (
    collect_submodules("app")
    + collect_submodules("alpaca")
)

a = Analysis(
    ["daemon.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", "PySide6", "shiboken6"],
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
    name="MizanDaemon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # no console at all - see note above
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # no icon asset in the repo yet; default PyInstaller icon
)
