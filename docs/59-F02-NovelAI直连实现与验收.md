# F02–F05 NovelAI 直连实现与验收

版本：0.52.0；日期：2026-10-28。

## 路线调整

用户确认改用本项目直连 NovelAI。落实 `03-模块化拆分分析.md` 中原先预留的 `NaiDirectBackend`，不再以修改 Launcher 为生成前置条件。既有 `nai_launcher` 草稿、只读诊断和设置租约测试保留；已锁定来源不会迁移，需新建 `nai_direct` 来源。

本轮不安装或调用 Launcher，不请求真实 NovelAI 生成，不上传 Git。实现可用不代表完成真实账户、扣费及模型质量验收。

## 已实现范围

- 独立 `backends/nai_direct` HTTP 后端和 `generation/direct.py` 串行调度，接入既有来源交接、启停、费用确认、图片检查、原子入库、审图及导出。
- 工作台新草稿默认直连，V5 Full high、Euler Ancestral、Karras、23 步、Guidance 5、Rescale 0。同时支持 V4.5 Full；步数 1–50、Guidance 1–10、Rescale 0–1，每边 64–1536 且为 64 倍数。这是本项目首版支持子集，不宣称是服务端全部能力。HTTP 客户端应明确传入 `backend: nai_direct`；省略字段的历史默认值继续兼容 Launcher。
- 每个请求明确正负提示词、模型、采样参数、随机 seed、尺寸和 `n_samples=1`。关闭质量增强、UC 映射为 None/3；无引用图、角色、图生图、流式请求、放大或批量继承。固定参数写入请求不等于服务器提供完整 effective 参数证明。
- 浏览器“NovelAI 直连”页保存/删除 Persistent API Token，使用已有 Windows CurrentUser DPAPI 与用户私有 DACL，独立 `nai-direct/secrets.json`。`STYLELORA_NAI_TOKEN` 环境变量优先；密钥不放进项目、恢复回执、日志或前端查询缓存。
- GET 只读本地状态；明确点击才查询订阅和两类 Anlas 余额。删除本机保存值不删除环境变量。生成或账户查询期间阻止修改 Token。
- Token 文件替换可能成功而确认失败时返回 `outcome=unknown`，更新凭据版本并清除旧账户快照；不把未知写入误报为确定失败。
- 仅调用固定的 `https://image.novelai.net/ai/generate-image` 与 `https://api.novelai.net/user/subscription`，关闭重定向、环境代理和自动重试。测试 transport 仅在 Python 内部注入，不接受 HTTP/项目提供的任意 URL。
- 接收 201 ZIP、单 PNG；响应上限 40 MiB、解压图片上限 32 MiB。验证成员数、路径、加密/链接、CRC、签名；从字节读取，不解压服务端路径。后续复用 F05 完整解码、尺寸、摘要、Comment 与原子入库检查。

## 费用合同补充

公开接口没有本项目已审核的单次报价接口。账户余额、Opus 等级或尺寸不能作为“本次免费”的保证，尤其 V5 的额度还会变化。本轮不移植 Launcher 的估算公式。

直连传 `estimated_anlas=null` 与内部 `unknown_cost=True`；仅 `nai_direct` 来源接受这个组合，既有整数合同保持。持久费用回执增加兼容版本 2，允许 null；版本 1 继续只接受整数。未知不是 0，不伪造扣款数或免费资格，不修改数据库结构。

- `deny`：费用未知时暂停，POST 为零。
- `ask`：暂停并呈现费用未知，用户确认当前运行周期后才派发；不是单图授权或金额上限。
- `allow`：按用户的显式策略授权派发。
- 暂停/策略变化/重启后的周期确认仍按原合同失效。
- 汇总显示未报价次数和不完整总额；未知金额不计作零，实际扣款始终未声明。

## 持久性与恢复

1. 检查能力和 Token，在项目内持久费用意图并将 item 原子标记为 sent。
2. 发送前写 `images/.nai-direct/<item>/<attempt>.json`，记录来源/请求/配置/契约摘要、seed、wire payload 摘要及费用 operation key；不含 Token。
3. 单次 POST。暂停/取消允许已经授权的一次请求落盘，停止下一次提交；串行请求至少间隔一秒。
4. 原子保存到既有 `.pending` 路径，持久 response 回执中的图片 SHA-256 后才调用 F05 登记。入库后观察费用提交事实，关闭回执。
5. 进程/项目重开后，有完整且绑定正确的 response 回执才允许显式本地核查入库。恢复不需要 Token，也不访问上游。数据库已登记时核对图片摘要，不重复创建。
6. 超时、掉线、非成功 HTTP、损坏响应或“图片已保存但回执未写成”，均可能已经执行/收费，保留 sent/unknown 并暂停，不自动或手动走 failed-item 重试。文件名、余额差异、相关 ID 均不作服务端幂等或结清证明。

