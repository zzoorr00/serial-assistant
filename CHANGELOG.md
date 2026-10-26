# 版本记录

## 1.6 debug — 2026-10-26

- I removed passive automatic baud detection and switching while keeping live manual Apply and explicit FreeBCI identity scanning.
- I translated the application controls, tooltips, status, identity names, errors and file dialogs into English, while preserving serial payloads.
- I retained aligned HEX data with separate RX/TX headers, configurable 20–90 byte rows, recording, reconnect and independent windows.
- I passed 35 unit/stream tests and Windows Qt simulations, including an exact 8 MiB raw recording comparison. Hardware acceptance remains pending; see docs/TESTING.md.

## 1.5 debug — 2026-10-26

- 我增加了在线应用波特率、驱动拒绝时回退和断线重连保留新速率。
- 我增加了可选 FreeBCI 被动轮询匹配及手动优先，试探/切换时暂停发送，并记录波特率元数据。
- 我加入了显式、可取消的 FreeBCI 身份扫描，显示采集板/接收板名称和 ID，跳过占用及未知接口；启动时仅枚举端口。
- 我通过了 37 项底层/格式化测试和 Windows Qt 模拟流程；实机验证范围见 docs/TESTING.md。

## 1.4 debug — 2026-10-26

- 我将 HEX 收发标记改为独立标题行，修复了界面样式覆盖等宽字体的问题，数据列保持对齐。
- 我保留了每行 20–90 字节、行首时间戳和自动重连，并通过了 24 项底层/格式化检查及界面像素对齐检查。

## 1.3 debug — 2026-10-26

- 我增加了 HEX 每行 20–90 字节设置、缓存重新排版及对应测试，默认每行 32 字节。
- 我通过了 22 项底层/格式化测试和 Windows Qt 界面模拟检查；实机验证范围见 docs/TESTING.md。

## 1.2 debug — 2026-10-26

- 我修复了读取分块造成的异常换行，支持跨块 CRLF、UTF-8、行首时间戳和连续 HEX 排版。
- 我增加了超长文本安全折行与屏幕字符上限。

## 1.1 debug — 2026-10-26

- 我增加了板子断线后的同 COM 端口自动重连、手动取消及旧命令取消。
- 我将 RX/TX 标记精简为仅在方向切换时显示。

## 1.0 debug — 2026-10-26

- 我实现了端口选择、串口参数、文本/HEX 收发、时间戳、后台文件记录、缓存导出和多窗口。

当前为开发版本，待实机验证，未同步 stable。
