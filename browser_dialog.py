"""
浏览器选择对话框

启动时列出本机检测到的浏览器，让用户决定用哪一个抓取价格。
"""
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QRadioButton, QGroupBox,
    QCheckBox, QDialogButtonBox
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont


class BrowserChooserDialog(QDialog):
    """列出本机已检测到的浏览器，供用户选择"""

    def __init__(self, detected: dict, current: str = "", parent=None):
        super().__init__(parent)
        self.detected = detected
        self.current = current
        self.selected = ""
        self.remember = False
        self._radios = {}

        self.setWindowTitle("选择浏览器")
        self.setMinimumWidth(580)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        title = QLabel("已在本机检测到以下浏览器")
        title.setFont(QFont("Microsoft YaHei", 12, QFont.Bold))
        title.setStyleSheet("color: #2c3e50;")
        layout.addWidget(title)

        hint = QLabel(
            "程序会调用所选浏览器打开商品页面来抓取价格。"
            "首次使用需要在该浏览器里登录一次淘宝/天猫/京东，登录状态会被保存，之后无需重复登录。"
        )
        hint.setFont(QFont("Microsoft YaHei", 9))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7f8c8d;")
        layout.addWidget(hint)

        group = QGroupBox("可用浏览器")
        group.setFont(QFont("Microsoft YaHei", 10))
        group_layout = QVBoxLayout(group)
        group_layout.setSpacing(6)

        # 上次用过的排在最前面并默认选中
        order = list(self.detected.keys())
        if self.current in order:
            order.remove(self.current)
            order.insert(0, self.current)

        for name in order:
            info = self.detected[name]
            version = info.get("version") or "版本未知"
            radio = QRadioButton(f" {info['display']}    版本 {version}")
            radio.setFont(QFont("Microsoft YaHei", 10))
            radio.setCursor(Qt.PointingHandCursor)
            group_layout.addWidget(radio)

            path_label = QLabel(info["path"])
            path_label.setFont(QFont("Microsoft YaHei", 8))
            path_label.setStyleSheet("color: #95a5a6; margin-left: 26px; margin-bottom: 4px;")
            group_layout.addWidget(path_label)

            self._radios[name] = radio

        if order:
            self._radios[order[0]].setChecked(True)

        layout.addWidget(group)

        self.remember_check = QCheckBox("记住这次选择，下次启动直接使用（不再询问）")
        self.remember_check.setFont(QFont("Microsoft YaHei", 9))
        self.remember_check.setChecked(False)
        layout.addWidget(self.remember_check)

        note = QLabel("以后想换浏览器，可点击主窗口右下角的「更换」。")
        note.setFont(QFont("Microsoft YaHei", 8))
        note.setStyleSheet("color: #95a5a6;")
        layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("使用这个浏览器")
        ok_btn.setFont(QFont("Microsoft YaHei", 10))
        ok_btn.setDefault(True)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("退出程序")
        cancel_btn.setFont(QFont("Microsoft YaHei", 10))
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_ok(self):
        for name, radio in self._radios.items():
            if radio.isChecked():
                self.selected = name
                break
        self.remember = self.remember_check.isChecked()
        self.accept()


def ask_browser(detected: dict, current: str = "", parent=None):
    """
    弹出浏览器选择框

    返回 (浏览器名, 是否记住)；用户取消时返回 (None, False)
    """
    dialog = BrowserChooserDialog(detected, current=current, parent=parent)
    if dialog.exec_() != QDialog.Accepted or not dialog.selected:
        return (None, False)
    return (dialog.selected, dialog.remember)
