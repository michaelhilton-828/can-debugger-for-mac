# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/dbc-viewer.png', 'assets')],
    hiddenimports=[
        'matplotlib.backends.backend_qtagg',
        'can.io.blf',
        'can.io.asc',
        'can.io.csv',
        'can.io.trc',
        'can.io.canutils',
        'can.io.sqlite',
        'can.io.mf4',
        'can.interfaces.pcan',
        'can.interfaces.pcan.basic',
        'can.interfaces.pcan.pcan',
    ],
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
    [],
    exclude_binaries=True,
    name='DBC Viewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DBC Viewer',
)
app = BUNDLE(
    coll,
    name='DBC Viewer.app',
    icon='assets/dbc-viewer.icns',
    bundle_identifier=None,
)
