"""
浏览器驱动管理（Edge 必需，Chrome 兜底）

Selenium 4 的 Selenium Manager 会自动下载驱动，但它为 Edge 使用的
msedgedriver.azureedge.net 域名已经停止解析，实际表现是：
    下载失败 → 回退到缓存里的旧驱动 → 启动时报
    "This version of Microsoft Edge WebDriver only supports Microsoft Edge version XXX"

因此这里直接从微软当前域名下载与浏览器版本严格匹配的驱动，缓存到 data/drivers 下。
分辨率说明：只处理 64 位 Windows，其他架构返回空串，交给 Selenium Manager。
"""
import os
import platform
import re
import shutil
import tempfile
import urllib.request
import zipfile

EDGE_DRIVER_URL = "https://msedgedriver.microsoft.com/{version}/edgedriver_win64.zip"
EDGE_LATEST_URL = "https://msedgedriver.microsoft.com/LATEST_RELEASE_{major}"
CHROME_DRIVER_URL = (
    "https://storage.googleapis.com/chrome-for-testing-public/"
    "{version}/win64/chromedriver-win64.zip"
)

DRIVER_EXE = {"edge": "msedgedriver.exe", "chrome": "chromedriver.exe"}

_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")


def clean_version(value) -> str:
    """清洗版本号：去掉 NUL、空白等，并校验格式，非法则返回空串"""
    if not isinstance(value, str):
        return ""
    text = value.replace("\x00", "").strip()
    return text if _VERSION_RE.match(text) else ""


def cache_path(root: str, browser: str, version: str) -> str:
    """驱动缓存文件路径：<root>/<browser>/<version>/<driver.exe>"""
    return os.path.join(root, browser, version, DRIVER_EXE[browser])


def find_cached_driver(root: str, browser: str, version: str) -> str:
    """查找已缓存的匹配版本驱动，没有则返回空串"""
    version = clean_version(version)
    if not version or browser not in DRIVER_EXE:
        return ""
    path = cache_path(root, browser, version)
    return path if os.path.exists(path) else ""


def _fetch(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _decode_version(payload: bytes) -> str:
    """
    解码微软 LATEST_RELEASE 接口的响应

    该接口返回的是 UTF-16LE（16 个字符 = 32 字节，每字符后跟一个 \\x00），
    按 UTF-8 解码会把 NUL 一起带进来，拼出的下载地址就是非法的。
    """
    if payload.startswith(b"\xff\xfe"):
        text = payload.decode("utf-16-le", "ignore")
    elif payload.startswith(b"\xfe\xff"):
        text = payload.decode("utf-16-be", "ignore")
    elif payload[1:2] == b"\x00":
        text = payload.decode("utf-16-le", "ignore")
    else:
        text = payload.decode("utf-8", "ignore")
    return text.replace("\x00", "").strip()


def _complete_version(browser: str, version: str) -> str:
    """把版本号补全为可直接下载的完整版本号"""
    version = clean_version(version)
    if not version or browser != "edge":
        return version

    major = version.split(".")[0]
    try:
        latest = _decode_version(_fetch(EDGE_LATEST_URL.format(major=major), timeout=20))
        if _VERSION_RE.match(latest):
            return latest
    except Exception as e:
        print(f"[DriverManager] 获取 Edge 最新版本号失败: {e}")
    return version


def download_driver(root: str, browser: str, version: str, timeout: int = 120) -> str:
    """
    下载并解压与浏览器版本匹配的驱动，返回 exe 路径；失败返回空串。
    """
    version = clean_version(version)
    if browser not in DRIVER_EXE or not version:
        return ""
    if platform.machine().lower() not in ("amd64", "x86_64"):
        return ""

    target = cache_path(root, browser, version)
    if os.path.exists(target):
        return target

    full_version = _complete_version(browser, version)
    template = EDGE_DRIVER_URL if browser == "edge" else CHROME_DRIVER_URL
    url = template.format(version=full_version)

    print(f"[DriverManager] 正在下载驱动: {url}")
    payload = _fetch(url, timeout=timeout)
    print(f"[DriverManager] 下载完成，{len(payload) / 1024 / 1024:.1f} MB")

    exe_name = DRIVER_EXE[browser].lower()
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, "driver.zip")
        with open(zip_path, "wb") as file:
            file.write(payload)

        with zipfile.ZipFile(zip_path) as archive:
            member = next(
                (name for name in archive.namelist() if name.lower().endswith(exe_name)),
                None,
            )
            if member is None:
                print(f"[DriverManager] 压缩包内未找到 {exe_name}")
                return ""
            archive.extract(member, temp_dir)

        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(os.path.join(temp_dir, member), target)

    if not os.path.exists(target):
        return ""

    print(f"[DriverManager] 驱动已缓存: {target}")
    return target
