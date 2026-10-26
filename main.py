"""SerialMonitor 1.4 debug: GUI work is bounded; serial and disk IO stay off GUI."""
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
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from serial_core import DisplayBuffer, SerialSession, discover_ports, encode_send, export_records
from display_format import StreamFormatter


VERSION = "1.4 debug"
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
        self.exporting = False
        self.recording = False
        self.log_pending = False
        self.log_path = ""
        self._build_ui()
        self._reset_decoders()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(40)
        self.refresh_ports()

    def _build_ui(self):
        self.setWindowTitle(f"串口助手 · SerialMonitor {VERSION}")
        self.resize(1080, 740)
        self.setMinimumSize(850, 610)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("串口助手")
        title.setObjectName("title")
        heading.addWidget(title)
        subtitle = QLabel("SERIAL MONITOR  /  1.4 DEBUG")
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
        self.refresh = QPushButton("刷新")
        self.refresh.clicked.connect(self.refresh_ports)
        self.baud = combo(["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600", "1000000", "2000000"], "115200", True)
        self.baud.setToolTip("支持手输波特率。更改参数前请关闭串口，再重新打开。")
        self.bits = combo(["5", "6", "7", "8"], "8")
        self.parity = combo(["N 无", "E 偶", "O 奇", "M 标记", "S 空格"])
        self.stopbits = combo(["1", "1.5", "2"])
        self.open_button = QPushButton("打开串口")
        self.open_button.setObjectName("primary")
        self.open_button.setMinimumWidth(115)
        self.open_button.clicked.connect(self.toggle_port)
        labels = ["串口 / 设备", "", "波特率", "数据位", "校验位", "停止位", ""]
        controls = [self.port, self.refresh, self.baud, self.bits, self.parity, self.stopbits, self.open_button]
        for col, (label, control) in enumerate(zip(labels, controls)):
            grid.addWidget(QLabel(label), 0, col)
            grid.addWidget(control, 1, col)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(2, 1)
        layout.addWidget(config_frame)
        self.config_controls = [self.port, self.refresh, self.baud, self.bits, self.parity, self.stopbits]

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
            QComboBox, QSpinBox { background: white; border: 1px solid #ced8e5; border-radius: 4px; padding: 5px; min-height: 22px; }
            QComboBox:disabled, QSpinBox:disabled { background: #edf0f4; color: #7c8a9b; }
            QPlainTextEdit { background: white; border: 1px solid #ced8e5; border-radius: 6px; padding: 7px; selection-background-color: #c6dbff; font-family: 'Consolas'; font-size: 13px; }
            QCheckBox { spacing: 5px; }
            QCheckBox::indicator { width: 15px; height: 15px; }
        """)

    def open_window(self):
        child = MonitorWindow()
        child.show()

    def refresh_ports(self):
        if self.port_scan:
            return
        self.port_scan = True
        self.refresh.setEnabled(False)

        def scan():
            try:
                self.ui_events.put(("ports", discover_ports()))
            except Exception as exc:
                self.ui_events.put(("scan_error", str(exc)))
        threading.Thread(target=scan, name="port-scan", daemon=True).start()

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
            port = self.selected_port()
            if not port:
                raise ValueError("请选择或输入串口号，例如 COM3")
            baud = int(self.baud.currentText().strip())
            if baud < 1 or baud > 12000000:
                raise ValueError("波特率请输入 1–12000000 的整数；实际支持范围取决于设备")
            self.session.open(dict(port=port, baudrate=baud, bytesize=int(self.bits.currentText()),
                                   parity=self.parity.currentText()[0], stopbits=float(self.stopbits.currentText())))
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
            self.send_button.setEnabled(not self.closing)
            self.status.setStyleSheet("color: #14745e;")
            self.status.setText(f"● 已连接 {detail}  ·  {self.baud.currentText()} baud  ·  {self.bits.currentText()}{self.parity.currentText()[0]}{self.stopbits.currentText()}")
        elif kind == "closed":
            self.connected = self.connecting = False
            self.send_button.setEnabled(False)
            self.open_button.setText("打开串口")
            self.open_button.setEnabled(not self.closing)
            for control in self.config_controls:
                control.setEnabled(not self.closing)
            if "#b52937" not in self.status.styleSheet():
                self.status.setStyleSheet("")
                self.status.setText("● 串口已关闭")
        elif kind == "reconnecting":
            self.connected = False
            self.connecting = True
            self.send_button.setEnabled(False)
            self.open_button.setText("关闭串口")
            self.status.setStyleSheet("color: #a26012;")
            self.status.setText("● " + detail)
            self.status.setToolTip(detail)
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
            self.port_scan = False
            self.refresh.setEnabled(not (self.connected or self.connecting or self.closing))
            if kind == "scan_error":
                self.show_error("刷新串口失败：" + detail)
                return
            old = self.selected_port()
            self.port.clear()
            for name, description in detail:
                self.port.addItem(f"{name} — {description}", name)
            self.port.addItem("loop:// — 模拟回环（无设备自测）", "loop://")
            match = self.port.findData(old)
            if match >= 0:
                self.port.setCurrentIndex(match)
            elif old:
                self.port.setEditText(old)
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
        if self.closing and not self.session.busy() and not self.exporting:
            self.close()

    def closeEvent(self, event):
        if not self.closing:
            self.closing = True
            self.session.close()
            if not self.session.is_alive():
                self.session.stop_recording()
            self.connected = self.connecting = False
            for widget in [*self.config_controls, self.open_button, self.send_button, self.record_button, self.export_button, self.new_window]:
                widget.setEnabled(False)
        if self.session.busy() or self.exporting:
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
