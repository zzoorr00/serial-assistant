"""Bounded serial transport and lossless-to-disk recording; no Qt dependency."""
from __future__ import annotations

from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import queue
import threading
import time

import serial
from serial.tools import list_ports
from ui_text import error_text


def validate_baud(value):
    try:
        rate = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Baud rate must be an integer from 1 to 12000000") from None
    if str(value).strip() != str(rate) or not 1 <= rate <= 12000000:
        raise ValueError("Baud rate must be an integer from 1 to 12000000")
    return rate


@dataclass(frozen=True)
class Record:
    timestamp: float
    direction: str
    data: bytes

    def log_line(self) -> str:
        stamp = datetime.fromtimestamp(self.timestamp).isoformat(sep=" ", timespec="milliseconds")
        return f"[{stamp}] {self.direction} ({len(self.data)} B) {self.data.hex(' ').upper()}\n"


@dataclass(frozen=True)
class LogNote:
    timestamp: float
    message: str
    data: bytes = b""

    def log_line(self):
        stamp = datetime.fromtimestamp(self.timestamp).isoformat(sep=" ", timespec="milliseconds")
        return f"# [{stamp}] {self.message}\n"


def encode_send(text: str, hex_mode: bool, encoding: str, ending: str) -> bytes:
    if hex_mode:
        # bytes.fromhex strictly rejects odd digits and non-hex characters.
        data = bytes.fromhex(text)
    else:
        data = text.encode(encoding, errors="strict")
    data += ending.encode("ascii")
    if not data:
        raise ValueError("Enter data to send")
    if len(data) > 65536:
        raise ValueError("Send at most 64 KiB per request; split larger data")
    return data


class DisplayBuffer:
    """Never let a slow or paused GUI block serial IO or grow memory forever."""

    def __init__(self, max_bytes=1024 * 1024, max_records=8192):
        self.max_bytes = max_bytes
        self.max_records = max_records
        self.items = deque()
        self.size = 0
        self.dropped = 0
        self.lock = threading.Lock()

    def put(self, record):
        with self.lock:
            self.items.append(record)
            self.size += len(record.data)
            while self.size > self.max_bytes or len(self.items) > self.max_records:
                self.dropped += len(self.items[0].data)
                self.size -= len(self.items.popleft().data)

    def drain(self, byte_limit=16384, record_limit=128):
        result = []
        with self.lock:
            while self.items and len(result) < record_limit and byte_limit > 0:
                item = self.items.popleft()
                self.size -= len(item.data)
                byte_limit -= len(item.data)
                result.append(item)
            dropped, self.dropped = self.dropped, 0
        return result, dropped

    def clear(self):
        with self.lock:
            self.items.clear()
            self.size = self.dropped = 0


class Recorder(threading.Thread):
    """Writer with bounded backlog; overload is reported, never silently lost."""

    def __init__(self, directory: Path, notify, max_bytes=32 * 1024 * 1024):
        super().__init__(name="serial-log", daemon=True)
        self.directory = Path(directory)
        self.notify = notify
        self.max_bytes = max_bytes
        self.items = deque()
        self.size = 0
        self.condition = threading.Condition()
        self.stopping = False
        self.failure = ""
        self.ready = threading.Event()
        self.finished = threading.Event()

    def submit(self, record):
        with self.condition:
            if self.stopping:
                return
            if self.size + len(record.data) > self.max_bytes or len(self.items) >= 65536:
                self.failure = "Recording queue is full; recording is incomplete. Check disk speed and free space"
                self.stopping = True
            else:
                self.items.append(record)
                self.size += len(record.data)
            self.condition.notify()

    def stop(self):
        with self.condition:
            self.stopping = True
            self.condition.notify()

    def run(self):
        try:
            # Exclusive creation prevents accidental overwrite by another instance.
            self.directory.mkdir(parents=True, exist_ok=False)
            with ExitStack() as stack:
                log = stack.enter_context((self.directory / "session.log").open("x", encoding="utf-8"))
                rx = stack.enter_context((self.directory / "rx.bin").open("xb"))
                tx = stack.enter_context((self.directory / "tx.bin").open("xb"))
                log.write("# SerialMonitor v1.6 debug | HEX / local receive timestamps\n")
                log.flush()
                self.ready.set()
                self.notify("log_started", str(self.directory))
                last_flush = time.monotonic()
                while True:
                    with self.condition:
                        if not self.items and not self.stopping:
                            self.condition.wait(0.2)
                        if self.items:
                            record = self.items.popleft()
                            self.size -= len(record.data)
                        elif self.stopping:
                            break
                        else:
                            record = None
                    if record:
                        log.write(record.log_line())
                        if isinstance(record, Record):
                            (rx if record.direction == "RX" else tx).write(record.data)
                    if time.monotonic() - last_flush >= 0.5:
                        for stream in (log, rx, tx):
                            stream.flush()
                        last_flush = time.monotonic()
                log.write(f"# {'INCOMPLETE: ' + self.failure if self.failure else 'STOP: recording closed normally'}\n")
        except Exception as exc:
            self.failure = f"Recording failed (the file may be incomplete): {error_text(exc)}"
        finally:
            with self.condition:
                self.stopping = True
                self.items.clear()
                self.size = 0
            self.finished.set()
            self.notify("log_error" if self.failure else "log_stopped", self.failure or str(self.directory))


