# -*- mode: python ; coding: utf-8 -*-
"""录放台打包配置：单文件 exe，直接放在项目根目录。

浏览器引擎不塞进 exe（约 420MB），而是作为同级 ms-playwright 目录随项目一起分发；
该目录缺失时程序自动回退到本机 Edge，再不行会在界面里给出修复指令。
"""

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# SPEC = <项目>/build/录放台.spec，SPECPATH = <项目>/build
_here = os.path.abspath(globals().get("SPEC") or os.path.join(SPECPATH, "录放台.spec"))
ROOT = os.path.dirname(os.path.dirname(_here))
ENTRY = os.path.join(ROOT, "run_app.py")
assert os.path.isfile(ENTRY), "找不到入口脚本：" + ENTRY
UI = os.path.join(ROOT, "ra", "ui")

datas = [
    (UI, "ra/ui"),
    (os.path.join(ROOT, "ra", "workflow.schema.json"), "ra"),
]
datas += collect_data_files("playwright")
datas += collect_data_files("jsonschema")

hiddenimports = collect_submodules("playwright") + collect_submodules("jsonschema")

a = Analysis(
    [os.path.join(ROOT, "run_app.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["PyQt5", "PySide6", "tkinter", "customtkinter", "webview", "flask", "matplotlib", "numpy",
              "playwright_stealth", "PIL", "PyMuPDF", "fitz"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="录放台",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(ROOT, "build", "app.ico"),
)
