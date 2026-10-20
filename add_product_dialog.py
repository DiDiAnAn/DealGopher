"""
添加/编辑商品对话框
"""
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QFormLayout, QTimeEdit, QDoubleSpinBox, QMessageBox,
    QComboBox, QSpinBox, QStackedWidget, QWidget, QPushButton
)
from PyQt5.QtCore import Qt, QTime
from PyQt5.QtGui import QFont

from platforms import all_domains


class AddProductDialog(QDialog):
    """添加/编辑商品对话框"""

    # 最小刷新间隔（分钟），与 MainWindow.MIN_REFRESH_INTERVAL 保持一致
    MIN_INTERVAL = 15

    def __init__(self, parent=None, product=None):
        super().__init__(parent)
        self.is_edit = product is not None
        self.product = product

        self.setWindowTitle("编辑商品设置" if self.is_edit else "添加监控商品")
        self.setFixedSize(480, 380)

        # 结果
        self.product_url = ""
        self.expected_price = 0.0
        self.refresh_mode = "daily"
        self.refresh_time = "10:00"
        self.refresh_interval = 60

        # 编辑模式下预填数据
        if self.is_edit:
            self.product_url = product.url
            self.expected_price = product.expected_price
            self.refresh_mode = product.refresh_mode
            self.refresh_time = product.refresh_time
            self.refresh_interval = product.refresh_interval

        self._init_ui()

        # 编辑模式下预填UI
        if self.is_edit:
            self.url_input.setText(product.url)
            self.price_input.setValue(product.expected_price)
            self.mode_combo.setCurrentIndex(0 if product.refresh_mode == "daily" else 1)
            h, m = map(int, product.refresh_time.split(":"))
            self.time_input.setTime(QTime(h, m))
            if product.refresh_interval >= 60 and product.refresh_interval % 60 == 0:
                self.interval_unit_combo.setCurrentIndex(0)  # 小时
                self.interval_value_spin.setValue(product.refresh_interval // 60)
            else:
                self.interval_unit_combo.setCurrentIndex(1)  # 分钟
                self.interval_value_spin.setValue(product.refresh_interval)
            self._on_mode_changed(self.mode_combo.currentIndex())

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 24, 24, 24)

        form = QFormLayout()
        form.setSpacing(14)

        # 商品链接
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("请粘贴淘宝/天猫/京东商品链接...")
        self.url_input.setFont(QFont("Microsoft YaHei", 10))
        if self.is_edit:
            self.url_input.setReadOnly(True)
            self.url_input.setStyleSheet("background-color: #f0f0f0;")
        form.addRow("商品链接：", self.url_input)

        # 预期价格
        self.price_input = QDoubleSpinBox()
        self.price_input.setRange(0, 9999999)
        self.price_input.setDecimals(2)
        self.price_input.setPrefix("¥ ")
        self.price_input.setFont(QFont("Microsoft YaHei", 10))
        form.addRow("预期价格：", self.price_input)

        # 刷新模式选择
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["每日刷新", "间隔刷新"])
        self.mode_combo.setFont(QFont("Microsoft YaHei", 10))
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("刷新方式：", self.mode_combo)

        # 堆叠组件：每日刷新 / 间隔刷新
        self.mode_stack = QStackedWidget()

        # ---- 页面0：每日刷新 ----
        page_daily = QWidget()
        daily_layout = QHBoxLayout(page_daily)
        daily_layout.setContentsMargins(0, 0, 0, 0)
        self.time_input = QTimeEdit()
        self.time_input.setDisplayFormat("HH:mm")
        self.time_input.setTime(QTime(10, 0))
        self.time_input.setFont(QFont("Microsoft YaHei", 10))
        daily_layout.addWidget(QLabel("每天"))
        daily_layout.addWidget(self.time_input)
        daily_layout.addWidget(QLabel("刷新"))
        daily_layout.addStretch()
        self.mode_stack.addWidget(page_daily)

        # ---- 页面1：间隔刷新 ----
        page_interval = QWidget()
        interval_layout = QHBoxLayout(page_interval)
        interval_layout.setContentsMargins(0, 0, 0, 0)

        self.interval_value_spin = QSpinBox()
        self.interval_value_spin.setRange(1, 999)
        self.interval_value_spin.setValue(1)
        self.interval_value_spin.setFont(QFont("Microsoft YaHei", 10))
        self.interval_value_spin.setFixedWidth(80)

        self.interval_unit_combo = QComboBox()
        self.interval_unit_combo.addItems(["小时", "分钟"])
        self.interval_unit_combo.setFont(QFont("Microsoft YaHei", 10))
        self.interval_unit_combo.currentIndexChanged.connect(self._on_unit_changed)

        interval_layout.addWidget(QLabel("每隔"))
        interval_layout.addWidget(self.interval_value_spin)
        interval_layout.addWidget(self.interval_unit_combo)
        interval_layout.addWidget(QLabel("刷新一次"))
        interval_layout.addStretch()
        self.mode_stack.addWidget(page_interval)

        form.addRow("", self.mode_stack)

        layout.addLayout(form)

        # 按钮
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("确定")
        ok_btn.setFont(QFont("Microsoft YaHei", 10))
        ok_btn.setFixedSize(100, 36)
        ok_btn.setStyleSheet("""
            QPushButton { background-color: #3498db; color: white; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #2980b9; }
        """)
        ok_btn.clicked.connect(self._on_ok)

        cancel_btn = QPushButton("取消")
        cancel_btn.setFont(QFont("Microsoft YaHei", 10))
        cancel_btn.setFixedSize(100, 36)
        cancel_btn.setStyleSheet("""
            QPushButton { background-color: #95a5a6; color: white; border: none; border-radius: 4px; }
            QPushButton:hover { background-color: #7f8c8d; }
        """)
        cancel_btn.clicked.connect(self.reject)

        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        btn_layout.setAlignment(Qt.AlignCenter)
        layout.addLayout(btn_layout)

    def _on_mode_changed(self, index):
        """切换刷新模式"""
        self.mode_stack.setCurrentIndex(index)

    def _on_unit_changed(self, index):
        """
        切换间隔单位时调整数值范围

        分钟下限设为 15：20 个商品抓一轮约 5~7 分钟，
        间隔过小会导致"上一轮没刷完、下一轮又到期"的任务堆积。
        """
        if index == 0:  # 小时
            self.interval_value_spin.setRange(1, 168)  # 最大7天
            self.interval_value_spin.setSuffix(" 小时")
        else:  # 分钟
            self.interval_value_spin.setRange(self.MIN_INTERVAL, 999)
            self.interval_value_spin.setSuffix(" 分钟")

    def _on_ok(self):
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请输入商品链接！")
            return
        if not any(domain in url.lower() for domain in all_domains()):
            QMessageBox.warning(
                self, "提示",
                "请输入有效的商品链接！\n"
                "当前支持：淘宝、天猫、京东、拼多多、抖音商城",
            )
            return

        self.product_url = url
        self.expected_price = self.price_input.value()

        if self.mode_combo.currentIndex() == 0:
            # 每日刷新
            self.refresh_mode = "daily"
            self.refresh_time = self.time_input.time().toString("HH:mm")
        else:
            # 间隔刷新
            self.refresh_mode = "interval"
            value = self.interval_value_spin.value()
            if self.interval_unit_combo.currentIndex() == 0:
                # 小时 → 转为分钟
                self.refresh_interval = value * 60
            else:
                # 分钟
                self.refresh_interval = value

        self.accept()