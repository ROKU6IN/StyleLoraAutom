# F02 Launcher 官方接口与只读接入验收

日期：2026-10-28；版本：0.50.0。接续 [F02–F05 设计](F02-F05-生成后端设计.md) 和 [连接诊断](14-F02-MCP连接诊断实现与验收.md)。本轮找到官方源码与下载，按固定版本实现独立的只读设置/运行状态接入。仅本地开发，无 Git 初始化、提交或上传；未下载、安装或运行 Launcher 二进制，未调用真实生成服务。

## 官方来源与下载

- 源码：[Aaalice233/Aaalice_NAI_Launcher](https://github.com/Aaalice233/Aaalice_NAI_Launcher)，MIT 许可证。
- 本轮核对的最新发布：[v4.3.1](https://github.com/Aaalice233/Aaalice_NAI_Launcher/releases/tag/v4.3.1)，发布日期 2026-10-23 UTC，构建号 46。
- [Windows 便携版 ZIP](https://github.com/Aaalice233/Aaalice_NAI_Launcher/releases/download/v4.3.1/NAI_Launcher_Windows_4.3.1%2B46_Portable.zip)：105115142 字节，发布元数据 SHA-256 `419c93a2b39d34c7517f29add735bb5878cfef1e2f2b2a4724d1fd361ba3260b`。
- [Windows 安装版 EXE](https://github.com/Aaalice233/Aaalice_NAI_Launcher/releases/download/v4.3.1/NAI_Launcher_Windows_4.3.1%2B46_Setup.exe)：77999323 字节，发布元数据 SHA-256 `fd28c412cffb07ea8d8bd6534a6832387083d4c9b9afdba6b623c61f80f8a78b`。
- [发布校验文件 checksums.txt](https://github.com/Aaalice233/Aaalice_NAI_Launcher/releases/download/v4.3.1/checksums.txt)。上述二进制摘要来自发布元数据，本轮没有下载二进制进行复算，不代表实机验收。
- [官方 MCP 接入文档（固定提交）](https://github.com/Aaalice233/Aaalice_NAI_Launcher/blob/60b2129308de8b04df0798dee36ce21e7e6f3872/docs/mcp_server.md)。官方服务默认关闭，在“设置 → 集成 → MCP”开启，仅监听本机。

本轮 main 与 v4.3.1 均解析到 `60b2129308de8b04df0798dee36ce21e7e6f3872`；实现固定此提交，不随 main 自动信任新版本。26 个参考文件下载到 `.test-tmp/launcher-4.3.1-source/`，逐一用 `SHA1("blob " + byte_length + NUL + bytes)` 对照官方提交 tree 的 Git blob 摘要。另复算 SHA-256，完整路径、大小、两种摘要见 [源码审阅清单](launcher-v4.3.1-source-review.json)。清单明确 SHA-256 为本地复算值，不冒充发布者声明。

## 核对的实际接口

以下路径均相对此固定提交：

| 官方源码 | 核对结论 |
| --- | --- |
| `lib/core/platform/launcher_discovery_directory.dart`、`lib/core/mcp/mcp_discovery_file.dart` | Windows 使用 `%APPDATA%/nai-launcher/mcp-server.json`；HTTP 端点、令牌、版本和协议从发现文件读取，沿用既有回环地址/大小/链接校验。 |
| `lib/core/constants/app_version.dart`、`lib/presentation/mcp/providers/mcp_server_notifier.dart`、`lib/core/mcp/mcp_launcher_server.dart` | 初始化的服务名是 `nai-launcher`，服务版本来自 `AppVersion.versionName`，本轮固定 4.3.1。 |
| `lib/presentation/agent_chat/services/generation_tool_definitions.dart` | `get_generation_settings` 和 `get_generation_status` 均为无参数 object：`{"type":"object","properties":{},"required":[]}`。 |
| `lib/core/mcp/mcp_tool_adapter.dart` | `ObjectSchema.fromMap` 保留工具参数；未声明 outputSchema。工具 JSON 文本转换为 structuredContent，客户端同时支持结构化内容与单文本 JSON。 |
| `lib/presentation/mcp/services/mcp_external_tool_registry_factory.dart` | 外部 registry 给部分 prepare/inspect 工具添加展示选项；本轮两个只读工具的参数结构保持原样。 |
| `lib/presentation/agent_chat/services/generation_settings_service.dart` | 设置返回为平铺字段；包含模型/effort/固定字段列表/采样参数/可用模型，不能按旧夹具的 settings 包装读取。 |
| `lib/core/constants/api_constants.dart` | UC None 的整数 API 值为 **3**，不能直接发送本地 profile 的字符串或 JSON null。 |
| `lib/presentation/agent_chat/services/generation_status_service.dart` | 运行状态为 generation/queue 两组；百分比是整数；错误与最近图片引用不适合直接透传到本项目。 |

同时核对 `pubspec.lock` 锁定的 `dart_mcp 0.5.2` 源码归档，SHA-256 为 `852b51da915d679be8d051e2e3b6c68ed19fe685f1c676bb35272a2d73ceac7a`，与锁文件一致。`lib/src/api/tools.dart` 的 ObjectSchema.fromMap 是 Map 包装，不补默认字段；`lib/src/api/api.dart` 包含 `2025-11-25`。只读兼容检查固定此协议；没有安装或执行 Dart 依赖。

## 本轮实现

- `backends/nai_launcher/readonly.py` 建立独立、最小只读契约，精确核对发现版本/服务版本 4.3.1、服务名、协议及每项工具 schema。开始前同时检查两个工具，每次派发和重连重试再次检查。新增无关工具不授予额外权限；未知版本、缺少工具或结构变化拒绝。生产 `REVIEWED_CONTRACTS` 仍为空，普通 `call_tool` 仍不接受未审核的生成契约。
- `McpClient.call_readonly` 只接受固定的两个工具名和空参数，沿用已有时限、并发限制与最多 3 次额外读取重试。不能调用 prepare、submit、update、history 或任意服务端工具。
- 依次读取设置和状态。同一个会话内的两次读取仍是不同时间点的概况；发生会话代次变化则整份结果丢弃，不能拼接重启前后的结果。
- 输出只含允许的模型/采样/尺寸/固定字段与队列计数，严格验证类型、长度、范围、有限数值；剔除路径、种子、提示词、错误文本、图片引用、模型展示名称及其它未知字段。允许字段中含实际连接令牌也拒绝采纳。错误仅返回固定原因码，不返回上游原文。
- `POST /api/backends/nai_launcher/inspect`：严格空 JSON、无查询参数、要求本机鉴权和 Idempotency-Key，返回后台 operation。并发读取复用尚未结束的任务；同键重放原受理结果。取消沿用 operation 的 If-Match 机制，结束后关闭 MCP 会话，取消保留上次成功或失败快照。
- `GET /api/backends` 新增 `inspection` 和 `inspection_operation_id`，仅读内存缓存，不读取发现文件或联网。新检查失败时发布 unavailable 和错误码，清除旧设置，避免冒充最新成功；重启后回到未检查。原连接 probe 的结果字段和行为保留，读取成功不会把生成 capabilities 标成可用。
- 工作台“Launcher / MCP”增加“读取 Launcher 状态”、取消、最近读取时间、模型/采样设置、队列概况、可用模型和固定字段，并提供官方源码/发布页/Windows 下载链接。页面加载与轮询只读本地缓存；发起请求后才连接 Launcher。明确显示只读成功与生成未启用。

## 使用

1. 从上方官方链接取得 v4.3.1，启动后在“设置 → 集成 → MCP”开启服务。
2. 本项目运行 `./scripts/run.ps1 serve`，进入“Launcher / MCP”。“检查连接”只握手和列工具；“读取 Launcher 状态”执行上述两项只读操作。
3. 成功后查看模型与队列。界面内容是历史快照，需要更新时再次显式读取。无需向本项目页面输入令牌。
4. 版本或结构不匹配时保留拒绝状态，不能通过修改发现文件版本号来获得真实兼容性。

新增错误码：`backend:readonly_version_unreviewed`、`backend:readonly_schema_mismatch`、`backend:readonly_invalid_response`、`backend:inspection_session_changed`。读取失败仍产生已完成的诊断任务，诊断结果自身为 unavailable；取消为 canceled。任务系统异常仍按已有 failed 处理。

## 真实生成仍需解决的差异

这不是“找到仓库后已完成全部生成适配”。源码审阅确认：

1. `update_generation_settings` 不支持原子 expected/CAS 或 revision。当前设计要求在写入的同一个原子操作核对原值，避免覆盖外部修改；不能用“先读后写”或本地锁冒充该保证。本轮不放宽恢复设计。
2. prepare 的快照字段为 `parameters`，并有外部 compact/`include_parameters` 行为；旧合成夹具的 `effective` 字段不能直接用于真实接口。必须逐项映射 UC、模型固定字段、prompt/quality/尺寸、估价和产物归属。
3. preparation 状态只包含 prepared/submitted/cancelled 且保存在进程内。submitted 和全局队列空闲均不证明某次 attempt 完成，也不能证明超时写入不会稍后生效。需补充针对具体任务的持久对账证据。
4. 官方支持在 prepare 中携带部分请求参数，未来需要先确认是否足够替代全局设置。后续 0.51.0 已核对：模型、Effort、采样与固定参数不在 prepare 输入中，参考图和 batch_size 仍继承页面，当前不能通过完整单次请求绕过全局设置；详见 [来源参数预检验收](58-F03-来源参数预检验收.md)。不把只读检查当生成验收。
5. 还需要真实 Launcher 程序的只读实机验收，以及解决上面契约差异后的真实生成、产物与恢复验收。本轮全部运行值为注明来源的合成数据，不消耗 Anlas，也不自动安装或启动外部应用。

## 验证记录

新增 Python/API 24 项，覆盖版本、名称、协议和 schema 门禁、未审核生成契约隔离、只读名单、秘密过滤、字段异常、文本结果、effort、重连重新审核、混合会话丢弃、有界重试、缓存/幂等/取消、严格请求/鉴权及真实回环 HTTP 的两次读取和会话关闭。与既有 MCP/连接 API 回归共 **54 项通过**（3.459 秒），日志 `.test-tmp/launcher-readonly-targeted.log`。初轮一个测试把缺少 Idempotency-Key 的预期写为 422，按既有合同修正为 428；生产行为未改。

前端新增 8 项，覆盖显式触发、下载链接、快照、错误、取消版本、防重复发起与重新鉴权；全量 **211 项通过**（31 个文件，6.94 秒），日志 `.test-tmp/launcher-readonly-web-full.log`。ESLint、设计边界、TypeScript 和 Vite 构建通过。

Python/API/真实 Windows 进程全量 **1232 项通过**（302.215 秒），日志 `.test-tmp/launcher-readonly-python-full.log`。加上前端，共 **1443 项独立测试，本轮新增 32 项**。OpenAPI 声明已重新生成，包含 inspect 路由，更新后 TypeScript 校验通过。

本地 wheel 完整隔离复验 **1066 项全部通过**（208.841 秒），日志 `.test-tmp/launcher-readonly-wheel-full.log`，包含本轮全部 24 项 Python/API 新用例。包内容核验确认 **164 个 Python 源文件、45 个前端资源**、SQL、运行时规则、版本和隔离导入与当前源码一致。专项与包内复验和源码全量有重叠，不重复计算独立测试数。

交付包 `.test-tmp/wheels/stylelora-0.50.0-py3-none-any.whl`，**696399 字节**，SHA-256：`dae61f825db39ba97739d912e817c512a43d303d9526a353a1dbe39b3ec93ef2`。离线使用本地依赖构建，未初始化、提交或上传 Git。完整 Launcher 程序与真实生成验收仍未执行。
