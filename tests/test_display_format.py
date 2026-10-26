from datetime import datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from display_format import StreamFormatter
from serial_core import Record


class DisplayTests(unittest.TestCase):
    def render(self, chunks, **options):
        formatter = StreamFormatter(**options)
        return "".join(formatter.render(Record(i + 1, "RX", data)) for i, data in enumerate(chunks))

    def test_fragmented_words_and_crlf_are_real_lines(self):
        chunks = [b"w", b"aiting...\r", b"\nF", b"reeBCI V3 8-16 channel\r\nADS1299 Device", b" ", b"ID2:0x3E\r", b"\n"]
        self.assertEqual(self.render(chunks, timestamps=False),
                         "RX  waiting...\nFreeBCI V3 8-16 channel\nADS1299 Device ID2:0x3E\n")

    def test_time_is_only_at_logical_line_start(self):
        stamp = datetime(2026, 10, 26, 17, 18, 2, 121000).timestamp()
        formatter = StreamFormatter()
        chunks = [(stamp, b"w"), (stamp + 1, b"aiting...\r"), (stamp + 2, b"\nnext"), (stamp + 3, b" line\n")]
        text = "".join(formatter.render(Record(t, "RX", data)) for t, data in chunks)
        self.assertEqual(text, "[17:18:02.121] RX  waiting...\n[17:18:04.121] next line\n")

    def test_cr_lf_blank_lines_and_split_crlf(self):
        self.assertEqual(self.render([b"A\r", b"\nB\rC\n\nD\r", b"\n\r\n"], timestamps=False),
                         "RX  A\nB\nC\n\nD\n\n")

    def test_utf8_byte_fragments_do_not_create_prefixes_or_break_words(self):
        raw = "你好\r\n世界".encode()
        self.assertEqual(self.render([bytes([b]) for b in raw], timestamps=False), "RX  你好\n世界")

    def test_partial_character_uses_first_byte_time(self):
        stamp = datetime(2026, 10, 26, 17, 18, 2, 121000).timestamp()
        formatter = StreamFormatter()
        self.assertEqual(formatter.render(Record(stamp, "RX", b"\xe4")), "")
        self.assertEqual(formatter.render(Record(stamp + 1, "RX", b"\xbd\xa0")), "[17:18:02.121] RX  你")

    def test_switches_have_separate_lines_without_repeating_direction(self):
        formatter = StreamFormatter(timestamps=False)
        records = [Record(1, "RX", b"fir"), Record(2, "RX", b"st"), Record(3, "TX", b"go\r\n"), Record(4, "RX", b"ok\nmore")]
        text = "".join(formatter.render(r) for r in records)
        self.assertEqual(text, "RX  first\nTX  go\nRX  ok\nmore")

    def test_hex_rows_are_independent_of_read_sizes(self):
        chunks = [b"\x00", b"\x01\x02", bytes(range(3, 40))]
        expected = "RX\n" + bytes(range(32)).hex(" ").upper() + "\n" + bytes(range(32, 40)).hex(" ").upper()
        self.assertEqual(self.render(chunks, timestamps=False, hex_mode=True), expected)

    def test_configurable_hex_width_entire_range_keeps_all_bytes(self):
        data = bytes(range(256)) * 2
        chunks = [data[:1], data[1:8], data[8:105], data[105:278], data[278:]]
        for width in range(20, 91):
            with self.subTest(width=width):
                text = self.render(chunks, timestamps=False, hex_mode=True, hex_bytes_per_line=width)
                rows = text.removeprefix("RX\n").splitlines()
                self.assertTrue(all(len(row.split()) == width for row in rows[:-1]))
                self.assertLessEqual(len(rows[-1].split()), width)
                self.assertEqual("".join(text.removeprefix("RX\n").split()), data.hex().upper())

    def test_hex_direction_headers_are_separate_from_data(self):
        formatter = StreamFormatter(timestamps=False, hex_mode=True)
        records = [Record(1, "RX", b"\x00"), Record(2, "RX", b"\x01"), Record(3, "TX", b"\xfe\xff"), Record(4, "RX", b"\x02")]
        text = "".join(formatter.render(r) for r in records)
        self.assertEqual(text, "RX\n00 01\nTX\nFE FF\nRX\n02")

    def test_hex_timestamp_rows_have_identical_data_columns(self):
        formatter = StreamFormatter(hex_mode=True, hex_bytes_per_line=20)
        text = formatter.render(Record(1, "RX", bytes(range(41))))
        rows = text.splitlines()
        self.assertEqual(rows[0], "RX")
        data_columns = [row.index("] ") + 2 for row in rows[1:]]
        self.assertEqual(len(set(data_columns)), 1)
        self.assertTrue(all("RX" not in row and "TX" not in row for row in rows[1:]))
        self.assertEqual([len(row.split("] ", 1)[1].split()) for row in rows[1:]], [20, 20, 1])

    def test_hex_width_rejects_values_outside_supported_range(self):
        for value in (19, 91, 0, -1, 20.5, "32", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                StreamFormatter(hex_bytes_per_line=value)

    def test_binary_controls_still_show_escapes(self):
        self.assertEqual(self.render([b"a\x00\xff\tb"], timestamps=False), "RX  a\\x00\\xff\tb")

    def test_pathological_unterminated_line_has_bounded_layout(self):
        self.assertEqual(self.render([b"a" * 15, b"bc\n"], timestamps=False, max_line_chars=16),
                         "RX  " + "a" * 15 + "b\nc\n")
        self.assertEqual(self.render([b"a" * 16, b"\n"], timestamps=False, max_line_chars=16),
                         "RX  " + "a" * 16 + "\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
