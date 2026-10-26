"""SerialMonitor 1.5 debug: GUI work is bounded; serial and disk IO stay off GUI."""
from __future__ import annotations

from collections import deque
from datetime import datetime
import os
from pathlib import Path
import queue
import sys
import threading

import PyQt5
from PyQt5.QtCore import QCoreApplication, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from serial_core import DisplayBuffer, SerialSession, encode_send, export_records
from display_format import StreamFormatter
from auto_baud import parse_rates, validate_baud
from device_scan import scan_ports


VERSION = "1.5 debug"
HISTORY_BYTES = 1024 * 1024

# Qt 5's wheel can encode its built-in plugin path incorrectly in Chinese
# directories. Supplying the real Unicode path also makes source launches work.
_plugin_path = Path(PyQt5.__file__).resolve().parent / "Qt5" / "plugins"
if _plugin_path.is_dir():
    QCoreApplication.addLibraryPath(str(_plugin_path))


def combo(values, current=None, editable=False):
    widget = QComboBox()
    widget.addItems(values)
    widget.setEditable(editable)
    if current:
        widget.setCurrentText(current)
    return widget


class MonitorWindow(QMainWindow):
    windows = set()

    def __init__(self):
        super().__init__()
        self.windows.add(self)
        self.session = SerialSession()
        self.ui_events = queue.SimpleQueue()
        self.history = deque()
        self.history_bytes = 0
        self.render_queue = DisplayBuffer()
        self.history_trimmed = 0
        self.screen_dropped = 0
        self.connected = False
        self.connecting = False
        self.closing = False
        self.port_scan = False
        self.scan_identify = False
        self.scan_stop = threading.Event()
        self.identities = {}
        self.active_port = ""
        self.baud_pending = False
        self.exporting = False
        self.recording = False
        self.log_pending = False
        self.log_path = ""
        self._build_ui()
        self._reset_decoders()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(40)
        self.refresh_ports(identify=False)

    def _build_ui(self):
        self.setWindowTitle(f"串口助手 · SerialMonitor {VERSION}")
        self.resize(1180, 840)
        self.setMinimumSize(1000, 700)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("串口助手")
        title.setObjectName("title")
        heading.addWidget(title)
        subtitle = QLabel("SERIAL MONITOR  /  1.5 DEBUG")
        subtitle.setObjectName("muted")
        heading.addWidget(subtitle)
        heading.addStretch()
        self.new_window = QPushButton("＋ 新窗口")
        self.new_window.clicked.connect(self.open_window)
        heading.addWidget(self.new_window)
        layout.addLayout(heading)

        config_frame = QFrame()
        config_frame.setObjectName("card")
        grid = QGridLayout(config_frame)
        grid.setContentsMargins(14, 12, 14, 12)
        self.port = combo([], editable=True)
        self.port.setMinimumWidth(225)
        self.port.setToolTip("选择或手输 COM 端口；loop:// 是不连接设备的模拟回环")
        self.refresh = QPushButton("扫描识别")
        self.refresh.setToolTip("点击查询已知 FreeBCI USB 接口的身份；占用端口和未知接口不会收到查询。再次点击停止扫描。")
        self.refresh.clicked.connect(lambda: self.refresh_ports(identify=True))
        self.baud = combo(["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600", "1000000", "2000000"], "115200", True)
        self.baud.setToolTip("连接中也可改波特率，然后点「应用」。只修改电脑端串口，不发送指令改变板子。")
        self.apply_baud = QPushButton("应用")
        self.apply_baud.setEnabled(False)
        self.apply_baud.clicked.connect(self.apply_baudrate)
        baud_panel = QWidget()
        baud_layout = QHBoxLayout(baud_panel)
        baud_layout.setContentsMargins(0, 0, 0, 0)
        baud_layout.setSpacing(5)
        baud_layout.addWidget(self.baud, 1)
        baud_layout.addWidget(self.apply_baud)
        self.bits = combo(["5", "6", "7", "8"], "8")
        self.parity = combo(["N 无", "E 偶", "O 奇", "M 标记", "S 空格"])
        self.stopbits = combo(["1", "1.5", "2"])
        self.open_button = QPushButton("打开串口")
        self.open_button.setObjectName("primary")
        self.open_button.setMinimumWidth(115)
        self.open_button.clicked.connect(self.toggle_port)
        labels = ["串口 / 设备", "", "波特率", "数据位", "校验位", "停止位", ""]
        controls = [self.port, self.refresh, baud_panel, self.bits, self.parity, self.stopbits, self.open_button]
        for col, (label, control) in enumerate(zip(labels, controls)):
            grid.addWidget(QLabel(label), 0, col)
            grid.addWidget(control, 1, col)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(2, 1)
        layout.addWidget(config_frame)
        self.config_controls = [self.port, self.bits, self.parity, self.stopbits]

        self.identity_label = QLabel("设备身份：点击「扫描识别」查询；启动时仅列出端口")
        self.identity_label.setTextFormat(Qt.PlainText)
        self.identity_label.setWordWrap(True)
        self.identity_label.setObjectName("muted")
        layout.addWidget(self.identity_label)
        self.port.currentTextChanged.connect(self.update_identity_label)

        auto_bar = QHBoxLayout()
        self.auto_baud = QCheckBox("自动波特率（FreeBCI 试探）")
        self.auto_baud.setToolTip("默认关闭。仅监听 FreeBCI 日志或连续有效帧；试探时暂停发送，可能漏收/误收。约 3 秒无有效特征且有未验证输入，或静默约 15 秒后重新试探。")
        self.auto_rates = QLineEdit("115200,921600,2000000")
        self.auto_rates.setMinimumWidth(225)
        self.auto_rates.setMaximumWidth(280)
        self.auto_rates.setCursorPosition(0)
        self.auto_rates.setToolTip("1–8 个候选波特率，逗号分隔；当前速率也会参与。")
        self.auto_restart = QPushButton("重新探测")
        self.auto_restart.setEnabled(False)
        self.auto_restart.clicked.connect(self.restart_auto)
        self.auto_label = QLabel("自动：关闭")
        self.auto_label.setTextFormat(Qt.PlainText)
        self.auto_label.setWordWrap(True)
        self.auto_label.setObjectName("muted")
        for widget in [self.auto_baud, QLabel("候选"), self.auto_rates, self.auto_restart]:
            auto_bar.addWidget(widget)
        auto_bar.addWidget(self.auto_label, 1)
        self.auto_baud.toggled.connect(self.configure_auto)
        layout.addLayout(auto_bar)

        display_bar = QHBoxLayout()
        self.hex_view = QCheckBox("HEX 显示")
        self.hex_columns = QSpinBox()
        self.hex_columns.setRange(20, 90)
        self.hex_columns.setValue(32)
        self.hex_columns.setSuffix(" 字节")
        self.hex_columns.setKeyboardTracking(False)
        self.hex_columns.setToolTip("HEX 每行 20–90 字节，默认 32；每组 00 算 1 字节，空格和时间戳不计入。修改后重新排列缓存。")
        self.hex_columns.setEnabled(False)
        self.hex_view.toggled.connect(self.hex_columns.setEnabled)
        self.timestamps = QCheckBox("时间戳")
        self.timestamps.setChecked(True)
        self.autoscroll = QCheckBox("自动滚动")
        self.autoscroll.setChecked(True)
        self.encoding = combo(["UTF-8", "GB18030", "ASCII", "Latin-1"])
        self.pause = QPushButton("暂停显示")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(self.pause_display)
        self.clear = QPushButton("清空缓存")
        self.clear.clicked.connect(self.clear_history)
        for widget in [self.hex_view, QLabel("每行"), self.hex_columns, self.timestamps, self.autoscroll, QLabel("编码"), self.encoding]:
            display_bar.addWidget(widget)
        display_bar.addStretch()
        display_bar.addWidget(self.pause)
        display_bar.addWidget(self.clear)
        layout.addLayout(display_bar)
        for control in [self.hex_view, self.timestamps]:
            control.toggled.connect(self.rebuild_display)
        self.encoding.currentTextChanged.connect(self.rebuild_display)
        self.hex_columns.valueChanged.connect(self.rebuild_display)

        splitter = QSplitter(Qt.Vertical)
        self.receive = QPlainTextEdit()
        self.receive.setReadOnly(True)
        self.receive.setPlaceholderText("接收与发送记录会显示在这里。\n先选择串口和波特率，再点击「打开串口」。\n没有设备时可选择 loop:// 模拟回环。")
        data_font = QFont("Consolas", 10)
        data_font.setStyleHint(QFont.Monospace)
        data_font.setFixedPitch(True)
        self.receive.setFont(data_font)
        self.receive.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.receive.document().setMaximumBlockCount(4000)
        splitter.addWidget(self.receive)
        send_panel = QWidget()
        send_layout = QVBoxLayout(send_panel)
        send_layout.setContentsMargins(0, 7, 0, 0)
        send_bar = QHBoxLayout()
        send_bar.addWidget(QLabel("发送数据"))
        self.hex_send = QCheckBox("HEX 发送")
        self.hex_send.setToolTip("例如 01 03 00 00 FF；不接受 0x 前缀、逗号或奇数个十六进制数字")
        send_bar.addWidget(self.hex_send)
        send_bar.addStretch()
        send_bar.addWidget(QLabel("追加结尾"))
        self.ending = combo(["无", "CRLF (\\r\\n)", "LF (\\n)", "CR (\\r)"])
        send_bar.addWidget(self.ending)
        self.send_button = QPushButton("发送  Ctrl+Enter")
        self.send_button.setObjectName("primary")
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self.send_data)
        send_bar.addWidget(self.send_button)
        send_layout.addLayout(send_bar)
        self.send_edit = QPlainTextEdit()
        self.send_edit.setPlaceholderText("文本或十六进制数据，例如：Hello 或 01 03 00 FF")
        self.send_edit.setFont(data_font)
        self.send_edit.setMaximumHeight(150)
        self.send_edit.setMinimumHeight(70)
        send_layout.addWidget(self.send_edit)
        splitter.addWidget(send_panel)
        splitter.setSizes([400, 130])
        layout.addWidget(splitter, 1)
        from PyQt5.QtWidgets import QShortcut
        from PyQt5.QtGui import QKeySequence
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.send_data)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self.send_data)

        save_bar = QHBoxLayout()
        self.export_button = QPushButton("导出缓存…")
        self.export_button.setToolTip("导出最近最多 1 MiB 的缓存；连续完整保存请先开始文件记录")
        self.export_button.clicked.connect(self.export_history)
        self.record_button = QPushButton("开始文件记录…")
        self.record_button.setToolTip("后台保存时间戳 HEX 日志、原始 RX/TX 二进制；暂停/清空显示不影响记录")
        self.record_button.clicked.connect(self.toggle_recording)
        self.log_label = QLabel("完整保存：先开始文件记录，再打开串口")
        self.log_label.setObjectName("muted")
        self.log_label.setWordWrap(True)
        save_bar.addWidget(self.export_button)
        save_bar.addWidget(self.record_button)
        save_bar.addWidget(self.log_label, 1)
        layout.addLayout(save_bar)
        self.status = QLabel("● 未连接")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.stats = QLabel("RX 0 B  ·  TX 0 B")
        self.stats.setObjectName("muted")
        layout.addWidget(self.stats)
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f5f7fb; color: #23344c; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
            QLabel#title { font-size: 25px; font-weight: 700; }
            QLabel#muted { color: #65758a; font-size: 12px; }
            QFrame#card { background: #ffffff; border: 1px solid #dce3ed; border-radius: 9px; }
            QFrame#card QLabel { background: #ffffff; color: #65758a; font-size: 12px; }
            QPushButton { background: #ffffff; border: 1px solid #ced8e5; padding: 7px 12px; border-radius: 5px; }
            QPushButton:hover { background: #eaf0f9; }
            QPushButton:checked { background: #e2ebf8; }
            QPushButton#primary { background: #2463d4; color: white; border-color: #2463d4; }
            QPushButton#primary:hover { background: #1b50b0; }
            QPushButton:disabled { background: #edf0f4; color: #95a1b1; border-color: #dce3ed; }
            QComboBox, QSpinBox, QLineEdit { background: white; border: 1px solid #ced8e5; border-radius: 4px; padding: 5px; min-height: 22px; }
            QComboBox:disabled, QSpinBox:disabled { background: #edf0f4; color: #7c8a9b; }
            QPlainTextEdit { background: white; border: 1px solid #ced8e5; border-radius: 6px; padding: 7px; selection-background-color: #c6dbff; font-family: 'Consolas'; font-size: 13px; }
            QCheckBox { spacing: 5px; }
            QCheckBox::indicator { width: 15px; height: 15px; }
        """)

    def open_window(self):
        child = MonitorWindow()
        child.show()

    def refresh_ports(self, identify=True):
        if self.port_scan:
            self.scan_stop.set()
            self.refresh.setText("正在停止…")
            return
        self.port_scan = True
        self.scan_identify = identify
        self.scan_stop.clear()
        self.refresh.setText("停止扫描")
        excluded = {w.active_port for w in self.windows if w.session.is_alive()}

        def scan():
            try:
                scan_ports(lambda kind, value: self.ui_events.put((kind, value)), self.scan_stop.is_set,
                           identify=identify, excluded=excluded)
            except Exception as exc:
                self.ui_events.put(("scan_error", str(exc)))
            finally:
                self.ui_events.put(("scan_done", ""))
        threading.Thread(target=scan, name="port-scan", daemon=True).start()

    def update_identity_label(self, *_):
        result = self.identities.get(self.selected_port())
        if result and result.identity:
            identity = result.identity
            self.identity_label.setText(f"设备：{identity.display_name}  ·  ID {identity.uid}  ·  查询速率 {result.baudrate}")
            self.identity_label.setToolTip(identity.details)
        elif result:
            self.identity_label.setText("设备身份：" + result.message)
            self.identity_label.setToolTip(result.message)
        else:
            self.identity_label.setText("设备身份：未查询；点击「扫描识别」")

    def apply_baudrate(self):
        try:
            rate = validate_baud(self.baud.currentText().strip())
            if self.auto_baud.isChecked():
                self.auto_baud.setChecked(False)
            self.session.set_baudrate(rate)
            self.baud_pending = True
            self.apply_baud.setEnabled(False)
            self.send_button.setEnabled(False)
        except Exception as exc:
            self.show_error(str(exc))

    def configure_auto(self, enabled):
        try:
            rates = parse_rates(self.auto_rates.text()) if enabled else (115200, 921600, 2000000)
            self.session.configure_auto(enabled, rates)
            if enabled:
                self.send_button.setEnabled(False)
            self.auto_rates.setEnabled(not enabled)
            self.auto_label.setText("自动：准备试探（匹配前暂停发送）" if enabled else "自动：正在关闭" if self.connected else "自动：关闭")
        except Exception as exc:
            self.auto_baud.blockSignals(True)
            self.auto_baud.setChecked(False)
            self.auto_baud.blockSignals(False)
            self.show_error(str(exc))

    def restart_auto(self):
        self.configure_auto(True)

    def update_controls(self):
        self.apply_baud.setEnabled(self.connected and not self.closing and not self.baud_pending)
        self.baud.setEnabled(not self.closing)
        self.auto_baud.setEnabled(not self.closing)
        self.auto_rates.setEnabled(not self.auto_baud.isChecked() and not self.closing)
        self.auto_restart.setEnabled(self.connected and self.auto_baud.isChecked() and not self.closing)
        self.send_button.setEnabled(self.connected and not self.baud_pending and not self.session.scanning_event.is_set() and not self.closing)
        self.refresh.setEnabled(not self.closing)

    def connected_status(self):
        self.status.setStyleSheet("color: #14745e;")
        self.status.setText(f"● 已连接 {self.active_port}  ·  {self.session.current_baud} baud  ·  {self.bits.currentText()}{self.parity.currentText()[0]}{self.stopbits.currentText()}")

    def selected_port(self):
        text = self.port.currentText().strip()
        index = self.port.currentIndex()
        if index >= 0 and text == self.port.itemText(index):
            return self.port.itemData(index)
        return text

    def toggle_port(self):
        if self.connected or self.connecting:
            self.session.close()
            self.status.setText("● 正在关闭串口…")
            self.open_button.setEnabled(False)
            self.send_button.setEnabled(False)
            return
        try:
            if any(w.port_scan and w.scan_identify for w in self.windows):
                raise ValueError("身份扫描进行中，请等待或停止扫描后再打开串口")
            port = self.selected_port()
            if not port:
                raise ValueError("请选择或输入串口号，例如 COM3")
            baud = validate_baud(self.baud.currentText().strip())
            self.session.open(dict(port=port, baudrate=baud, bytesize=int(self.bits.currentText()),
                                   parity=self.parity.currentText()[0], stopbits=float(self.stopbits.currentText())))
            self.active_port = port
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.connecting = True
        self.status.setText(f"● 正在打开 {port}…")
        self.open_button.setText("取消打开")
        for control in self.config_controls:
            control.setEnabled(False)

    def send_data(self):
        if not self.connected or not self.send_button.isEnabled():
            return
        try:
            ending = ["", "\r\n", "\n", "\r"][self.ending.currentIndex()]
            data = encode_send(self.send_edit.toPlainText(), self.hex_send.isChecked(), self.encoding.currentText(), ending)
            self.session.send(data)
        except Exception as exc:
            self.show_error(f"无法发送：{exc}")

    def _reset_decoders(self):
        self.formatter = StreamFormatter(self.encoding.currentText(), self.timestamps.isChecked(), self.hex_view.isChecked(),
                                         hex_bytes_per_line=self.hex_columns.value())

    def rebuild_display(self, *_):
        self.receive.clear()
        self.render_queue.clear()
        self._reset_decoders()
        if not self.pause.isChecked():
            for record in self.history:
                self.render_queue.put(record)

    def pause_display(self, paused):
        self.pause.setText("继续显示" if paused else "暂停显示")
        self.render_queue.clear()
        if not paused:
            self.rebuild_display()

    def clear_history(self):
        self.history.clear()
        self.history_bytes = 0
        self.history_trimmed = 0
        self.screen_dropped = 0
        self.session.display.clear()
        self.rebuild_display()

    def render_record(self, record):
        return self.formatter.render(record)

    def append_render(self, records):
        if not records:
            return
        scrollbar = self.receive.verticalScrollBar()
        old_scroll = scrollbar.value()
        cursor = QTextCursor(self.receive.document())
        cursor.movePosition(QTextCursor.End)
        # Group runs by direction to reduce QTextDocument calls under load.
        group = []
        direction = None
        for record in records:
            if direction is not None and record.direction != direction:
                self._insert_group(cursor, direction, group)
                group = []
            direction = record.direction
            group.append(self.render_record(record))
        self._insert_group(cursor, direction, group)
        document = self.receive.document()
        if document.characterCount() > HISTORY_BYTES + 1:
            trim = QTextCursor(document)
            trim.setPosition(document.characterCount() - HISTORY_BYTES - 1)
            trim.movePosition(QTextCursor.EndOfBlock)
            trim.movePosition(QTextCursor.NextCharacter)
            trim.setPosition(0, QTextCursor.KeepAnchor)
            trim.removeSelectedText()
        if self.autoscroll.isChecked():
            scrollbar.setValue(scrollbar.maximum())
        else:
            scrollbar.setValue(old_scroll)

    @staticmethod
    def _insert_group(cursor, direction, group):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#14745e" if direction == "RX" else "#245bb4"))
        cursor.insertText("".join(group), fmt)

    def toggle_recording(self):
        if self.recording:
            self.session.stop_recording()
            self.recording = False
            self.log_pending = True
            self.record_button.setEnabled(False)
            self.record_button.setText("正在写完记录…")
            return
        folder = QFileDialog.getExistingDirectory(self, "选择记录父文件夹（会创建独立会话子目录）")
        if not folder:
            return
        self.start_recording_at(Path(folder))

    def start_recording_at(self, parent):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        directory = Path(parent) / f"Serial_{stamp}_{os.getpid()}"
        try:
            self.session.start_recording(directory)
        except Exception as exc:
            self.show_error(str(exc))
            return
        self.log_pending = True
        self.log_path = str(directory)
        self.record_button.setEnabled(False)
        self.record_button.setText("正在创建记录…")

    def export_history(self):
        if not self.history:
            self.show_error("当前没有缓存数据")
            return
        path, chosen = QFileDialog.getSaveFileName(self, "导出缓存（最近最多 1 MiB；完整记录请用文件记录）",
            f"SerialCache_{datetime.now():%Y%m%d_%H%M%S}.log", "时间戳 HEX 日志 (*.log);;原始接收数据 (*.bin)")
        if path:
            self.export_to(path, "*.bin" in chosen)

    def export_to(self, path, raw=False):
        records = tuple(self.history)
        self.exporting = True
        self.export_button.setEnabled(False)

        def save():
            try:
                export_records(path, records, raw)
                self.ui_events.put(("exported", str(path)))
            except Exception as exc:
                self.ui_events.put(("export_error", str(exc)))
        threading.Thread(target=save, name="cache-export", daemon=True).start()

    def show_error(self, text):
        self.status.setText("● " + text)
        self.status.setStyleSheet("color: #b52937;")
        self.status.setToolTip(text)

    def handle_event(self, kind, detail):
        if kind == "opened":
            self.connected = True
            self.connecting = False
            self.open_button.setText("关闭串口")
            self.active_port = detail
            self.baud_pending = False
            self.connected_status()
        elif kind == "closed":
            self.connected = self.connecting = False
            self.baud_pending = False
            self.send_button.setEnabled(False)
            self.open_button.setText("打开串口")
            self.open_button.setEnabled(not self.closing)
            for control in self.config_controls:
                control.setEnabled(not self.closing)
            if "#b52937" not in self.status.styleSheet():
                self.status.setStyleSheet("")
                self.status.setText("● 串口已关闭")
        elif kind == "baud_changed":
            if detail["source"] == "manual":
                self.baud_pending = False
            if not self.baud.lineEdit().hasFocus() or detail["source"] == "manual":
                self.baud.setCurrentText(str(detail["baudrate"]))
            if self.connected:
                self.connected_status()
        elif kind == "baud_error":
            self.baud_pending = False
            self.show_error(detail)
        elif kind == "auto_status":
            state, rate = detail["state"], detail["baudrate"]
            if state == "locked":
                self.auto_label.setText(f"已匹配 {rate}：{detail.get('reason', '')}")
                if self.connected:
                    self.connected_status()
            elif state == "searching":
                self.auto_label.setText(f"试探 {rate}：仅监听，发送暂停")
            elif state == "unsupported":
                self.auto_label.setText(f"跳过不支持的速率 {rate}")
            else:
                self.auto_label.setText(f"自动：关闭  ·  当前 {rate}")
        elif kind == "reconnecting":
            self.connected = False
            self.connecting = True
            self.send_button.setEnabled(False)
            self.open_button.setText("关闭串口")
            self.status.setStyleSheet("color: #a26012;")
            self.status.setText("● " + detail)
            self.status.setToolTip(detail)
            self.auto_label.setText("设备断开，自动重连中")
        elif kind == "error":
            self.show_error(detail)
        elif kind == "log_started":
            self.recording = True
            self.log_pending = False
            self.record_button.setEnabled(not self.closing)
            self.record_button.setText("停止文件记录")
            self.log_label.setText("记录中：" + detail)
            self.log_label.setToolTip(detail)
        elif kind in ("log_stopped", "log_error"):
            self.recording = self.log_pending = False
            self.session.stop_recording()
            self.record_button.setEnabled(not self.closing)
            self.record_button.setText("开始文件记录…")
            self.log_label.setText(("记录异常：" if kind == "log_error" else "已保存：") + detail)
            self.log_label.setToolTip(detail)
            if kind == "log_error":
                self.show_error(detail)
        elif kind in ("ports", "scan_error"):
            if kind == "scan_error":
                self.show_error("刷新串口失败：" + detail)
                return
            old = self.selected_port()
            self.identities = {}
            self.port.clear()
            for name, description in detail:
                self.port.addItem(f"{name} — {description}", name)
            self.port.addItem("loop:// — 模拟回环（无设备自测）", "loop://")
            match = self.port.findData(old)
            if match >= 0:
                self.port.setCurrentIndex(match)
            elif old:
                self.port.setEditText(old)
        elif kind == "identity":
            self.identities[detail.device] = detail
            index = self.port.findData(detail.device)
            if index >= 0:
                label = f"{detail.device} — {detail.identity.display_name}" if detail.identity else self.port.itemText(index)
                if detail.occupied:
                    label += " [占用]"
                self.port.setItemText(index, label)
                self.port.setItemData(index, detail.identity.details if detail.identity else detail.message, Qt.ToolTipRole)
            self.update_identity_label()
        elif kind == "scan_done":
            self.port_scan = False
            self.scan_identify = False
            self.refresh.setText("扫描识别")
        elif kind in ("exported", "export_error"):
            self.exporting = False
            self.export_button.setEnabled(not self.closing)
            if kind == "export_error":
                self.show_error("导出失败：" + detail)
            else:
                self.log_label.setText("缓存已导出：" + detail)
                self.log_label.setToolTip(detail)

    def poll(self):
        for source in (self.session.events, self.ui_events):
            for _ in range(50):
                try:
                    kind, detail = source.get_nowait()
                except queue.Empty:
                    break
                self.handle_event(kind, detail)
        self.update_controls()
        records, dropped = self.session.display.drain(65536, 256)
        self.screen_dropped += dropped
        if dropped:
            self._reset_decoders()
        for record in records:
            self.history.append(record)
            self.history_bytes += len(record.data)
            while self.history_bytes > HISTORY_BYTES or len(self.history) > 8192:
                oldest = self.history.popleft()
                self.history_bytes -= len(oldest.data)
                self.history_trimmed += len(oldest.data)
            if not self.pause.isChecked():
                self.render_queue.put(record)
        if not self.pause.isChecked():
            render, skipped = self.render_queue.drain(32768, 128)
            self.screen_dropped += skipped
            if skipped:
                self._reset_decoders()
            self.append_render(render)
        rx, tx = self.session.counters()
        extra = f"  ·  显示已跳过 {self.screen_dropped:,} B（文件记录独立）" if self.screen_dropped else ""
        self.stats.setText(f"RX {rx:,} B  ·  TX {tx:,} B  ·  缓存 {self.history_bytes:,} / 1,048,576 B  ·  已淘汰 {self.history_trimmed:,} B" + extra)
        if self.closing and not self.session.is_alive():
            # Keep the recorder attached until the final in-flight read returns.
            self.session.stop_recording()
        if self.closing and not self.session.busy() and not self.exporting and not self.port_scan:
            self.close()

    def closeEvent(self, event):
        if not self.closing:
            self.closing = True
            self.scan_stop.set()
            self.session.close()
            if not self.session.is_alive():
                self.session.stop_recording()
            self.connected = self.connecting = False
            for widget in [*self.config_controls, self.baud, self.apply_baud, self.refresh, self.auto_baud, self.auto_rates, self.auto_restart, self.open_button, self.send_button, self.record_button, self.export_button, self.new_window]:
                widget.setEnabled(False)
        if self.session.busy() or self.exporting or self.port_scan:
            event.ignore()
            self.status.setText("● 正在关闭连接并写完文件，请稍候…")
            return
        self.timer.stop()
        self.windows.discard(self)
        event.accept()


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("串口助手")
    app.setStyle("Fusion")
    window = MonitorWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
