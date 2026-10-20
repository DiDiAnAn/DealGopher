"""
统一路径解析

- 源码运行：以脚本所在目录为基准
- PyInstaller 打包后：以 exe 所在目录为基准

注意：打包后 __file__ 指向 _MEIPASS 临时目录，程序退出即删除，
因此日志、数据、Excel 等一切需要留存的文件都必须基于 BASE_DIR，而不是 __file__。
"""
import os
import sys


def get_base_dir() -> str:
    """返回程序的工作基准目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = get_base_dir()
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
XLS_FILE = os.path.join(BASE_DIR, "price_records.xlsx")
# 崩溃现场记录（原生崩溃 / 未捕获异常）
CRASH_LOG = os.path.join(LOG_DIR, "crash.log")


def ensure_dirs():
    """确保数据目录和日志目录存在"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
