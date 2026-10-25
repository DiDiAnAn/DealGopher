"""
浏览器引擎 - 负责Selenium浏览器管理、登录、Cookie管理、商品信息抓取
"""
import json
import os
import subprocess
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from paths import DATA_DIR
from platforms import PLATFORMS
from browser_detector import detect_browsers
from driver_manager import download_driver, find_cached_driver
from status import (
    STATUS_OK, STATUS_NO_STOCK, STATUS_DELISTED,
    STATUS_PRICE_RANGE, STATUS_SKU_MISMATCH, STATUS_UNKNOWN,
)


@dataclass
class FetchResult:
    """一次抓取的结果：价格与状态分离"""

    name: str = ""
    price: float = 0.0
    original_price: float = 0.0
    status: str = STATUS_UNKNOWN
    status_text: str = ""      # 页面原文或失败原因，便于排查
    sku_text: str = ""         # 本次实际选中的规格

    @property
    def has_price(self) -> bool:
        return self.price > 0


def _js_string(text: str) -> str:
    """把 Python 字符串安全嵌入 JS 源码（转义引号、反斜杠、换行）"""
    return json.dumps(str(text), ensure_ascii=False)


class BrowserEngine:
    """浏览器引擎：管理浏览器实例、Cookie、商品抓取（Chrome / Edge 均支持）"""

    # 支持的浏览器及显示名
    BROWSER_NAMES = {"chrome": "Google Chrome", "edge": "Microsoft Edge"}

    # User-Agent：Edge 也是 Chromium 内核，带上 Edg/ 标识更接近真实环境
    USER_AGENTS = {
        "chrome": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "edge": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
        ),
    }

    def __init__(self, data_dir: str = "", browser: str = "chrome", headless: bool = False):
        self.data_dir = data_dir or DATA_DIR
        self.browser = browser if browser in self.BROWSER_NAMES else "chrome"
        self.headless = bool(headless)   # 服务器/容器环境无图形界面时必须开启
        self.binary_path = ""      # 由调用方按浏览器检测结果填入
        self.last_error = ""
        # 抓取时是否最小化浏览器窗口（主窗口会按用户设置覆盖）
        self.minimize_when_fetching = True       # 最近一次启动失败的可读原因

        # Linux / 容器：用环境变量直接指定 Chromium 可执行文件
        env_binary = os.environ.get("PM_CHROME_BINARY", "").strip()
        if env_binary and os.path.exists(env_binary):
            self.binary_path = env_binary

        self.cookie_dir = os.path.join(self.data_dir, "cookies")

        # 不同浏览器的用户数据目录必须分开，
        # 否则互相读写对方的配置目录会破坏登录态甚至让浏览器拒绝启动
        profile_name = "chrome_profile" if self.browser == "chrome" else "edge_profile"
        self.profile_dir = os.path.join(self.data_dir, profile_name)

        os.makedirs(self.cookie_dir, exist_ok=True)
        os.makedirs(self.profile_dir, exist_ok=True)

        self.driver = None
        self._loaded_platforms = set()

    @property
    def display_name(self) -> str:
        return self.BROWSER_NAMES.get(self.browser, self.browser)

    # ==================== 浏览器生命周期 ====================

    def _build_options(self, extra_args=None):
        """按所选浏览器构造启动参数（Chrome / Edge 共用同一套 Chromium 参数）"""
        if self.browser == "edge":
            from selenium.webdriver.edge.options import Options as EdgeOptions
            options = EdgeOptions()
        else:
            from selenium.webdriver.chrome.options import Options
            options = Options()

        # 核心：使用固定的用户数据目录，Cookie和登录状态会持久化
        options.add_argument(f"--user-data-dir={os.path.abspath(self.profile_dir)}")
        options.add_argument("--profile-directory=Default")

        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-popup-blocking")
        options.add_argument(f"--user-agent={self.USER_AGENTS[self.browser]}")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)

        # 指定浏览器可执行文件，避免自动查找找错版本
        if self.binary_path and os.path.exists(self.binary_path):
            options.binary_location = self.binary_path

        # 页面加载策略：eager 只等 DOMContentLoaded。
        # 淘宝/天猫页面资源极多，默认的 normal 策略会一直等到所有图片脚本加载完，
        # 在无头容器里常常"永远加载不完"，导致 driver.get() 长时间卡住。
        options.page_load_strategy = "eager"

        # 容器 / 服务器：无图形界面，必须 headless，并关掉沙箱等容器限制
        if self.headless:
            options.add_argument("--headless=new")
            options.add_argument("--disable-gpu")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1400,1000")

        for arg in (extra_args or ()):
            options.add_argument(arg)

        return options

    def _driver_root(self) -> str:
        return os.path.join(self.data_dir, "drivers")

    def _detected_version(self) -> str:
        return detect_browsers().get(self.browser, {}).get("version", "")

    def _resolve_driver_path(self) -> str:
        """
        返回与当前浏览器版本严格匹配的驱动路径

        优先级：
        1. 环境变量 PM_CHROME_DRIVER（容器/服务器里由镜像直接提供，最稳）
        2. 本程序缓存目录里按版本匹配好的驱动
        3. Edge 需自行下载（Selenium Manager 的旧域名已失效）
        """
        env_driver = os.environ.get("PM_CHROME_DRIVER", "").strip()
        if env_driver and os.path.exists(env_driver):
            return env_driver

        version = self._detected_version()
        if not version:
            # 非 Windows 环境下拿不到版本号，交给 Selenium Manager 或环境变量
            return ""

        root = self._driver_root()
        cached = find_cached_driver(root, self.browser, version)
        if cached:
            return cached

        if self.browser != "edge":
            return ""

        try:
            return download_driver(root, self.browser, version)
        except Exception as e:
            print(f"[BrowserEngine] 下载 Edge 驱动失败: {e}")
            return ""

    def _download_driver_fallback(self) -> str:
        """兜底：自行下载匹配驱动（主要给 Chrome 用）"""
        version = self._detected_version()
        if not version:
            return ""
        try:
            return download_driver(self._driver_root(), self.browser, version)
        except Exception as e:
            print(f"[BrowserEngine] 自行下载驱动失败: {e}")
            return ""

    def _create_driver(self, options, executable: str = ""):
        driver_cls = webdriver.Edge if self.browser == "edge" else webdriver.Chrome

        if executable:
            if self.browser == "edge":
                from selenium.webdriver.edge.service import Service as EdgeService
                service = EdgeService(executable_path=executable)
            else:
                from selenium.webdriver.chrome.service import Service as ChromeService
                service = ChromeService(executable_path=executable)
            return driver_cls(service=service, options=options)

        return driver_cls(options=options)

    def _launch(self, executable: str = "", extra_args=None) -> bool:
        """真正创建浏览器会话"""
        options = self._build_options(extra_args)
        self.driver = self._create_driver(options, executable)
        # 无头模式没有窗口管理器，set_window_size 会一直挂住；
        # 窗口尺寸已通过 --window-size=1400,1000 参数指定
        if not self.headless:
            self.driver.set_window_size(1280, 900)
        # 兜底：即使 eager 策略下仍有资源挂起，60 秒也必须返回，交给后续解析
        try:
            self.driver.set_page_load_timeout(60)
        except Exception:
            pass
        self._inject_stealth()
        return True

    def _inject_stealth(self):
        """注入反检测脚本"""
        try:
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": (
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                        "window.navigator.chrome = {runtime: {}};"
                        "Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});"
                        "Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});"
                    )
                },
            )
        except Exception as e:
            print(f"[BrowserEngine] 反检测脚本注入失败: {e}")

    @staticmethod
    def _describe_error(exc: Exception) -> str:
        """把 Selenium 的异常翻译成用户能看懂的原因"""
        text = str(exc).strip().replace("\n", " ")
        lowered = text.lower()

        if "cannot find" in lowered and "binary" in lowered:
            return "未找到浏览器程序，可能已被卸载或移动，请重新选择浏览器"
        if "unable to obtain" in lowered or "could not reach" in lowered or "download" in lowered:
            return "自动下载浏览器驱动失败（需要联网），请检查网络后重试"
        if "session not created" in lowered and "version" in lowered:
            return "浏览器驱动版本不匹配（浏览器可能刚更新），请联网后重试"
        if "unable to connect" in lowered or "connection refused" in lowered:
            return "无法连接浏览器，可能被安全软件拦截，请检查后重试"

        return (text[:200] if text else exc.__class__.__name__)

    def _clean_stale_browsers(self):
        """
        清理仍占用本程序配置目录的残留浏览器进程

        场景：程序被强制结束/崩溃时，Selenium 启动的 Chrome 不会跟着退出，
        会一直占着 data/chrome_profile。下次启动时新会话拿不到配置目录，
        Chrome 直接退出（报 "session not created: Chrome instance exited"），
        程序于是反复重开浏览器窗口 —— 用户看到的就是"刷新一次开一个窗口"。
        """
        if os.name != "nt":
            return
        profile = os.path.abspath(self.profile_dir)
        try:
            script = (
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                "Where-Object { $_.CommandLine -like '*" + profile + "*' } | "
                "ForEach-Object { $_.ProcessId }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, timeout=15,
                encoding="utf-8", errors="ignore",
            )
            pids = [line.strip() for line in (result.stdout or "").splitlines()
                    if line.strip().isdigit()]
            if not pids:
                return
            print(f"[BrowserEngine] 发现 {len(pids)} 个残留浏览器进程，正在清理")
            for pid in pids:
                subprocess.run(["taskkill", "/PID", pid, "/F"],
                               capture_output=True, timeout=10)
            time.sleep(1.0)
        except Exception as e:
            print(f"[BrowserEngine] 清理残留浏览器失败: {e}")
            return

        # 顺手删掉锁文件，避免 Chrome 认为配置目录仍在使用
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                path = Path(profile) / name
                if path.exists() or path.is_symlink():
                    path.unlink()
            except Exception:
                pass

    def start_browser(self) -> bool:
        """启动所选浏览器（使用固定用户数据目录，保持登录状态）"""
        if self.driver:
            return True

        self.last_error = ""
        executable = self._resolve_driver_path()
        if executable:
            print(f"[BrowserEngine] 使用驱动: {executable}")

        try:
            return self._launch(executable)
        except Exception as exc:
            self.driver = None
            # 启动失败最常见的原因：上次强制退出遗留的 Chrome 仍占着配置目录。
            # 此时才清理（而不是一上来就结束别人进程），减少被安全软件拦截的机会。
            print("[BrowserEngine] 启动失败，检查是否有残留浏览器占用配置目录")
            self._clean_stale_browsers()
            try:
                print("[BrowserEngine] 清理后重试启动")
                return self._launch(executable)
            except Exception as retry_exc:
                self.driver = None
                exc = retry_exc
            # 兜底：驱动缺失/不匹配时，自行下载一次再重试
            fallback = "" if executable else self._download_driver_fallback()
            if fallback:
                print(f"[BrowserEngine] 启动失败，改用自备驱动重试: {fallback}")
                try:
                    return self._launch(fallback)
                except Exception as exc2:
                    self.driver = None
                    exc = exc2

            self.last_error = self._describe_error(exc)
            print(f"[BrowserEngine] 启动浏览器失败: {self.last_error}")
            return False

    def minimize_window(self):
        """
        抓取时把浏览器窗口最小化

        用户不希望每抓一个商品就在眼前弹一次浏览器；
        最小化而不是隐藏，是为了随时能手动展开（比如要登录/看验证码）。
        """
        if not self.driver:
            return
        try:
            self.driver.minimize_window()
        except Exception:
            pass

    def restore_window(self):
        """把浏览器窗口恢复正常/最大化（登录、需要人工操作时调用）"""
        if not self.driver:
            return
        try:
            self.driver.maximize_window()
        except Exception:
            try:
                self.driver.set_window_rect(width=1400, height=1000, x=80, y=60)
            except Exception:
                pass

    def close_browser(self):
        """关闭浏览器"""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None

    def hard_reset(self, timeout: float = 5.0):
        """
        强制销毁当前浏览器会话

        用于抓取线程被强杀、driver 状态不可信的场景：此时若继续复用同一实例，
        下一次抓取会和残留的旧会话操作同一个 Chrome，导致报文错乱或直接崩溃。
        quit() 放在后台线程执行并设超时，避免主线程被卡死。
        """
        driver, self.driver = self.driver, None
        self._loaded_platforms.clear()
        if driver is None:
            return

        def _quit():
            try:
                driver.quit()
            except Exception:
                pass

        t = threading.Thread(target=_quit, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            print("[BrowserEngine] 浏览器会话销毁超时，放弃等待")

    @staticmethod
    def _sleep(seconds: float, abort) -> bool:
        """可中断的等待，返回 False 表示等待期间收到了中断请求"""
        deadline = time.time() + seconds
        while True:
            if abort():
                return False
            remaining = deadline - time.time()
            if remaining <= 0:
                return True
            time.sleep(min(0.2, remaining))

    def _wait_page_ready(self, timeout: float = 8.0):
        """等待 document.readyState 变为 complete（比固定 sleep 更快也更稳）"""
        try:
            WebDriverWait(self.driver, timeout).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            pass

    def _reset_if_dead(self):
        """检查浏览器是否存活，如果已关闭则重置driver"""
        if not self.driver:
            return
        try:
            _ = self.driver.current_url
        except Exception:
            print("[BrowserEngine] 检测到浏览器窗口已关闭，重置连接")
            self.driver = None
            self._loaded_platforms.clear()

    def is_alive(self) -> bool:
        """检查浏览器是否仍在运行"""
        self._reset_if_dead()
        return self.driver is not None

    # ==================== Cookie管理 ====================

    def _cookie_file(self, platform: str) -> str:
        return os.path.join(self.cookie_dir, f"{platform}_cookies.json")

    def save_cookies(self, platform: str):
        """保存当前浏览器Cookie"""
        if not self.driver:
            return
        try:
            cookies = self.driver.get_cookies()
            path = self._cookie_file(platform)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            print(f"[BrowserEngine] 已保存 {platform} Cookie ({len(cookies)}条)")
        except Exception as e:
            print(f"[BrowserEngine] 保存Cookie失败: {e}")

    def load_cookies(self, platform: str) -> bool:
        """加载已保存的Cookie到浏览器"""
        path = self._cookie_file(platform)
        if not os.path.exists(path):
            return False
        try:
            # utf-8-sig：兼容 Windows 记事本/PowerShell 写出的带 BOM 文件
            with open(path, "r", encoding="utf-8-sig") as f:
                cookies = json.load(f)
            url = PLATFORMS.get(platform, {}).get("home_url", "https://www.taobao.com")
            self.driver.get(url)
            time.sleep(2)
            for cookie in cookies:
                for key in ["sameSite", "expiry", "httpOnly", "secure"]:
                    cookie.pop(key, None)
                try:
                    self.driver.add_cookie(cookie)
                except Exception:
                    pass
            print(f"[BrowserEngine] 已加载 {platform} Cookie")
            return True
        except Exception as e:
            print(f"[BrowserEngine] 加载Cookie失败: {e}")
            return False

    def _ensure_platform_cookie(self, platform: str):
        """确保指定平台的Cookie已加载到浏览器中"""
        self._reset_if_dead()

        if platform in self._loaded_platforms:
            return

        cookie_path = self._cookie_file(platform)
        if not os.path.exists(cookie_path):
            print(f"[BrowserEngine] 未找到 {platform} 的Cookie文件，需要手动登录")
            return

        if not self.driver:
            if not self.start_browser():
                return

        print(f"[BrowserEngine] 自动加载 {platform} 的Cookie")
        if self.load_cookies(platform):
            self._loaded_platforms.add(platform)
            print(f"[BrowserEngine] 已自动加载 {platform} 的Cookie")
        else:
            print(f"[BrowserEngine] 加载 {platform} Cookie失败，需要手动登录")

    # ==================== 登录流程 ====================

    def login_platform(self, platform: str) -> bool:
        """打开浏览器让用户手动登录指定平台，登录成功后保存Cookie"""
        if not self.start_browser():
            return False

        spec = PLATFORMS.get(platform, PLATFORMS["taobao"])
        url = spec["login_url"]
        self.driver.get(url)
        print(f"[BrowserEngine] 已打开 {platform} 登录页面，请在浏览器中完成登录...")

        max_wait = 300
        start = time.time()
        home_url = spec["home_url"]
        indicators = spec["login_indicators"]
        required = spec["login_cookies"]

        while time.time() - start < max_wait:
            try:
                current_url = self.driver.current_url
                if home_url and any(ind in current_url for ind in indicators):
                    self.driver.get(home_url)
                    time.sleep(2)
                    cookies = self.driver.get_cookies()
                    cookie_names = [c["name"] for c in cookies]
                    if any(name in cookie_names for name in required):
                        self.save_cookies(platform)
                        self._loaded_platforms.add(platform)
                        print(f"[BrowserEngine] {platform} 登录成功！")
                        return True
            except Exception:
                pass
            time.sleep(3)

        print(f"[BrowserEngine] {platform} 登录超时")
        return False

    def ensure_login(self, platform: str) -> bool:
        """确保已登录指定平台"""
        if not self.start_browser():
            return False

        spec = PLATFORMS.get(platform, PLATFORMS["taobao"])
        if self.load_cookies(platform):
            self.driver.get(spec["home_url"])
            time.sleep(3)
            cookies = self.driver.get_cookies()
            cookie_names = [c["name"] for c in cookies]
            if any(name in cookie_names for name in spec["login_cookies"]):
                self._loaded_platforms.add(platform)
                print(f"[BrowserEngine] {platform} Cookie有效，无需重新登录")
                return True

        print(f"[BrowserEngine] {platform} 需要手动登录")
        return self.login_platform(platform)

    # ==================== 商品信息抓取 ====================

    def fetch_product_info(self, url: str, platform: str, should_abort=None,
                           sku_spec=None) -> "FetchResult":
        """
        抓取商品信息，返回 FetchResult（名称 + 价格 + 状态）

        与旧版的区别：价格和「能不能买」分离。
        - 页面已下架       → status=delisted，不返回价格
        - 选中规格无货     → status=no_stock，不返回价格
        - 规格锁定但选不中 → status=sku_mismatch，不返回价格
        - 页面显示"最低价起" → status=price_range，价格不可信
        - 正常             → status=ok

        sku_spec: 形如 ["版本类型=中国大陆", "套餐=单机标配"]，抓取前会自动选中这些规格
        """
        abort = should_abort or (lambda: False)

        if abort():
            return FetchResult(status=STATUS_UNKNOWN, status_text="已中断")

        self._reset_if_dead()

        if not self.driver:
            if not self.start_browser():
                return FetchResult(status=STATUS_UNKNOWN,
                                   status_text=self.last_error or "浏览器启动失败")

        self._ensure_platform_cookie(platform)
        if abort():
            return FetchResult(status=STATUS_UNKNOWN, status_text="已中断")

        try:
            try:
                self.driver.get(url)
            except Exception as e:
                # 页面加载超时不算失败：DOM 通常已就绪，继续往下解析
                print(f"[BrowserEngine] 页面加载超时/异常，继续尝试解析: {str(e)[:120]}")
            self._wait_page_ready(8)
            # 页面已在加载，用户不需要盯着看：按设置把浏览器最小化，
            # 避免每抓一个商品就有一个窗口弹到眼前（需要登录时会自动还原）
            if getattr(self, "minimize_when_fetching", True):
                self.minimize_window()
            if not self._sleep(1.5, abort):
                return FetchResult(status=STATUS_UNKNOWN, status_text="已中断")

            self._try_close_popups()
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 4);")
            if not self._sleep(1.5, abort):
                return FetchResult(status=STATUS_UNKNOWN, status_text="已中断")
            self.driver.execute_script("window.scrollTo(0, 0);")
            if not self._sleep(0.8, abort):
                return FetchResult(status=STATUS_UNKNOWN, status_text="已中断")

            # ---- 1) 页面级状态：商品是否已下架/失效 ----
            page_status, page_text = self._detect_page_status()
            if page_status != STATUS_OK:
                print(f"[BrowserEngine] 页面状态异常: {page_status} / {page_text}")
                return FetchResult(name=self._safe_name(platform), status=page_status,
                                   status_text=page_text)

            # ---- 2) 规格锁定：选中指定版本/套餐 ----
            sku_text = ""
            # 未锁定规格时：用户给的链接本身就带选好的版本（淘宝链接含 skuId），
            # 页面打开时已选中正确规格 —— 直接读取当前选中项作为本次目标，
            # 不再要求用户右键「记录当前规格」。
            if not sku_spec and self._wait_for_sku_ready(5.0):
                detected = self._read_selected_sku()
                if detected:
                    sku_spec = detected
                    print(f"[BrowserEngine] 自动识别已选规格: {' / '.join(detected)}")
            if sku_spec:
                applied, sku_text, message = self._apply_sku_spec(sku_spec)
                if not applied:
                    print(f"[BrowserEngine] 规格锁定失败: {message}；当前选中={sku_text!r}")
                    return FetchResult(name=self._safe_name(platform),
                                       status=STATUS_SKU_MISMATCH,
                                       status_text=message, sku_text=sku_text)
                # 选完规格后价格与库存会异步刷新
                if not self._sleep(1.5, abort):
                    return FetchResult(status=STATUS_UNKNOWN, status_text="已中断")

            # ---- 3) 选中规格是否可购买 ----
            stock_status, stock_text = self._detect_stock_status()
            if stock_status != STATUS_OK:
                print(f"[BrowserEngine] 库存状态: {stock_status} / {stock_text}")
                return FetchResult(name=self._safe_name(platform), status=stock_status,
                                   status_text=stock_text, sku_text=sku_text)

            # ---- 4) 解析价格 ----
            name, price, original_price = self._parse_by_platform(platform)

            # 解析不到价格时再等 2 秒重试一次：价格区常为异步渲染
            if price <= 0 and not abort():
                if self._sleep(2.0, abort):
                    print("[BrowserEngine] 首次解析未取到价格，重试一次")
                    name2, price, original_price = self._parse_by_platform(platform)
                    name = name or name2

            if price <= 0:
                return FetchResult(name=name, status=STATUS_UNKNOWN,
                                   status_text="未能解析到价格", sku_text=sku_text)

            # ---- 5) 价格是否为"最低价起"这种区间价 ----
            if self._detect_price_range(platform):
                print("[BrowserEngine] 检测到页面显示的是区间价（最低价起），价格不可信")
                return FetchResult(name=name, price=price, original_price=original_price,
                                   status=STATUS_PRICE_RANGE,
                                   status_text="页面显示为最低价起，需锁定具体规格",
                                   sku_text=sku_text)

            return FetchResult(name=name, price=price, original_price=original_price,
                               status=STATUS_OK, sku_text=sku_text)
        except Exception as e:
            print(f"[BrowserEngine] 抓取商品信息失败: {e}")
            self._reset_if_dead()
            return FetchResult(status=STATUS_UNKNOWN, status_text=f"抓取异常: {str(e)[:80]}")

    # ==================== 页面状态 / 库存 / 规格 ====================

    # 下架类关键词（只认明确的失效文案，避免把推荐位里的"已下架"误判）
    DELISTED_KEYWORDS = (
        "宝贝不存在", "商品不存在", "您查看的宝贝", "该宝贝已删除",
        "商品已失效", "该商品已下架", "商品已下架", "页面找不到",
        "很抱歉，您查看的宝贝", "该商品已删除",
    )

    # 购买按钮上的无货文案
    NO_STOCK_BUTTON_WORDS = (
        "无货", "售罄", "缺货", "已抢光", "补货中", "到货通知", "已售完", "暂时缺货",
    )

    def _safe_name(self, platform: str) -> str:
        """尽力取一个商品名（状态异常时也要能显示是哪件商品）"""
        try:
            name = self._get_generic_name(platform) if platform in ("pdd", "douyin") else ""
            if not name and platform in ("taobao", "tmall"):
                name = self._get_taobao_name()
            elif not name and platform == "jd":
                name = self._get_jd_name()
            if not name:
                name = self._extract_name_from_title()
            return name or ""
        except Exception:
            return ""

    def _detect_page_status(self) -> Tuple[str, str]:
        """
        判断页面本身是否有效（下架 / 失效）

        只检查标题与明确的失效文案，避免把页面其它区域的文字误判成下架。
        """
        try:
            info = self.driver.execute_script(r"""
                var title = document.title || '';
                var body = document.body ? (document.body.innerText || '') : '';
                return {title: title, head: body.slice(0, 1500)};
            """)
        except Exception:
            return (STATUS_OK, "")

        title = (info or {}).get("title", "") or ""
        head = (info or {}).get("head", "") or ""
        for keyword in self.DELISTED_KEYWORDS:
            if keyword in title or keyword in head:
                return (STATUS_DELISTED, keyword)
        return (STATUS_OK, "")

    def _detect_stock_status(self) -> Tuple[str, str]:
        """
        判断当前选中规格是否可购买

        判定依据（收紧后只认两种信号）：
          1) 购买按钮（立即购买/加入购物车/马上抢…）的文案或禁用状态
          2) 页面里完整的缺货短语（如"该套餐暂时缺货"）

        特别注意：不能对整页做"售罄"子串匹配。
        淘宝详情页常挂"数量即将售罄""即将抢光"这类营销角标，表示"卖得快"，
        并不代表无货 —— 旧实现就是这样把在售商品误判成无货的。
        """
        try:
            info = self.driver.execute_script(r"""
                function norm(s){return (s||'').replace(/\s/g,'');}
                // 营销/紧迫感文案：含"售罄/抢光"但表示"快卖完"，必须排除
                // 营销角标：含"售罄/抢光"但只是"快卖完"，必须整体剔除（长的放前面）
                var promoWords = ['数量即将售罄','数量即将抢光','即将售罄','即将抢光','马上售罄',
                                  '马上抢光','快售罄','快抢光','仅剩','剩余','热卖','抢购中'];
                var noStockWords = ['无货','已售罄','售罄','缺货','已抢光','已售完','暂时缺货',
                                    '该地区无货','无法购买','到货通知','补货中'];
                var buyWords = ['立即购买','加入购物车','马上抢','领券购买','立即抢购','立即下单',
                                '去购买','立即订购','立即抢'];
                function stripPromo(t){
                    var r = t;
                    for (var i=0;i<promoWords.length;i++){ r = r.split(promoWords[i]).join(''); }
                    return r;
                }
                // 只有"按钮类"元素才拿来做无货判断，避免把页面上的文字角标当成无货
                function looksLikeButton(el){
                    var tag = (el.tagName||'').toUpperCase();
                    if (tag === 'BUTTON' || tag === 'A') return true;
                    var cls = (el.className||'').toString();
                    return /btn|button|Btn|Button/.test(cls);
                }
                var out = {found:false, disabled:false, text:'', noStockText:''};

                var nodes = document.querySelectorAll('button,a,div,span');
                var limit = Math.min(nodes.length, 4000);
                for (var i=0;i<limit;i++){
                    var el = nodes[i];
                    if (el.getClientRects().length === 0) continue;
                    var t = norm(el.innerText);
                    if (!t || t.length > 14) continue;

                    var isBuy = false;
                    for (var b=0;b<buyWords.length;b++){ if (t.indexOf(buyWords[b])>=0){isBuy=true;break;} }
                    var isAction = looksLikeButton(el);
                    if (!isBuy && !isAction) continue;

                    // 购买按钮：记录是否可用
                    if (isBuy){
                        out.found = true;
                        if (!out.text) out.text = t;
                        var dis = el.disabled === true ||
                                  (el.getAttribute && el.getAttribute('aria-disabled')==='true') ||
                                  /disabled|unable/i.test((el.className||'').toString());
                        if (dis) out.disabled = true;
                    }

                    // 无货文案（剔除营销词后再判断）
                    var clean = stripPromo(t);
                    for (var k=0;k<noStockWords.length;k++){
                        if (clean.indexOf(noStockWords[k])>=0){ out.noStockText = t; break; }
                    }
                    if (out.noStockText) break;
                }

                // ② 页面完整缺货短语
                if (!out.noStockText) {
                    var body = norm(document.body ? document.body.innerText : '');
                    var phrases = ['该套餐暂时缺货','此规格暂时缺货','该规格暂时缺货',
                                   '该商品暂时缺货','所选规格无货','该地区暂时无货',
                                   '商品已售完','该商品已下架'];
                    for (var p=0;p<phrases.length;p++){
                        if (body.indexOf(phrases[p])>=0){ out.noStockText = phrases[p]; break; }
                    }
                }
                return out;
            """)
        except Exception as e:
            print(f"[BrowserEngine] 库存检测脚本失败: {e}")
            return (STATUS_OK, "")

        info = info or {}
        no_stock_text = info.get("noStockText") or ""
        if no_stock_text:
            return (STATUS_NO_STOCK, no_stock_text)
        if info.get("found") and info.get("disabled"):
            return (STATUS_NO_STOCK, info.get("text") or "购买按钮不可用")
        return (STATUS_OK, "")
    def _detect_price_range(self, platform: str) -> bool:
        """
        判断页面价格是否为"最低价起"的区间价

        这类价格是"全商品最低 SKU 价"，不是你要的规格的价格，
        一旦采信就会把别的套餐价格记下来（本次要修的核心问题）。
        """
        try:
            texts = self.driver.execute_script(r"""
                var out = [];
                var nodes = document.querySelectorAll('[class*="price"],[class*="Price"]');
                var limit = Math.min(nodes.length, 300);
                for (var i=0;i<limit;i++){
                    var el = nodes[i];
                    if (el.children.length > 6) continue;
                    var t = (el.innerText||'').replace(/\s/g,'');
                    if (!t || t.length > 18) continue;
                    if (/\d/.test(t)) out.push(t);
                }
                return out;
            """)
        except Exception:
            return False

        for text in (texts or []):
            # "¥9198起" / "9198-11910" / "9198~11910"
            if "起" in text and any(ch.isdigit() for ch in text):
                return True
            if "-" in text or "~" in text:
                numbers = [seg for seg in text.replace("¥", "").replace("￥", "").split("-") if seg.strip().isdigit()]
                if len(numbers) >= 2:
                    return True
        return False

    def _wait_for_sku_ready(self, timeout: float = 8.0) -> bool:
        """
        等规格区渲染完成

        淘宝等页面是前端异步渲染的，刚打开时规格区可能还不存在，
        此时直接读/点会误判为"找不到规格"。这里轮询并配合滚动触发懒加载。
        """
        deadline = time.time() + timeout
        probe = (
            "return document.querySelectorAll("
            "'[class*=\"sku\"],[class*=\"Sku\"],[class*=\"valueItem\"]').length;"
        )
        while time.time() < deadline:
            try:
                count = self.driver.execute_script(probe)
                if count and int(count) > 0:
                    return True
            except Exception:
                pass
            try:
                self.driver.execute_script("window.scrollTo(0, 420);")
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _apply_sku_spec(self, sku_spec) -> Tuple[bool, str, str]:
        """
        按记录的规格依次选中页面上的选项，并校验是否真的生效

        返回 (是否成功, 实际选中的规格文本, 失败说明)
        """
        if not sku_spec:
            return (True, "", "")

        # 规格区可能是懒加载的：先等它渲染出来，否则会误判为"找不到规格"
        if not self._wait_for_sku_ready(8.0):
            return (False, "", "页面规格区未加载完成")

        # 先读一次当前选中状态：已经选中的规格不要再点。
        # 实测淘宝上再次点击"已选中"的规格会折叠该分组，反而导致读不到选中项。
        current = self._read_selected_sku()
        current_norm = [self._norm_text(item) for item in current]

        applied = []
        failed = []
        for item in sku_spec:
            option = str(item).split("=", 1)[-1].strip()
            if not option:
                continue
            option_norm = self._norm_text(option)
            if any(option_norm in sel for sel in current_norm):
                # 已经是选中态，跳过点击
                applied.append(str(item))
                continue
            if self._click_sku_option(option):
                applied.append(str(item))
            else:
                failed.append(option)

        # 点完规格后页面会异步重渲染，读取"当前选中"要重试几次，
        # 否则会在过渡期间读到空列表，误判为"选中未生效"
        selected = []
        for _ in range(4):
            self._sleep(0.5, lambda: False)
            selected = self._read_selected_sku()
            if applied and all(
                any(self._norm_text(item.split("=", 1)[-1]) in self._norm_text(sel) for sel in selected)
                for item in applied
            ):
                break

        selected_text = " / ".join(selected)
        # 校验：点过的规格必须出现在"当前选中"列表里（忽略空格差异）
        missing = []
        for item in applied:
            option = self._norm_text(item.split("=", 1)[-1])
            if not any(option in self._norm_text(sel) for sel in selected):
                missing.append(item.split("=", 1)[-1].strip())

        if failed or missing:
            reasons = []
            if failed:
                reasons.append("找不到规格：" + "、".join(failed))
            if missing:
                reasons.append("选中未生效：" + "、".join(missing))
            return (False, selected_text, "；".join(reasons))
        return (True, selected_text, "")

    @staticmethod
    def _norm_text(text: str) -> str:
        """
        归一化规格文本：去空白 + 去尾部标点

        页面上规格文字常带句点/顿号（如 "2TB."），记录时可能没有，
        不处理会出现"明明有这个规格却匹配不上"。
        """
        s = "".join(str(text or "").split())
        return s.rstrip(".。·,，;；:：、-—_")

    def _click_sku_option(self, option_text: str) -> bool:
        """
        点击与给定文本一致的规格选项

        真实页面结构（淘宝已验证）：
          div.skuItem--哈希 > div.skuItemClipX--哈希 > div.skuValueWrap--哈希
            > div.content--哈希 > div.valueItem--哈希(选中时追加 isSelected--哈希)
              > span.valueItemText--哈希
        所以：限定在 sku 容器内匹配文字，并优先点击 valueItem 这一层。
        """
        try:
            return bool(self.driver.execute_script(r"""
                function norm(s){return (s||'').replace(/\s/g,'');}
                function trimPunct(s){return s.replace(/[.。·,，;；:：、\-—_'"“”‘’]+$/g,'');}
                var want = trimPunct(norm(WANT_TEXT));
                function inSku(el){
                    var p = el;
                    for (var k=0;k<8 && p;k++){
                        var cls = (p.className||'').toString().toLowerCase();
                        if (cls.indexOf('sku') >= 0) return true;
                        p = p.parentElement;
                    }
                    return false;
                }
                var nodes = document.querySelectorAll(
                    'div[class*="valueItem"], span[class*="valueItemText"],' +
                    'div[class*="skuItem"], span[class*="sku-item"],' +
                    'div[class*="sku"], span[class*="sku"],' +
                    'li, button, a, label, [role="radio"], [role="option"]'
                );
                var limit = Math.min(nodes.length, 6000);

                // 两阶段匹配：先找文字完全一致的，再允许"短后缀"（如角标"多人加购"）。
                // 只做前缀匹配会先命中分组容器这类"文字以选项开头"的大元素，点了没反应。
                function pick(allowPrefix){
                    var best = null, bestExtra = 99;
                    for (var i=0;i<limit;i++){
                        var el = nodes[i];
                        if (el.getClientRects().length === 0) continue;
                        if (el.children.length > 3) continue;
                        if (!inSku(el)) continue;
                        var text = trimPunct(norm(el.innerText));
                        if (!text) continue;
                        if (text === want) return el;
                        if (allowPrefix && text.indexOf(want) === 0){
                            var extra = text.length - want.length;
                            if (extra <= 8 && extra < bestExtra){ best = el; bestExtra = extra; }
                        }
                        // 反向前缀：记录的规格比页面文字更长（多了标点/后缀）也要能命中
                        if (allowPrefix && want.indexOf(text) === 0){
                            var extra2 = want.length - text.length;
                            if (extra2 <= 6 && extra2 < bestExtra){ best = el; bestExtra = extra2; }
                        }
                    }
                    return best;
                }

                var hit = pick(false) || pick(true);
                if (!hit) return false;

                // 向上找 valueItem 这一层再点，命中率更高
                var target = hit;
                var p = hit;
                for (var d=0; d<4 && p; d++){
                    var cls = (p.className||'').toString();
                    if (cls.indexOf('valueItem') >= 0) { target = p; break; }
                    p = p.parentElement;
                }
                try { target.scrollIntoView({block:'center'}); } catch(e) {}
                try { target.click(); } catch(e) {}
                return true;
            """.replace("WANT_TEXT", _js_string(option_text))))
        except Exception as e:
            print(f"[BrowserEngine] 点击规格失败: {e}")
            return False

    def _read_selected_sku(self) -> list:
        """
        读取页面上当前选中的规格，返回 ["版本类型=日版", "套餐=单机标配"] 形式

        实测淘宝的选中态是 isSelected--哈希（大写 S），
        之前只匹配小写 selected 因此读不到任何规格。
        """
        try:
            texts = self.driver.execute_script(r"""
                function norm(s){return (s||'').replace(/\s+/g,' ').trim();}
                function groupTitle(el){
                    var p = el;
                    for (var d=0; d<8 && p; d++){
                        var cls = (p.className||'').toString();
                        if (/skuItem--/.test(cls)){
                            // ① 优先找显式的标题元素（如 class 含 title）
                            var tEl = p.querySelector('[class*="title"], [class*="Title"]');
                            if (tEl){
                                var tt = norm(tEl.innerText);
                                if (tt && tt.length <= 8) return tt;
                            }
                            // ② 退而取规格区首行（实测淘宝首行就是"版本类型"这类组名）
                            var clip = p.querySelector('[class*="skuItemClipX"]') || p;
                            var first = norm((clip.innerText || '').split('\n')[0]);
                            if (first && first.length <= 8) return first;
                            return '';
                        }
                        p = p.parentElement;
                    }
                    return '';
                }
                var out = [];
                var nodes = document.querySelectorAll(
                    '[class*="isSelected"], [class*="selected"], [class*="Selected"],' +
                    '[aria-checked="true"], [aria-selected="true"]'
                );
                var limit = Math.min(nodes.length, 300);
                for (var i=0;i<limit;i++){
                    var el = nodes[i];
                    // 用 getClientRects 判断可见性：规格面板常用 transform/scroll 容器，
                    // offsetParent 在某些情况下为 null，会把正常元素误过滤掉
                    if (el.getClientRects().length === 0) continue;
                    var cls = (el.className||'').toString();
                    // 只认规格项：valueItem 或位于 sku 容器内
                    if (cls.indexOf('valueItem') < 0 && !/sku/i.test(cls)
                        && !el.getAttribute('aria-checked') && !el.getAttribute('aria-selected')) continue;

                    var span = el.querySelector('[class*="valueItemText"]');
                    var text = norm(span ? span.innerText : el.innerText);
                    if (!text || text.length > 30) continue;

                    var group = groupTitle(el);
                    var item = group ? (group + '=' + text) : text;
                    if (out.indexOf(item) < 0) out.push(item);
                }
                return out;
            """)
        except Exception as e:
            print(f"[BrowserEngine] 读取规格失败: {e}")
            return []
        return list(texts or [])

    def record_current_sku(self, url: str, platform: str) -> list:
        """
        打开商品页，读取用户当前手动选中的规格

        用于「记录当前规格」功能：用户在浏览器里选好想要的版本/套餐后，
        由主窗口调用本方法把选中项抓下来存进商品配置。
        """
        if not self.driver:
            if not self.start_browser():
                return []
        self._ensure_platform_cookie(platform)
        try:
            self.driver.get(url)
            self._wait_page_ready(10)
            self._sleep(2.0, lambda: False)
            self._try_close_popups()
            return self._read_selected_sku()
        except Exception as e:
            print(f"[BrowserEngine] 读取当前规格失败: {e}")
            return []

    def _parse_by_platform(self, platform: str) -> Tuple[str, float, float]:
        """按平台分派到对应解析器"""
        if platform in ("taobao", "tmall"):
            return self._parse_taobao_tmall()
        if platform == "jd":
            return self._parse_jd()
        if platform == "pdd":
            return self._parse_pdd()
        if platform == "douyin":
            return self._parse_douyin()
        print(f"[BrowserEngine] 不支持的平台: {platform}")
        return ("", 0.0, 0.0)

    def _try_close_popups(self):
        """
        尝试关闭页面上的弹窗/遮罩层

        注意：选择器必须限定在"弹窗容器内"，不能用裸的 [class*='close']。
        实测淘宝页面里规格面板也有 class 含 close 的元素，宽泛匹配会把
        规格面板一起点掉，导致后续读不到规格、抓不到价格。
        """
        close_selectors = [
            ".baxia-dialog-close",
            ".J_Close",
            "[class*='dialog'] [class*='close']",
            "[class*='Dialog'] [class*='close']",
            "[class*='modal'] [class*='close']",
            "[class*='Modal'] [class*='close']",
            "[class*='mask'] [class*='close']",
            "button[aria-label='Close']",
        ]
        for sel in close_selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                # 最多点 3 个，避免误伤；且只点尺寸很小的关闭按钮
                for el in elements[:3]:
                    try:
                        if not el.is_displayed():
                            continue
                        rect = el.rect or {}
                        if rect.get("width", 0) > 80 or rect.get("height", 0) > 80:
                            continue
                        el.click()
                        time.sleep(0.4)
                    except Exception:
                        continue
            except Exception:
                continue

    # ==================== 淘宝/天猫解析 ====================

    def _parse_taobao_tmall(self) -> Tuple[str, float, float]:
        """解析淘宝/天猫商品页面（多策略兜底）"""
        name = self._get_taobao_name()
        price, original_price = self._get_taobao_price()

        if not name:
            name = self._js_extract_taobao_name()
        if price <= 0:
            price = self._js_extract_taobao_price()
        if original_price <= 0 and price > 0:
            original_price = self._find_original_price(price, "taobao")

        if not name:
            name = self._extract_name_from_title()

        print(f"[BrowserEngine] 淘宝/天猫解析结果: name={name[:30] if name else '(空)'}, "
              f"price={price}, original_price={original_price}")
        return (name, price, original_price)

    def _get_taobao_name(self) -> str:
        """通过CSS选择器获取淘宝/天猫商品名称"""
        name_selectors = [
            "[class*='ItemHeader'] h1",
            "[class*='mainTitle']",
            "[class*='item-header'] h1",
            "[class*='itemTitle']",
            "h1.tb-detail-hd",
            ".tb-detail-hd h1",
            ".tb-detail-hd h3",
            "h1[data-spm='1000983']",
            "h1.tb-main-title",
            ".tb-main-title",
            ".item-title",
            ".J_Title",
            ".tb-title h1",
            "h3[data-title]",
            ".tb-detail-hd",
            "h1",
        ]
        for sel in name_selectors:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                text = el.text.strip()
                if text and len(text) > 2:
                    text = re.sub(r'\s+', ' ', text).strip()
                    return text
            except Exception:
                continue
        return ""

    def _get_taobao_price(self) -> Tuple[float, float]:
        """通过CSS选择器获取淘宝/天猫价格"""
        price = 0.0
        original_price = 0.0

        price_selectors = [
            "[class*='Price'] [class*='text']",
            "[class*='price'] [class*='current']",
            "[class*='Price'][class*='price']",
            "[class*='priceNow']",
            "[class*='price-now']",
            "[class*='promotionPrice']",
            ".tm-price .tm-promo-price .tm-price",
            ".tm-promo-price",
            ".tm-price",
            ".tb-rmb-num",
            "#J_StrPrice",
            ".tb-price",
            ".tb-price .tb-rmb-num",
            ".J_Price",
            "[class*='price']",
        ]
        for sel in price_selectors:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                text = el.text.strip()
                p = self._extract_price(text)
                if p > 0:
                    price = p
                    print(f"[BrowserEngine] 价格匹配成功: selector={sel}, text={text}, price={p}")
                    break
            except Exception:
                continue

        # 原价（划线价）：分层策略见 _find_original_price
        original_price = self._find_original_price(price, "taobao")

        return (price, original_price)

    def _js_extract_taobao_name(self) -> str:
        """通过JS从淘宝/天猫页面提取商品名称"""
        js_scripts = [
            "return document.querySelector('meta[property=\"og:title\"]')?.content || '';",
            (
                "var h1s = document.querySelectorAll('h1');"
                "for (var h of h1s) {"
                "  var t = h.innerText.trim();"
                "  if (t.length > 5 && t.length < 200) return t;"
                "}"
                "return '';"
            ),
            (
                "var title = document.title || '';"
                "title = title.replace(/[-_|].*(淘宝|天猫|taobao|tmall).*/i, '').trim();"
                "return title;"
            ),
            (
                "var scripts = document.querySelectorAll('script');"
                "for (var s of scripts) {"
                "  var text = s.innerText || s.textContent || '';"
                "  var match = text.match(/\"title\"\\\\s*:\\\\s*\"([^\"]{5,200})\"/);"
                "  if (match) return match[1];"
                "}"
                "return '';"
            ),
        ]
        for js in js_scripts:
            try:
                result = self.driver.execute_script(js)
                if result and len(str(result).strip()) > 2:
                    name = str(result).strip()
                    name = re.sub(r'\s+', ' ', name)
                    if len(name) > 200:
                        name = name[:200]
                    return name
            except Exception:
                continue
        return ""

    def _js_extract_taobao_price(self) -> float:
        """通过JS从淘宝/天猫页面提取当前价格"""
        js_scripts = [
            (
                "var allElements = document.querySelectorAll('span, div, em, strong, p');"
                "var prices = [];"
                "for (var el of allElements) {"
                "  var text = el.innerText || '';"
                "  var match = text.match(/^[¥￥]?\\s*(\\d+\\.?\\d*)\\s*$/);"
                "  if (match) {"
                "    var p = parseFloat(match[1]);"
                "    if (p > 0 && p < 10000000) {"
                "      var parent = el.parentElement;"
                "      var classes = (parent ? parent.className : '') + ' ' + el.className;"
                "      if (/price|Price|价|promo/i.test(classes)) {"
                "        return p;"
                "      }"
                "      prices.push(p);"
                "    }"
                "  }"
                "}"
                "if (prices.length > 0) return prices[0];"
                "return 0;"
            ),
            (
                "var scripts = document.querySelectorAll('script');"
                "for (var s of scripts) {"
                "  var text = s.innerText || s.textContent || '';"
                "  var patterns = ["
                "    /\"price\"\\\\s*:\\\\s*\"?([\\d.]+)\"?/,"
                "    /\"promotionPrice\"\\\\s*:\\\\s*\"?([\\d.]+)\"?/,"
                "    /\"currentPrice\"\\\\s*:\\\\s*\"?([\\d.]+)\"?/,"
                "    /price['\":\\s]+['\"]*(\\d+\\.?\\d*)/"
                "  ];"
                "  for (var pat of patterns) {"
                "    var match = text.match(pat);"
                "    if (match) {"
                "      var p = parseFloat(match[1]);"
                "      if (p > 0 && p < 10000000) return p;"
                "    }"
                "  }"
                "}"
                "return 0;"
            ),
            (
                "var elements = document.querySelectorAll('[class*=\"price\"], [class*=\"Price\"]');"
                "for (var el of elements) {"
                "  var text = el.innerText || '';"
                "  var match = text.match(/(\\d+\\.?\\d*)/);"
                "  if (match) {"
                "    var p = parseFloat(match[1]);"
                "    if (p > 0 && p < 10000000) return p;"
                "  }"
                "}"
                "return 0;"
            ),
        ]
        for js in js_scripts:
            try:
                result = self.driver.execute_script(js)
                if result:
                    p = float(result)
                    if p > 0:
                        print(f"[BrowserEngine] JS提取价格成功: {p}")
                        return p
            except Exception:
                continue
        return 0.0

    # ==================== 京东解析 ====================

    def _parse_jd(self) -> Tuple[str, float, float]:
        """解析京东商品页面（多策略兜底）"""
        name = self._get_jd_name()
        price, original_price = self._get_jd_price()

        if not name:
            name = self._js_extract_jd_name()
        if price <= 0:
            price = self._js_extract_jd_price()
        if original_price <= 0 and price > 0:
            original_price = self._find_original_price(price, "jd")

        if not name:
            name = self._extract_name_from_title()

        print(f"[BrowserEngine] 京东解析结果: name={name[:30] if name else '(空)'}, "
              f"price={price}, original_price={original_price}")
        return (name, price, original_price)

    def _get_jd_name(self) -> str:
        """通过CSS选择器获取京东商品名称"""
        name_selectors = [
            ".sku-name",
            ".itemInfo-wrap .sku-name",
            "[class*='itemTitle']",
            ".product-intro .name",
            "div.title",
            ".product-name",
            ".sku-title",
            ".item-title",
            "h1.title",
            ".itemInfo .name",
            ".p-name",
            "h1",
        ]
        for sel in name_selectors:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                text = el.text.strip()
                if text and len(text) > 2:
                    text = re.sub(r'\s+', ' ', text).strip()
                    return text
            except Exception:
                continue
        return ""

    def _get_jd_price(self) -> Tuple[float, float]:
        """通过CSS选择器获取京东价格"""
        price = 0.0
        original_price = 0.0

        try:
            WebDriverWait(self.driver, 8).until(
                lambda d: any(
                    d.find_element(By.CSS_SELECTOR, sel).text.strip() != ""
                    for sel in [".p-price .price", "#jd-price", ".p-price"]
                    if d.find_elements(By.CSS_SELECTOR, sel)
                )
            )
        except Exception:
            pass

        price_selectors = [
            ".p-price .price",
            ".p-price span:nth-child(2)",
            "#jd-price",
            ".summary-price .price",
            ".p-price",
            "[class*='price'] [class*='Price']",
            ".J-p-price",
        ]
        for sel in price_selectors:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                text = el.text.strip()
                p = self._extract_price(text)
                if p > 0:
                    price = p
                    print(f"[BrowserEngine] 京东价格匹配成功: selector={sel}, text={text}, price={p}")
                    break
            except Exception:
                continue

        # 原价（划线价）：分层策略见 _find_original_price
        original_price = self._find_original_price(price, "jd")

        return (price, original_price)

    def _js_extract_jd_name(self) -> str:
        """通过JS从京东页面提取商品名称"""
        js_scripts = [
            "return document.querySelector('meta[property=\"og:title\"]')?.content || '';",
            (
                "var h1s = document.querySelectorAll('h1, .sku-name, .title');"
                "for (var h of h1s) {"
                "  var t = h.innerText.trim();"
                "  if (t.length > 5 && t.length < 200) return t;"
                "}"
                "return '';"
            ),
            (
                "var scripts = document.querySelectorAll('script');"
                "for (var s of scripts) {"
                "  var text = s.innerText || s.textContent || '';"
                "  var match = text.match(/\"name\"\\\\s*:\\\\s*\"([^\"]{5,200})\"/);"
                "  if (match) return match[1];"
                "}"
                "return '';"
            ),
        ]
        for js in js_scripts:
            try:
                result = self.driver.execute_script(js)
                if result and len(str(result).strip()) > 2:
                    return str(result).strip()[:200]
            except Exception:
                continue
        return ""

    def _js_extract_jd_price(self) -> float:
        """通过JS从京东页面提取价格"""
        js_scripts = [
            (
                "var elements = document.querySelectorAll('[class*=\"price\"], [class*=\"Price\"]');"
                "for (var el of elements) {"
                "  var text = el.innerText || '';"
                "  var match = text.match(/(\\d+\\.?\\d*)/);"
                "  if (match) {"
                "    var p = parseFloat(match[1]);"
                "    if (p > 0 && p < 10000000) return p;"
                "  }"
                "}"
                "return 0;"
            ),
            (
                "var scripts = document.querySelectorAll('script');"
                "for (var s of scripts) {"
                "  var text = s.innerText || s.textContent || '';"
                "  var match = text.match(/\"price\"\\\\s*:\\\\s*\"?([\\d.]+)\"?/);"
                "  if (match) {"
                "    var p = parseFloat(match[1]);"
                "    if (p > 0 && p < 10000000) return p;"
                "  }"
                "}"
                "return 0;"
            ),
        ]
        for js in js_scripts:
            try:
                result = self.driver.execute_script(js)
                if result:
                    p = float(result)
                    if p > 0:
                        return p
            except Exception:
                continue
        return 0.0

    # ==================== 拼多多 / 抖音商城解析 ====================

    # 这两个平台的详情页由前端框架渲染、class 名混淆，选择器只作为第一层
    NAME_SELECTORS = {
        "pdd": [
            "[class*='goodsName']", "[class*='goods-name']", "[class*='goodsTitle']",
            "[class*='title']", "h1",
        ],
        "douyin": [
            "[class*='productName']", "[class*='productTitle']",
            "[class*='title']", "h1",
        ],
    }

    PRICE_SELECTORS = {
        "pdd": [
            "[class*='goodsPrice']", "[class*='salePrice']", "[class*='price']",
            "[class*='Price']",
        ],
        "douyin": [
            "[class*='salePrice']", "[class*='priceText']", "[class*='price']",
            "[class*='Price']",
        ],
    }

    # 页面脚本里的价格字段（拼多多/抖音接口普遍以“分”为单位）
    SCRIPT_PRICE_KEYS = {
        "pdd": ["min_group_price", "group_price", "normal_price", "price",
                "minPrice", "groupPrice", "priceStr"],
        "douyin": ["price", "minPrice", "skuPrice", "salePrice", "priceStr"],
    }

    def _parse_pdd(self) -> Tuple[str, float, float]:
        """解析拼多多商品页"""
        return self._parse_generic("pdd", "拼多多")

    def _parse_douyin(self) -> Tuple[str, float, float]:
        """解析抖音商城商品页"""
        return self._parse_generic("douyin", "抖音商城")

    def _parse_generic(self, platform: str, label: str) -> Tuple[str, float, float]:
        """通用解析流程：名称 → 价格 → 原价"""
        name = self._get_generic_name(platform)
        if not name:
            name = self._js_extract_taobao_name()
        if not name:
            name = self._extract_name_from_title()

        price = self._get_generic_price(platform)
        original_price = self._find_original_price(price, platform) if price > 0 else 0.0

        print(f"[BrowserEngine] {label}解析结果: name={name[:30] if name else '(空)'}, "
              f"price={price}, original_price={original_price}")
        return (name, price, original_price)

    def _get_generic_name(self, platform: str) -> str:
        """按选择器取商品名称"""
        for selector in self.NAME_SELECTORS.get(platform, []):
            try:
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    text = (element.text or "").strip()
                    if len(text) > 2:
                        return re.sub(r"\s+", " ", text)[:200]
            except Exception:
                continue
        return ""

    def _get_generic_price(self, platform: str) -> float:
        """
        通用价格解析

        顺序：语义选择器 → 可见文本里的 ￥ 金额 → 页面脚本价格字段。
        脚本里的价格通常以“分”为单位，需要除以 100。
        """
        # 1) 选择器
        for selector in self.PRICE_SELECTORS.get(platform, []):
            try:
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    price = self._extract_price((element.text or "").strip())
                    if price > 0:
                        print(f"[BrowserEngine] {platform} 价格(选择器): {selector} -> {price}")
                        return price
            except Exception:
                continue

        # 2) 可见文本里的 ¥ 金额（渲染后的真实值，最可靠）
        text_prices = self._prices_from_visible_text()
        if text_prices:
            price = min(text_prices)
            print(f"[BrowserEngine] {platform} 价格(文本): {price}  候选={text_prices[:6]}")
            return price

        # 3) 页面脚本字段，按“分”换算成“元”
        for value in self._script_price_candidates(platform):
            price = round(value / 100.0, 2)
            if 0.01 <= price < 1000000:
                print(f"[BrowserEngine] {platform} 价格(脚本, 分→元): {price}")
                return price

        return 0.0

    def _prices_from_visible_text(self) -> List[float]:
        """扫描可见文本里带 ￥ 符号的金额"""
        script = r"""
        var out = [];
        var nodes = document.querySelectorAll('span,div,em,strong,p,b,i');
        var limit = Math.min(nodes.length, 6000);
        for (var i = 0; i < limit; i++) {
            var el = nodes[i];
            if (el.children.length > 3) continue;
            var t = (el.innerText || '').trim();
            if (!t || t.length > 16) continue;
            var m = t.match(/[¥￥]\s*(\d+(?:\.\d+)?)/);
            if (m) out.push(parseFloat(m[1]));
        }
        return out;
        """
        return self._run_price_script(script)

    def _script_price_candidates(self, platform: str) -> List[float]:
        """从页面脚本里抓取价格字段的原始数值（可能是“分”）"""
        keys = self.SCRIPT_PRICE_KEYS.get(platform, ["price"])
        keys_js = ",".join(f'"{key}"' for key in keys)
        script = r"""
        var keys = [__KEYS__];
        var pattern = new RegExp('"(' + keys.join('|') + ')"\\s*:\\s*"?([\\d.]+)', 'gi');
        var out = [];
        var scripts = document.querySelectorAll('script');
        var limit = Math.min(scripts.length, 400);
        for (var i = 0; i < limit; i++) {
            var text = scripts[i].textContent || '';
            if (!text || text.length > 3000000) continue;
            pattern.lastIndex = 0;
            var m = null;
            var guard = 0;
            while ((m = pattern.exec(text)) !== null && guard++ < 60) {
                var v = parseFloat(m[2]);
                if (v > 0) out.push(v);
            }
        }
        return out;
        """
        return self._run_price_script(script.replace("__KEYS__", keys_js))

    # ==================== 原价（划线价）解析 ====================

    ORIGINAL_SELECTORS = {
        "taobao": [
            "[class*='originPrice']",
            "[class*='origin-price']",
            "[class*='OriginPrice']",
            "[class*='originalPrice']",
            "[class*='priceOrigin']",
            "[class*='marketPrice']",
            "[class*='Price'] [class*='origin']",
            ".tm-price-original .tm-price",
            ".tb-price-original .tb-rmb-num",
            ".tb-price-del .tb-rmb-num",
        ],
        "jd": [
            "[class*='originPrice']",
            "[class*='originalPrice']",
            "[class*='linePrice']",
            ".p-price .del",
            ".summary-price .del",
            ".J-original-price",
        ],
        "pdd": [
            "[class*='marketPrice']",
            "[class*='MarketPrice']",
            "[class*='linePrice']",
            "[class*='normalPrice']",
        ],
        "douyin": [
            "[class*='marketPrice']",
            "[class*='originPrice']",
            "[class*='originalPrice']",
            "[class*='linePrice']",
        ],
    }

    def _find_original_price(self, current_price: float, platform: str) -> float:
        """
        查找原价（划线价）

        淘宝/天猫/京东详情页的 class 名大多被混淆成 priceWrap--xxxx 这类形式，
        只靠 CSS 选择器命中率很低（旧实现因此一直返回 0）。这里按可靠性分层：
          1) 语义明确的 class 选择器
          2) 带删除线样式的元素 —— 划线价最本质的特征，与 class 名无关
          3) 页面脚本里的 originalPrice / reservePrice 等字段
          4) 可见文本里"原价/划线价/市场价"附近的数字

        并做合理性校验：原价必须大于当前价且不超过当前价的 10 倍
        （淘宝脚本里的价格有时以"分"为单位，会大 100 倍，用区间过滤掉）。
        """
        if current_price <= 0:
            return 0.0

        candidates = []
        candidates += self._original_price_by_selector(platform)
        candidates += self._original_price_by_line_through()
        candidates += self._original_price_by_script()
        candidates += self._original_price_by_label()

        # 拼多多/抖音等接口里的原价常以“分”为单位，补一份除以 100 的候选
        candidates += [value / 100.0 for value in list(candidates)]

        lower = current_price
        upper = current_price * 10
        valid = sorted({p for p in candidates if lower < p <= upper})

        if not valid:
            if candidates:
                print(f"[BrowserEngine] 原价候选 {candidates[:6]} 均不合理（当前价 {current_price}），忽略")
            else:
                print(f"[BrowserEngine] 未找到原价（当前价 {current_price}）")
            return 0.0

        # 划线价通常只略高于售价，取最接近的，避免误取套餐价/运费等
        value = valid[0]
        print(f"[BrowserEngine] 原价解析成功: {value}（当前价 {current_price}）")
        return value

    def _original_price_by_selector(self, platform: str) -> List[float]:
        """按语义明确的 class 选择器找原价"""
        values = []
        selectors = self.ORIGINAL_SELECTORS.get(platform, self.ORIGINAL_SELECTORS["taobao"])
        for selector in selectors:
            try:
                for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                    price = self._extract_price((element.text or "").strip())
                    if price > 0:
                        values.append(price)
            except Exception:
                continue
        return values

    def _original_price_by_line_through(self) -> List[float]:
        """找带删除线样式的元素：划线价的本质特征，不依赖 class 名"""
        script = r"""
        var out = [];
        var nodes = document.querySelectorAll('span,div,em,strong,p,del,s,i');
        var limit = Math.min(nodes.length, 5000);
        for (var i = 0; i < limit; i++) {
            var el = nodes[i];
            var text = (el.innerText || '').replace(/\s/g, '');
            if (!text || text.length > 20) continue;
            if (!/\d/.test(text)) continue;
            var style = window.getComputedStyle(el);
            var decoration = (style.textDecorationLine || '') + ' ' +
                             (style.textDecoration || '') + ' ' +
                             (style.textDecorationStyle || '');
            if (decoration.indexOf('line-through') < 0) continue;
            var m = text.replace(/,/g, '').match(/(\d+(?:\.\d+)?)/);
            if (m) out.push(parseFloat(m[1]));
        }
        return out;
        """
        return self._run_price_script(script)

    def _original_price_by_script(self) -> List[float]:
        """从页面内嵌脚本/JSON 里找原价字段"""
        script = r"""
        var keys = ['originalPrice','originPrice','reservePrice','marketPrice',
                    'delistPrice','origPrice','linePrice','priceOrigin','original_price',
                    'market_price','normal_price','delPrice'];
        var pattern = new RegExp('"(' + keys.join('|') + ')"\\s*:\\s*"?([\\d.]+)', 'gi');
        var out = [];
        var scripts = document.querySelectorAll('script');
        var limit = Math.min(scripts.length, 400);
        for (var i = 0; i < limit; i++) {
            var text = scripts[i].textContent || '';
            if (!text || text.length > 3000000) continue;
            pattern.lastIndex = 0;
            var m = null;
            var guard = 0;
            while ((m = pattern.exec(text)) !== null && guard++ < 50) {
                var v = parseFloat(m[2]);
                if (v > 0) out.push(v);
            }
        }
        return out;
        """
        return self._run_price_script(script)

    def _original_price_by_label(self) -> List[float]:
        """
        兜底：从可见文本里"优惠前/原价/划线价"这类标签后面取数字

        淘宝现在的写法是「券后 ￥9498　优惠前 ￥11910」，用的是"优惠前"而非"原价"，
        且没有删除线样式，所以这个标签必须包含进来。
        """
        script = r"""
        var body = document.body;
        if (!body) return [];
        var text = body.innerText || '';
        if (!text) return [];
        var out = [];
        var pattern = /(?:优惠前|原价|划线价|市场价|专柜价|吊牌价|建议零售价)\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d+)?)/g;
        var m = null;
        var guard = 0;
        while ((m = pattern.exec(text)) !== null && guard++ < 50) {
            var v = parseFloat(m[1]);
            if (v > 0) out.push(v);
        }
        return out;
        """
        return self._run_price_script(script)

    def _run_price_script(self, script: str) -> List[float]:
        """执行返回数字数组的 JS，并过滤掉明显不合理的值"""
        try:
            result = self.driver.execute_script(script)
        except Exception as e:
            print(f"[BrowserEngine] 原价脚本执行失败: {e}")
            return []

        values = []
        for item in (result or []):
            try:
                value = float(item)
            except (TypeError, ValueError):
                continue
            if 0 < value < 10000000:
                values.append(value)
        return values

    # ==================== 通用方法 ====================

    def _extract_name_from_title(self) -> str:
        """从页面title标签提取商品名称（最后兜底方案）"""
        try:
            title = self.driver.title or ""
            if not title:
                return ""
            for suffix in ["-淘宝", "-天猫", "-京东", "-jd.com", "-taobao.com",
                           "-tmall.com", "淘宝网", "天猫", "京东", "taobao", "tmall", "jd"]:
                if suffix in title:
                    title = title.split(suffix)[0]
            for sep in [" - ", " — ", " | ", " _ ", "【"]:
                if sep in title:
                    title = title.split(sep)[0]
            title = title.strip()
            if len(title) > 5:
                print(f"[BrowserEngine] 从title提取名称: {title}")
                return title
        except Exception:
            pass
        return ""

    @staticmethod
    def _extract_price(text: str) -> float:
        """从文本中提取价格数字"""
        if not text:
            return 0.0
        match = re.search(r"[¥￥]?\s*(\d+\.?\d*)", text.replace(",", ""))
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return 0.0
        return 0.0
