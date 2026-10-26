"""GUI integration of online baud changes, passive matching and identity scanning."""
import faulthandler
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
faulthandler.dump_traceback_later(25, repeat=True)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication
from main import MonitorWindow
from device_identity import IDENTITY_PREFIX, parse_identity
from device_scan import ScanResult

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
app.setStyle("Fusion")
output = Path(__file__).resolve().parents[1] / "test-output"
output.mkdir(exist_ok=True)
results = {}


def wait_for(predicate, timeout=6):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate(): return
        QTest.qWait(5)
    raise AssertionError("GUI feature wait timed out")


value = dict(protocol="freebci.identity", version=1, chip="C5", role="eeg_acquisition",
             uid="AABBCCDDEEFF", address="", supported_channels=[8, 16], active_channels=16,
             firmware="demo-identity1", transport="usb_uart_ch343")
identity = parse_identity(IDENTITY_PREFIX + json.dumps(value).encode("ascii") + b"\n")
hold_scan = [False]
scan_started = threading.Event()
scan_calls = []


def scanner(notify, cancelled, identify=False, excluded=()):
    scan_calls.append((identify, excluded))
    notify("ports", [("COM51", "模拟采集板"), ("COM52", "模拟占用端口")])
    if identify:
        scan_started.set()
        while hold_scan[0] and not cancelled(): time.sleep(0.003)
        if cancelled(): return
        notify("identity", ScanResult("COM51", occupied=True, message="当前窗口已连接") if "COM51" in excluded
               else ScanResult("COM51", identity, False, 115200))
        notify("identity", ScanResult("COM52", occupied=True, message="端口已占用"))


target = [115200]
opened, writes = [], []
class Port:
    in_waiting = 12
    def __init__(self, baud): self.baudrate = baud
    def open(self): opened.append(self)
    def read(self, size):
        time.sleep(0.008)
        return b"waiting...\r\n" if self.baudrate == target[0] else b"\xff\x80\x00bad\n"
    def write(self, data): writes.append((self.baudrate, data)); return len(data)
    def close(self): pass


with patch("main.scan_ports", scanner), tempfile.TemporaryDirectory() as temp:
    window = MonitorWindow()
    window.show()
    wait_for(lambda: not window.port_scan)
    assert scan_calls == [(False, set())], scan_calls
    QTest.mouseClick(window.refresh, Qt.LeftButton)
    wait_for(lambda: not window.port_scan)
    assert window.selected_port() == "COM51"
    assert "C5" in window.port.currentText()
    assert "AABBCCDDEEFF" in window.identity_label.text()
    assert "占用" in window.port.itemText(window.port.findData("COM52"))
    results["identity_rows_keep_com_and_show_name_id_occupied"] = True

    window.session.factory = lambda *a, **k: Port(k["baudrate"])
    window.session.auto_dwell = 0.1
    window.session.auto_idle = 0.07
    window.start_recording_at(Path(temp))
    wait_for(lambda: window.recording)
    writer = window.session.writers[-1]
    QTest.mouseClick(window.open_button, Qt.LeftButton)
    wait_for(lambda: window.connected)
    assert window.baud.isEnabled() and window.apply_baud.isEnabled()
    assert not window.bits.isEnabled()
    target[0] = 921600
    window.baud.setCurrentText("921600")
    QTest.mouseClick(window.apply_baud, Qt.LeftButton)
    wait_for(lambda: window.session.current_baud == 921600 and not window.baud_pending)
    assert len(opened) == 1
    assert "921600 baud" in window.status.text()
    results["manual_baud_applies_without_reopening_handle"] = True

    window.auto_baud.setChecked(True)
    wait_for(lambda: "已匹配 921600" in window.auto_label.text())
    target[0] = 2000000
    wait_for(lambda: window.session.scanning_event.is_set())
    wait_for(lambda: not window.send_button.isEnabled())
    wait_for(lambda: "已匹配 2000000" in window.auto_label.text())
    wait_for(lambda: window.send_button.isEnabled())
    assert writes == [], writes
    assert len(opened) == 1
    window.clear_history()
    window.hex_view.setChecked(True)
    window.hex_columns.setValue(20)
    wait_for(lambda: bool(window.receive.toPlainText()))
    window.log_label.setText("记录中：模拟会话（示例数据）")
    window.grab().save(str(output / "gui_features.png"))
    results["passive_auto_follows_change_and_gates_send"] = True

    window.send_edit.setPlainText("v")
    QTest.mouseClick(window.send_button, Qt.LeftButton)
    wait_for(lambda: bool(writes))
    assert writes == [(2000000, b"v")]
    target[0] = 115200
    window.baud.setCurrentText("115200")
    QTest.mouseClick(window.apply_baud, Qt.LeftButton)
    wait_for(lambda: window.session.current_baud == 115200 and not window.baud_pending)
    assert not window.auto_baud.isChecked()
    assert len(opened) == 1
    results["manual_apply_overrides_auto"] = True

    window.baud.setCurrentText("bad")
    QTest.mouseClick(window.apply_baud, Qt.LeftButton)
    assert window.connected and window.session.current_baud == 115200
    window.baud.setCurrentText("115200")
    QTest.mouseClick(window.apply_baud, Qt.LeftButton)
    wait_for(lambda: not window.baud_pending)

    scan_started.clear()
    hold_scan[0] = True
    QTest.mouseClick(window.refresh, Qt.LeftButton)
    wait_for(scan_started.is_set)
    assert "COM51" in scan_calls[-1][1]
    QTest.mouseClick(window.refresh, Qt.LeftButton)
    wait_for(lambda: not window.port_scan)
    hold_scan[0] = False
    results["connected_scan_excludes_active_port_and_cancels"] = True

    child = MonitorWindow()
    wait_for(lambda: not child.port_scan)
    assert not child.auto_baud.isChecked() and child.baud.currentText() == "115200"
    child.close()
    wait_for(lambda: child not in MonitorWindow.windows)
    results["new_window_settings_independent"] = True

    scan_started.clear()
    hold_scan[0] = True
    QTest.mouseClick(window.refresh, Qt.LeftButton)
    wait_for(scan_started.is_set)
    window.close()
    wait_for(lambda: window not in MonitorWindow.windows)
    assert writer.finished.is_set() and not writer.failure
    log = (writer.directory / "session.log").read_text(encoding="utf-8")
    assert "AUTO MATCH 2000000" in log and "BAUD" in log
    assert (writer.directory / "tx.bin").read_bytes() == b"v"
    results["close_cancels_scan_and_finishes_recording"] = True

(output / "feature_results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
print(json.dumps(results, indent=2))
faulthandler.cancel_dump_traceback_later()
