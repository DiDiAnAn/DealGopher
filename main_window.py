"""
主窗口 - PyQt5界面
"""
import sys
import os
import logging
import threading
from datetime import datetime
from typing import List, Optional

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QPushButton, QLabel, QDialog,
    QMessageBox, QHeaderView, QSplitter, QTabWidget,
    QAbstractItemView, QMenu, QApplication,
    QAction, QFileDialog
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt5.QtGui import QFont, QColor

from models import Product, DataStore
import status as st
from browser_engine import BrowserEngine
from browser_detector import detect_browsers
from browser_dialog import ask_browser
from chart_widget import PriceChartWidget
from alert_dialog import AlertDialog
from add_product_dialog import AddProductDialog
from paths import BASE_DIR, LOG_DIR, XLS_FILE, ensure_dirs
from excel_export import write_csv, write_xlsx
from about_info import APP_NAME, APP_VERSION, AUTHOR, SHORT_NOTICE, about_html
from platforms import display_names, name_of_display

try:
    import openpyxl  # noqa: F401  仅用于检测依赖是否可用
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

# ==================== 日志配置 ====================
# 必须基于 BASE_DIR（打包后=exe所在目录），不能用 __file__：
# onefile 模式下 __file__ 位于 _MEIPASS 临时目录，退出即删除，日志会全部丢失
ensure_dirs()
_log_file = os.path.join(LOG_DIR, f"app_{datetime.now().strftime('%Y%m%d')}.log")

# 打包成窗口程序（console=False）后 sys.stdout/sys.stderr 都是 None，
# 此时 logging.StreamHandler(None) 会在每次写日志时抛异常，因此必须有条件地加控制台输出
_log_handlers = [logging.FileHandler(_log_file, encoding="utf-8")]
if sys.stdout is not None:
    _log_handlers.insert(0, logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_log_handlers,
)

