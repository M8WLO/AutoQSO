# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for AutoQSO

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None

# Collect soundfile/sounddevice native libs
soundfile_datas  = collect_data_files('soundfile')
sounddevice_datas = collect_data_files('sounddevice')

a = Analysis(
    ['main.py'],
    pathex=[str(Path('.').resolve())],
    binaries=(
        collect_dynamic_libs('soundfile') +
        collect_dynamic_libs('sounddevice') +
        collect_dynamic_libs('ctranslate2') +
        collect_dynamic_libs('faster_whisper')
    ),
    datas=(
        soundfile_datas +
        sounddevice_datas +
        collect_data_files('faster_whisper') +
        collect_data_files('ctranslate2') +
        # Include tokeniser data for whisper
        collect_data_files('tokenizers') +
        collect_data_files('huggingface_hub')
    ),
    hiddenimports=[
        # Serial
        'serial',
        'serial.tools',
        'serial.tools.list_ports',
        'serial.tools.list_ports_windows',
        # STT
        'faster_whisper',
        'faster_whisper.transcribe',
        'ctranslate2',
        'tokenizers',
        'huggingface_hub',
        # TTS
        'pyttsx3',
        'pyttsx3.drivers',
        'pyttsx3.drivers.sapi5',
        'edge_tts',
        # Audio
        'pyaudio',
        'sounddevice',
        'soundfile',
        'cffi',
        # Qt6
        'PyQt6',
        'PyQt6.QtWidgets',
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        # Logging / network
        'requests',
        'urllib3',
        'charset_normalizer',
        # App modules
        'config',
        'phonetics',
        'radio_interface',
        'audio_engine',
        'stt_engine',
        'tts_engine',
        'qso_controller',
        'qso_logger',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'scipy', 'PIL', 'cv2',
        'IPython', 'jupyter', 'notebook',
        'tkinter', 'wx',
    ],
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
    name='AutoQSO',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,              # add 'autoqso.ico' here when you have one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='AutoQSO',
)
