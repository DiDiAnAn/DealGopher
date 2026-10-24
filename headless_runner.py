"""
蹲价 · 无界面运行器（NAS / 服务器版）

和桌面版的区别：
- 不依赖 PyQt5 / matplotlib / pygame，可在 Linux 容器里跑（镜像更小、无图形界面）
- 用 while 循环做调度，而不是 Qt 定时器
- 抓取结果除了写本地文件，还会推送到云端看板（可选）
- 价格达标时写告警日志并调用 webhook 推送（企业微信/钉钉机器人）

完全复用桌面版的核心：
- browser_engine.py  浏览器控制 + 五个平台的解析器
- models.py          商品与价格记录模型、JSON 持久化
- uploader.py        云端上传与失败重试

环境变量：
  PM_DATA_DIR        数据目录（默认 /data），容器里由 compose 挂载
  PM_CHROME_BINARY   Chromium 路径（默认 /usr/bin/chromium）
  PM_CHROME_DRIVER   chromedriver 路径（默认 /usr/bin/chromedriver）
  PM_HEADLESS        是否无头运行（默认 1；设 0 可用 Xvfb 可视化调试）
  PM_TICK_SECONDS    调度检查间隔秒数（默认 30）
  PM_UPLOAD_URL      看板接收地址；留空则不上传
  PM_UPLOAD_TOKEN    上传密钥，与 push.php 约定
  PM_WEBHOOK_URL     达标推送 webhook；留空则不推送
  PM_WRITE_XLSX      是否同时生成 price_records.xlsx（默认 0）

启动：
  python -u headless_runner.py
"""
import json
import os
import signal
import sys
import time
import traceback
import urllib.request
from datetime import datetime

# ---------------- 环境准备 ----------------
DATA_DIR = os.environ.get("PM_DATA_DIR", "/data")
if not os.path.isabs(DATA_DIR):
    DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATA_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
os.environ.setdefault("PM_DATA_DIR", DATA_DIR)

# 让核心模块使用容器里的数据目录
import paths  # noqa: E402

paths.DATA_DIR = DATA_DIR
paths.LOG_DIR = os.path.join(DATA_DIR, "logs")
paths.XLS_FILE = os.path.join(DATA_DIR, "price_records.xlsx")
os.makedirs(paths.LOG_DIR, exist_ok=True)

from browser_engine import BrowserEngine  # noqa: E402
from models import DataStore, Product  # noqa: E402
from uploader import Uploader, build_record  # noqa: E402

TICK_SECONDS = int(os.environ.get("PM_TICK_SECONDS", "30"))
HEADLESS = os.environ.get("PM_HEADLESS", "1") not in ("0", "false", "False")
WRITE_XLSX = os.environ.get("PM_WRITE_XLSX", "0") in ("1", "true", "True")
WEBHOOK_URL = os.environ.get("PM_WEBHOOK_URL", "").strip()

_running = True


def log(message: str, level: str = "INFO"):
    """同时输出到 stdout（docker logs 可见）和日志文件"""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{stamp} [{level}] {message}"
    print(line, flush=True)
    try:
        path = os.path.join(paths.LOG_DIR, f"runner_{datetime.now():%Y%m%d}.log")
        with open(path, "a", encoding="utf-8") as file:
            file.write(line + "\n")
    except Exception:
        pass


def notify_webhook(product) -> None:
    """价格达标时推送一条消息到 webhook（企业微信群机器人格式）"""
    if not WEBHOOK_URL:
        return
    text = (
        f"【蹲价】价格达标提醒\n"
        f"商品：{product.name or product.url}\n"
        f"平台：{product.platform_display_name()}\n"
        f"当前：¥{product.current_price:.2f}\n"
        f"预期：¥{product.expected_price:.2f}\n"
        f"链接：{product.url}"
    )
    payload = json.dumps({"msgtype": "text", "text": {"content": text}},
                         ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        WEBHOOK_URL, data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        log(f"[推送] 已发送达标提醒: {product.name[:20]}")
    except Exception as e:
        log(f"[推送] 发送失败: {e}", "WARN")


def setup_signals():
    """docker stop 时会发 SIGTERM，这里优雅退出"""

    def handler(signum, frame):
        global _running
        _running = False
        log(f"收到信号 {signum}，准备退出…", "WARN")

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def load_products(store: DataStore):
    products = store.load_products()
    if store._load_failed:
        log("[启动] products.json 解析失败，为避免覆盖已停止保存", "ERROR")
    return products


def main():
    setup_signals()

    log("=" * 60)
    log(f"蹲价 · 无界面运行器启动   数据目录={DATA_DIR}   headless={HEADLESS}")
    log(f"上传看板: {'开启 -> ' + os.environ.get('PM_UPLOAD_URL', '') if os.environ.get('PM_UPLOAD_URL') else '关闭'}")
    log("=" * 60)

    store = DataStore(DATA_DIR)
    products = load_products(store)
    if not products:
        log("[启动] 没有商品。请把桌面版导出的 data/products.json 放进数据目录后重启容器。", "WARN")
        log("[启动] 目录结构应为: /data/products.json、/data/cookies/*.json（可选）")
        # 空配置时也保持运行，方便用户挂载文件后重启（restart: unless-stopped 会自动拉起）

    uploader = Uploader(DATA_DIR)
    engine = BrowserEngine(DATA_DIR, browser="chrome", headless=HEADLESS)

    log(f"[启动] 浏览器: {engine.binary_path or '(未指定，交给 Selenium Manager)'}")
    log(f"[启动] 驱动  : {os.environ.get('PM_CHROME_DRIVER', '(未指定)')}")
    log(f"[启动] 商品数: {len(products)}")

    while _running:
        try:
            now = datetime.now()
            for product in list(products):
                if not _running:
                    break
                if not product.is_due(now):
                    continue

                log(f"[抓取] 开始: {product.name or product.url[:40]}")
                name, price, original = engine.fetch_product_info(product.url, product.platform)

                if price > 0:
                    if name:
                        product.name = name
                    product.add_price_record(price, original)
                    log(f"[抓取] 成功: {product.name[:24]} 当前¥{price:.2f} 原价¥{original:.2f}")
                else:
                    log(f"[抓取] 失败: {product.name[:24] or product.url[:40]} "
                        f"（{engine.last_error or '未获取到价格，可能需要重新登录'}）", "WARN")

                # 无论成功失败都保存，避免 last_refresh 状态丢失导致重复触发
                store.save_products(products)

                if price > 0:
                    record = build_record(product, price, original)
                    if uploader.enabled:
                        uploader.push(record)
                    uploader.flush_queue()

                    if product.is_price_reached():
                        log(f"[达标] {product.name[:24]} ¥{price:.2f} <= ¥{product.expected_price:.2f}")
                        notify_webhook(product)

            if WRITE_XLSX:
                try:
                    from excel_export import write_xlsx
                    rows = write_xlsx(products, paths.XLS_FILE)
                    log(f"[记录] 已写入 xlsx: {rows} 条", "DEBUG")
                except Exception as e:
                    log(f"[记录] 写 xlsx 失败: {e}", "WARN")

            # 分段 sleep，保证收到 SIGTERM 能及时退出
            for _ in range(TICK_SECONDS):
                if not _running:
                    break
                time.sleep(1)
        except Exception as e:
            log(f"[主循环] 异常: {e}\n{traceback.format_exc()}", "ERROR")
            time.sleep(5)

    log("[退出] 关闭浏览器…")
    try:
        engine.close_browser()
    except Exception:
        pass
    log("[退出] 已停止")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(f"[致命] {traceback.format_exc()}", "ERROR")
        sys.exit(1)
