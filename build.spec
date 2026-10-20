# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置（onedir 模式）

几个关键取舍：
1. 用 onedir（文件夹）而不是 onefile：
   onefile 每次启动都要把 PyQt5 + matplotlib 解压到临时目录，启动慢 5~10 秒，
   而且 data/logs 会被写进临时目录、退出即丢。onedir 下数据和日志都在 exe 旁边，便于备份。
2. 不打包 data/ 目录：
   里面是 chrome_profile / edge_profile / cookies，属于用户登录态，
   既不应该随包分发（等于把账号交出去），也应该由程序首次运行自己创建。
3. selenium-manager.exe 由 pyinstaller-hooks-contrib 的 hook-selenium 带入
   （collect_data_files('selenium')），打包后需确认
   _internal/selenium/webdriver/common/windows/selenium-manager.exe 存在，
   否则 Chrome 驱动解析会失败。
"""
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

# 应用名与版本号统一从 about_info.py 读取，避免和界面/文档不一致
# （spec 由 PyInstaller 在当前工作目录执行，这里把项目根目录加入搜索路径）
import os
import sys

sys.path.insert(0, os.path.abspath(os.getcwd()))
try:
    from about_info import APP_NAME, APP_VERSION
except Exception:
    APP_NAME, APP_VERSION = "商品价格监控系统", "1.5.0"

# exe 文件名带版本号：商品价格监控系统V1.5.0.exe
EXE_NAME = f"{APP_NAME}V{APP_VERSION}"

datas = []
# matplotlib 的字体与 mpl-data（缺失会导致画图报错）
datas += collect_data_files("matplotlib")
# 使用说明随程序分发，「帮助 → 使用说明」菜单直接打开它
datas.append(("使用说明.txt", "."))

hiddenimports = [
    # PyQt5
    "PyQt5.QtWidgets",
    "PyQt5.QtCore",
    "PyQt5.QtGui",
    "PyQt5.sip",
    # matplotlib 画布后端（动态导入，静态分析看不到）
    "matplotlib.backends.backend_qt5agg",
    "matplotlib.backends.backend_agg",
    # Excel 导出（延迟导入）
    "openpyxl",
    "openpyxl.styles",
    "openpyxl.utils",
    # 提示音（延迟导入）
    "pygame",
    "pygame.mixer",
    # 浏览器驱动
    "selenium",
    "selenium.webdriver",
    "selenium.webdriver.chrome.service",
    "selenium.webdriver.chrome.options",
    "selenium.webdriver.edge.service",
    "selenium.webdriver.edge.options",
    "selenium.webdriver.common.by",
]
hiddenimports += collect_submodules("openpyxl")
hiddenimports += collect_submodules("selenium.webdriver")

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",       # 界面用的是 PyQt5
        "pytest",
        # 注意：不要排除 unittest！matplotlib 在导入阶段就会 import unittest，
        # 排除后程序会在 import matplotlib 时直接崩溃（已实测）。
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=EXE_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,                  # 窗口程序，不弹黑框
    disable_windowed_traceback=False,  # 崩溃时弹窗提示，而不是静默退出
    version="version_info.txt",     # exe 属性里显示作者 / 版权 / 版本号
    icon="app.ico",                 # 应用图标（H1 明亮金）
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
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
