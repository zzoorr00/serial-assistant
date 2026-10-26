import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serial_core import validate_baud
from device_identity import IDENTITY_PREFIX, IDENTITY_QUERY, parse_identity
from device_scan import ScanResult, probe_identity, scan_ports
from serial_core import SerialSession


def until(predicate, timeout=4):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate(): return
        time.sleep(0.003)
    raise AssertionError("Timed out")


def response(**updates):
    value = dict(protocol="freebci.identity", version=1, chip="C5", role="eeg_acquisition",
                 uid="AABBCCDDEEFF", address="", supported_channels=[8, 16], active_channels=16,
                 firmware="test", transport="usb_serial_jtag")
    value.update(updates)
    return IDENTITY_PREFIX + json.dumps(value).encode("ascii") + b"\n"


class LiveBaudTests(unittest.TestCase):
    def session(self, **options):
        session = SerialSession(**options)
        def stop():
            session.close()
            until(lambda: not session.is_alive())
            session.stop_recording()
            until(lambda: not session.busy())
        self.addCleanup(stop)
        return session

    def test_incoming_rate_announcements_never_change_host_baud(self):
        reads = []
        rate_changes = []
        class Port:
            in_waiting = 80
            _baud = 115200
            @property
            def baudrate(self): return self._baud
            @baudrate.setter
            def baudrate(self, rate):
                rate_changes.append(rate)
                self._baud = rate
            def open(self): pass
            def read(self, n):
                time.sleep(0.01)
                reads.append(True)
                return b"UART baud switched to 2000000\r\n\xff\x80"
            def close(self): pass
        session = self.session(factory=lambda *a, **k: Port())
        session.open(dict(port="COM_TEST", baudrate=115200))
        until(lambda: len(reads) >= 160)
        self.assertEqual(session.current_baud, 115200)
        self.assertEqual(rate_changes, [])
        session.set_baudrate(921600)
        until(lambda: session.current_baud == 921600)
        self.assertEqual(rate_changes, [921600])

    def test_manual_changes_same_handle_and_reconnect_remembers_rate(self):
        opened = []
        writes = []
        disconnect = threading.Event()
        class Port:
            in_waiting = 0
            def __init__(self, baud): self.baudrate = baud; self.closed = False
            def open(self): opened.append(self)
            def read(self, n):
                time.sleep(0.002)
                if disconnect.is_set():
                    disconnect.clear()
                    raise OSError("unplug")
                return b""
            def write(self, data): writes.append((self.baudrate, data)); return len(data)
            def close(self): self.closed = True
        session = self.session(factory=lambda *a, **k: Port(k["baudrate"]), reconnect_interval=0.01)
        session.open(dict(port="COM_TEST", baudrate=115200))
        until(session.connected_event.is_set)
        session.set_baudrate(921600)
        until(lambda: session.current_baud == 921600)
        self.assertEqual(len(opened), 1)
        self.assertFalse(opened[0].closed)
        session.send(b"new-rate")
        until(lambda: bool(writes))
        self.assertEqual(writes, [(921600, b"new-rate")])
        disconnect.set()
        until(lambda: len(opened) == 2)
        self.assertEqual(opened[1].baudrate, 921600)

    def test_driver_rejection_rolls_back_without_closing(self):
        class Port:
            in_waiting = 0
            closed = False
            def __init__(self): self._baud = 115200
            @property
            def baudrate(self): return self._baud
            @baudrate.setter
            def baudrate(self, rate):
                if rate == 999999: raise ValueError("unsupported")
                self._baud = rate
            def open(self): pass
            def read(self, n): time.sleep(0.002); return b""
            def close(self): self.closed = True
        port = Port()
        session = self.session(factory=lambda *a, **k: port)
        session.open(dict(port="COM_TEST", baudrate=115200))
        until(session.connected_event.is_set)
        session.set_baudrate(999999)
        events = []
        def rejected():
            while True:
                try: events.append(session.events.get_nowait())
                except queue.Empty: break
            return any(kind == "baud_error" for kind, detail in events)
        until(rejected)
        self.assertEqual((session.current_baud, port.baudrate), (115200, 115200))
        self.assertTrue(session.connected_event.is_set())
        self.assertFalse(port.closed)



class IdentityTests(unittest.TestCase):
    def test_protocol_and_role_names(self):
        identity = parse_identity(b"boot\r\n" + response())
        self.assertIn("Acquisition", identity.display_name)
        self.assertEqual(identity.uid, "AABBCCDDEEFF")
        receiver = parse_identity(response(role="ble_receiver", supported_channels=[], active_channels=0,
                                          address="AA:BB:CC:DD:12:34", receiver_target="S3"))
        self.assertIn("S3-1234", receiver.display_name)
        self.assertIn("ESP32-C5", receiver.details)

    def test_rejects_partial_bad_identity_and_accepts_later_valid(self):
        for data in (response()[:-1], response(version=2), response(uid="bad"), response(active_channels=True),
                     response(supported_channels=[8]), response(firmware="bad\n")):
            self.assertIsNone(parse_identity(data))
        self.assertIsNotNone(parse_identity(response(uid="bad") + response()))

    def test_probe_readonly_query_split_response_and_baud_trials(self):
        writes = []
        class Port:
            baudrate = 0
            def __init__(self): self.parts = []; self.closed = False
            @property
            def in_waiting(self): return len(self.parts[0]) if self.parts else 0
            def open(self):
                assert self.dtr is False and self.rts is False
            def read(self, n):
                time.sleep(0.002)
                return self.parts.pop(0) if self.parts else b""
            def write(self, data):
                writes.append((self.baudrate, data))
                if self.baudrate == 921600: self.parts.extend([response()[:30], response()[30:]])
            def close(self): self.closed = True
        port = Port()
        result = probe_identity("COM_TEST", lambda: False, factory=lambda **k: port, dwell=0.22)
        self.assertEqual(result.baudrate, 921600)
        self.assertIsNotNone(result.identity)
        self.assertTrue(port.closed)
        self.assertTrue(all(data == IDENTITY_QUERY for rate, data in writes))

    def test_probe_binary_stream_and_cancellation_send_nothing(self):
        for cancel in (False, True):
            class Port:
                in_waiting = 33
                writes = []
                closed = False
                def open(self): pass
                def read(self, n): return b"\xa0" + b"\x00" * 32
                def write(self, data): self.writes.append(data)
                def close(self): self.closed = True
            port = Port()
            result = probe_identity("COM_TEST", lambda: cancel, factory=lambda **k: port, dwell=0.2)
            self.assertIsNone(result.identity)
            self.assertEqual(port.writes, [])
            self.assertTrue(port.closed)

    def test_scan_skips_excluded_busy_unknown_and_sets_transport(self):
        infos = [SimpleNamespace(device=f"COM{i}", description="test", vid=0x1A86, pid=0x55D3) for i in range(1, 5)]
        infos[2].vid = 123
        probed, notices = [], []
        def probe(name, cancelled):
            probed.append(name)
            return ScanResult(name, parse_identity(response()), False, 115200)
        scan_ports(lambda *a: notices.append(a), lambda: False, identify=True, excluded={"COM1"},
                   port_lister=lambda: infos, occupied_check=lambda name: name == "COM2", probe=probe)
        self.assertEqual(probed, ["COM4"])
        self.assertEqual(notices[-1][1].identity.transport, "usb_uart_ch343")
        probed.clear()
        scan_ports(lambda *a: None, lambda: False, identify=False, port_lister=lambda: infos, probe=probe)
        self.assertEqual(probed, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
