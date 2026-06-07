# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

# These modules are massive and unused in your app. 
# Keeping this list is critical to size reduction without UPX.
excluded_modules = [
    'PySide6.QtWebEngine', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
    'PySide6.Qt3D', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.Qt3DInput',
    'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.QtSql',
    'PySide6.QtTest', 'PySide6.QtNetwork', 'PySide6.QtBluetooth',
    'PySide6.QtMultimedia', 'PySide6.QtPositioning', 'PySide6.QtQuick', 'PySide6.QtQml'
]

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excluded_modules,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='TRN_Control_Panel_Driver',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='icon.ico'
)
