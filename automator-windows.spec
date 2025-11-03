# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all
import os
import sys

# Add current directory to sys.path to ensure root modules are found
current_dir = os.path.dirname(os.path.abspath(SPEC))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

exe_name = 'gluesync-automator-windows.exe'
runtime_tmpdir = 'C:\\Windows\\Temp'

datas = [('automator_app\\static', 'static')]
binaries = []
hiddenimports = [
    'create_user_defined_functions',
    'create_all_tables',
    'create_all_entities',
    'commons',
    'utils.log',
    'utils.core_hub_client',
    'utils.chronos_client',
    'utils.gluesync_sdk_client',
    'automator_app.app',
    'automator_app.cli',
    'automator_app.corehub',
    'automator_app.state',
    'automator_app.version',
]


a = Analysis(
    ['run_automator.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=runtime_tmpdir,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='automator_app\\static\\gluesync-icon.ico',
    version='windows-version-info.py',
)
