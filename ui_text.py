"""English UI fallbacks for localized system/device metadata; never for payloads."""
import re

_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")


def english_text(value, fallback="Details are not available in English"):
    text = str(value)
    return fallback if _CJK.search(text) else text


def error_text(error):
    text = str(error)
    if not _CJK.search(text):
        return text
    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
    suffix = f" (system code {code})" if code is not None else ""
    return f"{type(error).__name__}{suffix}: check the device, port, permissions and settings"
