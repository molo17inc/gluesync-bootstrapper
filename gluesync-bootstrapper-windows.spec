# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec of gluesync-bootstrapper.exe, the one-file exe inside the Windows container image
# (Dockerfile.windows). Entry point: bootstrapper_exe.py (bootstrapper, hub-first-login, self-test).
import os
import sys

current_dir = os.path.dirname(os.path.abspath(SPEC))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from bootstrapper_exe import BUNDLED_MODULES

# main.py is loaded at run time (runpy), so PyInstaller does not see it as an import.
hiddenimports = ['main'] + list(BUNDLED_MODULES)

datas = [
    # commons.py and main.py read agents.json next to themselves
    ('agents.json', '.'),
]
# VERSION is written by the image build (CI: the version of the tag); `--version` and `self-test` print it.
if os.path.isfile(os.path.join(current_dir, 'VERSION')):
    datas.append(('VERSION', '.'))

a = Analysis(
    ['bootstrapper_exe.py'],
    pathex=[current_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # none of these is used by the bootstrapper chain; keeps the exe (and its start-up unpacking) small
    excludes=['tkinter', 'fastapi', 'uvicorn', 'mcp', 'cairosvg', 'PyQt6', 'automator_app'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    # UTF-8 mode: main.py opens config and YAML files with the default encoding, which is cp1252 in a Windows
    # container (the Linux image runs with a UTF-8 locale), and its banner is not ASCII.
    [('X utf8', None, 'OPTION')],
    name='gluesync-bootstrapper',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
