# 全局 LLM 配置与协议适配验收

版本 0.13.0，2026-10-25。实现 `F08-F12-视觉与执行器设计.md` §6 和 `F11-F14-API与进程设计.md` §5.2 中的全局 LLM 基础能力。仍仅在本地开发，没有初始化 Git、提交、推送或发布。未读取用户已有服务凭据，未向真实 LLM 发送图片或提示词。

## 本轮实现

- `llm/config.py`：全局连接、模型、角色、按版本并发检查、同内容无操作、分页及过期游标检查。修改连接时核对角色引用；clean/clean_nsfw 保留，不可绑定执行。图片角色要求明确的图片能力。调用开始后持有内存配置副本，密钥按引用读取当前值。
- `llm/secrets.py`：DPAPI CurrentUser 加密，密文临时文件以当前用户独占 DACL 创建并原子替换，无明文落盘或非 Windows 降级。环境变量优先，空值显式禁用；接口不返回值、后缀或环境变量名。删除关联会报告环境覆盖仍存在，但不修改系统环境变量。
- `llm/adapters.py`：OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 的认证、系统指令、单图、文本提取和模型身份验证。Base URL 包含用户配置的版本前缀，程序追加具体资源路径。支持声明响应模型别名；没有声明时必须精确匹配。拒绝 URL 用户信息、查询串、片段、重定向以及不完整或结构错误的响应。
- `llm/service.py`：跨角色/跨事件循环共用 2 个并发槽；初始队列及后续槽位等待不扣请求网络预算。总网络/退避预算默认 300 秒，连接上限 10 秒，单次读等待上限 60 秒。密钥轮换、429 冷却、Retry-After、有限 408/网络/502/503/504 重试，退避释放槽位。401/403、普通 400 不轮换重试；文本超时不重发。重复 request_id 只保护本机同时在途请求，不承诺远端幂等。
- 取消在发送前为 not_sent，发送后为 unknown；关闭服务通知所有在途请求取消，丢弃迟到结果。网络不确定性不能等同远端未执行。
- `llm/contracts.py`：严格 JSON、重复键/非有限数/围栏/尾随文本检查、JSON Schema、本地额外字段限制、拒绝外部 schema 引用。一步最多一次 JSON 修复，RepairBudget 原子共享；不把原始错误响应拼进规则。`complete_json(..., repair_limit=0)` 可禁用修复，语义拒绝/invalid_input 留给调用方路由。传输重试与剩余网络预算跨修复共享。
- `llm/images.py`：内存 JPEG、EXIF 转正、透明底白色合成、不放大、不改原文件。2MP/q92 → 1.6MP/q88 → 1MP/q84 仅因允许的图片超时降档，每次从原始字节重新编码，降档共享传输重试预算。
- `llm/audit.py`：开始/结果追加 JSONL，以 attempt_id 关联；只存请求/连接 ID、角色、协议、模型、密钥序号、状态分类、重试、图档、时长及 UTC。不保存 URL、密钥、提示词、图片或响应。日志文件超过 4 MiB 时轮转一次，失败只累计内部警告数。
- `llm/templates.py` 与包内 `data/`：按固定 SHA-256 校验现有 R1 SFW 原文和输出 schema，提供内容与 hash 快照。运行时不读取任意磁盘路径、不执行 Markdown 代码。R1 NSFW、R2、R5 当前未提供，明确 unavailable；本轮未自行编造缺失合同。

HTTP 提供 endpoints CRUD、tokens 增改删、roles GET/PUT、models/probe、models/test-image，以及只读 templates 列表。POST 走会话幂等键；已有资源修改及探测检查 If-Match。探测是可取消后台 operation，提交结果时再次检查原版本，不能覆盖并发编辑。上游 401/403 转为安全的 `llm:unauthorized` 操作错误，不使本地会话失效。没有通用外部模型调用代理接口。

前端侧栏新增“LLM 连接与角色”：连接/模型/别名编辑、密钥新增与轮换、角色绑定、模型查询、固定图片检查、模板可用状态。连接草稿有离开保护；密钥仅在组件内存输入框暂存，提交后无论结果均清空并刷新状态，不写浏览器存储。

## 存储与使用

正常服务的非密钥配置为 `%APPDATA%/StyleLoraAutom/llm.json`，密钥为 `%LOCALAPPDATA%/StyleLoraAutom/secrets.json`，安全审计位于后者同目录。测试的显式 runtime/product 目录使用隔离子目录，不访问用户配置。裸 Runtime() 使用内存配置。

