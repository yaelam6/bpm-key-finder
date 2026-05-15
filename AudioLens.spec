# -*- mode: python ; coding: utf-8 -*-
import os, customtkinter

ctk_path = os.path.dirname(customtkinter.__file__)

a = Analysis(
    ['audioLens.py'],
    pathex=[],
    binaries=[],
    datas=[
        (ctk_path, 'customtkinter'),
    ],
    hiddenimports=[
        'customtkinter',
        'PIL',
        'PIL._imagingtk',
        'scipy.signal',
        'scipy.special',
        'scipy._lib',
        'soundfile',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AudioLens',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name='AudioLens',
)

app = BUNDLE(
    coll,
    name='AudioLens.app',
    icon='AudioLens.icns',
    bundle_identifier='com.yaelamitay.audiolens',
    info_plist={
        'CFBundleName': 'AudioLens',
        'CFBundleDisplayName': 'AudioLens',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0',
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
    },
)
