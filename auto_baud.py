"""Passive FreeBCI baud trials. No commands are sent to the device."""
import re


DEFAULT_RATES = (115200, 921600, 2000000)


def validate_baud(value):
    rate = int(value)
    if str(value).strip() != str(rate) or not 1 <= rate <= 12000000:
        raise ValueError("波特率请输入 1–12000000 的整数")
    return rate


def parse_rates(text):
    parts = re.split(r"[,，;；\s]+", text.strip())
    rates = tuple(dict.fromkeys(validate_baud(part) for part in parts if part))
    if not 1 <= len(rates) <= 8:
        raise ValueError("请填写 1–8 个候选波特率，用逗号分隔")
    return rates


class StreamEvidence:
    """Only newly completed recognizable text or three sequential frames count."""
    TEXT = re.compile(rb"(?:^|[\r\n])((?:FreeBCI|waiting\.\.\.|UART baud switched to |ADS1299 Device ID|Firmware:|BLE MAC:|BLE Inited|Initialized BLE|Loaded commMode)[\x20-\x7e\t]{0,240})(?=[\r\n])")

    def __init__(self):
        self.buffer = b""
        self.total = 0
        self.accepted_end = 0

    def feed(self, data):
        self.total += len(data)
        self.buffer = (self.buffer + data)[-4096:]
        base = self.total - len(self.buffer)
        best_end, reason = self.accepted_end, ""
        for match in self.TEXT.finditer(self.buffer):
            end = base + match.end()
            if len(match.group(1)) >= 8 and end > best_end:
                best_end, reason = end, "FreeBCI 文本"
        # Current FreeBCI acquisition frames: 8ch=33 bytes, 16ch=57 bytes.
        for length in (33, 57):
            start = self.buffer.find(b"\xa0")
            while start >= 0 and start + length * 3 <= len(self.buffer):
                seq = self.buffer[start + 1]
                if all(self.buffer[start + n * length] == 0xA0
                       and self.buffer[start + n * length + length - 1] & 0xF0 == 0xC0
                       and self.buffer[start + n * length + 1] == (seq + n) % 256 for n in range(3)):
                    end = base + start + length * 3
                    if end > best_end:
                        best_end, reason = end, f"连续有效 {8 if length == 33 else 16} 通道帧"
                start = self.buffer.find(b"\xa0", start + 1)
        if best_end > self.accepted_end:
            self.accepted_end = best_end
            return reason
        return ""


class BaudTrial:
    def __init__(self, current, rates, now, dwell=1.0, idle=3.0):
        self.rates = tuple(dict.fromkeys((current, *rates)))
        self.current = current
        self.confirmed = current
        self.dwell = dwell
        self.idle = idle
        self.since = now
        self.last_good = now
        self.locked = False
        self.unverified_bytes = 0
        self.evidence = StreamEvidence()

    def observe(self, data, now):
        reason = self.evidence.feed(data)
        if reason:
            self.locked = True
            self.confirmed = self.current
            self.last_good = now
            self.unverified_bytes = 0
        else:
            self.unverified_bytes += len(data)
        return reason

    def tick(self, now):
        if self.locked:
            elapsed = now - self.last_good
            if elapsed < self.idle or self.unverified_bytes < 16 and elapsed < max(15.0, self.idle * 5):
                return None
            self.locked = False
            self.since = now
            self.evidence = StreamEvidence()
            self.unverified_bytes = 0
            return "search", self.current
        if now - self.since >= self.dwell:
            index = (self.rates.index(self.current) + 1) % len(self.rates)
            self.current = self.rates[index]
            self.since = now
            self.evidence = StreamEvidence()
            self.unverified_bytes = 0
            return "rate", self.current
        return None
