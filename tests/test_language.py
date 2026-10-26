import ast
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from serial_core import validate_baud
from ui_text import english_text, error_text


class EnglishInterfaceTests(unittest.TestCase):
    def test_runtime_literals_are_english(self):
        for name in ("main.py", "serial_core.py", "display_format.py", "device_identity.py",
                     "device_scan.py", "ui_text.py"):
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertIsNone(re.search(r"[\u3400-\u9fff]", node.value), (name, node.lineno))

    def test_chinese_os_metadata_and_errors_have_english_fallbacks(self):
        self.assertEqual(english_text("USB 串行设备", "Serial device"), "Serial device")
        self.assertEqual(english_text("USB Serial Device"), "USB Serial Device")
        self.assertNotRegex(error_text(OSError(5, "拒绝访问")), r"[\u3400-\u9fff]")
        self.assertIn("system code 5", error_text(OSError(5, "拒绝访问")))
        self.assertEqual(error_text(ValueError("unsupported baud")), "unsupported baud")

    def test_manual_baud_validation(self):
        for value in ("bad", "0", "12000001", "1.5", None, float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_baud(value)
        self.assertEqual(validate_baud("921600"), 921600)
        self.assertEqual(validate_baud(2000000), 2000000)


if __name__ == "__main__":
    unittest.main()
