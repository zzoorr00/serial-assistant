# Testing scope

Version **1.6 debug**, tested with Python 3.12.4 on Windows 10 x64.

## Unit and stream checks

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py' -v
```

35 tests cover encoding, HEX validation, binary loopback, recording/export, reconnect and
cancellation, no replay of stale commands, timeouts and bounded queues; stream formatting,
split UTF-8, CR/LF/CRLF, direction headers and all 71 HEX widths from 20 to 90; live baud changes
on the same handle, driver rejection rollback and reconnect at the last applied rate; identity
protocol validation, read-only probing and scan exclusions; English runtime messages, localized
OS-error fallback and baud validation. A fake serial stream repeatedly announces another baud
and sends invalid bytes; the host rate stays fixed until explicitly changed.

## Windows GUI simulations

```powershell
.\.venv\Scripts\python.exe tests\smoke_gui.py
.\.venv\Scripts\python.exe tests\smoke_features.py
```

The tests temporarily open windows and use loopback or simulated ports, never real device ports.
They check text/HEX rendering, row-width rerendering, actual monospace glyph widths and first-byte
pixel alignment; timestamps, pause/clear, exports, recording, independent windows, reconnect and
cancel, and the final in-flight read during close. An 8 MiB simulated stream is compared byte for
byte with its raw recording, while checking display/cache bounds and GUI heartbeat gaps.

Feature simulations check identity names/IDs/busy markers, live Apply without reopening, no
automatic rate changes, manual sends, English labels/tooltips/device descriptions/error fallbacks,
non-native English recording dialogs, connected-port scan exclusion, scan cancellation and orderly
shutdown. Demo COM names and IDs are synthetic. Results and screenshots are in `test-output/`.

## Remaining hardware checks

Connect a real device, verify COM/settings and text/HEX send/receive. Change the board baud
externally and manually Apply the same rate here; verify traffic resumes without closing the port.
Scan compatible idle acquisition boards and receivers and check their identity. Record for 10–30
minutes, toggle views and pause/resume, restart the board and reconnect USB at the same COM number.
Confirm recording continues, Close stops reconnect attempts, and raw files match expected traffic.

Software simulations do not establish actual driver compatibility or device-side data integrity.
Real USB rate changes, identity responses, power cycles, sustained traffic and another PC are
pending hardware validation. This debug build has not been synced to stable.
