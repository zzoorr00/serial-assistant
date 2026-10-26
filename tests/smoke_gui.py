"""Qt integration + sustained simulated input. No physical device is used."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
import faulthandler
faulthandler.dump_traceback_later(25, repeat=True)

import json
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QFontMetrics, QTextCursor
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from main import MonitorWindow
from serial_core import Record


app = QApplication([])
app.setQuitOnLastWindowClosed(False)
app.setStyle("Fusion")
results = {}


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        QTest.qWait(5)
    raise AssertionError("GUI wait timed out")


def open_loop(window):
    wait_for(lambda: not window.port_scan)
    window.port.setCurrentIndex(window.port.findData("loop://"))
    QTest.mouseClick(window.open_button, Qt.LeftButton)
    wait_for(lambda: window.connected)


def close_window(window):
    window.close()
    wait_for(lambda: window not in MonitorWindow.windows)


output = Path(__file__).resolve().parents[1] / "test-output"
output.mkdir(exist_ok=True)

# Repeated blocks of a direction have only one direction label, across UI ticks.
labels = MonitorWindow()
labels.timestamps.setChecked(False)
labels.append_render([Record(1, "RX", b"first")])
labels.append_render([Record(2, "RX", b"second"), Record(3, "TX", b"sent"), Record(4, "TX", b"again"), Record(5, "RX", b"reply")])
text = labels.receive.toPlainText()
assert text.count("RX") == 2 and text.count("TX") == 1, text
labels.receive.clear()
labels.hex_view.setChecked(True)
labels.append_render([Record(1, "RX", b"\x01"), Record(2, "RX", b"\x02"), Record(3, "TX", b"\x03"), Record(4, "RX", b"\x04")])
text = labels.receive.toPlainText()
assert text.count("RX") == 2 and text.count("TX") == 1, text
close_window(labels)
results["direction_labels_only_on_switch_text_and_hex"] = True

# Changing HEX width rerenders cached data and preserves all bytes.
width_view = MonitorWindow()
width_view.show()
assert not width_view.hex_columns.isEnabled()
assert width_view.hex_columns.value() == 32
width_view.timestamps.setChecked(False)
width_view.hex_view.setChecked(True)
assert width_view.hex_columns.isEnabled()
payload = bytes(range(203))
for part in (payload[:1], payload[1:74], payload[74:]):
    width_view.session._record("RX", part)
wait_for(lambda: width_view.history_bytes == len(payload) and not width_view.render_queue.size)
for width in (20, 47, 90):
    width_view.hex_columns.setValue(width)
    wait_for(lambda: not width_view.render_queue.size)
    rows = width_view.receive.toPlainText().removeprefix("RX\n").splitlines()
    assert all(len(row.split()) == width for row in rows[:-1]), (width, rows)
    assert len(rows[-1].split()) <= width
    assert "".join("".join(rows).split()) == payload.hex().upper()
width_view.hex_columns.setValue(19)
assert width_view.hex_columns.value() == 20
width_view.hex_columns.setValue(91)
assert width_view.hex_columns.value() == 90
width_view.hex_columns.setValue(20)
width_view.timestamps.setChecked(True)
wait_for(lambda: not width_view.render_queue.size)
rows = width_view.receive.toPlainText().splitlines()
assert rows[0] == "RX"
assert all("RX" not in row for row in rows[1:])
metrics = QFontMetrics(width_view.receive.font())
assert metrics.horizontalAdvance("00") == metrics.horizontalAdvance("FF") == metrics.horizontalAdvance("11")
pixel_columns = []
for index in range(1, min(6, len(rows))):
    block = width_view.receive.document().findBlockByNumber(index)
    cursor = QTextCursor(block)
    cursor.setPosition(block.position() + rows[index].index("] ") + 2)
    pixel_columns.append(width_view.receive.cursorRect(cursor).x())
assert len(set(pixel_columns)) == 1, pixel_columns
width_view.grab().save(str(output / "gui_hex_width.png"))
width_view.hex_view.setChecked(False)
assert not width_view.hex_columns.isEnabled()
width_view.hex_view.setChecked(True)
assert width_view.hex_columns.value() == 20
close_window(width_view)
results["hex_width_20_47_90_cached_rerender_and_limits"] = True
results["hex_headers_separate_and_monospace_pixel_alignment"] = True

# Replay fragmented board output across distinct GUI refreshes.
lines = MonitorWindow()
lines.show()
stamp = datetime(2026, 10, 26, 17, 18, 2, 121000).timestamp()
fragments = [b"w", b"aiting...\r", b"\nF", b"reeBCI V3 8-16 channel\r\nADS1299 Device ID1:0x3E\r\nADS1299 Device",
             b" ", b"ID2:0x3E\r\nLIS3DH Device ID: 0x33\r\nFirmware: v3.1.2\r\n$$$\r\nLoaded",
             b" ", b"commMode from EEPROM: 1\r\n", b"B", b"LE MAC: 00:11:22:33:44:55\r\nBLE Inited, pair state: paired with ",
             b"r", b"eceiver AA:BB:CC:DD:EE:FF\r\nInitialized BLE mode (Mode 1)\r\n", b"w", b"aiting...\r\n"]
for i, data in enumerate(fragments):
    lines.append_render([Record(stamp + i / 1000, "RX", data)])
    app.processEvents()
text = lines.receive.toPlainText()
assert "waiting..." in text and "FreeBCI V3 8-16 channel" in text
assert "ADS1299 Device ID2:0x3E" in text
assert "Loaded commMode from EEPROM: 1" in text
assert "paired with receiver AA:BB:CC:DD:EE:FF" in text
assert "\\x0d" not in text and text.count("RX") == 1
assert text.count("[") == 12, text
lines.grab().save(str(output / "gui_lines.png"))
close_window(lines)
results["fragmented_board_boot_log_real_lines"] = True

# Unterminated high-volume text is still bounded in layout and document size.
long_text = MonitorWindow()
long_text.timestamps.setChecked(False)
for i in range(300):
    long_text.append_render([Record(i, "RX", b"A" * 4096)])
assert long_text.receive.document().characterCount() <= 1048577
assert long_text.receive.document().blockCount() <= 4000
close_window(long_text)
results["long_unterminated_text_document_bounded"] = True

# UI remains armed during restart; the same close button cancels reconnect.
class RestartPort:
    in_waiting = 1
    def open(self): pass
    def read(self, size): raise OSError("board reset")
    def close(self): pass
restart = MonitorWindow()
restart.session.factory = lambda *a, **k: RestartPort()
restart.session.reconnect_interval = 5
wait_for(lambda: not restart.port_scan)
restart.port.setCurrentIndex(restart.port.findData("loop://"))
QTest.mouseClick(restart.open_button, Qt.LeftButton)
wait_for(lambda: restart.connecting and "reconnect" in restart.status.text())
assert restart.open_button.text() == "Close port"
assert not restart.send_button.isEnabled()
assert not restart.port.isEnabled()
QTest.mouseClick(restart.open_button, Qt.LeftButton)
wait_for(lambda: not restart.session.is_alive() and not restart.connecting)
assert restart.open_button.text() == "Open port"
assert restart.port.isEnabled()
close_window(restart)
results["gui_reconnect_wait_and_manual_cancel"] = True

window = MonitorWindow()
window.show()
print("GUI: window created", flush=True)
with tempfile.TemporaryDirectory() as temp:
    window.start_recording_at(Path(temp))
    wait_for(lambda: window.recording)
    writer = window.session.writers[-1]
    open_loop(window)
    print("GUI: loopback opened", flush=True)
    window.send_edit.setPlainText("Hello 串口\n")
    QTest.mouseClick(window.send_button, Qt.LeftButton)
    expected = "Hello 串口\n".encode()
    wait_for(lambda: window.session.counters()[0] == len(expected))
    window.hex_send.setChecked(True)
    window.send_edit.setPlainText("00 01 FE FF")
    window.ending.setCurrentIndex(1)
    QTest.mouseClick(window.send_button, Qt.LeftButton)
    expected += b"\x00\x01\xfe\xff\r\n"
    wait_for(lambda: window.session.counters()[0] == len(expected))
    wait_for(lambda: window.history_bytes == len(expected) * 2)
    window.hex_view.setChecked(True)
    wait_for(lambda: "FE FF" in window.receive.toPlainText())
    window.timestamps.setChecked(False)
    wait_for(lambda: window.receive.toPlainText().startswith(("TX\n", "RX\n")))
    window.timestamps.setChecked(True)
    window.pause.setChecked(True)
    window.clear_history()
    window.session.send(b"while-paused")
    expected += b"while-paused"
    wait_for(lambda: window.session.counters()[0] == len(expected))
    wait_for(lambda: bool(window.history))
    assert not window.receive.toPlainText()
    window.pause.setChecked(False)
    wait_for(lambda: bool(window.receive.toPlainText()))
    raw_path = Path(temp) / "cache.bin"
    window.export_to(raw_path, True)
    wait_for(lambda: not window.exporting)
    assert raw_path.read_bytes() == b"while-paused"
    window.toggle_recording()
    wait_for(lambda: writer.finished.is_set() and not window.log_pending)
    assert not writer.failure, writer.failure
    assert (writer.directory / "rx.bin").read_bytes() == expected
    assert (writer.directory / "tx.bin").read_bytes() == expected
    QTest.mouseClick(window.open_button, Qt.LeftButton)
    wait_for(lambda: not window.connected and not window.session.is_alive())
    window.baud.setCurrentText("9600")
    QTest.mouseClick(window.open_button, Qt.LeftButton)
    wait_for(lambda: window.connected)
    assert "9600 baud" in window.status.text()
    window.open_window()
    child = next(w for w in MonitorWindow.windows if w is not window)
    open_loop(child)
    child.session.send(b"second window")
    wait_for(lambda: child.session.counters() == (13, 13))
    close_window(child)
    results["gui_roundtrip_record_export_reopen_multiwindow"] = "passed"
    print("GUI: functional checks passed", flush=True)
    close_window(window)

# Deliberately faster than common UART links: UI must remain responsive and bounded.
class StreamPort:
    block = bytes(range(256)) * 16
    def __init__(self):
        self.remaining = 2048  # 8 MiB
    @property
    def in_waiting(self):
        return 4096 if self.remaining else 0
    def open(self): pass
    def read(self, size):
        time.sleep(0.001)
        if self.remaining:
            self.remaining -= 1
            return self.block
        return b""
    def close(self): pass

stress = MonitorWindow()
stress.show()
print("GUI: stress start", flush=True)
stress.hex_view.setChecked(True)
stress.session.factory = lambda *a, **k: StreamPort()
heartbeats = []
heartbeat = QTimer()
heartbeat.timeout.connect(lambda: heartbeats.append(time.monotonic()))
heartbeat.start(10)
with tempfile.TemporaryDirectory() as temp:
    stress.start_recording_at(Path(temp))
    wait_for(lambda: stress.recording)
    writer = stress.session.writers[-1]
    stress.session.open(dict(port="COM_SIMULATED", baudrate=2000000))
    wait_for(lambda: stress.session.counters()[0] >= 2 * 1024 * 1024, 20)
    stress.pause.setChecked(True)
    stress.clear_history()
    QTest.qWait(100)
    stress.pause.setChecked(False)
    wait_for(lambda: stress.session.counters()[0] == 8 * 1024 * 1024, 30)
    stress.session.close()
    wait_for(lambda: not stress.session.is_alive())
    stress.toggle_recording()
    wait_for(lambda: writer.finished.is_set() and not stress.log_pending, 15)
    assert not writer.failure, writer.failure
    assert (writer.directory / "rx.bin").read_bytes() == StreamPort.block * 2048
    assert (writer.directory / "tx.bin").stat().st_size == 0
    assert stress.history_bytes <= 1024 * 1024
    assert stress.session.display.size <= 1024 * 1024
    assert stress.render_queue.size <= 1024 * 1024
    assert stress.receive.document().blockCount() <= 4000
    wait_for(lambda: stress.render_queue.size == 0, 15)
    stress.receive.moveCursor(stress.receive.textCursor().End)
    app.processEvents()
    stress.grab().save(str(output / "gui_hex.png"))
    gaps = [b - a for a, b in zip(heartbeats, heartbeats[1:])]
    assert len(heartbeats) > 20
    assert max(gaps) < 1.0, f"UI stalled {max(gaps):.3f}s"
    results["simulated_stream_bytes"] = stress.session.counters()[0]
    results["raw_recording_matches"] = True
    results["max_gui_heartbeat_gap_ms"] = round(max(gaps) * 1000, 1)
    results["bounded_memory_and_document"] = True
    close_window(stress)
heartbeat.stop()

# Closing during an in-flight read must retain that last received block in the file.
read_started = threading.Event()
class FinalReadPort:
    in_waiting = 1
    def open(self): pass
    def read(self, size):
        read_started.set()
        time.sleep(0.1)
        return b"final-read"
    def close(self): pass

shutdown = MonitorWindow()
shutdown.session.factory = lambda *a, **k: FinalReadPort()
with tempfile.TemporaryDirectory() as temp:
    shutdown.start_recording_at(Path(temp))
    wait_for(lambda: shutdown.recording)
    writer = shutdown.session.writers[-1]
    shutdown.session.open(dict(port="COM_SIMULATED"))
    wait_for(read_started.is_set)
    close_window(shutdown)
    assert (writer.directory / "rx.bin").read_bytes() == b"final-read"
    results["close_retains_final_inflight_read"] = True

# A clean first-open screenshot without stress-test status.
preview = MonitorWindow()
preview.show()
open_loop(preview)
preview.send_edit.setPlainText("Hello 串口监视器！")
preview.session.send("Hello 串口监视器！\r\n".encode())
wait_for(lambda: preview.history_bytes > 0)
wait_for(lambda: "Hello" in preview.receive.toPlainText())
preview.grab().save(str(output / "gui_text.png"))
close_window(preview)
(output / "smoke_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(results, ensure_ascii=False, indent=2))
faulthandler.cancel_dump_traceback_later()
