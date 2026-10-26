"""Cancellable, explicit identity scan; unknown and occupied ports are not probed."""
from dataclasses import dataclass, replace
import os
import time

import serial
from serial.tools import list_ports
from device_identity import IDENTITY_QUERY, parse_identity
from ui_text import error_text

IDENTITY_BAUD_RATES = (115200, 921600, 2000000)

IDENTITY_INTERFACES = {(0x303A, 0x1001), (0x1A86, 0x55D3)}


@dataclass(frozen=True)
class ScanResult:
    device: str
    identity: object = None
    occupied: object = None
    baudrate: object = None
    message: str = ""


def port_occupied(device):
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW("\\\\.\\" + device, 0, 0, None, 3, 0, None)
    if not handle or handle == wintypes.HANDLE(-1).value:
        return True
    kernel.CloseHandle(handle)
    return False


def probe_identity(device, cancelled, factory=None, dwell=1.2, rates=IDENTITY_BAUD_RATES):
    port = (factory or serial.Serial)(port=None, baudrate=rates[0], timeout=0.04, write_timeout=0.15)
    try:
        port.dtr = False
        port.rts = False
        port.port = device
        if cancelled():
            return ScanResult(device, message="Canceled")
        port.open()
        for rate in rates:
            if cancelled():
                break
            try:
                port.baudrate = rate
            except (ValueError, OSError, serial.SerialException):
                continue
            deadline = time.monotonic() + dwell
            query_at = time.monotonic() + 0.18
            received = b""
            while not cancelled() and time.monotonic() < deadline:
                chunk = port.read(min(max(port.in_waiting, 1), 1024))
                if b"\xa0" in chunk:
                    return ScanResult(device, message="Binary data is streaming; identity query stopped")
                received = (received + chunk)[-4096:]
                identity = parse_identity(received)
                if identity:
                    return ScanResult(device, identity, False, rate)
                if time.monotonic() >= query_at and not cancelled():
                    port.write(IDENTITY_QUERY)
                    query_at = time.monotonic() + 0.8
        return ScanResult(device, message="Canceled" if cancelled() else "Identity unavailable; manual connection is still supported")
    except (ValueError, OSError, serial.SerialException) as exc:
        return ScanResult(device, message=f"Identity query failed: {error_text(exc)}")
    finally:
        port.close()


def scan_ports(notify, cancelled, identify=False, excluded=(), port_lister=None, occupied_check=None, probe=None):
    infos = sorted((port_lister or list_ports.comports)(), key=lambda p: p.device)
    notify("ports", [(p.device, p.description) for p in infos])
    if not identify:
        return
    for info in infos:
        if cancelled():
            break
        if info.device in excluded:
            result = ScanResult(info.device, occupied=True, message="Connected in this window; query skipped")
        else:
            busy = (occupied_check or port_occupied)(info.device)
            interface = (getattr(info, "vid", None), getattr(info, "pid", None))
            if busy:
                result = ScanResult(info.device, occupied=True, message="Port busy or inaccessible; query skipped")
            elif interface not in IDENTITY_INTERFACES:
                result = ScanResult(info.device, occupied=busy, message="Unknown USB interface; listed without querying")
            else:
                result = (probe or probe_identity)(info.device, cancelled)
                if result.identity:
                    transport = "usb_serial_jtag" if interface == (0x303A, 0x1001) else "usb_uart_ch343"
                    result = replace(result, identity=replace(result.identity, transport=transport))
        notify("identity", result)
