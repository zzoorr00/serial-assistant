"""Incremental stream display. Serial read boundaries are never text lines."""
import codecs
from datetime import datetime


class StreamFormatter:
    def __init__(self, encoding="UTF-8", timestamps=True, hex_mode=False, max_line_chars=4096, hex_bytes_per_line=32):
        if type(hex_bytes_per_line) is not int or not 20 <= hex_bytes_per_line <= 90:
            raise ValueError("HEX row width must be an integer from 20 to 90")
        self.timestamps = timestamps
        self.hex_mode = hex_mode
        self.hex_bytes_per_line = hex_bytes_per_line
        self.max_line_chars = max_line_chars
        self.decoders = {d: codecs.getincrementaldecoder(encoding)("backslashreplace") for d in ("RX", "TX")}
        self.pending_stamp = {d: None for d in ("RX", "TX")}
        self.skip_lf = {d: False for d in ("RX", "TX")}
        self.direction = None
        self.line_start = True
        self.column = 0

    def _begin(self, direction, timestamp, output):
        if direction != self.direction and not self.line_start:
            output.append("\n")
            self.line_start = True
            self.column = 0
        if self.line_start:
            if self.timestamps:
                stamp = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S.%f")[:-3]
                output.append(f"[{stamp}] ")
            if direction != self.direction:
                output.append(direction + "  ")
            self.direction = direction
            self.line_start = False

    def render(self, record):
        if not record.data:
            return ""
        if self.hex_mode:
            return self._hex(record)
        direction = record.direction
        decoder = self.decoders[direction]
        had_pending = bool(decoder.getstate()[0])
        timestamp = self.pending_stamp[direction] if had_pending else record.timestamp
        text = decoder.decode(record.data)
        if decoder.getstate()[0]:
            if not had_pending or text:
                self.pending_stamp[direction] = record.timestamp
        else:
            self.pending_stamp[direction] = None
        output = []
        for char in text:
            if char == "\n" and self.skip_lf[direction]:
                self.skip_lf[direction] = False
                continue
            self.skip_lf[direction] = char == "\r"
            if char == "\r":
                char = "\n"
            # Prevent pathological unterminated binary/text lines from making
            # QTextDocument layout grow without bound. Ordinary lines are intact.
            if char != "\n" and self.column >= self.max_line_chars:
                output.append("\n")
                self.line_start = True
                self.column = 0
            self._begin(direction, timestamp, output)
            if char == "\n":
                output.append("\n")
                self.line_start = True
                self.column = 0
            else:
                value = char if char.isprintable() or char == "\t" else f"\\x{ord(char):02x}"
                output.append(value)
                self.column += len(value)
            # Only the first decoded character can have begun in an older read.
            timestamp = record.timestamp
        return "".join(output)

    def _hex(self, record):
        output = []
        if record.direction != self.direction:
            if not self.line_start:
                output.append("\n")
            output.append(record.direction + "\n")
            self.direction = record.direction
            self.line_start = True
            self.column = 0
        for byte in record.data:
            self._begin(record.direction, record.timestamp, output)
            if self.column:
                output.append(" ")
            output.append(f"{byte:02X}")
            self.column += 1
            if self.column == self.hex_bytes_per_line:
                output.append("\n")
                self.line_start = True
                self.column = 0
        return "".join(output)