直连没有共享页面设置，因此不获取或伪造 Launcher CAS/租约；Launcher 恢复记录不阻止直连。0.52.0 没有人工处置入口；后续 0.57.0 已增加 [人工结束来源并保留未知记录](64-F02-直连未知请求人工处置验收.md)。仍不提供强制判定“未执行”或重复购买入口。

## HTTP 与使用路径

- `GET /api/nai-direct`：凭据可用性、缓存账户、支持配置与版本。
- `PUT/DELETE /api/nai-direct/token`：原版本 `If-Match`；不会回显 Token。
- `POST /api/nai-direct/account/probe`：带 Idempotency-Key 的本地后台查询任务。
- `GET /api/backends/nai_direct/capabilities` 与既有 account-status 来源费用汇总。
- 生成继续使用 `/api/projects/{pid}/sources`、handoff-preview、start、control、confirm-cost 和 recovery。

使用时先在“NovelAI 直连”配置 Token；在来源页新建直连草稿，准备已就绪提示词任务，保存并预览交接，确认启动。默认询问费用，需再次确认周期才能发送。接收图片后沿用标注、审核、导出入口。

## 来源依据

- [官方 API 文档](https://image.novelai.net/docs/index.html) 及 [公开 schema](https://image.novelai.net/docs/doc.json)：请求字段、Bearer Token、ZIP/201、订阅结构。2026-10-28 通过文档读取工具核对；直接下载 schema 返回 403，未声称保存了完整原始副本。
- [官方 Effort 说明](https://docs.novelai.net/en/image/effort/)：V5 medium 的固定步数与负向限制，本轮明确不开放该档位。
- [官方 FAQ](https://docs.novelai.net/en/faq/)：V5 Opus 使用额度不等同于所有请求免费。
- 模型 ID 与 UC None/3 另交叉核对已固定提交、已校验摘要的官方 Launcher 源码，见 `57-F02-Launcher官方接口与只读接入验收.md`。本轮独立实现 HTTP，不复制 Launcher 实现。

## 验收记录

新增 35 项 Python/API 测试通过，包括真实本机 HTTP 流、服务端收到请求后断线、Windows DPAPI 持久化、请求参数、ZIP 边界、敏感字段过滤、未知费用、版本冲突、禁止重发、故障注入、Token 替换确认丢失和重新打开后的恢复。沙箱内 DPAPI 被权限限制，沙箱外复验通过。

新增 6 项前端测试覆盖只读加载、密码字段与保存后清空、原版本提交、显式账户查询、失败不重试和环境变量优先级。前端全量 **225 项 / 33 文件通过**（7.26 秒），ESLint、设计边界、OpenAPI 类型生成、TypeScript 与 Vite 构建通过。

源码 Python/API/Windows 全量 **1285 项通过**（322.987 秒，`.test-tmp/nai-direct-python-full.log`）。之后补充的后端隔离与 Token 写入未知两项测试，连同全部直连和相关 Launcher/API 用例末次 **61 项通过**（11.878 秒，`.test-tmp/nai-direct-final-targeted.log`）。当前实际收集 1287 项 Python 测试，连同前端共有 **1512 项独立测试，本轮新增 41 项**；不把专项或包内重复运行再次累计。

最终 0.52.0 wheel 完整隔离复验 **1121 项通过**（221.332 秒，`.test-tmp/nai-direct-wheel-final.log`），已包含最后的 Token 写入未知处理。包内核对 **171 个 Python 源文件、46 个前端资源**、SQL、运行时规则、版本与隔离导入一致。构建时发现旧的 `build/lib` 前端资源残留，核对绝对路径后清理该构建缓存并重新离线构建，最终包未混入旧资源。

交付包：`.test-tmp/wheels/stylelora-0.52.0-py3-none-any.whl`，**715070 字节**；SHA-256 `8605a3234a1b7ac18676cf4a60b35c91efe310d0329fa70797c26f3a87a1a5fc`。未初始化、提交或上传 Git；未调用真实 NovelAI 生成和收费服务。
