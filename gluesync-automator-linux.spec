# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['run_automator.py'],
    pathex=['.'],
    binaries=[],
    datas=[('automator_app/static', 'static')],
    hiddenimports=['create_user_defined_functions', 'create_all_tables', 'create_all_entities', 'commons', 'utils.log', 'utils.core_hub_client', 'utils.chronos_client', 'utils.gluesync_sdk_client', 'automator_app.app', 'automator_app.cli', 'automator_app.corehub', 'automator_app.state', 'automator_app.version'],
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
    name='gluesync-automator-linux',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir='/tmp',
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
