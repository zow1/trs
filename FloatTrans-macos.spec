# -*- mode: python ; coding: utf-8 -*-
# macOS 专用 PyInstaller spec：生成 FloatTrans.app
import sys

block_cipher = None

a = Analysis(
    ['float_trans.py'],
    pathex=[],
    binaries=[],
    datas=[('app.ico', '.')],
    hiddenimports=[
        'screen_ocr',
        'Quartz',
        'Vision',
        'CoreFoundation',
        'ApplicationServices',
        'pynput',
        'pynput.keyboard',
        'objc',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['winsdk', '_winreg', 'winreg'],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FloatTrans',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity='-',
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='FloatTrans',
)

app = BUNDLE(
    coll,
    name='FloatTrans.app',
    icon='app.ico',
    bundle_identifier='com.zow1.FloatTrans',
    info_plist={
        'CFBundleName': 'FloatTrans',
        'CFBundleDisplayName': 'FloatTrans',
        'CFBundleShortVersionString': '1.1.1',
        'CFBundleVersion': '1.1.1',
        'LSMinimumSystemVersion': '11.0',
        'NSHighResolutionCapable': True,
        'LSUIElement': True,
        'NSScreenCaptureUsageDescription':
            'FloatTrans 需要屏幕录制权限，才能截取屏幕上的文字并翻译。',
        'NSAppleEventsUsageDescription':
            'FloatTrans 需要此权限以实现全局热键与自动化。',
        'NSAccessibilityUsageDescription':
            'FloatTrans 需要辅助功能权限，以监听全局热键与悬停取词。',
    },
)