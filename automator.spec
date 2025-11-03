# -*- mode: python ; coding: utf-8 -*-

import os

block_cipher = None


exe_name = os.getenv('PYI_EXE_NAME', 'gluesync-automator')
runtime_tmpdir = os.getenv('PYI_RUNTIME_TMPDIR', '/tmp')


a = Analysis(
    ['run_automator.py'],
    pathex=['.'],
    binaries=[],
    datas=[('automator_app/static', 'automator_app/static')],
    hiddenimports=[
        'create_user_defined_functions',
        'create_all_tables',
        'commons',
        'utils.log',
        'utils.core_hub_client',
        'utils.chronos_client',
        'utils.gluesync_sdk_client',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=runtime_tmpdir,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
