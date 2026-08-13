# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('src\\voice_orb_static', 'src\\voice_orb_static')]
binaries = [('D:\\shirley_space\\software\\anaconda3\\envs\\real-time-voice\\Library\\bin\\ffi.dll', '.'), ('D:\\shirley_space\\software\\anaconda3\\envs\\real-time-voice\\Library\\bin\\libcrypto-3-x64.dll', '.'), ('D:\\shirley_space\\software\\anaconda3\\envs\\real-time-voice\\Library\\bin\\libssl-3-x64.dll', '.'), ('D:\\shirley_space\\software\\anaconda3\\envs\\real-time-voice\\Library\\bin\\liblzma.dll', '.'), ('D:\\shirley_space\\software\\anaconda3\\envs\\real-time-voice\\Library\\bin\\LIBBZ2.dll', '.'), ('D:\\shirley_space\\software\\anaconda3\\envs\\real-time-voice\\Library\\bin\\libexpat.dll', '.')]
hiddenimports = ['aec_audio_processing.audio_processing']
tmp_ret = collect_all('aec_audio_processing')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('_sounddevice_data')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['main.py'],
    pathex=['D:\\shirley_space\\develop\\project\\ai-voice-assistant'],
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
    [],
    exclude_binaries=True,
    name='Ava',
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
    icon=['assets\\ava.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Ava',
)
