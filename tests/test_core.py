import pathlib
import queue
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import serial
from serial_core import DisplayBuffer, Record, Recorder, SerialSession, encode_send, export_records


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Timed out")


class CoreTests(unittest.TestCase):
    def test_encoding_and_hex_validation(self):
        self.assertEqual(encode_send("中文", False, "UTF-8", "\r\n"), "中文\r\n".encode())
        self.assertEqual(encode_send("01 ff 00\nA2", True, "UTF-8", ""), b"\x01\xff\x00\xa2")
        self.assertEqual(encode_send("你好", False, "GB18030", ""), "你好".encode("gb18030"))
        for text in ("0", "0x01", "GG", "01,02", ""):
            with self.assertRaises(ValueError):
                encode_send(text, True, "UTF-8", "")
        with self.assertRaises(UnicodeEncodeError):
            encode_send("中文", False, "ASCII", "")
        with self.assertRaises(ValueError):
            encode_send("A" * 65537, False, "ASCII", "")

    def test_bounded_display_reports_loss(self):
        buffer = DisplayBuffer(max_bytes=10, max_records=3)
        for i in range(10):
            buffer.put(Record(i, "RX", bytes([i]) * 4))
        items, dropped = buffer.drain()
        self.assertEqual(dropped, 32)
        self.assertEqual([r.data for r in items], [b"\x08" * 4, b"\x09" * 4])
        self.assertEqual(buffer.size, 0)

    def test_loopback_binary_recording_and_reopen(self):
        session = SerialSession()
        with tempfile.TemporaryDirectory() as temp:
            writer = session.start_recording(pathlib.Path(temp) / "capture")
            until(writer.ready.is_set)
            session.open(dict(port="loop://", baudrate=2000000))
            self.addCleanup(self.stop, session)
            until(lambda: any(e[0] == "opened" for e in self.drain_events(session)))
            payload = bytes(range(256)) * 128
            session.send(payload)
            until(lambda: session.counters() == (len(payload), len(payload)), 10)
            session.close()
            until(lambda: not session.is_alive())
            session.stop_recording()
            until(writer.finished.is_set)
            self.assertEqual(writer.failure, "")
            folder = pathlib.Path(temp) / "capture"
            self.assertEqual((folder / "rx.bin").read_bytes(), payload)
            self.assertEqual((folder / "tx.bin").read_bytes(), payload)
            text = (folder / "session.log").read_text(encoding="utf-8")
            self.assertIn("RX (", text)
            self.assertIn("TX (", text)
            self.assertTrue(text.endswith("# STOP: recording closed normally\n"))
            session.open(dict(port="loop://", baudrate=115200))
            until(lambda: any(e[0] == "opened" for e in self.drain_events(session)))
            session.send(b"again")
            until(lambda: session.counters()[0] == len(payload) + 5)

    def test_two_independent_sessions(self):
        sessions = [SerialSession(), SerialSession()]
        for session in sessions:
            self.addCleanup(self.stop, session)
            session.open(dict(port="loop://", baudrate=115200))
            until(lambda: any(e[0] == "opened" for e in self.drain_events(session)))
        sessions[0].send(b"first")
        sessions[1].send(b"second")
        until(lambda: sessions[0].counters() == (5, 5) and sessions[1].counters() == (6, 6))

    def test_open_failure_and_unplug(self):
        def bad_factory(*args, **kwargs):
            raise serial.SerialException("device busy")
        session = SerialSession(factory=bad_factory)
        session.open(dict(port="COM_TEST"))
        until(lambda: not session.is_alive())
        events = self.drain_events(session)
        self.assertEqual([kind for kind, detail in events], ["error", "closed"])

        class Unplugged:
            in_waiting = 0
            def open(self): pass
            def read(self, size): raise serial.SerialException("unplugged")
            def close(self): self.closed = True
        port = Unplugged()
        session = SerialSession(factory=lambda *a, **k: port, reconnect_interval=5)
        self.addCleanup(self.stop, session)
        session.open(dict(port="COM_TEST"))
        events = []
        until(lambda: (events.extend(self.drain_events(session)) or any(e[0] == "reconnecting" for e in events)))
        self.assertTrue(session.is_alive())
        self.assertFalse(session.connected_event.is_set())
        self.assertEqual([e[0] for e in events], ["opened", "reconnecting"])
        started = time.monotonic()
        session.close()
        until(lambda: not session.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual([e[0] for e in self.drain_events(session)], ["closed"])
        self.assertTrue(port.closed)

    def test_board_restart_reconnect_keeps_recording_and_discards_old_send(self):
        read_pending = threading.Event()
        unplug = threading.Event()
        calls = []
        writes = []

        class FirstPort:
            in_waiting = 1
            def __init__(self): self.first = True
            def open(self): pass
            def read(self, size):
                if self.first:
                    self.first = False
                    return b"before"
                read_pending.set()
                unplug.wait(3)
                raise serial.SerialException("board restarting")
            def close(self): pass

        class RecoveredPort:
            in_waiting = 1
            def __init__(self): self.first = True
            def open(self): pass
            def read(self, size):
                if self.first:
                    self.first = False
                    return b"after"
                time.sleep(0.002)
                return b""
            def write(self, data): writes.append(data); return len(data)
            def close(self): pass

        def factory(name, **config):
            calls.append((name, config))
            if len(calls) == 1:
                return FirstPort()
            if len(calls) < 4:
                raise serial.SerialException("COM_TEST temporarily absent")
            return RecoveredPort()

        session = SerialSession(factory=factory, reconnect_interval=0.02)
        self.addCleanup(self.stop, session)
        with tempfile.TemporaryDirectory() as temp:
            writer = session.start_recording(pathlib.Path(temp) / "restart")
            until(writer.ready.is_set)
            session.open(dict(port="COM_TEST", baudrate=230400, bytesize=8, parity="N", stopbits=1))
            until(read_pending.is_set)
            session.send(b"stale command")
            unplug.set()
            until(lambda: session.counters()[0] == 11)
            events = self.drain_events(session)
            self.assertEqual([e[0] for e in events if not e[0].startswith("log_")], ["opened", "reconnecting", "opened"])
            self.assertTrue(session.connected_event.is_set())
            self.assertEqual(writes, [])
            session.send(b"new")
            until(lambda: session.counters()[1] == 3)
            self.assertEqual(writes, [b"new"])
            self.assertEqual(len(calls), 4)
            self.assertTrue(all(name == "COM_TEST" and config["baudrate"] == 230400 for name, config in calls))
            session.close()
            until(lambda: not session.is_alive())
            count = len(calls)
            time.sleep(0.05)
            self.assertEqual(len(calls), count)
            session.stop_recording()
            until(writer.finished.is_set)
            self.assertFalse(writer.failure)
            self.assertEqual((writer.directory / "rx.bin").read_bytes(), b"beforeafter")
            self.assertEqual((writer.directory / "tx.bin").read_bytes(), b"new")

    def test_no_data_keeps_original_handle_open(self):
        calls = []
        class IdlePort:
            in_waiting = 0
            def open(self): pass
            def read(self, size): time.sleep(0.002); return b""
            def close(self): pass
        def factory(*a, **k): calls.append(1); return IdlePort()
        session = SerialSession(factory=factory, reconnect_interval=0.01)
        self.addCleanup(self.stop, session)
        session.open(dict(port="COM_TEST"))
        until(session.connected_event.is_set)
        time.sleep(0.05)
        self.assertEqual(len(calls), 1)
        self.assertTrue(session.connected_event.is_set())

    def test_write_timeout_stops_connection(self):
        class TimedOut:
            in_waiting = 0
            def open(self): pass
            def read(self, size): time.sleep(0.001); return b""
            def write(self, data): raise serial.SerialTimeoutException("write timed out")
            def close(self): pass
        session = SerialSession(factory=lambda *a, **k: TimedOut())
        session.open(dict(port="COM_TEST"))
        until(lambda: any(e[0] == "opened" for e in self.drain_events(session)))
        session.send(b"x")
        until(lambda: not session.is_alive())
        events = self.drain_events(session)
        self.assertEqual([e[0] for e in events], ["error", "closed"])
        self.assertEqual(session.counters(), (0, 0))

    def test_full_record_queue_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            notices = []
            writer = Recorder(pathlib.Path(temp) / "overload", lambda *a: notices.append(a), max_bytes=3)
            writer.submit(Record(time.time(), "RX", b"1234"))
            writer.start()
            until(writer.finished.is_set)
            self.assertTrue(writer.failure)
            self.assertEqual(notices[-1][0], "log_error")
            self.assertIn("INCOMPLETE", (writer.directory / "session.log").read_text(encoding="utf-8"))

    def test_record_open_error_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp) / "existing"
            directory.mkdir()
            marker = directory / "rx.bin"
            marker.write_bytes(b"keep")
            writer = Recorder(directory, lambda *a: None)
            writer.start()
            until(writer.finished.is_set)
            self.assertTrue(writer.failure)
            self.assertEqual(marker.read_bytes(), b"keep")

    def test_cache_export_raw_and_log(self):
        records = [Record(1720000000, "RX", b"\x00\xff"), Record(1720000001, "TX", b"hello")]
        with tempfile.TemporaryDirectory() as temp:
            raw, log = pathlib.Path(temp) / "rx.bin", pathlib.Path(temp) / "out.log"
            export_records(raw, records, True)
            export_records(log, records)
            self.assertEqual(raw.read_bytes(), b"\x00\xff")
            self.assertIn("RX (2 B) 00 FF", log.read_text(encoding="utf-8"))
            self.assertIn("TX (5 B) 68 65 6C 6C 6F", log.read_text(encoding="utf-8"))

    @staticmethod
    def drain_events(session):
        result = []
        while True:
            try:
                result.append(session.events.get_nowait())
            except queue.Empty:
                return result

    @staticmethod
    def stop(session):
        session.close()
        session.stop_recording()
        until(lambda: not session.busy())


if __name__ == "__main__":
    unittest.main(verbosity=2)
