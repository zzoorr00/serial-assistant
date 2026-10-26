"""SerialMonitor 1.6 debug: GUI work is bounded; serial and disk IO stay off GUI."""
from __future__ import annotations

from collections import deque
from datetime import datetime
import os
from pathlib import Path
import queue
import sys
import threading

import PyQt5
from PyQt5.QtCore import QCoreApplication, QLocale, Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit, QPushButton,
    QSpinBox, QSplitter, QVBoxLayout, QWidget,
)

from serial_core import DisplayBuffer, SerialSession, encode_send, export_records
from display_format import StreamFormatter
from serial_core import validate_baud
from ui_text import english_text, error_text
from device_scan import scan_ports


VERSION = "1.6 debug"
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
        self.setWindowTitle(f"Serial Assistant · SerialMonitor {VERSION}")
        self.resize(1180, 840)
        self.setMinimumSize(1000, 700)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("Serial Assistant")
        title.setObjectName("title")
        heading.addWidget(title)
        subtitle = QLabel("SERIAL MONITOR  /  1.6 DEBUG")
        subtitle.setObjectName("muted")
        heading.addWidget(subtitle)
        heading.addStretch()
        self.new_window = QPushButton("+ New window")
        self.new_window.clicked.connect(self.open_window)
        heading.addWidget(self.new_window)
        layout.addLayout(heading)

        config_frame = QFrame()
        config_frame.setObjectName("card")
        grid = QGridLayout(config_frame)
        grid.setContentsMargins(14, 12, 14, 12)
        self.port = combo([], editable=True)
        self.port.setMinimumWidth(225)
        self.port.setToolTip("Select or type a COM port; loop:// is a software loopback test")
        self.refresh = QPushButton("Scan devices")
        self.refresh.setToolTip("Query supported FreeBCI USB devices. Busy ports and unknown interfaces are skipped. Click again to cancel.")
        self.refresh.clicked.connect(lambda: self.refresh_ports(identify=True))
        self.baud = combo(["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600", "1000000", "2000000"], "115200", True)
        self.baud.setToolTip("Change the baud rate while connected, then click Apply. This changes the host port only.")
        self.apply_baud = QPushButton("Apply")
        self.apply_baud.setEnabled(False)
        self.apply_baud.clicked.connect(self.apply_baudrate)
        baud_panel = QWidget()
        baud_layout = QHBoxLayout(baud_panel)
        baud_layout.setContentsMargins(0, 0, 0, 0)
        baud_layout.setSpacing(5)
        baud_layout.addWidget(self.baud, 1)
        baud_layout.addWidget(self.apply_baud)
        self.bits = combo(["5", "6", "7", "8"], "8")
        self.parity = combo(["N None", "E Even", "O Odd", "M Mark", "S Space"])
        self.stopbits = combo(["1", "1.5", "2"])
        self.open_button = QPushButton("Open port")
        self.open_button.setObjectName("primary")
        self.open_button.setMinimumWidth(115)
        self.open_button.clicked.connect(self.toggle_port)
        labels = ["Port / device", "", "Baud rate", "Data bits", "Parity", "Stop bits", ""]
        controls = [self.port, self.refresh, baud_panel, self.bits, self.parity, self.stopbits, self.open_button]
        for col, (label, control) in enumerate(zip(labels, controls)):
            grid.addWidget(QLabel(label), 0, col)
            grid.addWidget(control, 1, col)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(2, 1)
        layout.addWidget(config_frame)
        self.config_controls = [self.port, self.bits, self.parity, self.stopbits]

        self.identity_label = QLabel("Device identity: click Scan devices to query; startup only lists ports")
        self.identity_label.setTextFormat(Qt.PlainText)
        self.identity_label.setWordWrap(True)
        self.identity_label.setObjectName("muted")
        layout.addWidget(self.identity_label)
        self.port.currentTextChanged.connect(self.update_identity_label)

        display_bar = QHBoxLayout()
        self.hex_view = QCheckBox("HEX view")
        self.hex_columns = QSpinBox()
        self.hex_columns.setRange(20, 90)
        self.hex_columns.setValue(32)
        self.hex_columns.setSuffix(" bytes")
        self.hex_columns.setKeyboardTracking(False)
        self.hex_columns.setToolTip("20-90 bytes per HEX row, default 32. Spaces, timestamps and direction headers are excluded.")
        self.hex_columns.setEnabled(False)
        self.hex_view.toggled.connect(self.hex_columns.setEnabled)
        self.timestamps = QCheckBox("Timestamps")
        self.timestamps.setChecked(True)
        self.autoscroll = QCheckBox("Auto scroll")
        self.autoscroll.setChecked(True)
        self.encoding = combo(["UTF-8", "GB18030", "ASCII", "Latin-1"])
        self.pause = QPushButton("Pause display")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(self.pause_display)
        self.clear = QPushButton("Clear cache")
        self.clear.clicked.connect(self.clear_history)
        for widget in [self.hex_view, QLabel("Per row"), self.hex_columns, self.timestamps, self.autoscroll, QLabel("Encoding"), self.encoding]:
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
        self.receive.setPlaceholderText("Received and sent data appears here.\nSelect a port and baud rate, then click Open port.\nUse loop:// to test without a device.")
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
        send_bar.addWidget(QLabel("Send data"))
        self.hex_send = QCheckBox("HEX send")
        self.hex_send.setToolTip("Example: 01 03 00 00 FF. No 0x prefixes, commas or odd hex digits.")
        send_bar.addWidget(self.hex_send)
        send_bar.addStretch()
        send_bar.addWidget(QLabel("Append ending"))
        self.ending = combo(["None", "CRLF (\\r\\n)", "LF (\\n)", "CR (\\r)"])
        send_bar.addWidget(self.ending)
        self.send_button = QPushButton("Send  Ctrl+Enter")
        self.send_button.setObjectName("primary")
        self.send_button.setEnabled(False)
        self.send_button.clicked.connect(self.send_data)
        send_bar.addWidget(self.send_button)
        send_layout.addLayout(send_bar)
        self.send_edit = QPlainTextEdit()
        self.send_edit.setPlaceholderText("Text or hex data, for example: Hello or 01 03 00 FF")
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
        self.export_button = QPushButton("Export cache...")
        self.export_button.setToolTip("Export up to 1 MiB of recent cached data. Start recording for a complete session.")
        self.export_button.clicked.connect(self.export_history)
        self.record_button = QPushButton("Start recording...")
        self.record_button.setToolTip("Save timestamped HEX logs and raw RX/TX bytes. Pause and Clear affect the display only.")
        self.record_button.clicked.connect(self.toggle_recording)
        self.log_label = QLabel("For a complete session, start recording before opening the port")
        self.log_label.setObjectName("muted")
        self.log_label.setWordWrap(True)
        save_bar.addWidget(self.export_button)
        save_bar.addWidget(self.record_button)
        save_bar.addWidget(self.log_label, 1)
        layout.addLayout(save_bar)
        self.status = QLabel("● Disconnected")
        self.status.setObjectName("status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.stats = QLabel("RX 0 B  ·  TX 0 B")
        self.stats.setObjectName("muted")
        layout.addWidget(self.stats)
        self.setStyleSheet("""
            QMainWindow, QWidget { background: #f5f7fb; color: #23344c; font-family: 'Segoe UI'; font-size: 13px; }
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
            self.refresh.setText("Canceling...")
            return
        self.port_scan = True
        self.scan_identify = identify
        self.scan_stop.clear()
        self.refresh.setText("Cancel scan")
        excluded = {w.active_port for w in self.windows if w.session.is_alive()}

        def scan():
            try:
                scan_ports(lambda kind, value: self.ui_events.put((kind, value)), self.scan_stop.is_set,
                           identify=identify, excluded=excluded)
            except Exception as exc:
                self.ui_events.put(("scan_error", error_text(exc)))
            finally:
                self.ui_events.put(("scan_done", ""))
        threading.Thread(target=scan, name="port-scan", daemon=True).start()

    def update_identity_label(self, *_):
        result = self.identities.get(self.selected_port())
        if result and result.identity:
            identity = result.identity
            self.identity_label.setText(f"Device: {identity.display_name}  ·  ID {identity.uid}  ·  Query baud {result.baudrate}")
            self.identity_label.setToolTip(identity.details)
        elif result:
            self.identity_label.setText("Device identity: " + english_text(result.message))
            self.identity_label.setToolTip(english_text(result.message))
        else:
            self.identity_label.setText("Device identity: not queried; click Scan devices")

    def apply_baudrate(self):
        try:
            rate = validate_baud(self.baud.currentText().strip())
            self.session.set_baudrate(rate)
            self.baud_pending = True
            self.apply_baud.setEnabled(False)
            self.send_button.setEnabled(False)
        except Exception as exc:
            self.show_error(error_text(exc))

    def update_controls(self):
        self.apply_baud.setEnabled(self.connected and not self.closing and not self.baud_pending)
        self.baud.setEnabled(not self.closing)
        self.send_button.setEnabled(self.connected and not self.baud_pending and not self.closing)
        self.refresh.setEnabled(not self.closing)

    def connected_status(self):
        self.status.setStyleSheet("color: #14745e;")
        self.status.setText(f"● Connected {self.active_port}  ·  {self.session.current_baud} baud  ·  {self.bits.currentText()}{self.parity.currentText()[0]}{self.stopbits.currentText()}")

    def selected_port(self):
        text = self.port.currentText().strip()
        index = self.port.currentIndex()
        if index >= 0 and text == self.port.itemText(index):
            return self.port.itemData(index)
        return text

    def toggle_port(self):
        if self.connected or self.connecting:
            self.session.close()
            self.status.setText("Closing port...")
            self.open_button.setEnabled(False)
            self.send_button.setEnabled(False)
            return
        try:
            if any(w.port_scan and w.scan_identify for w in self.windows):
                raise ValueError("Identity scan is running. Wait or cancel it before opening a port.")
            port = self.selected_port()
            if not port:
                raise ValueError("Select or enter a serial port, for example COM3")
            baud = validate_baud(self.baud.currentText().strip())
            self.session.open(dict(port=port, baudrate=baud, bytesize=int(self.bits.currentText()),
                                   parity=self.parity.currentText()[0], stopbits=float(self.stopbits.currentText())))
            self.active_port = port
        except Exception as exc:
            self.show_error(error_text(exc))
            return
        self.connecting = True
        self.status.setText(f"● Opening {port}…")
        self.open_button.setText("Cancel open")
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
            self.show_error(f"Send failed: {error_text(exc)}")

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
        self.pause.setText("Resume display" if paused else "Pause display")
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
            self.record_button.setText("Finishing recording...")
            return
        folder = QFileDialog.getExistingDirectory(self, "Select a recording folder (a session subfolder will be created)", options=QFileDialog.DontUseNativeDialog)
        if not folder:
            return
        self.start_recording_at(Path(folder))

    def start_recording_at(self, parent):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        directory = Path(parent) / f"Serial_{stamp}_{os.getpid()}"
        try:
            self.session.start_recording(directory)
        except Exception as exc:
            self.show_error(error_text(exc))
            return
        self.log_pending = True
        self.log_path = str(directory)
        self.record_button.setEnabled(False)
        self.record_button.setText("Creating recording...")

    def export_history(self):
        if not self.history:
            self.show_error("There is no cached data to export")
            return
        path, chosen = QFileDialog.getSaveFileName(self, "Export recent cache (up to 1 MiB; use recording for complete sessions)",
            f"SerialCache_{datetime.now():%Y%m%d_%H%M%S}.log", "Timestamped HEX log (*.log);;Raw received bytes (*.bin)", options=QFileDialog.DontUseNativeDialog)
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
                self.ui_events.put(("export_error", error_text(exc)))
        threading.Thread(target=save, name="cache-export", daemon=True).start()

    def show_error(self, text):
        text = english_text(text, "Operation failed. Check the device, port, permissions and settings.")
        self.status.setText("● " + text)
        self.status.setStyleSheet("color: #b52937;")
        self.status.setToolTip(text)

    def handle_event(self, kind, detail):
        if kind == "opened":
            self.connected = True
            self.connecting = False
            self.open_button.setText("Close port")
            self.active_port = detail
            self.baud_pending = False
            self.connected_status()
        elif kind == "closed":
            self.connected = self.connecting = False
            self.baud_pending = False
            self.send_button.setEnabled(False)
            self.open_button.setText("Open port")
            self.open_button.setEnabled(not self.closing)
            for control in self.config_controls:
                control.setEnabled(not self.closing)
            if "#b52937" not in self.status.styleSheet():
                self.status.setStyleSheet("")
                self.status.setText("● Port closed")
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
        elif kind == "reconnecting":
            self.connected = False
            self.connecting = True
            self.send_button.setEnabled(False)
            self.open_button.setText("Close port")
            self.status.setStyleSheet("color: #a26012;")
            detail = english_text(detail, "Device disconnected; waiting to reconnect")
            self.status.setText("● " + detail)
            self.status.setToolTip(detail)
        elif kind == "error":
            self.show_error(detail)
        elif kind == "log_started":
            detail = english_text(detail, "selected location")
            self.recording = True
            self.log_pending = False
            self.record_button.setEnabled(not self.closing)
            self.record_button.setText("Stop recording")
            self.log_label.setText("Recording: " + detail)
            self.log_label.setToolTip(detail)
        elif kind in ("log_stopped", "log_error"):
            detail = english_text(detail, "selected location")
            self.recording = self.log_pending = False
            self.session.stop_recording()
            self.record_button.setEnabled(not self.closing)
            self.record_button.setText("Start recording...")
            self.log_label.setText(("Recording error: " if kind == "log_error" else "Saved: ") + detail)
            self.log_label.setToolTip(detail)
            if kind == "log_error":
                self.show_error(detail)
        elif kind in ("ports", "scan_error"):
            if kind == "scan_error":
                self.show_error("Port scan failed: " + detail)
                return
            old = self.selected_port()
            self.identities = {}
            self.port.clear()
            for name, description in detail:
                self.port.addItem(f"{name} - {english_text(description, 'Serial device')}", name)
            self.port.addItem("loop:// - Software loopback (no device)", "loop://")
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
                    label += " [Busy]"
                self.port.setItemText(index, label)
                self.port.setItemData(index, detail.identity.details if detail.identity else english_text(detail.message), Qt.ToolTipRole)
            self.update_identity_label()
        elif kind == "scan_done":
            self.port_scan = False
            self.scan_identify = False
            self.refresh.setText("Scan devices")
        elif kind in ("exported", "export_error"):
            detail = english_text(detail, "selected location")
            self.exporting = False
            self.export_button.setEnabled(not self.closing)
            if kind == "export_error":
                self.show_error("Export failed: " + detail)
            else:
                self.log_label.setText("Cache exported: " + detail)
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
        extra = f"  ·  Display skipped {self.screen_dropped:,} B (recording is separate)" if self.screen_dropped else ""
        self.stats.setText(f"RX {rx:,} B  ·  TX {tx:,} B  ·  Cache {self.history_bytes:,} / 1,048,576 B  ·  Evicted {self.history_trimmed:,} B" + extra)
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
            for widget in [*self.config_controls, self.baud, self.apply_baud, self.refresh, self.open_button, self.send_button, self.record_button, self.export_button, self.new_window]:
                widget.setEnabled(False)
        if self.session.busy() or self.exporting or self.port_scan:
            event.ignore()
            self.status.setText("● Closing the connection and finishing files. Please wait...")
            return
        self.timer.stop()
        self.windows.discard(self)
        event.accept()


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    QLocale.setDefault(QLocale(QLocale.English, QLocale.UnitedStates))
    app = QApplication(sys.argv)
    app.setApplicationName("Serial Assistant")
    app.setStyle("Fusion")
    window = MonitorWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
