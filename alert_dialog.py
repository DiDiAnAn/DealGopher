"""
弹窗提醒 - 价格达标时弹出
"""
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
)
from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QFont, QDesktopServices

from models import Product


class AlertDialog(QDialog):
    """价格达标提醒弹窗"""

    def __init__(self, product: Product, alert_sound_path: str = "", parent=None):
        super().__init__(parent)
        self.product = product
        self.alert_sound_path = alert_sound_path
        self.setWindowTitle("🔔 价格达标提醒")
        self.setFixedSize(420, 300)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self._init_ui()
        self._play_alert_sound()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(28, 24, 28, 24)

        # 标题
        title = QLabel("🔔 价格达标提醒！")
        title.setFont(QFont("Microsoft YaHei", 16, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("color: #e74c3c;")
        layout.addWidget(title)

        # 商品名称
        name = self.product.name or "未知商品"
        if len(name) > 40:
            name = name[:40] + "..."
        name_label = QLabel(f"商品：{name}")
        name_label.setFont(QFont("Microsoft YaHei", 10))
        name_label.setWordWrap(True)
        name_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(name_label)

        # 价格信息
        price_text = (
            f"当前价格：¥{self.product.current_price:.2f}    "
            f"预期价格：¥{self.product.expected_price:.2f}"
        )
        price_label = QLabel(price_text)
        price_label.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        price_label.setAlignment(Qt.AlignCenter)
        price_label.setStyleSheet("color: #27ae60;")
        layout.addWidget(price_label)

        # 提示
        hint = QLabel("🎵 提示音正在循环播放，关闭弹窗后停止")
        hint.setFont(QFont("Microsoft YaHei", 9))
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color: #7f8c8d;")
        layout.addWidget(hint)

        # 按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(16)

        # 跳转链接按钮
        jump_btn = QPushButton("🌐 跳转商品链接")
        jump_btn.setFont(QFont("Microsoft YaHei", 11))
        jump_btn.setFixedSize(160, 40)
        jump_btn.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white;
                border: none; border-radius: 4px;
            }
            QPushButton:hover { background-color: #2980b9; }
        """)
        jump_btn.clicked.connect(self._jump_and_close)
        btn_layout.addWidget(jump_btn)

        # 我知道了按钮
        close_btn = QPushButton("我知道了")
        close_btn.setFont(QFont("Microsoft YaHei", 11))
        close_btn.setFixedSize(140, 40)
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c; color: white;
                border: none; border-radius: 4px;
            }
            QPushButton:hover { background-color: #c0392b; }
        """)
        close_btn.clicked.connect(self._stop_and_close)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

    def _stop_sound(self):
        """停止提示音"""
        try:
            import pygame
            pygame.mixer.music.stop()
            pygame.mixer.quit()
            print("[AlertDialog] 提示音已停止")
        except Exception:
            pass

    def _jump_and_close(self):
        """跳转商品链接并关闭弹窗"""
        self._stop_sound()
        if self.product.url:
            QDesktopServices.openUrl(QUrl(self.product.url))
        self.accept()

    def _stop_and_close(self):
        """停止音乐并关闭弹窗"""
        self._stop_sound()
        self.accept()

    def _play_alert_sound(self):
        """播放提示音（循环）"""
        if not self.alert_sound_path:
            return
        try:
            import pygame
            pygame.mixer.init()
            pygame.mixer.music.load(self.alert_sound_path)
            pygame.mixer.music.play(-1)  # -1 表示无限循环
            print(f"[AlertDialog] 正在播放提示音: {self.alert_sound_path}")
        except Exception as e:
            print(f"[AlertDialog] 播放提示音失败: {e}")

    def closeEvent(self, event):
        """窗口被关闭时（包括点X）也停止音乐"""
        self._stop_sound()
        super().closeEvent(event)