# matplotlib 每次查找字体都会刷成千上万行 DEBUG（实测能把日志撑到 300MB+），
# 这些三方库统一压到 WARNING；本程序自己的日志仍保持 DEBUG
for _noisy in ("matplotlib", "matplotlib.font_manager", "PIL", "selenium", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("PriceMonitor")
logger.setLevel(logging.DEBUG)


class FetchWorker(QThread):
    """后台抓取线程"""
    finished = pyqtSignal(int, object)   # (索引, FetchResult)
    error = pyqtSignal(int, str)

    def __init__(self, engine: BrowserEngine, product: Product, index: int):
        super().__init__()
        self.engine = engine
        self.product = product
        self.index = index
        self._abort = threading.Event()

    def request_abort(self):
        """请求中断本次抓取（协作式，引擎会在下一个检查点尽快返回）"""
        self._abort.set()

    def run(self):
        try:
            result = self.engine.fetch_product_info(
                self.product.url, self.product.platform,
                should_abort=self._abort.is_set,
                sku_spec=self.product.sku_spec,
            )
            if self._abort.is_set():
                # 已请求中断，结果作废，不再回传
                logger.warning(f"[抓取队列] 商品{self.index + 1} 抓取已被中断")
                return
            self.finished.emit(self.index, result)
        except Exception as e:
            if not self._abort.is_set():
                self.error.emit(self.index, str(e))


class SkuRecordWorker(QThread):
    """读取用户在浏览器里手动选中的规格（用于「记录当前规格」）"""
    done = pyqtSignal(int, list, str)   # (索引, 规格列表, 错误信息)

    def __init__(self, engine: BrowserEngine, product: Product, index: int, gate):
        super().__init__()
        self.engine = engine
        self.product = product
        self.index = index
        self.gate = gate   # threading.Event：主窗口点「确定」后放行

    def run(self):
        try:
            # 先打开页面，等主窗口提示用户去选规格
            if not self.engine.driver:
                self.engine.start_browser()
            self.engine.driver.get(self.product.url)
            self.engine._wait_page_ready(10)
            self.engine._sleep(2.0, lambda: False)
            self.engine._try_close_popups()

            # 等用户在界面上点「确定」
            while not self.gate.wait(0.3):
                pass

            specs = self.engine._read_selected_sku()
            self.done.emit(self.index, specs, "")
        except Exception as e:
            self.done.emit(self.index, [], str(e))


class MainWindow(QMainWindow):
    """主窗口"""

    alert_signal = pyqtSignal(object)

    # worker 超时阈值（秒），超过此时间认为抓取卡死并强制回收
    WORKER_TIMEOUT = 180
    # 最多监控商品数
    MAX_PRODUCTS = 20
    # 最小刷新间隔（分钟）：单个商品抓一轮约 15~20 秒，
    # 20 个商品一轮约 5~7 分钟，间隔低于 15 分钟会出现"上一轮没刷完下一轮又到期"的堆积
    MIN_REFRESH_INTERVAL = 15
    # 记录文件合并写入的延迟（毫秒）
    XLS_SAVE_DELAY = 3000

    def __init__(self, browser: str = "chrome"):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  V{APP_VERSION}")
        # 最小尺寸按 125% 缩放的小屏（1536×864）留余量：
        # 1200×700 在 125% 下需要 1500×875 物理像素，会超出屏幕高度导致内容被裁
        self.setMinimumSize(1100, 620)

        logger.info("=" * 60)
        logger.info(f"{APP_NAME} V{APP_VERSION} 启动")
        logger.info(SHORT_NOTICE)
        logger.info(f"使用浏览器: {BrowserEngine.BROWSER_NAMES.get(browser, browser)}")
        logger.info("=" * 60)

        self.store = DataStore()
        self.products: List[Product] = self.store.load_products()
        self.engine = self._create_engine(browser)
        self._browser_error_shown = False
        self._current_worker: Optional[FetchWorker] = None
        self._worker_start_time: Optional[datetime] = None
        self._fetch_queue: List[int] = []

        self._alert_queue: List[Product] = []
        self._showing_alert = False
        # 补货提醒队列（静默弹窗，不播声音）
        self._restock_queue: List[Product] = []
        self._restock_showing = False
        # 正在关闭：所有定时器/队列回调在此之后直接返回，避免窗口销毁过程中被回调访问
        self._closing = False

        self._config = self.store.load_config()
        self.alert_sound_path = self._config.get("alert_sound_path", "")
        self._validate_alert_sound()

        # 启动时是否清空历史记录（默认 true，保持"每次运行从零开始"的原有行为）
        self.reset_history_on_startup = bool(
            self._config.get("reset_history_on_startup", True)
        )

        # XLS记录文件路径（项目根目录 / exe 所在目录）
        self.xls_file_path = XLS_FILE

        # --- 记录落盘节流：合并写入，避免每抓一个商品就全量重写整个文件 ---
        self._xls_dirty = False
        self._xls_lock_warned = False
        self._xls_save_timer = QTimer(self)
        self._xls_save_timer.setSingleShot(True)
        self._xls_save_timer.timeout.connect(self._flush_xls)

        self.alert_signal.connect(self._show_alert_dialog)

        # --- 启动时自动修正商品刷新时间 ---
        self._fix_refresh_times()

        self.countdown_timer = QTimer()
        self.countdown_timer.timeout.connect(self._update_countdown)
        self.countdown_timer.start(1000)

        self.check_timer = QTimer()
        self.check_timer.timeout.connect(self._check_and_fetch)
        self.check_timer.start(30000)

        self._init_ui()
        self._fit_to_screen()
        self._refresh_table()

        # 按配置清空价格记录表和XLS文件
        self._clear_records_on_startup()

        if self.products:
            logger.info(f"检测到 {len(self.products)} 个商品，1秒后开始首次抓取")
            QTimer.singleShot(1000, self._fetch_all)

    def _validate_alert_sound(self):
        """校验提示音文件是否存在（换机器/换盘符后绝对路径会失效）"""
        if not self.alert_sound_path:
            return
        if os.path.exists(self.alert_sound_path):
            return
        logger.warning(f"[启动] 提示音文件不存在，已重置: {self.alert_sound_path}")
        self.alert_sound_path = ""
        self._config["alert_sound_path"] = ""
        self.store.save_config(self._config)

    @staticmethod
    def _create_engine(browser: str) -> BrowserEngine:
        """按所选浏览器创建引擎，并填入检测到的可执行文件路径"""
        info = detect_browsers().get(browser, {})
        engine = BrowserEngine(browser=browser)
        engine.binary_path = info.get("path", "")
        if not engine.binary_path:
            logger.warning(f"[浏览器] 未检测到 {browser} 的安装路径，交由 Selenium 自动查找")
        return engine

    # ==================== 启动修正 ====================

    def _fix_refresh_times(self):
        """启动时修正所有商品的刷新时间，防止因配置错误导致不刷新"""
        logger.info("[启动修正] 正在检查商品刷新时间...")
        now = datetime.now()
        corrected_count = 0

        for product in self.products:
            # 间隔下限校验：防止历史配置里存在过小的间隔导致任务堆积
            if (product.refresh_mode == "interval"
                    and product.refresh_interval < self.MIN_REFRESH_INTERVAL):
                logger.warning(
                    f"[启动修正] 商品 [{product.name or product.url[:30]}] "
                    f"刷新间隔 {product.refresh_interval} 分钟过小，"
                    f"已调整为 {self.MIN_REFRESH_INTERVAL} 分钟"
                )
                product.refresh_interval = self.MIN_REFRESH_INTERVAL
                corrected_count += 1

            if not product.last_refresh:
                # last_refresh 为空，无需修正
                continue
            try:
                last_dt = datetime.strptime(product.last_refresh, "%Y-%m-%d %H:%M:%S")
                if last_dt > now:
                    logger.warning(
                        f"[启动修正] 商品 [{product.name or product.url[:30]}] "
                        f"last_refresh={product.last_refresh} 在未来，修正为当前时间"
                    )
                    product.last_refresh = now.strftime("%Y-%m-%d %H:%M:%S")
                    corrected_count += 1
            except ValueError:
                logger.warning(
                    f"[启动修正] 商品 [{product.name or product.url[:30]}] "
                    f"last_refresh='{product.last_refresh}' 格式错误，修正为当前时间"
                )
                product.last_refresh = now.strftime("%Y-%m-%d %H:%M:%S")
                corrected_count += 1

        if corrected_count > 0:
            logger.info(f"[启动修正] 共修正 {corrected_count} 个商品的时间，保存配置文件")
            self.store.save_products(self.products)
        else:
            logger.info("[启动修正] 所有商品时间正常，无需修正")

    def _clear_records_on_startup(self):
        """
        启动时清空价格记录表和XLS文件

        可通过 config.json 的 "reset_history_on_startup": false 关闭，改为跨次累积历史。
        注意：清空后必须立刻落盘，否则下一次抓取保存时会把磁盘上的历史一并抹掉，
        造成"内存已清空、磁盘还留着、下次保存才真正删除"的不一致状态。
        """
        if not self.reset_history_on_startup:
            logger.info("[启动] reset_history_on_startup=false，保留已有历史记录")
            self._update_records_table()
            return

        logger.info("[启动] 清空价格记录表，从零开始记录")

        # 清空界面表格
        self.record_table.setRowCount(0)

        # 清空各商品的历史并立即写回磁盘
        for product in self.products:
            product.price_history.clear()
        self.store.save_products(self.products)

        # 重建空的 XLS（只含表头，样式统一由 excel_export 负责）
        try:
            write_xlsx(self.products, self.xls_file_path)
            logger.info(f"[启动] 已重建空的XLS记录文件: {self.xls_file_path}")
        except PermissionError:
            self._on_xls_locked()
        except Exception as e:
            logger.error(f"[启动] 创建XLS文件失败: {e}")

        logger.info("[启动] 已清空所有商品的历史价格记录（内存 + products.json）")

    # ==================== UI初始化 ====================

    def _init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(12, 12, 12, 12)

        # ---- 顶部工具栏 ----
        toolbar = QHBoxLayout()

        title = QLabel("蹲价")
        title.setFont(QFont("Microsoft YaHei", 16, QFont.Bold))
        title.setStyleSheet("color: #2c3e50;")
        toolbar.addWidget(title)

        toolbar.addStretch()

        add_btn = QPushButton("添加商品")
        add_btn.setFont(QFont("Microsoft YaHei", 10))
        add_btn.setFixedSize(120, 36)
        add_btn.setStyleSheet("""
            QPushButton { background-color: #3498db; color: white; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #2980b9; }
        """)
        add_btn.clicked.connect(self._add_product)
        toolbar.addWidget(add_btn)

        refresh_btn = QPushButton("立即刷新")
        refresh_btn.setFont(QFont("Microsoft YaHei", 10))
        refresh_btn.setFixedSize(120, 36)
        refresh_btn.setStyleSheet("""
            QPushButton { background-color: #27ae60; color: white; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #219a52; }
        """)
        refresh_btn.clicked.connect(self._fetch_all)
        toolbar.addWidget(refresh_btn)

        login_btn = QPushButton("登录平台")
        login_btn.setFont(QFont("Microsoft YaHei", 10))
        login_btn.setFixedSize(120, 36)
        login_btn.setStyleSheet("""
            QPushButton { background-color: #f39c12; color: white; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #d68910; }
        """)
        login_btn.clicked.connect(self._login_platform)
        toolbar.addWidget(login_btn)

        sound_btn = QPushButton("提示音")
        sound_btn.setFont(QFont("Microsoft YaHei", 10))
        sound_btn.setFixedSize(120, 36)
        sound_btn.setStyleSheet("""
            QPushButton { background-color: #9b59b6; color: white; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #8e44ad; }
        """)
        sound_btn.clicked.connect(self._select_alert_sound)
        toolbar.addWidget(sound_btn)

        export_btn = QPushButton("导出记录表")
        export_btn.setFont(QFont("Microsoft YaHei", 10))
        export_btn.setFixedSize(120, 36)
        export_btn.setStyleSheet("""
            QPushButton { background-color: #1abc9c; color: white; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #16a085; }
        """)
        export_btn.clicked.connect(self._export_records)
        toolbar.addWidget(export_btn)

        main_layout.addLayout(toolbar)

        # ---- 主体区域（左右分栏） ----
        splitter = QSplitter(Qt.Horizontal)

        # 左侧：商品列表
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        table_label = QLabel("监控商品列表")
        table_label.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))
        table_label.setStyleSheet("color: #2c3e50; margin-bottom: 4px;")
        left_layout.addWidget(table_label)

        self.table = QTableWidget()
        self.table.setColumnCount(9)
        self.table.setHorizontalHeaderLabels([
            "序号", "商品名称", "平台", "当前价格", "预期价格",
            "上次刷新", "下次刷新倒计时", "刷新设置", "状态"
        ])
        self.table.setFont(QFont("Microsoft YaHei", 9))
        self.table.horizontalHeader().setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.currentCellChanged.connect(self._on_row_selected)
        self.table.setStyleSheet("""
            QTableWidget {
                gridline-color: #e0e0e0;
                alternate-background-color: #f8f9fa;
            }
            QHeaderView::section {
                background-color: #34495e;
                color: white;
                padding: 6px;
                border: none;
            }
        """)
        left_layout.addWidget(self.table)

        splitter.addWidget(left_widget)

        # 右侧：走势图 + 价格记录表（Tab切换）
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.right_tabs = QTabWidget()
        self.right_tabs.setFont(QFont("Microsoft YaHei", 10))

        # ---- Tab 1: 走势图 ----
        chart_page = QWidget()
        chart_layout = QVBoxLayout(chart_page)
        chart_layout.setContentsMargins(0, 0, 0, 0)

        chart_label = QLabel("历史价格走势图")
        chart_label.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))
        chart_label.setStyleSheet("color: #2c3e50; margin-bottom: 4px;")
        chart_layout.addWidget(chart_label)

        self.chart = PriceChartWidget()
        self.chart.clear_chart()
        chart_layout.addWidget(self.chart)

        self.right_tabs.addTab(chart_page, "走势图")

        # ---- Tab 2: 价格记录表 ----
        record_page = QWidget()
        record_layout = QVBoxLayout(record_page)
        record_layout.setContentsMargins(0, 0, 0, 0)
        record_layout.setSpacing(0)

        record_label = QLabel("全部商品价格记录表")
        record_label.setFont(QFont("Microsoft YaHei", 11, QFont.Bold))
        record_label.setStyleSheet("color: #2c3e50; margin-bottom: 4px;")
        record_layout.addWidget(record_label)

        self.record_table = QTableWidget()
        self.record_table.setColumnCount(5)
        self.record_table.setHorizontalHeaderLabels([
            "序号", "商品名称", "刷新日期时间", "当前价格", "预期价格"
        ])
        self.record_table.setFont(QFont("Microsoft YaHei", 9))
        self.record_table.horizontalHeader().setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
        self.record_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.record_table.horizontalHeader().setStretchLastSection(True)
        self.record_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.record_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.record_table.setAlternatingRowColors(True)
        self.record_table.verticalHeader().setVisible(False)
        self.record_table.setStyleSheet("""
            QTableWidget {
                gridline-color: #e0e0e0;
                alternate-background-color: #f8f9fa;
            }
            QHeaderView::section {
                background-color: #34495e;
                color: white;
                padding: 6px;
                border: none;
            }
        """)
        record_layout.addWidget(self.record_table)

        self.right_tabs.addTab(record_page, "价格记录表")

        right_layout.addWidget(self.right_tabs)

        splitter.addWidget(right_widget)
        splitter.setSizes([650, 500])

        main_layout.addWidget(splitter)

        # ---- 底部状态栏 ----
        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)

        # 左下角：版本号
        self.version_label = QLabel(f"V{APP_VERSION}")
        self.version_label.setFont(QFont("Microsoft YaHei", 9))
        self.version_label.setStyleSheet("color: #95a5a6; padding: 4px 8px 4px 4px;")
        self.version_label.setToolTip(f"{APP_NAME} v{APP_VERSION}\n作者：{AUTHOR}")
        bottom.addWidget(self.version_label)

        self.status_label = QLabel("就绪")
        self.status_label.setFont(QFont("Microsoft YaHei", 9))
        self.status_label.setStyleSheet("color: #7f8c8d; padding: 4px;")
        bottom.addWidget(self.status_label, 1)

        # 当前浏览器 + 一键更换
        self.browser_label = QLabel()
        self.browser_label.setFont(QFont("Microsoft YaHei", 9))
        self.browser_label.setStyleSheet("color: #7f8c8d; padding: 4px;")
        self.browser_label.setTextFormat(Qt.RichText)
        self.browser_label.setCursor(Qt.PointingHandCursor)
        self.browser_label.linkActivated.connect(self._switch_browser)
        bottom.addWidget(self.browser_label)

        main_layout.addLayout(bottom)
        self._update_browser_label()

        # ---- 顶部菜单（帮助 / 关于） ----
        self._init_menu()

    def _fit_to_screen(self):
        """
        按屏幕可用区域收敛初始窗口尺寸

        打包后程序是 DPI 感知的，125% 缩放下窗口按真实物理像素计算，
        直接用固定的 1200×700 会超出小屏高度，导致右侧按钮和状态栏被裁掉。
        """
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        width = min(1200, max(self.minimumWidth(), avail.width() - 30))
        height = min(700, max(self.minimumHeight(), avail.height() - 50))
        self.resize(width, height)

        logger.info(
            f"[界面] 屏幕可用={avail.width()}x{avail.height()} "
            f"逻辑DPI={screen.logicalDotsPerInch():.1f} DPR={screen.devicePixelRatio():.2f} "
            f"目标尺寸={width}x{height} 实际={self.width()}x{self.height()}"
        )
        # 显示后再记录一次，用于排查高 DPI 缩放导致的窗口尺寸异常
        QTimer.singleShot(300, self._log_window_state)

    def _log_window_state(self):
        geo = self.geometry()
        frame = self.frameGeometry()
        logger.info(
            f"[界面] 显示后 客户区={geo.width()}x{geo.height()} "
            f"外框={frame.width()}x{frame.height()} 位置=({geo.x()},{geo.y()})"
        )

    # ==================== 菜单：使用说明 / 关于 ====================

    def _init_menu(self):
        bar = self.menuBar()
        bar.setFont(QFont("Microsoft YaHei", 9))

        help_menu = bar.addMenu("帮助")

        doc_action = QAction("使用说明", self)
        doc_action.triggered.connect(self._open_manual)
        help_menu.addAction(doc_action)

        about_action = QAction("关于 / 版权信息", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _open_manual(self):
        """打开使用说明文档（优先程序目录，其次打包内的 _internal）"""
        candidates = []
        for filename in ("使用说明.txt", "README.md"):
            candidates.append(os.path.join(BASE_DIR, filename))
            # onedir 打包时 datas 会被放进 _internal（sys._MEIPASS），这里作为兜底
            bundle_dir = getattr(sys, "_MEIPASS", "")
            if bundle_dir and bundle_dir != BASE_DIR:
                candidates.append(os.path.join(bundle_dir, filename))

        for path in candidates:
            if os.path.exists(path):
                try:
                    os.startfile(path)
                    return
                except Exception as e:
                    logger.warning(f"[帮助] 打开 {path} 失败: {e}")

        QMessageBox.information(
            self, "使用说明",
            "未找到 使用说明.txt。\n\n"
            "请确认该文件与程序放在同一目录下。",
        )

    def _show_about(self):
        """关于对话框：作者与版权声明"""
        box = QMessageBox(self)
        box.setWindowTitle(f"关于 {APP_NAME}")
        box.setTextFormat(Qt.RichText)
        box.setText(about_html())
        box.setIcon(QMessageBox.Information)
        box.exec_()

    # ==================== 浏览器显示与切换 ====================

    def _browser_text(self) -> str:
        """当前浏览器的显示文本（带版本号）"""
        info = detect_browsers().get(self.engine.browser, {})
        display = info.get("display") or self.engine.display_name
        version = info.get("version")
        return f"{display} {version}" if version else display

    def _update_browser_label(self):
        self.browser_label.setText(
            f'当前浏览器：{self._browser_text()} · '
            f'<a href="switch" style="color:#3498db; text-decoration:none;">更换</a>'
        )
        self.browser_label.setToolTip(
            f"使用 {self.engine.display_name} 抓取商品页面\n"
            f"用户数据目录：{self.engine.profile_dir}"
        )

    def _switch_browser(self, _href=""):
        """运行中更换浏览器（需要重新登录）"""
        detected = detect_browsers()
        if not detected:
            QMessageBox.warning(self, "提示", "未检测到可用的浏览器")
            return

        choice, remember = ask_browser(detected, current=self.engine.browser, parent=self)
        if not choice or choice == self.engine.browser:
            return

        # 先停掉抓取线程并关闭旧浏览器，再创建新引擎
        self._reset_worker_state(abort=True)
        self.engine.close_browser()

        self.engine = self._create_engine(choice)
        self._browser_error_shown = False

        self._config["browser"] = choice
        self._config["remember_browser"] = remember
        self.store.save_config(self._config)

        self._update_browser_label()
        logger.info(f"[浏览器] 已切换为 {self.engine.display_name}")

        QMessageBox.information(
            self,
            "浏览器已切换",
            f"已切换为 {self.engine.display_name}。\n\n"
            "新浏览器首次使用需要重新登录：请点击工具栏的「登录平台」按钮完成登录。",
        )

    # ==================== 表格操作 ====================

    def _refresh_table(self):
        """
        刷新左侧商品列表

        重建期间屏蔽信号：setRowCount/setItem 会触发 currentCellChanged，
        若回调此时去重绘走势图，会在控件重建过程中访问到不一致的表格状态。
        """
        self.table.blockSignals(True)
        try:
            self._refresh_table_rows()
        finally:
            self.table.blockSignals(False)

    def _refresh_table_rows(self):
        self.table.setRowCount(len(self.products))
        for i, product in enumerate(self.products):
            idx_item = QTableWidgetItem(str(i + 1))
            idx_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 0, idx_item)

            name = product.name if product.name else "(未获取)"
            if len(name) > 30:
                name = name[:30] + "..."
            name_item = QTableWidgetItem(name)
            if product.has_sku_lock():
                name_item.setToolTip("已锁定规格：" + product.sku_display())
            self.table.setItem(i, 1, name_item)

            platform_item = QTableWidgetItem(product.platform_display_name())
            platform_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 2, platform_item)

            # ---- 当前价格：无货/下架时显示 --，但保留上次有效价作为提示 ----
            available = product.last_status == st.STATUS_OK
            if available and product.current_price > 0:
                price_text = f"¥{product.current_price:.2f}"
            elif product.last_valid_price > 0:
                price_text = "--"
            else:
                price_text = "--"
            price_item = QTableWidgetItem(price_text)
            price_item.setTextAlignment(Qt.AlignCenter)
            if available and product.last_valid_price > 0:
                price_item.setToolTip(f"上次有效价 ¥{product.last_valid_price:.2f}"
                                      f"（{product.last_valid_time or '未知时间'}）")
            if available and product.is_price_reached():
                price_item.setForeground(QColor("#e74c3c"))
                price_item.setFont(QFont("Microsoft YaHei", 10, QFont.Bold))
            elif not available and product.last_valid_price > 0:
                price_item.setForeground(QColor("#bdc3c7"))
            self.table.setItem(i, 3, price_item)

            if product.expected_price > 0:
                expected_text = f"¥{product.expected_price:.2f}"
            else:
                expected_text = "--"
            expected_item = QTableWidgetItem(expected_text)
            expected_item.setTextAlignment(Qt.AlignCenter)
            expected_item.setForeground(QColor("#27ae60"))
            self.table.setItem(i, 4, expected_item)

            last_refresh = product.last_refresh if product.last_refresh else "--"
            refresh_item = QTableWidgetItem(last_refresh)
            refresh_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 5, refresh_item)

            countdown_item = QTableWidgetItem("--")
            countdown_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 6, countdown_item)

            setting_text = product.get_refresh_time_display()
            setting_item = QTableWidgetItem(setting_text)
            setting_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 7, setting_item)

            # ---- 状态列：优先显示库存/上下架状态 ----
            if product.last_status == st.STATUS_NO_STOCK:
                status_text, status_color = "无货", QColor("#e67e22")
            elif product.last_status == st.STATUS_DELISTED:
                status_text, status_color = "已下架", QColor("#7f8c8d")
            elif product.last_status == st.STATUS_SKU_MISMATCH:
                status_text, status_color = "规格失效", QColor("#c0392b")
            elif product.last_status == st.STATUS_PRICE_RANGE:
                status_text, status_color = "价格区间", QColor("#8e44ad")
            elif product.last_status == st.STATUS_UNKNOWN and not product.last_valid_price:
                status_text, status_color = "抓取异常", QColor("#95a5a6")
            elif product.is_price_reached():
                status_text, status_color = "已达标", QColor("#e74c3c")
            elif product.current_price > 0:
                status_text, status_color = "监控中", QColor("#27ae60")
            else:
                status_text, status_color = "待刷新", QColor("#95a5a6")

            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(status_color)
            status_item.setTextAlignment(Qt.AlignCenter)
            status_item.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
            detail = product.last_status_text or ""
            if detail:
                status_item.setToolTip(detail)
            self.table.setItem(i, 8, status_item)

    def _update_countdown(self):
        """更新倒计时列"""
        for i, product in enumerate(self.products):
            next_time = product.get_next_refresh_time()
            if next_time is None:
                countdown_text = "--"
            else:
                now = datetime.now()
                if next_time <= now:
                    countdown_text = "即将刷新"
                else:
                    delta = next_time - now
                    hours, remainder = divmod(int(delta.total_seconds()), 3600)
                    minutes, seconds = divmod(remainder, 60)
                    countdown_text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

            item = self.table.item(i, 6)
            if item:
                item.setText(countdown_text)

    def _on_row_selected(self, row, col, prev_row, prev_col):
        """选中行时更新走势图"""
        if 0 <= row < len(self.products):
            product = self.products[row]
            self.chart.update_chart(
                product.price_history,
                product.expected_price,
                product.name
            )

    # ==================== 右键菜单 ====================

    def _show_context_menu(self, pos):
        """表格右键菜单"""
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.products):
            return

        menu = QMenu(self)
        menu.setStyleSheet("QMenu { font-family: 'Microsoft YaHei'; font-size: 10pt; }")

        edit_action = QAction("编辑设置", self)
        edit_action.triggered.connect(lambda: self._edit_product(row))
        menu.addAction(edit_action)

        refresh_action = QAction("刷新此商品", self)
        refresh_action.triggered.connect(lambda: self._fetch_single(row))
        menu.addAction(refresh_action)

        open_action = QAction("打开商品链接", self)
        open_action.triggered.connect(lambda: self._open_product_url(row))
        menu.addAction(open_action)

        menu.addSeparator()

        # 规格锁定：解决"选了套餐但抓到最低价"的问题
        product = self.products[row]
        sku_action = QAction("记录当前规格（锁定版本/套餐）", self)
        sku_action.triggered.connect(lambda: self._record_sku(row))
        menu.addAction(sku_action)

        if product.has_sku_lock():
            clear_action = QAction(f"清除规格锁定（当前：{product.sku_display()}）", self)
            clear_action.triggered.connect(lambda: self._clear_sku(row))
            menu.addAction(clear_action)

        menu.addSeparator()

        delete_action = QAction("删除商品", self)
        delete_action.triggered.connect(lambda: self._delete_product(row))
        menu.addAction(delete_action)

        menu.exec_(self.table.viewport().mapToGlobal(pos))

    # ==================== 商品操作 ====================

    def _add_product(self):
        """添加新商品"""
        if len(self.products) >= self.MAX_PRODUCTS:
            QMessageBox.warning(self, "提示", f"最多只能监控{self.MAX_PRODUCTS}个商品！")
            return

        dialog = AddProductDialog(self)
        if dialog.exec_() != QDialog.Accepted:
            return

        product = Product(
            url=dialog.product_url,
            expected_price=dialog.expected_price,
            refresh_mode=dialog.refresh_mode,
            refresh_time=dialog.refresh_time,
            refresh_interval=dialog.refresh_interval,
        )
        # 兜底：间隔不得小于下限，避免任务堆积
        if (product.refresh_mode == "interval"
                and product.refresh_interval < self.MIN_REFRESH_INTERVAL):
            product.refresh_interval = self.MIN_REFRESH_INTERVAL

        self.products.append(product)
        self.store.save_products(self.products)
        self._refresh_table()
        self.status_label.setText("已添加商品，正在获取商品信息...")

        idx = len(self.products) - 1
        self._fetch_queue.append(idx)
        self._process_fetch_queue()

    def _edit_product(self, row: int):
        """编辑商品设置"""
        if row < 0 or row >= len(self.products):
            return
        product = self.products[row]

        dialog = AddProductDialog(self, product=product)
        if dialog.exec_() != QDialog.Accepted:
            return

        product.expected_price = dialog.expected_price
        product.refresh_mode = dialog.refresh_mode
        product.refresh_time = dialog.refresh_time
        product.refresh_interval = dialog.refresh_interval
        if (product.refresh_mode == "interval"
                and product.refresh_interval < self.MIN_REFRESH_INTERVAL):
            product.refresh_interval = self.MIN_REFRESH_INTERVAL
        self.store.save_products(self.products)
        self._refresh_table()

        current_row = self.table.currentRow()
        if current_row == row:
            self.chart.update_chart(
                product.price_history, product.expected_price, product.name
            )

    def _delete_product(self, row: int):
        """删除商品"""
        if row < 0 or row >= len(self.products):
            return
        name = self.products[row].name or "此商品"
        reply = QMessageBox.question(
            self, "确认删除", f"确定要删除 {name} 吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.products.pop(row)
            # 队列里存的是列表索引，删除后必须同步调整，否则会抓错商品/记录错价格
            self._fetch_queue = [i for i in self._fetch_queue if i != row]
            self._fetch_queue = [i - 1 if i > row else i for i in self._fetch_queue]
            if self._current_worker is not None:
                if self._current_worker.index == row:
                    # 正在抓取的正是被删除的商品：中断本次抓取，结果直接丢弃，
                    # 否则回传的索引会指向下一个商品，把价格记到别人头上
                    self._current_worker.request_abort()
                elif self._current_worker.index > row:
                    self._current_worker.index -= 1
            self.store.save_products(self.products)
            self._refresh_table()
            self.chart.clear_chart()
            self.status_label.setText("已删除商品")

    def _open_product_url(self, row: int):
        """在浏览器中打开商品链接"""
        if 0 <= row < len(self.products):
            from PyQt5.QtCore import QUrl
            from PyQt5.QtGui import QDesktopServices
            QDesktopServices.openUrl(QUrl(self.products[row].url))

    # ==================== 规格锁定 ====================

    def _record_sku(self, row: int):
        """
        记录当前选中的规格

        流程：后台打开商品页 → 主窗口提示用户手动选规格 → 点「确定」→ 后台读取选中项
        """
        if row < 0 or row >= len(self.products):
            return
        if self._current_worker and self._current_worker.isRunning():
            QMessageBox.information(self, "提示", "正在抓取中，请等本次抓取完成后再记录规格。")
            return

        product = self.products[row]
        gate = threading.Event()
        self._sku_record_ok = False

        worker = SkuRecordWorker(self.engine, product, row, gate)
        worker.done.connect(self._on_sku_recorded)
        worker.start()
        self._sku_worker = worker

        reply = QMessageBox.information(
            self, "记录当前规格",
            "浏览器已打开该商品页面。\n\n"
            "① 在弹出的浏览器里选好你要监控的「版本 / 套餐 / 颜色」\n"
            "② 选好后回到这个窗口点「确定」\n\n"
            "程序会记下这次选中的规格，之后每次抓取都只抓这一款的价格。",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        self._sku_record_ok = (reply == QMessageBox.Ok)
        gate.set()

    def _on_sku_recorded(self, index: int, specs: list, error: str):
        """「记录当前规格」完成回调"""
        if not getattr(self, "_sku_record_ok", False):
            self.status_label.setText("已取消记录规格")
            return
        if error:
            QMessageBox.warning(self, "记录失败", f"读取规格时出错：{error}")
            return
        if not specs:
            QMessageBox.warning(
                self, "没读到规格",
                "没有读到选中的规格。\n\n"
                "常见原因：\n"
                "· 还没有在浏览器里点选规格\n"
                "· 该页面规格区域的页面结构比较特殊\n\n"
                "可以再试一次，或先不锁定规格（此时抓到的可能是最低价）。",
            )
            return
        if index >= len(self.products):
            return
        product = self.products[index]
        product.sku_spec = list(specs)
        product.sku_recorded_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.store.save_products(self.products)
        self._refresh_table()
        logger.info(f"[规格锁定] 商品{index + 1} 已锁定规格: {' / '.join(specs)}")
        self.status_label.setText("已锁定规格：" + " / ".join(specs))
        QMessageBox.information(
            self, "记录成功",
            "已锁定规格：\n\n" + " / ".join(specs) +
            "\n\n之后只抓这一款的价格；如果这一款无货，会显示「无货」而不是抓最低价。",
        )

    def _clear_sku(self, row: int):
        """清除规格锁定"""
        if row < 0 or row >= len(self.products):
            return
        product = self.products[row]
        if not product.has_sku_lock():
            return
        reply = QMessageBox.question(
            self, "清除规格锁定",
            f"确定清除「{product.name or product.url[:30]}」的规格锁定吗？\n\n"
            "清除后将按页面默认显示抓取，可能抓到的是最低价。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        product.sku_spec = []
        product.sku_recorded_at = ""
        self.store.save_products(self.products)
        self._refresh_table()
        logger.info(f"[规格锁定] 商品{row + 1} 已清除规格锁定")
        self.status_label.setText("已清除规格锁定")

    # ==================== 提示音设置 ====================

    def _select_alert_sound(self):
        """选择提示音文件"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择提示音文件", "",
            "音频文件 (*.mp3 *.wav *.ogg *.flac);;所有文件 (*.*)"
        )
        if file_path:
            self.alert_sound_path = file_path
            self._config["alert_sound_path"] = file_path
            self.store.save_config(self._config)
            sound_name = os.path.basename(file_path)
            self.status_label.setText(f"提示音已设置: {sound_name}")
            QMessageBox.information(
                self, "提示音设置",
                f"提示音已设置为：\n{sound_name}\n\n当商品价格达标时会自动播放。"
            )

    # ==================== 登录 ====================

    def _login_platform(self):
        """打开登录平台选择对话框"""
        dialog = QDialog(self)
        dialog.setWindowTitle("登录购物平台")
        dialog.setFixedSize(300, 180)
        layout = QVBoxLayout(dialog)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        label = QLabel("请选择要登录的平台：\n（将在浏览器中打开登录页面）")
        label.setFont(QFont("Microsoft YaHei", 10))
        label.setWordWrap(True)
        layout.addWidget(label)

        from PyQt5.QtWidgets import QComboBox
        combo = QComboBox()
        combo.addItems(display_names())
        combo.setFont(QFont("Microsoft YaHei", 10))
        layout.addWidget(combo)

        btn = QPushButton("打开登录页面")
        btn.setFont(QFont("Microsoft YaHei", 10))
        btn.setStyleSheet("""
            QPushButton { background-color: #f39c12; color: white; border: none; border-radius: 4px; padding: 8px; }
            QPushButton:hover { background-color: #d68910; }
        """)

        platform_map = {name: name_of_display(name) for name in display_names()}

        def do_login():
            platform = platform_map[combo.currentText()]
            dialog.accept()
            self.status_label.setText(f"正在打开 {combo.currentText()} 登录页面...")

            class LoginThread(QThread):
                result = pyqtSignal(bool)

                def __init__(self, engine, plat):
                    super().__init__()
                    self.engine = engine
                    self.plat = plat

                def run(self):
                    ok = self.engine.login_platform(self.plat)
                    self.result.emit(ok)

            t = LoginThread(self.engine, platform)
            t.result.connect(lambda ok: self._on_login_done(combo.currentText(), ok))
            t.start()
            self._login_thread = t

        btn.clicked.connect(do_login)
        layout.addWidget(btn)
        dialog.exec_()

    def _on_login_done(self, platform_name: str, success: bool):
        """登录完成回调"""
        if success:
            self.status_label.setText(f"{platform_name} 登录成功！Cookie已保存")
            QMessageBox.information(self, "登录成功", f"{platform_name} 登录成功！")
        else:
            self.status_label.setText(f"{platform_name} 登录失败或超时")
            QMessageBox.warning(self, "登录失败", f"{platform_name} 登录失败或超时，请重试")

    # ==================== 价格抓取（串行队列） ====================

    def _check_and_fetch(self):
        """
        核心调度器：由 check_timer 每 30 秒触发一次

        判定规则统一走 Product.is_due()，与界面"下次刷新倒计时"列使用同一套逻辑，
        避免出现"显示每日 8 点刷新、实际按间隔刷新"的不一致。
        只负责把到期商品加入队列，不直接启动线程。
        """
        now = datetime.now()

        # 1. 看门狗：当前 worker 运行超时，强制回收，避免队列永久卡死
        if self._current_worker and self._worker_start_time:
            elapsed = (now - self._worker_start_time).total_seconds()
            if elapsed > self.WORKER_TIMEOUT:
                logger.warning(f"[看门狗] 任务超时 ({elapsed:.0f}s)，强制回收并重建浏览器会话")
                self._reset_worker_state(abort=True)

        # 2. 如果当前有任务在跑，直接返回，等待下次检查
        if self._current_worker and self._current_worker.isRunning():
            return

        # 3. 空闲状态，寻找第一个到期的商品加入队列
        for i, product in enumerate(self.products):
            if i in self._fetch_queue:
                continue
            if product.is_due(now):
                logger.info(f"[调度] 命中任务: 商品 [{product.name or product.url[:30]}] "
                            f"（{product.get_refresh_time_display()}）已到刷新时间，加入队列")
                # 关键修改：只加入队列，不直接启动
                self._fetch_queue.append(i)
                # 尝试处理队列
                self._process_fetch_queue()
                return  # 一次只处理一个到期商品

    def _fetch_all(self):
        """将所有商品加入串行抓取队列"""
        if self._current_worker and self._current_worker.isRunning():
            self.status_label.setText("正在抓取中，请等待完成...")
            logger.info("[手动刷新] worker运行中，拒绝重复触发")
            return

        self._fetch_queue = list(range(len(self.products)))
        self.status_label.setText(f"正在刷新所有商品价格（共{len(self.products)}个）...")
        logger.info(f"[手动刷新] 全部 {len(self.products)} 个商品加入抓取队列")
        self._process_fetch_queue()

    def _process_fetch_queue(self):
        """从队列中取出下一个商品进行抓取"""
        # 1. 严格检查：如果 worker 还在运行，绝对不要启动新的
        if self._current_worker and self._current_worker.isRunning():
            logger.debug("[抓取队列] Worker 正在运行，等待完成...")
            return

        # 2. 清理可能存在的僵尸 worker 引用
        if self._current_worker and not self._current_worker.isRunning():
            self._reset_worker_state()

        if not self._fetch_queue:
            return

        index = self._fetch_queue.pop(0)

        if index < 0 or index >= len(self.products):
            logger.warning(f"[抓取队列] 无效索引: {index}")
            self._process_fetch_queue()  # 递归处理下一个
            return

        product = self.products[index]
        logger.info(f"[抓取队列] 开始处理商品{index + 1}: {product.name or product.url[:40]}")

        # Cookie检查
        cookie_file = os.path.join(self.engine.cookie_dir, f"{product.platform}_cookies.json")
        if not os.path.exists(cookie_file):
            logger.warning(f"[抓取队列] 商品{index + 1} [{product.platform_display_name()}] 未找到Cookie文件")

        self.status_label.setText(
            f"正在获取 ({len(self.products) - len(self._fetch_queue)}/{len(self.products)}): "
            f"{product.name or product.url[:40]}..."
        )

        # 创建并启动 Worker
        worker = FetchWorker(self.engine, product, index)

        # 关键：使用 lambda 包裹 index，防止闭包陷阱
        worker.finished.connect(lambda idx, res: self._on_fetch_done(idx, res))
        worker.error.connect(lambda idx, msg: self._on_fetch_error(idx, msg))

        self._current_worker = worker
        self._worker_start_time = datetime.now()

        try:
            worker.start()
            logger.debug(f"[抓取队列] Worker 线程已启动")
        except Exception as e:
            logger.error(f"[抓取队列] 启动 Worker 失败: {e}")
            self._reset_worker_state()
            self._process_fetch_queue()

    def _fetch_single(self, index: int):
        """抓取单个商品价格"""
        if index < 0 or index >= len(self.products):
            return
        if index not in self._fetch_queue:
            self._fetch_queue.append(index)
        self._process_fetch_queue()

    def _on_fetch_done(self, index: int, result):
        """抓取完成回调：按状态决定是否采信价格、是否提醒"""
        self._reset_worker_state()

        if index >= len(self.products):
            logger.warning(f"[抓取完成] 索引{index}超出范围")
            self._process_fetch_queue()
            return

        product = self.products[index]
        previous_status = product.last_status
        prev_name = product.name or product.url[:30]

        logger.info(
            f"[抓取完成] 商品{index + 1} [{prev_name[:20]}] "
            f"status={result.status}({result.status_text or '-'}) "
            f"price={result.price} original={result.original_price} "
            f"sku={result.sku_text or '-'}"
        )

        if result.name:
            product.name = result.name

        # 无论什么状态都落一条记录（价格字段对非 ok 状态无效）
        product.add_price_record(
            result.price, result.original_price,
            status=result.status, status_text=result.status_text,
            sku_text=result.sku_text,
        )
        self.store.save_products(self.products)

        display_name = (product.name or "商品")[:20]
        if result.status == st.STATUS_OK:
            self.status_label.setText(f"{display_name}... 当前价格: ¥{result.price:.2f}")
            logger.info(f"[价格记录] 商品{index + 1} [{display_name}] ¥{result.price:.2f}")
        else:
            status_text = st.display(result.status)
            detail = result.status_text or ""
            self.status_label.setText(
                f"{display_name}... {status_text}{('：' + detail) if detail else ''}"
            )
            logger.warning(
                f"[状态变化] 商品{index + 1} [{display_name}] {status_text} {detail}"
            )

        self._refresh_table()

        current_row = self.table.currentRow()
        if current_row == index:
            self.chart.update_chart(
                product.price_history, product.expected_price, product.name
            )

        self._update_records_table()

        # 达标提醒：只有价格可信且在售时才判断
        if result.status == st.STATUS_OK and product.is_price_reached():
            logger.info(
                f"[价格达标] 商品{index + 1} [{display_name}] "
                f"当前¥{product.current_price:.2f} <= 预期¥{product.expected_price:.2f}"
            )
            self._alert_queue.append(product)
            self._process_alert_queue()

        # 补货提醒：上一次买不到，这一次能买了
        if st.is_unavailable(previous_status) and result.status == st.STATUS_OK:
            logger.info(f"[补货提醒] 商品{index + 1} [{display_name}] 已恢复可购买")
            self._notify_restock(product)

        self._process_fetch_queue()

    # ==================== 补货提醒（静默弹窗，不播声音） ====================

    def _notify_restock(self, product: Product):
        """补货只做静默弹窗：反复补货时不会因为播放提示音而吵闹"""
        self._restock_queue.append(product)
        if not self._restock_showing:
            QTimer.singleShot(300, self._process_restock_queue)

    def _process_restock_queue(self):
        if self._closing or self._restock_showing or not self._restock_queue:
            return
        self._restock_showing = True
        try:
            lines = []
            while self._restock_queue:
                item = self._restock_queue.pop(0)
                price = item.last_valid_price or item.current_price
                price_text = f"¥{price:.2f}" if price > 0 else "价格未知"
                lines.append(f"· {item.name or item.url[:40]}　当前 {price_text}")
            QMessageBox.information(
                self,
                "补货提醒",
                "以下商品已恢复可购买：\n\n" + "\n".join(lines) +
                "\n\n（补货提醒只弹窗，不播放提示音）",
            )
        finally:
            self._restock_showing = False

    def _on_fetch_error(self, index: int, error_msg: str):
        self._reset_worker_state()  # <--- 新增这一行

        if index < len(self.products):
            product = self.products[index]
            self.status_label.setText(
                f"{product.name[:20] if product.name else '商品'}... "
                f"获取失败: {error_msg}"
            )
            logger.error(f"[抓取失败] 商品{index+1} [{product.name[:20]}]: {error_msg}")
        else:
            logger.error(f"[抓取失败] 无效索引{index}: {error_msg}")

        # 浏览器根本没起来时，给出明确原因（比"可能需要重新登录"准确得多）
        if self.engine.last_error:
            logger.error(f"[浏览器] 启动失败: {self.engine.last_error}")
            if not self._browser_error_shown:
                self._browser_error_shown = True
                QMessageBox.warning(
                    self,
                    "浏览器启动失败",
                    f"{self.engine.last_error}\n\n"
                    "可以点击窗口右下角的「更换」换用另一个浏览器。",
                )

        self._process_fetch_queue()

    def _reset_worker_state(self, abort: bool = False):
        """
        重置 Worker 状态，允许下一个任务进入

        abort=True：看门狗超时或程序退出时使用。
        注意：QThread.run() 是普通函数、没有事件循环，quit() 对它无效，
        所以这里用"协作式中断 + 有限等待 + 最后强杀"的阶梯处理；
        线程被强杀后 driver 状态不可信，必须重建浏览器会话，否则下一次抓取
        会和残留线程操作同一个 Chrome 实例。
        """
        worker = self._current_worker
        self._current_worker = None
        self._worker_start_time = None
        if worker is None:
            return

        # 断开信号连接，防止重复触发
        try:
            worker.finished.disconnect()
            worker.error.disconnect()
        except Exception:
            pass

        if worker.isRunning():
            logger.warning("[状态] Worker 仍在运行，请求协作式中断")
            worker.request_abort()
            if not worker.wait(8000):
                logger.error("[状态] Worker 8 秒内未退出，强制终止线程并重建浏览器会话")
                worker.terminate()
                worker.wait(3000)
                self.engine.hard_reset()

        logger.debug("[状态] Worker 已重置")

    # ==================== 价格记录表 ====================

    def _update_records_table(self):
        """更新右侧价格记录表（全部商品的记录）"""
        all_records = []
        for product in self.products:
            for record in product.price_history:
                all_records.append({
                    "name": product.name or "(未获取)",
                    "timestamp": record.timestamp,
                    "price": record.price,
                    "expected_price": product.expected_price,
                })

        # 按时间从远到近排序
        all_records.sort(key=lambda r: r["timestamp"], reverse=False)

        self.record_table.setRowCount(len(all_records))
        for i, rec in enumerate(all_records):
            idx_item = QTableWidgetItem(str(i + 1))
            idx_item.setTextAlignment(Qt.AlignCenter)
            self.record_table.setItem(i, 0, idx_item)

            name_item = QTableWidgetItem(rec["name"])
            name_item.setTextAlignment(Qt.AlignCenter)
            self.record_table.setItem(i, 1, name_item)

            time_item = QTableWidgetItem(rec["timestamp"])
            time_item.setTextAlignment(Qt.AlignCenter)
            self.record_table.setItem(i, 2, time_item)

            if rec["price"] > 0:
                price_text = f"¥{rec['price']:.2f}"
            else:
                price_text = "--"
            price_item = QTableWidgetItem(price_text)
            price_item.setTextAlignment(Qt.AlignCenter)
            self.record_table.setItem(i, 3, price_item)

            if rec["expected_price"] > 0:
                expected_text = f"¥{rec['expected_price']:.2f}"
            else:
                expected_text = "--"
            expected_item = QTableWidgetItem(expected_text)
            expected_item.setTextAlignment(Qt.AlignCenter)
            expected_item.setForeground(QColor("#27ae60"))
            self.record_table.setItem(i, 4, expected_item)

        self._mark_xls_dirty()

    # ==================== XLS 落盘 ====================

    def _mark_xls_dirty(self):
        """
        标记记录有变化，稍后合并写入

        旧实现是每次抓完一个商品就重建整本工作簿（O(n²) 且阻塞界面），
        这里改成脏标记 + 延时合并：连续多次抓取只落盘一次。
        """
        self._xls_dirty = True
        if not self._xls_save_timer.isActive():
            self._xls_save_timer.start(self.XLS_SAVE_DELAY)

    def _flush_xls(self, force: bool = False) -> bool:
        """把内存中的记录写入 XLS，返回是否成功"""
        if not HAS_OPENPYXL:
            return False
        if not force and not self._xls_dirty:
            return True

        try:
            rows = write_xlsx(self.products, self.xls_file_path)
        except PermissionError:
            self._on_xls_locked()
            return False
        except Exception as e:
            logger.error(f"[XLS] 保存失败: {e}")
            self._xls_dirty = True
            return False

        self._xls_dirty = False
        self._xls_lock_warned = False
        logger.debug(f"[XLS] 已写入 {rows} 条记录 -> {self.xls_file_path}")
        return True

    def _on_xls_locked(self):
        """记录文件被占用（多半是正被 Excel 打开）：提示一次，并稍后自动重试"""
        logger.warning("[XLS] 记录文件被占用，本次写入跳过，稍后自动重试")
        self._xls_dirty = True
        self.status_label.setText(
            "价格记录文件正被 Excel 占用，已暂停写入；关闭 Excel 后会自动补写"
        )
        self._xls_save_timer.start(5000)

        if not self._xls_lock_warned:
            self._xls_lock_warned = True
            QMessageBox.warning(
                self,
                "记录文件被占用",
                f"无法写入：\n{self.xls_file_path}\n\n"
                "该文件可能正被 Excel 打开。\n"
                "本次写入已跳过，数据仍在内存中，关闭 Excel 后会自动补写。",
            )

    def _export_records(self):
        """导出价格记录到用户指定位置（支持 .xlsx / .csv）"""
        default_name = f"价格记录_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        default_path = os.path.join(BASE_DIR, default_name)

        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出价格记录",
            default_path,
            "Excel 工作簿 (*.xlsx);;CSV 文件 (*.csv)",
        )
        if not path:
            return

        # 用户没写扩展名时，按所选筛选器补全
        if not os.path.splitext(path)[1]:
            path += ".csv" if "csv" in selected_filter.lower() else ".xlsx"

        is_csv = path.lower().endswith(".csv")
        if not is_csv and not HAS_OPENPYXL:
            QMessageBox.warning(
                self, "无法导出",
                "缺少 openpyxl 库，无法导出 Excel。\n可以改为导出 CSV 文件。",
            )
            return

        try:
            rows = write_csv(self.products, path) if is_csv else write_xlsx(self.products, path)
        except PermissionError:
            QMessageBox.warning(
                self, "导出失败",
                f"文件无法写入，可能正被 Excel 打开：\n{path}\n\n请关闭后重试。",
            )
            return
        except Exception as e:
            logger.exception("[XLS] 导出失败")
            QMessageBox.critical(self, "导出失败", f"{e}")
            return

        if rows == 0:
            QMessageBox.information(self, "暂无记录", "当前还没有价格记录可以导出。")
            return

        logger.info(f"[XLS] 已导出 {rows} 条记录 -> {path}")
        QMessageBox.information(
            self, "导出成功",
            f"已导出 {rows} 条记录到：\n{path}",
        )

    # ==================== 弹窗提醒 ====================

    def _process_alert_queue(self):
        """处理弹窗队列"""
        if self._closing or self._showing_alert or not self._alert_queue:
            return
        self._showing_alert = True
        product = self._alert_queue.pop(0)
        self.alert_signal.emit(product)

    def _show_alert_dialog(self, product: Product):
        """显示弹窗提醒（任何异常都不能让提醒队列卡死）"""
        try:
            dialog = AlertDialog(product, self.alert_sound_path, self)
            dialog.exec_()
        except Exception:
            logger.exception("[提醒] 弹窗显示失败")
        finally:
            self._showing_alert = False
            QTimer.singleShot(500, self._process_alert_queue)

    # ==================== 关闭事件 ====================

    def closeEvent(self, event):
        """窗口关闭时保存数据并清理"""
        logger.info("蹲价关闭")
        self._closing = True
        self.countdown_timer.stop()
        self.check_timer.stop()

        # 先回收抓取线程，再关闭浏览器：
        # 否则线程还在操作 driver 时被关闭，退出阶段容易崩溃或残留 chromedriver 进程
        self._reset_worker_state(abort=True)

        self.store.save_products(self.products)
        self.store.save_config(self._config)
        # 退出前把尚未落盘的记录强制写一次（节流器可能仍在等待）
        self._xls_save_timer.stop()
        self._flush_xls(force=True)
        self.engine.close_browser()
        event.accept()