在工作台保存连接和模型不会请求上游。需要用户点击“查询模型列表”或“检查图片能力”才会访问指定地址；图片检查仅发送程序绘制的红色正方形/蓝色圆形图。回答必须正确识别位置、颜色、形状，HTTP 200 本身不代表通过。失败保留原配置；模型列表中已有手动配置也保留。缺少显式 input modalities 的新模型默认不声明支持图片，不从名称猜测。

环境变量映射规则为 token_ref 转大写且 `-` 替换为 `_`，再加 `STYLELORA_LLM_TOKEN_` 前缀；变量由用户在启动服务的进程环境配置。HTTP 返回的 token_ref 是引用 ID，不是密钥，也不包含密钥后缀。服务不改系统环境。

删除连接前须先解除角色绑定、显式移除密钥，以免删除连接时发生部分密钥清理。密钥写入结果未知时查询最新状态/版本，勿盲目重复写入。密钥状态不表示已向服务验证授权，只有显式诊断才发生认证请求。

## 协议依据与有意保留的边界

请求字段依据官方 [OpenAI Chat 创建接口](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)、[Responses 迁移说明](https://developers.openai.com/api/docs/guides/migrate-to-responses) 与 [Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)。Anthropic 固定 `anthropic-version: 2026-10-21`，按该版 [版本说明](https://platform.claude.com/docs/en/api/versioning) 不发送 temperature/top_p；传入不支持的采样项会在本地报错。兼容服务需要支持该合同。

只有用户声明支持原生 JSON Schema 且 schema 属于保守公共子集时，适配器才启用原生格式参数。复杂条件 schema（包括 R1 的成功/拒绝分支）仍通过提示词约束并在本地完整验证，避免把供应商不支持的 JSON Schema 关键字直接发送为原生合同。

模型发现目前接受完整的 data 列表（上限 1000）。服务若报告 has_more，不会将第一页伪装成完整结果，而是报安全诊断并保留配置；分页发现扩展待后续服务兼容验收。端点可继续手工录入模型。

此阶段的三协议合同使用本地 MockTransport 验证，另有真实本机 TCP HTTP 请求验证基础传输；不代表已通过用户供应商的真实模型行为/质量验收。在 0.13.0 阶段，F15 图片描述与分级持久任务、R2/R5 最终模板、F03/F06/F07 编排及 NAI 真生成链路仍属于后续阶段。`llm_config=true`，`llm_vision=false`。后续 F15 进展见 `21-F15-视觉描述与评级路由验收.md`。编号更正：F13 是美学评分，安装发布不属于 F13。

## 验证

测试覆盖 T09–T14 的核心边界：Windows 实际 DPAPI 和文件 DACL、环境覆盖/空值、配置重启/CAS、三种协议、固定模型身份、真实回环 HTTP、并发 2、冷却释放槽位、有限重试、取消、文本超时、图片降档、严格 JSON、一次修复、模型能力来源、图片特征识别、后台旧版本拒绝覆盖、敏感信息不回显、模板内容和打包。

最终结果：

| 检查 | 结果 |
|---|---|
| Python 全量回归 | 398 项通过；本轮新增 35 项 LLM/HTTP 测试 |
| SSE 主题增量复验 | LLM API 与事件模块合计 12 项通过 |
| 前端 Vitest | 35 项通过 |
| Edge 浏览器完整闭环 | 18 项通过，含新增 LLM 配置、密钥轮换、上游认证失败后本地会话保持、图片识别与草稿保护 |
| 前端 ESLint、设计边界、TypeScript、Vite | 全部通过；OpenAPI 类型已重新生成 |
| 本地 wheel 隔离回归 | 236 项通过，验证 35 个前端资源、SQL、版本和 R1 模板装载 |
| 既有 GPU 虚拟环境 | 补齐 jsonschema 后启动及模板加载通过；本轮未改 WD 推理实现 |

Windows ACL 首轮测试发现 SDDL 把本机 Administrator SID 写成 `LA`，已将断言改为比较真实 ACE 内的 SID，并验证只有一个当前用户 ACE 和受保护 DACL。不是放宽文件权限。

本地包：`.test-tmp/wheels/stylelora-0.13.0-py3-none-any.whl`，431,224 字节，SHA-256 `cd457000155f7f89d805cd3fa2f8856f39549b11a969bb524db9ca6d8568a10e`。仅生成在本地，没有上传。

日志保留于 `.test-tmp/v013-python-tests.log`、`.test-tmp/v013-e2e.log`、`.test-tmp/v013-wheel-tests.log`；界面截图 `.test-tmp/llm-settings.png`。测试临时项目和服务按既有夹具结束回收，浏览器证据留在本地。既有 FastAPI/Starlette 关于 TestClient 的依赖弃用提示未导致失败，本轮没有顺带升级 HTTP 客户端。
