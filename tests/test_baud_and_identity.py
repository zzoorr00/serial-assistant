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
from auto_baud import BaudTrial, StreamEvidence, parse_rates, validate_baud
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


class BaudEvidenceTests(unittest.TestCase):
    def test_rate_input_bounds_duplicates_and_delimiters(self):
        self.assertEqual(parse_rates("115200，921600 115200;2000000"), (115200, 921600, 2000000))
        for value in ("", "abc", "0", "12000001", "115200.0", ",".join(map(str, range(1, 10)))):
            with self.subTest(value=value), self.assertRaises(ValueError): parse_rates(value)
        self.assertEqual(validate_baud("921600"), 921600)

    def test_text_evidence_fragmentation_and_old_data_not_reused(self):
        evidence = StreamEvidence()
        self.assertFalse(evidence.feed(b"wai"))
        self.assertTrue(evidence.feed(b"ting...\r\n"))
        self.assertFalse(evidence.feed(b"\xff\x00"))
        self.assertTrue(evidence.feed(b"\nFirmware: test\r\n"))
        self.assertFalse(StreamEvidence().feed(b"just printable unrelated data\n"))

    def test_binary_requires_three_valid_sequential_frames(self):
        for length in (33, 57):
            frames = b"".join(bytes([0xA0, seq]) + b"\x00" * (length - 3) + b"\xc0" for seq in (254, 255, 0))
            evidence = StreamEvidence()
            self.assertFalse(evidence.feed(frames[:length * 2]))
            self.assertTrue(evidence.feed(frames[length * 2:]))
            self.assertFalse(evidence.feed(b"garbage"))
            bad = bytearray(frames)
            bad[length + 1] = 12
            self.assertFalse(StreamEvidence().feed(bytes(bad)))

    def test_lock_idle_retrial_and_window_reset(self):
        trial = BaudTrial(115200, (921600, 2000000), 0, dwell=1, idle=3)
        self.assertTrue(trial.observe(b"waiting...\r\n", 0.1))
        self.assertIsNone(trial.tick(2))
        self.assertIsNone(trial.tick(3.2))
        self.assertIsNone(trial.tick(10.2))
        trial.observe(b"invalid payload data", 10.3)
        self.assertEqual(trial.tick(10.4), ("search", 115200))
        self.assertFalse(trial.locked)
        self.assertEqual(trial.tick(11.5), ("rate", 921600))
        self.assertFalse(trial.observe(b"...\n", 11.6))
        quiet = BaudTrial(115200, (921600,), 0)
        quiet.observe(b"waiting...\r\n", 0.1)
        self.assertEqual(quiet.tick(15.2), ("search", 115200))


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

    def test_passive_auto_follows_two_changes_and_logs_trials(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        target = [115200]
        writes = []
        opened = []
        class Port:
            in_waiting = 12
            def open(self): opened.append(self)
            def read(self, n):
                time.sleep(0.003)
                return b"waiting...\r\n" if self.baudrate == target[0] else b"\xff\x00\x80bad\n"
            def write(self, data): writes.append(data); return len(data)
            def close(self): pass
        port = Port()
        def factory(*args, **config):
            port.baudrate = config["baudrate"]
            return port
        session = self.session(factory=factory, auto_dwell=0.02, auto_idle=0.04)
        session.configure_auto(True)
        writer = session.start_recording(Path(temp) / "auto")
        until(writer.ready.is_set)
        session.open(dict(port="COM_TEST", baudrate=115200))
        until(lambda: session.connected_event.is_set() and not session.scanning_event.is_set())
        for rate in (921600, 2000000):
            target[0] = rate
            until(lambda: session.current_baud == rate and not session.scanning_event.is_set())
        self.assertEqual(len(opened), 1)
        self.assertEqual(writes, [])
        session.close()
        until(lambda: not session.is_alive())
        session.stop_recording()
        until(writer.finished.is_set)
        self.assertFalse(writer.failure)
        text = (writer.directory / "session.log").read_text(encoding="utf-8")
        self.assertIn("AUTO MATCH 921600", text)
        self.assertIn("AUTO MATCH 2000000", text)
        self.assertEqual((writer.directory / "tx.bin").read_bytes(), b"")

    def test_disable_restores_confirmed_and_manual_overrides_search(self):
        class Port:
            in_waiting = 0
            baudrate = 115200
            def open(self): pass
            def read(self, n): time.sleep(0.003); return b"\xff"
            def close(self): pass
        port = Port()
        session = self.session(factory=lambda *a, **k: port, auto_dwell=0.015)
        session.configure_auto(True)
        session.open(dict(port="COM_TEST", baudrate=115200))
        until(lambda: session.current_baud == 921600)
        with self.assertRaises(RuntimeError): session.send(b"do not send")
        session.configure_auto(False)
        until(lambda: not session.scanning_event.is_set())
        self.assertEqual(session.current_baud, 115200)
        session.configure_auto(True)
        until(lambda: session.current_baud == 921600)
        session.set_baudrate(2000000)
        until(lambda: session.current_baud == 2000000 and not session.scanning_event.is_set())
        time.sleep(0.05)
        self.assertEqual(session.current_baud, 2000000)


class IdentityTests(unittest.TestCase):
    def test_protocol_and_role_names(self):
        identity = parse_identity(b"boot\r\n" + response())
        self.assertIn("采集板", identity.display_name)
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
