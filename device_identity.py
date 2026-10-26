"""FreeBCI identity protocol v1, standalone from the acquisition GUI."""
from dataclasses import dataclass
import json
import re
from ui_text import english_text

IDENTITY_QUERY = b"\x1d"
IDENTITY_PREFIX = b"\x1eFBCI1 "


@dataclass(frozen=True)
class DeviceIdentity:
    chip: str
    role: str
    uid: str
    address: str
    supported_channels: tuple
    active_channels: int
    firmware: str
    transport: str
    receiver_target: str = ""

    @property
    def display_name(self):
        if self.role == "ble_receiver":
            if self.receiver_target == "UNIVERSAL":
                return "FreeBCI BLE Receiver (Universal)"
            family = self.receiver_target or self.chip
            return f"FreeBCI BLE Receiver ({family}-{self.address.replace(':', '')[-4:]})"
        return f"FreeBCI {self.chip} Acquisition ({'/'.join(map(str, self.supported_channels))} channels)"

    @property
    def details(self):
        return (f"{self.display_name}\nDevice ID: {self.uid}\nHardware: ESP32-{self.chip}\n"
                f"BLE address: {self.address or 'not provided'}\nActive channels: {self.active_channels}\n"
                f"Firmware: {english_text(self.firmware)}\nInterface: {english_text(self.transport)}")


def parse_identity(data):
    position = 0
    while True:
        start = data.find(IDENTITY_PREFIX, position)
        if start < 0:
            return None
        end = data.find(b"\n", start)
        if end < 0:
            return None
        position = end + 1
        if end - start > 1024:
            continue
        try:
            value = json.loads(data[start + len(IDENTITY_PREFIX):end].decode("ascii"))
            if not isinstance(value, dict) or value.get("protocol") != "freebci.identity" or type(value.get("version")) is not int or value["version"] != 1:
                continue
            chip = value["chip"].removeprefix("ESP32-")
            role = value["role"]
            target = value.get("receiver_target", "")
            uid = value["uid"].upper()
            address = value.get("address", "").upper()
            channels, active = value["supported_channels"], value["active_channels"]
            if chip not in ("S3", "C5") or role not in ("eeg_acquisition", "ble_receiver") or not re.fullmatch(r"[0-9A-F]{12}", uid):
                continue
            if not isinstance(channels, list) or any(type(n) is not int for n in channels) or type(active) is not int:
                continue
            if target not in ("", "S3", "C5", "UNIVERSAL") or role != "ble_receiver" and target:
                continue
            if role == "ble_receiver":
                if chip != "C5" or channels or active != 0 or not re.fullmatch(r"(?:[0-9A-F]{2}:){5}[0-9A-F]{2}", address):
                    continue
            elif channels != [8, 16] or active not in channels:
                continue
            firmware, transport = value["firmware"], value["transport"]
            if not all(isinstance(s, str) and 0 < len(s) <= 64 and s.isprintable() for s in (firmware, transport)):
                continue
            return DeviceIdentity(chip, role, uid, address, tuple(channels), active, firmware, transport, target)
        except (KeyError, ValueError, TypeError, AttributeError, UnicodeError):
            continue
