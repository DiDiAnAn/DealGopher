"""
本机浏览器检测（Chrome / Edge）

两者都是 Chromium 内核，Selenium 的驱动方式、CDP 注入、Cookie 处理、
页面解析逻辑完全一致，所以程序只需要在这两个之间让用户选一个。

检测顺序：
1. 注册表 App Paths（最可靠，卸载残留也能识别到真实安装路径）
2. 常见安装路径（Program Files / Program Files(x86) / LocalAppData）
3. 版本号：卸载注册表 DisplayVersion → 安装目录下的版本号子目录

winreg 仅 Windows 可用，本程序本身也只面向 Windows。
"""
import os
import re
import winreg
from typing import Dict, List

APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"

BROWSERS = {
    "chrome": {
        "display": "Google Chrome",
        "exe": "chrome.exe",
        "paths": [
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        ],
        "uninstall": ["Google Chrome"],
    },
    "edge": {
        "display": "Microsoft Edge",
        "exe": "msedge.exe",
        "paths": [
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
            r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe",
        ],
        "uninstall": ["Microsoft Edge"],
    },
}

# 安装目录里形如 155.0.8059.80 的版本号目录
_VERSION_DIR_RE = re.compile(r"^\d+(\.\d+){1,3}$")

# 注册表读取时依次尝试的视图（64 位 / 32 位 / 默认）
_HIVES = (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER)
_VIEWS = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY, 0)


def _registry_app_path(exe_name: str) -> str:
    """从注册表 App Paths 读取可执行文件完整路径"""
    for hive in _HIVES:
        for view in _VIEWS:
            try:
                with winreg.OpenKey(hive, f"{APP_PATHS_KEY}\\{exe_name}", 0,
                                    winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, "")
            except OSError:
                continue
            path = str(value).strip().strip('"')
            if path and os.path.exists(path):
                return path
    return ""


def _common_path(raw_paths: List[str]) -> str:
    """在常见安装路径里找一个存在的"""
    for raw in raw_paths:
        candidate = os.path.expandvars(raw)
        if os.path.exists(candidate):
            return candidate
    return ""


def _version_from_uninstall(names: List[str]) -> str:
    """从"应用和功能"注册表项读取版本号"""
    for name in names:
        for hive in _HIVES:
            for view in _VIEWS:
                try:
                    with winreg.OpenKey(hive, f"{UNINSTALL_KEY}\\{name}", 0,
                                        winreg.KEY_READ | view) as key:
                        for value_name in ("DisplayVersion", "Version"):
                            try:
                                value, _ = winreg.QueryValueEx(key, value_name)
                            except OSError:
                                continue
                            if value:
                                return str(value)
                except OSError:
                    continue
    return ""


def _version_from_install_dir(exe_path: str) -> str:
    """
    从安装目录结构推断版本号

    Chrome: ...\\Application\\chrome.exe，同目录下有 155.0.8059.80 之类的版本子目录
    Edge:   ...\\Application\\msedge.exe，同目录下有 155.0.4283.62 之类的版本子目录
    """
    parent = os.path.dirname(exe_path)
    base_name = os.path.basename(parent)
    if _VERSION_DIR_RE.match(base_name):
        return base_name

    try:
        candidates = [d for d in os.listdir(parent) if _VERSION_DIR_RE.match(d)]
    except OSError:
        return ""
    if not candidates:
        return ""
    return max(candidates, key=lambda v: [int(x) for x in v.split(".")])


def _clean_version(value) -> str:
    """清洗版本号：去掉 NUL、空白等不可见字符，并校验格式"""
    if not isinstance(value, str):
        return ""
    text = value.replace("\x00", "").strip()
    return text if _VERSION_DIR_RE.match(text) else ""


def detect_browsers() -> Dict[str, dict]:
    """
    检测本机已安装的浏览器

    返回：{"chrome": {"name","display","path","version"}, "edge": {...}}
    未安装的浏览器不会出现在结果里。
    """
    found: Dict[str, dict] = {}
    for name, spec in BROWSERS.items():
        path = _registry_app_path(spec["exe"]) or _common_path(spec["paths"])
        if not path:
            continue
        version = (
            _clean_version(_version_from_uninstall(spec["uninstall"]))
            or _clean_version(_version_from_install_dir(path))
        )
        found[name] = {
            "name": name,
            "display": spec["display"],
            "path": path,
            "version": version,
        }
    return found


def get_browser_info(name: str) -> dict:
    """获取单个浏览器的检测信息，未安装时返回空字典"""
    return detect_browsers().get(name, {})
