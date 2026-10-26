# Serial Assistant

A simple Windows serial monitor built with Python, PyQt5 and pySerial.

**Version: 1.6 debug · Python 3.12 · English interface**

![Serial Assistant: device identity and aligned HEX data](docs/images/serial-assistant.png)

## Features

- Select or enter a COM port; configure baud rate, data bits, parity and stop bits.
- Change baud rate while connected using **Apply**, without closing the port.
- Explicit FreeBCI identity scan: board/receiver name, chip, device ID and busy-port status.
- Text and HEX send/receive; UTF-8, GB18030, ASCII and Latin-1 encoding.
- Streaming text preserves CRLF/LF/CR and split UTF-8 characters across reads.
- Aligned monospace HEX rows with **20–90 bytes per row** (default 32).
- RX/TX direction appears once per direction change; HEX headers occupy their own line.
- Optional millisecond timestamps, pause display, clear cache and independent windows.
- Automatic reconnect to the same COM port after a successful initial connection.
- Background recording and cache export with bounded display memory.

## Start from source

Use Python 3.12 x64 (tested with 3.12.4). Run in this folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

In VS Code, open this folder, select `.venv\Scripts\python.exe`, then run `main.py`.
Runtime files are `main.py`, `serial_core.py`, `display_format.py`, `device_identity.py`,
`device_scan.py` and `ui_text.py`. No other project is required.

## Use

1. Plug in a device. Startup only lists ports. Click **Scan devices** to query supported
   FreeBCI interfaces, then select a COM port. Unknown devices can still be selected manually.
2. Choose serial settings (default 115200, 8N1) and click **Open port**.
3. While connected, choose or type a new baud rate and click **Apply**. This changes only
   the computer-side port settings. It does not send a baud configuration command to the board.
4. Enter text and click **Send** or press Ctrl+Enter. For HEX send, use `01 03 00 00 FF`,
   without `0x` prefixes or commas. **Append ending** adds CRLF/LF/CR in either send mode.
5. Enable **HEX view** and set **Per row** to 20–90. Each pair such as `AF` counts as one byte;
   spaces, timestamps and RX/TX headers are excluded. Changes reformat the cached data.
6. For a complete session, click **Start recording...** before opening the port. Choose a
   parent folder, then wait for **Saved** after stopping. Closing the window also finishes files.

Run the EXE repeatedly, or click **+ New window**, for independent connections and settings.
A COM port is normally exclusive: close its connection in other software before opening it here.
Select `loop:// - Software loopback (no device)` to test without hardware.

The application controls, tooltips and status messages are English. Received/sent data is
preserved in its original encoding. User-selected filesystem names are not renamed.

## Manual baud changes and reconnect

There is no automatic baud detection or switching. Incoming baud announcements and invalid
data never change the active port's baud rate. If another program changes the board's baud,
select the matching value here and click **Apply**. Other serial settings require closing first.

Apply reconfigures the existing handle in the serial worker. Pending sends are cancelled during
changes; if the driver rejects a rate, the previous setting is restored when possible and an error
is shown. The latest successfully applied rate is used for reconnects. Bytes in flight during a
rate change may be lost or decoded incorrectly.

After the first successful open, a silent or restarting board does not cause the handle to close.
If USB disappears or the driver reports a disconnect, the monitor retries the same COM port every
0.5 seconds, keeping the settings and recorder. Click **Close port** to stop retrying. A new COM
number must be selected manually. Pending commands are not replayed after reconnect. Initial
open errors and write timeouts require a manual reopen; timed-out writes may have sent some bytes.

## FreeBCI identity scan

**Scan devices** queries compatible acquisition boards and BLE receivers, showing their names,
COM numbers and IDs. Hover for chip, firmware and address details. Click again to cancel.
The active connection in any window, occupied ports and unknown USB interfaces are skipped.

The scanner uses the existing `freebci.identity` v1 protocol: a read-only `0x1D` query on known
Espressif USB `303A:1001` and CH343 `1A86:55D3` interfaces. An explicit scan tries 115200,
921600 and 2000000 on temporary idle-port handles, up to about 3.6 seconds per device.
This does not change a connected monitor's settings. Binary streams stop the query for that port.
Scanning briefly opens and reads idle ports; finish or cancel scanning before opening a new connection.
DTR/RTS are set low before opening, though drivers may still produce a transient on open.

## Recording and export

Recording creates a unique `Serial_date_time_process` subfolder containing:

- `session.log`: UTF-8 timestamped HEX records, direction, byte counts and host baud metadata.
- `rx.bin`: all received bytes in order, including incorrectly decoded bytes at mismatched baud rates.
- `tx.bin`: bytes accepted by the serial write API; this does not confirm receipt by the device.

Recording starts when requested and cannot recover earlier data. It continues across port closes
and reopens until stopped. Files flush about every 0.5 seconds. Normal completion ends the log
with `STOP: recording closed normally`; recording errors may leave incomplete files.

**Export cache...** saves only the recent cache (up to 1 MiB / 8192 blocks), as a timestamped HEX
`.log` or received-only raw `.bin`. It is independent of the current display mode. The screen is
limited to 4000 lines and about one million characters. Text lines without terminators wrap at
about 4096 characters for responsiveness; raw files remain unchanged. Read blocks and display
rows are byte-stream fragments, not protocol packet boundaries.

**Pause display** affects only rendering. Receiving, counters and recording continue. **Clear cache**
does not delete recorded files or reset total RX/TX counts. The display queue is limited to 1 MiB /
8192 blocks and reports skipped bytes. The independent recording queue is limited to 32 MiB /
65536 blocks; overflow stops recording with an error. These bounds cannot prevent losses in the
device, driver or operating system.

## Test and build

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
.\.venv\Scripts\python.exe tests\smoke_gui.py
.\.venv\Scripts\python.exe tests\smoke_features.py
powershell -ExecutionPolicy Bypass -File tools\build.ps1
```

Build output: `dist\串口助手.exe`, a standalone Windows x64 executable; Python is not required
to run it. Build and simulation output stays in the development project. Test scripts temporarily
open GUI windows and use software loopback or fake devices. See [testing scope](docs/TESTING.md).

This debug build passed software tests on Windows 10 x64. Real board traffic, identity scans,
USB-driver live rate changes, board restarts, long sessions and another computer still need
hardware validation. **Hardware acceptance is pending; this build has not been synced to stable.**
Manual DTR/RTS controls and hardware flow control are not provided.
