# -*- mode: python ; coding: utf-8 -*-
import os
import subprocess
import sys

# Add current directory to sys.path to ensure root modules are found
current_dir = os.path.dirname(os.path.abspath(SPEC))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

exe_name = 'gluesync-automator-macos'
runtime_tmpdir = '/tmp'


def _write_version_file() -> None:
    """Best-effort: derive Automator version from latest `automator-*` git tag.

    Falls back silently if git is unavailable or no matching tag exists.
    """
    try:
        # Try to get version from git
        result = subprocess.run(
            ['git', 'describe', '--tags', '--always'],
            capture_output=True,
            text=True,
            check=False
        )
        if result.returncode == 0:
            version = result.stdout.strip()
        else:
            version = 'unknown'
    except Exception:
        version = 'unknown'
    
    try:
        version_path.parent.mkdir(parents=True, exist_ok=True)
        with open(version_path, 'w', encoding='utf-8') as fh:  # noqa: PTH123
            fh.write(version + '\n')
    except Exception:
        # Do not fail the build if we cannot write the VERSION file
        pass


_write_version_file()

datas = [
    ('automator_app', 'automator_app'),
    ('automator_app/static', 'static'),
    ('automator_app/VERSION', 'automator_app'),
]
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
    'uvicorn.logging',
    'uvicorn.loops',
    'uvicorn.loops.auto',
    'uvicorn.protocols',
    'uvicorn.protocols.http',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan',
    'uvicorn.lifespan.on',
    'AppKit',
    'Foundation',
    'WebKit',
    'objc',
    'PyQt6',
    'PyQt6.QtCore',
    'PyQt6.QtGui',
    'PyQt6.QtWidgets',
    'PyQt6.QtWebEngineCore',
    'PyQt6.QtWebEngineWidgets',
    'PyQt6.QtWebEngineQuick',
    'automator_app',
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
    exclude_binaries=True,
    name=exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=runtime_tmpdir,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='automator_app/static/favicon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=exe_name,
)
