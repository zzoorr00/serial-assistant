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
from auto_baud import BaudTrial, DEFAULT_RATES, validate_baud


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
        raise ValueError("请输入要发送的数据")
    if len(data) > 65536:
        raise ValueError("单次发送最多 64 KiB，请拆分发送")
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
                self.failure = "磁盘记录队列已满，记录不完整；请检查磁盘速度/剩余空间"
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
                log.write("# SerialMonitor v1.5 debug | HEX / local receive timestamps\n")
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
            self.failure = f"文件记录失败（记录可能不完整）：{exc}"
        finally:
            with self.condition:
                self.stopping = True
                self.items.clear()
                self.size = 0
            self.finished.set()
            self.notify("log_error" if self.failure else "log_stopped", self.failure or str(self.directory))


class SerialSession:
    def __init__(self, factory=None, reconnect_interval=0.5, auto_dwell=1.0, auto_idle=3.0):
        self.factory = factory or serial.serial_for_url
        self.reconnect_interval = reconnect_interval
        self.events = queue.SimpleQueue()
        self.display = DisplayBuffer()
        self.send_queue = queue.Queue(maxsize=16)
        self.stop_event = threading.Event()
        self.connected_event = threading.Event()
        self.scanning_event = threading.Event()
        self.baud_changing_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()
        self.recorder = None
        self.writers = []
        self.rx = self.tx = 0
        self.current_baud = 115200
        self._requested_baud = None
        self._auto_settings = (False, DEFAULT_RATES)
        self._settings_revision = 0
        self.auto_dwell = auto_dwell
        self.auto_idle = auto_idle

    def notify(self, kind, detail=""):
        self.events.put((kind, detail))

    def open(self, config):
        if self.is_alive():
            raise RuntimeError("上一个连接仍在关闭，请稍候")
        self.stop_event.clear()
        self.scanning_event.clear()
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
                raise RuntimeError("串口未连接，正在等待设备恢复或尚未打开")
            if self.scanning_event.is_set():
                raise RuntimeError("波特率正在试探，匹配前不能发送数据；可关闭自动试探后手动设置")
            if self.baud_changing_event.is_set():
                raise RuntimeError("正在切换波特率，请等待应用完成")
            try:
                self.send_queue.put_nowait(data)
            except queue.Full:
                raise ValueError("发送队列已满，请稍后再发送") from None

    def close(self):
        self.stop_event.set()

    def set_baudrate(self, value):
        rate = validate_baud(value)
        with self.lock:
            if self.stop_event.is_set() or not self.connected_event.is_set():
                raise RuntimeError("请在串口已连接时应用波特率")
            self._requested_baud = rate
            self.baud_changing_event.set()
            self._auto_settings = (False, self._auto_settings[1])
            self._settings_revision += 1

    def configure_auto(self, enabled, rates=DEFAULT_RATES):
        rates = tuple(dict.fromkeys(validate_baud(r) for r in rates))
        if not 1 <= len(rates) <= 8:
            raise ValueError("候选波特率需要 1–8 个")
        with self.lock:
            self._auto_settings = (bool(enabled), rates)
            self._settings_revision += 1
            if enabled and self.connected_event.is_set():
                self.scanning_event.set()

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
            self.scanning_event.clear()
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
                raise RuntimeError("上一次记录尚未结束")
            self.writers = []
            writer = Recorder(Path(directory), self.notify)
            self.recorder = writer
            self.writers.append(writer)
            writer.submit(LogNote(time.time(), f"CURRENT BAUD {self.current_baud}; automatic trials are host-side only"))
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
                raise ValueError("仅支持本机串口或 loop:// 模拟回环")
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
                        self.notify("error", f"发送超时：{exc}\n部分数据可能已发出，请检查设备后重新打开串口。")
                except (serial.SerialException, OSError) as exc:
                    if not self.stop_event.is_set():
                        if opened_once:
                            retry = True
                            if not waiting:
                                self.notify("reconnecting", f"{name} 暂时断开，等待设备恢复后自动重连。\n{exc}\n未完成的发送已取消。")
                            waiting = True
                        else:
                            self.notify("error", f"{exc}\n请检查端口占用、设备连接和串口参数。")
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
                self.notify("error", str(exc))
        finally:
            self._disconnected()
            self.stop_event.set()
            self.notify("closed")

    def _change_baud(self, port, rate, config, source, remember=True):
        previous = self.current_baud
        try:
            port.baudrate = rate
        except (ValueError, OSError, serial.SerialException) as exc:
            try:
                port.baudrate = previous
            except Exception as rollback:
                raise serial.SerialException(f"切换失败且无法恢复串口参数：{rollback}") from exc
            if source == "manual":
                self.baud_changing_event.clear()
            self.notify("auto_status" if source == "probe" else "baud_error",
                        {"state": "unsupported", "baudrate": rate, "reason": str(exc)} if source == "probe" else f"无法设置 {rate}，已保留 {previous}：{exc}")
            return False
        with self.lock:
            self.current_baud = rate
            if source == "manual":
                self.baud_changing_event.clear()
        if remember:
            config["baudrate"] = rate
        self._note(f"BAUD {previous} -> {rate} ({source}); in-flight bytes may span settings")
        self.notify("baud_changed", {"baudrate": rate, "source": source})
        return True

    def _transfer(self, port, config):
        payload = b""
        offset = 0
        trial = None
        revision = -1
        while not self.stop_event.is_set():
            now = time.monotonic()
            with self.lock:
                requested, self._requested_baud = self._requested_baud, None
                settings_revision = self._settings_revision
                enabled, rates = self._auto_settings
            if requested is not None:
                trial = None
                self.scanning_event.clear()
                payload, offset = b"", 0
                self._drop_pending_sends()
                self._change_baud(port, requested, config, "manual")
                self.notify("auto_status", {"state": "off", "baudrate": self.current_baud})
            elif revision != settings_revision:
                if enabled:
                    confirmed = trial.confirmed if trial else self.current_baud
                    trial = BaudTrial(self.current_baud, rates, now, self.auto_dwell, self.auto_idle)
                    trial.confirmed = confirmed
                    self.scanning_event.set()
                    payload, offset = b"", 0
                    self._drop_pending_sends()
                    self._note("AUTO SEARCH (passive); sends paused; candidate bytes are retained unvalidated")
                    self.notify("auto_status", {"state": "searching", "baudrate": self.current_baud})
                elif trial is not None:
                    restore = trial.confirmed
                    trial = None
                    self.scanning_event.clear()
                    if self.current_baud != restore:
                        self._change_baud(port, restore, config, "restore")
                    self.notify("auto_status", {"state": "off", "baudrate": self.current_baud})
            revision = settings_revision
            if trial:
                action = trial.tick(now)
                if action:
                    state, rate = action
                    self.scanning_event.set()
                    payload, offset = b"", 0
                    self._drop_pending_sends()
                    if state == "rate":
                        if not self._change_baud(port, rate, config, "probe", remember=False):
                            trial.since = now - trial.dwell
                        else:
                            self.notify("auto_status", {"state": "searching", "baudrate": rate})
                    else:
                        self._note("AUTO SEARCH: recognizable data absent; sends paused")
                        self.notify("auto_status", {"state": "searching", "baudrate": rate})
            # Interleave bounded writes and reads, including for large payloads.
            if not self.scanning_event.is_set() and offset >= len(payload):
                try:
                    payload = self.send_queue.get_nowait()
                    offset = 0
                except queue.Empty:
                    payload = b""
            if payload:
                chunk = payload[offset:offset + 1024]
                written = port.write(chunk)
                if not written:
                    raise serial.SerialTimeoutException("发送未完成")
                self._record("TX", chunk[:written])
                offset += written
                # No flush(): some drivers can block it indefinitely.
            data = port.read(min(max(port.in_waiting, 1), 4096))
            if data:
                self._record("RX", data)
                if trial and trial.current == self.current_baud:
                    was_locked = trial.locked
                    reason = trial.observe(data, time.monotonic())
                    if reason and not was_locked:
                        config["baudrate"] = self.current_baud
                        self.scanning_event.clear()
                        self._note(f"AUTO MATCH {self.current_baud}: {reason}")
                        self.notify("auto_status", {"state": "locked", "baudrate": self.current_baud, "reason": reason})


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
