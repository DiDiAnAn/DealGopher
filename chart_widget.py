"""
历史价格走势图组件（嵌入PyQt5）
"""
import matplotlib

# 画布要嵌入 PyQt5 窗口，必须使用 Qt5 后端。
# 原来这里设的是 "Agg"（纯非交互后端），与下面的 FigureCanvasQTAgg 相互矛盾。
matplotlib.use("Qt5Agg", force=True)

# 中文字体：不依赖调用方（main.py）先设置，避免图例里的中文变成方块
matplotlib.rcParams["font.sans-serif"] = [
    "Microsoft YaHei", "SimHei", "Microsoft JhengHei", "DejaVu Sans",
]
matplotlib.rcParams["axes.unicode_minus"] = False

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.dates as mdates
from datetime import datetime
from typing import List
from models import PriceRecord


class PriceChartWidget(FigureCanvas):
    """价格走势图组件"""

    def __init__(self, parent=None):
        self.fig = Figure(figsize=(6, 3), dpi=100)
        self.fig.patch.set_facecolor("#f8f9fa")
        self.fig.subplots_adjust(left=0.12, right=0.95, top=0.92, bottom=0.18)
        super().__init__(self.fig)
        self.ax = self.fig.add_subplot(111)
        self._setup_axes()

    def _setup_axes(self):
        """初始化坐标轴样式"""
        self.ax.clear()
        self.ax.set_facecolor("#ffffff")
        self.ax.set_title("历史价格走势图", fontsize=12, fontweight="bold",
                          fontfamily="Microsoft YaHei")
        self.ax.set_xlabel("日期", fontsize=9, fontfamily="Microsoft YaHei")
        self.ax.set_ylabel("价格 (¥)", fontsize=9, fontfamily="Microsoft YaHei")
        self.ax.tick_params(axis="both", labelsize=8)
        self.ax.grid(True, linestyle="--", alpha=0.5)

    def update_chart(self, history: List[PriceRecord], expected_price: float = 0.0,
                     product_name: str = ""):
        """更新走势图"""
        self.ax.clear()
        self._setup_axes()

        if not history:
            self.ax.text(0.5, 0.5, "暂无价格数据", ha="center", va="center",
                         fontsize=14, color="#999999",
                         fontfamily="Microsoft YaHei", transform=self.ax.transAxes)
            self.draw()
            return

        # 解析时间：只画"可信价格"的点（无货/下架/规格失效等状态不入曲线）
        dates = []
        prices = []
        skipped = 0
        for record in history:
            record_status = getattr(record, "status", "ok") or "ok"
            if record_status != "ok" or (record.price or 0) <= 0:
                skipped += 1
                continue
            try:
                dt = datetime.strptime(record.timestamp, "%Y-%m-%d %H:%M:%S")
                dates.append(dt)
                prices.append(record.price)
            except (ValueError, TypeError):
                continue

        if not dates:
            self.ax.text(0.5, 0.5,
                         "暂无有效价格数据" + (f"\n（{skipped} 条无货/异常记录已跳过）" if skipped else ""),
                         ha="center", va="center", fontsize=12, color="#999999",
                         fontfamily="Microsoft YaHei", transform=self.ax.transAxes)
            self.draw()
            return

        # 绘制价格折线
        self.ax.plot(dates, prices, color="#e74c3c", linewidth=2, marker="o",
                     markersize=4, label="当前价格", zorder=3)

        # 填充折线下方区域
        self.ax.fill_between(dates, prices, alpha=0.1, color="#e74c3c")

        # 绘制预期价格线
        if expected_price > 0:
            self.ax.axhline(y=expected_price, color="#27ae60", linestyle="--",
                            linewidth=1.5, label=f"预期价格 ¥{expected_price:.2f}",
                            zorder=2)

        # 格式化X轴日期
        if len(dates) > 1:
            self.ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
            self.ax.xaxis.set_major_locator(mdates.AutoDateLocator())
            self.fig.autofmt_xdate(rotation=30)

        # 设置Y轴范围（留一些余量）
        if prices:
            min_p = min(prices)
            max_p = max(prices)
            if expected_price > 0:
                min_p = min(min_p, expected_price)
                max_p = max(max_p, expected_price)
            margin = (max_p - min_p) * 0.15 if max_p > min_p else max_p * 0.1
            self.ax.set_ylim(max(0, min_p - margin), max_p + margin)

        self.ax.legend(loc="upper right", fontsize=8, framealpha=0.9)
        self.fig.tight_layout()
        self.draw()

    def clear_chart(self):
        """清空图表"""
        self.ax.clear()
        self._setup_axes()
        self.ax.text(0.5, 0.5, "请选择商品查看价格趋势", ha="center", va="center",
                     fontsize=14, color="#999999",
                     fontfamily="Microsoft YaHei", transform=self.ax.transAxes)
        self.draw()