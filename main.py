"""
蹲价 - 入口文件
支持平台：淘宝、天猫、京东、拼多多、抖音商城
功能：商品监控、定时刷新、价格走势图、弹窗提醒
"""
import faulthandler
import os
import sys
import threading
import traceback
from datetime import datetime

from paths import BASE_DIR, CRASH_LOG, ensure_dirs


# 打包后资源路径适配（源码运行=脚本目录，打包后=exe目录）
os.chdir(BASE_DIR)
ensure_dirs()

# ==================== 崩溃诊断 ====================
# 窗口程序没有控制台，未捕获异常和原生崩溃（Qt 等 C++ 层）默认什么都不留，
# 表现就是"闪退"。这里把所有崩溃现场都写进 logs\crash.log：
#   · faulthandler：捕获致命信号（段错误/abort），输出各线程的 Python 栈
#   · excepthook：捕获未处理异常
#   · threading.excepthook：捕获抓取线程里的异常
_crash_fp = open(CRASH_LOG, "a", encoding="utf-8", buffering=1)
_crash_fp.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} 程序启动 =====\n")
faulthandler.enable(file=_crash_fp, all_threads=True)

_previous_excepthook = sys.excepthook


def _log_uncaught(exc_type, exc_value, exc_tb):
    _crash_fp.write(f"\n[未捕获异常] {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    traceback.print_exception(exc_type, exc_value, exc_tb, file=_crash_fp)
    _crash_fp.flush()
    if _previous_excepthook:
        _previous_excepthook(exc_type, exc_value, exc_tb)


def _log_thread_exception(args):
    _crash_fp.write(f"\n[线程未捕获异常] {datetime.now():%Y-%m-%d %H:%M:%S} "
                    f"thread={args.thread.name if args.thread else '?'}\n")
    traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=_crash_fp)
    _crash_fp.flush()


sys.excepthook = _log_uncaught
threading.excepthook = _log_thread_exception


# 设置matplotlib中文字体
import matplotlib
matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "STSong"]
matplotlib.rcParams["axes.unicode_minus"] = False

from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from browser_detector import detect_browsers
from browser_dialog import ask_browser
from models import DataStore
from main_window import MainWindow


def choose_browser() -> str:
    """
    启动时检测本机浏览器并让用户选择

    返回浏览器名（"chrome"/"edge"）；用户取消或本机没有可用浏览器时返回空串。
    选择结果写入 config.json；若勾选了"记住选择"，下次启动跳过询问。
    """
    detected = detect_browsers()
    if not detected:
        QMessageBox.critical(
            None,
            "未找到可用浏览器",
            "本机没有检测到 Google Chrome 或 Microsoft Edge。\n\n"
            "本程序需要借助其中之一来打开商品页面抓取价格，\n"
            "请先安装 Chrome 或 Edge 后再启动。",
        )
        return ""

    store = DataStore()
    config = store.load_config()
    remembered = config.get("browser", "")
    auto_use = bool(config.get("remember_browser", False))

    # 用户上次勾选了"记住选择"，且该浏览器仍然存在 → 不再询问
    if auto_use and remembered in detected:
        return remembered

    choice, remember = ask_browser(detected, current=remembered)
    if not choice:
        return ""

    config["browser"] = choice
    config["remember_browser"] = remember
    store.save_config(config)
    return choice


def main():
    # 高DPI支持
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setFont(QFont("Microsoft YaHei", 9))

    # 全局样式
    app.setStyleSheet("""
        QMainWindow {
            background-color: #ecf0f1;
        }
        QWidget {
            font-family: 'Microsoft YaHei', 'Microsoft JhengHei', sans-serif;
        }
        QToolTip {
            background-color: #2c3e50;
            color: white;
            border: none;
            padding: 6px;
            font-size: 9pt;
        }
    """)

    # 先检测本机浏览器，让用户决定用哪一个抓取价格
    browser = choose_browser()
    if not browser:
        sys.exit(1)

    window = MainWindow(browser=browser)
    window.show()

    sys.exit(app.exec_())


if __name__ == '__main__':
    main()