class SerialSession:
    def __init__(self, factory=None, reconnect_interval=0.5):
        self.factory = factory or serial.serial_for_url
        self.reconnect_interval = reconnect_interval
        self.events = queue.SimpleQueue()
        self.display = DisplayBuffer()
        self.send_queue = queue.Queue(maxsize=16)
        self.stop_event = threading.Event()
        self.connected_event = threading.Event()
        self.baud_changing_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.recorder = None
        self.writers = []
        self.rx = self.tx = 0
        self.current_baud = 115200
        self._requested_baud = None

    def notify(self, kind, detail=""):
        self.events.put((kind, detail))

    def open(self, config):
        if self.is_alive():
            raise RuntimeError("The previous connection is still closing; please wait")
        self.stop_event.clear()
        self.baud_changing_event.clear()
        with self.lock:
            self._requested_baud = None
        while not self.send_queue.empty():
            self.send_queue.get_nowait()
        self.thread = threading.Thread(target=self._run, args=(dict(config),), name="serial-io", daemon=True)
        self.thread.start()

    def is_alive(self):
        return self.thread is not None and self.thread.is_alive()

    def send(self, data):
        with self.lock:
            if self.stop_event.is_set() or not self.connected_event.is_set():
                raise RuntimeError("The port is not connected; open it or wait for the device to return")
            if self.baud_changing_event.is_set():
                raise RuntimeError("Baud rate change is pending; wait for Apply to finish")
            try:
                self.send_queue.put_nowait(data)
            except queue.Full:
                raise ValueError("The send queue is full; try again shortly") from None

    def close(self):
        self.stop_event.set()

    def set_baudrate(self, value):
        rate = validate_baud(value)
        with self.lock:
            if self.stop_event.is_set() or not self.connected_event.is_set():
                raise RuntimeError("Connect the port before applying a baud rate")
            self._requested_baud = rate
            self.baud_changing_event.set()

    def _drop_pending_sends(self):
        with self.lock:
            while not self.send_queue.empty():
                self.send_queue.get_nowait()

    def _note(self, message):
        with self.lock:
            if self.recorder:
                self.recorder.submit(LogNote(time.time(), message))

    def _disconnected(self):
        with self.lock:
            self.connected_event.clear()
            self.baud_changing_event.clear()
            # Never replay stale commands after a board reset.
            while not self.send_queue.empty():
                self.send_queue.get_nowait()

    def counters(self):
        with self.lock:
            return self.rx, self.tx

    def _record(self, direction, data):
        record = Record(time.time(), direction, data)
        with self.lock:
            if direction == "RX":
                self.rx += len(data)
            else:
                self.tx += len(data)
            if self.recorder:
                self.recorder.submit(record)
        self.display.put(record)

    def start_recording(self, directory):
        with self.lock:
            if self.recorder or any(not w.finished.is_set() for w in self.writers):
                raise RuntimeError("The previous recording is still finishing")
            self.writers = []
            writer = Recorder(Path(directory), self.notify)
            self.recorder = writer
            self.writers.append(writer)
            writer.submit(LogNote(time.time(), f"CURRENT HOST BAUD {self.current_baud}"))
            writer.start()
        return writer

    def stop_recording(self):
        with self.lock:
            writer, self.recorder = self.recorder, None
        if writer:
            writer.stop()

    def busy(self):
        return self.is_alive() or any(not w.finished.is_set() for w in self.writers)

    def _run(self, config):
        try:
            name = config.pop("port")
            # Native device names and the explicitly exposed local loopback only.
            if "://" in name and name != "loop://":
                raise ValueError("Only local serial ports and loop:// are supported")
            opened_once = False
            waiting = False
            while not self.stop_event.is_set():
                port = None
                retry = False
                try:
                    self.current_baud = int(config.get("baudrate", 115200))
                    port = self.factory(name, do_not_open=True, timeout=0.03, write_timeout=0.5, **config)
                    port.dtr = False
                    port.rts = False
                    port.open()
                    if self.stop_event.is_set():
                        break
                    self.connected_event.set()
                    opened_once = True
                    waiting = False
                    self.notify("opened", name)
                    self._note(f"OPEN {name} BAUD {self.current_baud}")
                    self._transfer(port, config)
                except serial.SerialTimeoutException as exc:
                    # A timed-out command may be partially sent. Do not replay it.
                    if not self.stop_event.is_set():
                        self.notify("error", f"Write timed out: {error_text(exc)}\nSome bytes may already have been sent. Check the device and reopen the port.")
                except (serial.SerialException, OSError) as exc:
                    if not self.stop_event.is_set():
                        if opened_once:
                            retry = True
                            if not waiting:
                                self.notify("reconnecting", f"{name} disconnected; waiting to reconnect when the device returns.\n{error_text(exc)}\nUnfinished sends were canceled.")
                            waiting = True
                        else:
                            self.notify("error", f"{error_text(exc)}\nCheck port access, device connection and serial settings.")
                finally:
                    self._disconnected()
                    if port is not None:
                        try:
                            port.close()
                        except Exception:
                            pass
                if not retry or self.stop_event.wait(self.reconnect_interval):
                    break
        except Exception as exc:
            if not self.stop_event.is_set():
                self.notify("error", error_text(exc))
        finally:
            self._disconnected()
            self.stop_event.set()
            self.notify("closed")

    def _change_baud(self, port, rate, config):
        previous = self.current_baud
        try:
            port.baudrate = rate
        except (ValueError, OSError, serial.SerialException) as exc:
            try:
                port.baudrate = previous
            except Exception as rollback:
                raise serial.SerialException(f"Unable to restore port settings: {error_text(rollback)}") from exc
            self.baud_changing_event.clear()
            self.notify("baud_error", f"Cannot apply {rate}; kept {previous}. {error_text(exc)}")
            return False
        with self.lock:
            self.current_baud = rate
            self.baud_changing_event.clear()
        config["baudrate"] = rate
        self._note(f"BAUD {previous} -> {rate} (manual); in-flight bytes may span settings")
        self.notify("baud_changed", {"baudrate": rate, "source": "manual"})
        return True

    def _transfer(self, port, config):
        payload = b""
        offset = 0
        while not self.stop_event.is_set():
            with self.lock:
                requested, self._requested_baud = self._requested_baud, None
            if requested is not None:
                payload, offset = b"", 0
                self._drop_pending_sends()
                self._change_baud(port, requested, config)
            if offset >= len(payload):
                try:
                    payload = self.send_queue.get_nowait()
                    offset = 0
                except queue.Empty:
                    payload = b""
            if payload:
                chunk = payload[offset:offset + 1024]
                written = port.write(chunk)
                if not written:
                    raise serial.SerialTimeoutException("Write did not complete")
                self._record("TX", chunk[:written])
                offset += written
            data = port.read(min(max(port.in_waiting, 1), 4096))
            if data:
                self._record("RX", data)


def discover_ports():
    return [(p.device, p.description) for p in sorted(list_ports.comports())]


def export_records(path, records, raw=False):
    """Caller runs this on a background thread; overwrite follows Save dialog."""
    if raw:
        with Path(path).open("wb") as stream:
            for record in records:
                if record.direction == "RX":
                    stream.write(record.data)
    else:
        with Path(path).open("w", encoding="utf-8") as stream:
            stream.write("# Cached records only; HEX with local timestamps\n")
            for record in records:
                stream.write(record.log_line())
