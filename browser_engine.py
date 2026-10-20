"""
浏览器引擎 - 负责Selenium浏览器管理、登录、Cookie管理、商品信息抓取
"""
import json
import os
import re
import threading
import time
from typing import List, Tuple

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from paths import DATA_DIR
from platforms import PLATFORMS
from browser_detector import detect_browsers
from driver_manager import download_driver, find_cached_driver


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

    def __init__(self, data_dir: str = "", browser: str = "chrome"):
        self.data_dir = data_dir or DATA_DIR
        self.browser = browser if browser in self.BROWSER_NAMES else "chrome"
        self.binary_path = ""      # 由调用方按浏览器检测结果填入
        self.last_error = ""       # 最近一次启动失败的可读原因

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

        Edge 必须自己下载：Selenium Manager 用的旧域名已失效，会回退到不匹配的缓存驱动。
        Chrome 交给 Selenium Manager（工作正常），失败时再由 start_browser 兜底下载。
        """
        version = self._detected_version()
        if not version:
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
        self.driver.set_window_size(1280, 900)
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
            with open(path, "r", encoding="utf-8") as f:
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

    def fetch_product_info(self, url: str, platform: str,
                           should_abort=None) -> Tuple[str, float, float]:
        """
        抓取商品信息：返回 (商品名称, 当前价格, 原价)

        should_abort: 可选回调，返回 True 时尽快中断本次抓取（用于超时回收 / 退出程序），
                      中断时返回空结果，调用方据此丢弃本次抓取。
        """
        abort = should_abort or (lambda: False)

        if abort():
            return ("", 0.0, 0.0)

        self._reset_if_dead()

        if not self.driver:
            if not self.start_browser():
                return ("", 0.0, 0.0)

        self._ensure_platform_cookie(platform)
        if abort():
            return ("", 0.0, 0.0)

        try:
            self.driver.get(url)
            self._wait_page_ready(8)
            if not self._sleep(1.5, abort):
                return ("", 0.0, 0.0)

            self._try_close_popups()
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight / 4);")
            if not self._sleep(1.5, abort):
                return ("", 0.0, 0.0)
            self.driver.execute_script("window.scrollTo(0, 0);")
            if not self._sleep(0.8, abort):
                return ("", 0.0, 0.0)

            result = self._parse_by_platform(platform)

            # 解析不到价格时再等 2 秒重试一次：价格区常为异步渲染，首屏可能还没填充
            if result[1] <= 0 and not abort():
                if self._sleep(2.0, abort):
                    print("[BrowserEngine] 首次解析未取到价格，重试一次")
                    result = self._parse_by_platform(platform)

            return result
        except Exception as e:
            print(f"[BrowserEngine] 抓取商品信息失败: {e}")
            self._reset_if_dead()
            return ("", 0.0, 0.0)

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
        """尝试关闭页面上的弹窗/遮罩层"""
        close_selectors = [
            ".baxia-dialog-close",
            ".J_Close",
            "[class*='close']",
            ".dialog-close",
            ".modal-close",
            "button[aria-label='Close']",
        ]
        for sel in close_selectors:
            try:
                elements = self.driver.find_elements(By.CSS_SELECTOR, sel)
                for el in elements:
                    if el.is_displayed():
                        el.click()
                        time.sleep(0.5)
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