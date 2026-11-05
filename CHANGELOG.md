# 版本记录

## 2026-11-05 13:42 — FreeBCI Serial Assistant 1.8

- **修改内容**：将应用、窗口及 exe 名称统一为 `FreeBCI Serial Assistant`，去除显示名称中的 debug 字样；产品版本更新为 1.8，使用黑色 USB 与脑电波形透明图标，更新运行资源清单、构建属性、文档和截图。
- **本版本能力**：与已验收功能版本保持相同的通信与记录逻辑；38 项自动测试和 Qt 功能模拟通过，运行模块及图标与对应发布源码一致，独立目录的源码回环和 exe 启动检查通过。
- **已知问题**：额外设备及另一台电脑尚未复测。

## 2026-11-05 13:02 — 更新银白 USB 脑电图标

- **修改内容**：新增银白蓝色 USB 与脑电波形图标，应用窗口及 exe 打包流程使用统一的多尺寸 ICO；运行文件清单补充图标资源。保留 `Serial Assistant` 应用名称、`串口助手` exe 名称及 1.7 debug 版本标记。
- **本版本能力**：38 项自动测试和 Qt 功能模拟通过，确认自定义窗口图标加载、原有名称保留及既有收发、扫描和波特率切换模拟流程正常。
- **远端范围**：GitHub `caf429b8cf5d444fc5b75d4d1d1b9d00c6c8b884` 仅提交 ICO、窗口图标加载及打包参数；配套检查与文档调整保存在本地开发快照。
- **已知问题**：待实机验证，未同步 stable。

## 2026-11-02 18:47 — 1.7 debug 动态通道校验与独立程序

- **修改内容**：移除固定 `[8,16]` 通道限制，校验非空、无重复的正整数通道列表及列表内当前通道数；名称按 `supported_channels` 动态显示，悬停详情单独显示 `active_channels`。更新版本标记、下载说明及源码文件清单，构建 Windows x64 单文件 exe。
- **本版本能力**：38 项自动测试与 Qt 功能模拟通过；原有 Qt 界面回归及 8 MiB 模拟记录逐字节核对通过。exe 冻结模块的 4 组动态通道检查与独立路径启动、响应、正常关闭检查通过。
- **已知问题**：待实机验证，未同步 stable。

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
