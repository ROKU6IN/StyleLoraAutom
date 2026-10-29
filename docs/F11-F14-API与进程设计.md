# F11–F14 本地 API、进程与 CLI 设计

实现补充（0.59.0）：产品默认配置 schema v2 与前台独立 `session` 已接入；独立写操作在同一自有 Runtime 中预览、确认、执行与控制，EOF 排空关闭，不跨会话重绑计划。下文旧阶段的“独立写入未实现”由本补充取代，详见 [本地综合验收](66-本地功能收尾与综合验收.md)。

实现补充（0.58.0）：§9 独立服务容器第一阶段已接入 `source list/status/items/item/cost-state`、`job list/status/outcomes`、`import summary`、`export list/status/report` 和当前项目 `storage scan`。显式 `--standalone --project PATH` 持产品锁、通过 Runtime/项目打开协议工作，复用进程内 ASGI 路由与服务校验；无端口或控制管道。列表在本次容器内读完分页，退出不保留可重用的会话授权；成功、中断和失败均排空并关闭自有 Runtime，关闭失败保留排他并返回未知。预览/确认/提交、生成与标注执行、控制写入、清理及产品级命令尚未接入独立模式，继续使用既有长驻服务。见 [独立查询与扫描验收](65-F11-独立CLI查询与扫描验收.md)。

实现补充（0.57.0）：`GET/POST /api/projects/{project_id}/sources/{source_id}/unknown-disposition` 提供直连未知请求的本地核对摘要和显式结束决定。POST 要求会话鉴权、项目句柄、Idempotency-Key、当前 If-Match 及两个严格 true 确认；同步完成本地持久化，不受理生成任务。记录 prepared/completed 可重开后读取并明确完成，GET 不自动修复；无 Token/网络需求。见 [直连未知请求人工处置验收](64-F02-直连未知请求人工处置验收.md)。

实现补充（0.56.0）：§9 的 `doctor --probe-network` 已实现为只读网络诊断，显式使用配置中的凭据访问 LLM `/models` 和 NovelAI 直连账户读取接口；不执行模型测试/生成、不刷新配置或缓存。`--network-timeout` 为整个网络阶段共用的 5–120 秒预算（默认 30）；最多 16 个 LLM 端点和一个直连账户，不重试、不轮换失败凭据、不跟随重定向、不使用环境代理。默认与仅 `--verify-content` 均保持离线。Launcher/MCP 仍使用既有专用检查入口，本次未加入通用网络诊断。见 [显式网络诊断验收](63-F11-显式网络诊断验收.md)。

实现补充（0.55.0）：§9 doctor 增加显式 `--verify-content`，核对已安装的内置审核模型文件完整 SHA-256；传入 `--project` 时，对无占用、无 WAL/SHM/journal/恢复痕迹的项目执行只读 SQLite 完整性、版本、内置结构、CHECK 约束及外键检查。`--content-timeout` 为每个模型/项目的协作式截止时间（5–600 秒，默认 120）；不下载、推理、启动服务、恢复或修复。默认仍是轻量元数据诊断。边界与验收见 [离线内容核验验收](62-F11-离线内容核验验收.md)。

实现补充（0.54.0）：`ui`、`project init/open` 增加显式 `--start-service`（以及 5–120 秒的 `--startup-timeout`）。无实例时隐藏后台启动长驻 `serve --no-browser`，已有实例时附着；先验证当前用户管道身份与同实例 HTTP 就绪，再按原有 grant/HTTP/浏览器引导流程执行。并发启动由产品互斥锁裁决，超时/Ctrl+C 不杀服务，不自动重发业务请求。原请求重放标识和 standalone 模式不能与启动选项混用；状态查询、doctor 及其它 CLI 不隐式拉起服务。通用独立服务容器仍待实现。详细命令、生命周期和验收见 [按需启动服务验收](61-F11-按需启动服务验收.md)。

实现补充（0.49.0）：A057/A058 单条查询与人工重试已通过 HTTP 和 `source item/retry-item` 实现，保留 F01 的 attempt 幂等与 done→paused 事务。`source update` 使用既有完整 Draft PUT，必须携带原版本；显式请求键绑定草稿/条目请求和 If-Match，响应丢失可核对原请求，跨实例拒绝旧请求。浏览器既有不带键的 PUT 仍兼容；本轮未增加 DDL、自动生成或自动重试。见 [来源编辑与失败项重试验收](56-F11-来源编辑与失败项重试验收.md)。

实现补充（0.48.0）：§9 的 export 服务路径已实现为 preview/run 两阶段命令，另有 list/status/report/cancel。预览固定实例、项目、方案与请求键；run 要求计划摘要，触发词警告和硬链接分别要求对应摘要，不能由预览文件中的隐式标志替代。status/report 使用持久 export_id，cancel 默认等待实际终态，不将取消请求等同于已取消。保留原 export PROJECT 离线入口。详见 [服务化导出 CLI 验收](55-F11-服务化导出CLI验收.md)。

实现补充（0.47.0）：§9 的 import 服务路径已实现为 preview ROOT/run --preview 两阶段命令，并提供 status/summary。原始目录只经 ACL 管道授权，HTTP 使用当前实例/项目的 grant；run 审核摘要并固定请求键，后台导入完成与持久来源状态分别核对。source pause/resume/cancel 已支持导入来源，import-root 提供重新授权与完整输入验证。原 import PROJECT ROOT 离线入口保留；导出服务化、失败项重试和通用独立容器仍待后续。见 [服务化导入 CLI 验收](54-F11-服务化导入CLI验收.md)。

实现补充（0.46.0）：§9 的 nai 来源命令已通过现有 HTTP 服务实现；create、preview/run/reconcile、list/status/items、控制及费用命令见 [生成来源 CLI 验收](53-F11-生成来源CLI验收.md)。run 使用保存的交接预览和确认摘要，不直接接受任意 seeds 文件；暂停来源用明确 resume。导入来源创建/控制及 retry-item 仍待后续。confirm-cost 的 cycle-id 沿用 F01 的 32 位小写十六进制周期标识（非 UUID）。set-cost-policy allow 和创建 allow 草稿要求显式 confirm-allow，费用 ask 不自动同意。

实现补充（0.45.0）：§9 prompt 命令组已通过产品级 HTTP 服务实现，不要求项目。preview --spec 接受 SamplingRequest；run --spec 接受保存的预览计划并要求 confirm-digest，冻结服务返回的 random_seed、任务编号和请求键。提供任务列表、状态、逐条结果、修订号控制及 seeds 内容投影；seeds 要求显式 expected-revision，成功 stdout 不携带产品引用或 CLI 元数据。沿用已有 ready/部分失败/暂停语义；不自动重试或跨实例重放旧计划。详见 [提示词任务 CLI 验收](52-F11-提示词任务CLI验收.md)。

实现补充（0.44.0）：§9 的 job 命令组已附着既有 HTTP 批次服务。为明确确认冻结范围与配置，执行器/mode/ids/filter/config 参数放在 job preview，job run 使用保存的预览 JSON 与 confirm-digest；计划预分配 batch_id 和请求键，同计划再次运行只重放原请求。支持单 job/整批 status、pause/resume/cancel、list 与分页 outcomes。默认等待整批完成，保留部分失败、暂停和依赖阻塞语义；无独立模式或自动重试。详见 [标注任务 CLI 验收](51-F11-标注任务CLI验收.md)。确认摘要是客户端计划一致性检查，服务仍按现有范围摘要、CAS 和冻结合同执行。

实现补充（0.43.0）：§9 的 project init/open/close/status 已通过现有 HTTP 项目服务实现。默认要求先运行 serve；open 不隐式启动进程。init --standalone 显式持产品锁，复用与 HTTP create 相同的 F01 创建/关闭服务，不创建完整 Runtime 或提供通用独立容器。默认等待，支持 detach 和任务查询；关闭绑定当前句柄与版本，未知请求保存原请求键、grant、版本与实例，跨重启拒绝重放。详细参数、退出码及边界见 [项目管理 CLI 验收](50-F11-项目管理CLI验收.md)。其它领域专用 CLI、自动拉起 serve 及通用独立服务容器仍待实现。

实现补充（0.42.0）：§9 doctor 的离线本地阶段已实现，`doctor --project PATH? --json --runtime-dir PATH? --product-dir PATH?`。独立只读检查，不附着/启动服务、不调用 ProjectService.open，不打开项目 SQLite、迁移或恢复；只读容器与文件头的边界见 [doctor 验收](49-F11-本地诊断命令验收.md)。serve 与 doctor 共用目录解析。未提供 --probe-network 或修复选项，模型仅做清单和大小检查，不声明哈希或推理已通过。

实现补充（0.41.0）：A065 `POST P/images/{image_id}/verify-file` 已实现，严格空 JSON、If-Match 和请求键，返回受生命周期管理的后台任务。仅核验用户已放回原位置的文件，大小/SHA-256 一致后解除 missing；保留审图/评级/标注。没有上传或任意路径参数。见 [原图恢复核验验收](48-缺失原图恢复核验与工作台验收.md)。

实现补充（0.40.0）：§9 存储 CLI 已通过当前用户管道附着已有服务，提供 scan/cleanup/summary/status/results。清理确认显式要求 project_id、preview_id 和 confirm-digest，确认阶段不重新预览；受理输出可查询的任务标识，Ctrl+C 只停止等待，未知写入保留原请求键且不自动重试。无实例退出 3；本阶段不提供独立容器或路径模式，不自动打开/切换项目。见 [存储 CLI 与后台任务验收](47-存储CLI与后台任务验收.md)。

实现补充（0.39.0）：A009/A010 本阶段仅开放 ProductConfig 的 storage_thresholds，严格白名单、CAS、请求键、原子文件替换和 no-op；其它统一产品配置字段未开放。新增只读 `GET /api/storage/summary` 用于跨页提醒，A122 保留项目摘要并增加 config_changed。扫描冻结配置版本和阈值。见 [存储阈值与提醒验收](46-存储阈值配置与跨页提醒验收.md)。

实现补充（0.38.0）：A039 最近项目列表、A041 的 known_id 重开、A118/A119 已知项目只读统计已实现。known_id 跨重启持久，project_id 仍是打开句柄；列表签名游标绑定历史版本，扫描游标绑定 scan_id 和排序，错配均沿用 422 page:invalid_cursor。跨项目扫描不要求打开当前项目。见 [最近项目与跨项目存储验收](45-最近项目与跨项目存储验收.md)。

实现补充（0.37.0）：A117/A120/A121/A122 当前项目扫描、清理预览/确认与历史摘要已实现；清理逐项审计到 `.cleanup/`，媒体与导出共用项目文件锁。A118/A119 跨项目汇总仍待最近项目记录。详见 [存储验收](44-项目存储扫描与安全清理验收.md)。

实现更新（0.36.0）：新增来源范围 GET recovery、GET recovery/{item_id}、POST recovery 三条接口，细化 A046/A047 的生成条目恢复；原项目级 GET recovery 摘要保持兼容。POST 带条目恢复 If-Match、请求键和显式 restore 确认，返回 202 Operation；后台执行前再次校验并占用全局调度槽位。见 [恢复工作台验收](43-F02-F05-恢复工作台与HTTP验收.md)。

实现补充（0.35.0）：注入式 SerialDriver 已接入既有 start/control/confirm-cost 的后台派发约定，dispatch 返回前保留项目引用；关闭项目通知调度停止接收新图，并等待已授权结果结清。只读对账和设置恢复暂为内部接口，生产无 driver，不增加恢复 HTTP/UI。详见 [调度验收](42-F02-F05-串行调度与未知结果对账验收.md)。

版本：设计稿 v0.1；日期：2026-10-12。仅设计合同，不含实现代码。本文件不设计前端组件、布局或交互样式；前端设计归 `docs/F14-前端设计.md`，本文定义其消费的接口。

## 1. 权威来源、范围与版本口径

实现补充（0.34.0）：增加来源 `GET cost-state`、`PATCH /sources/{id}`（费用策略）和 `POST confirm-cost`，并扩展 account-status 的项目/来源本地估算查询，见 [费用验收](41-F04-F21-费用策略与持久估算验收.md)。确认路由用 If-Match、周期及会话 Idempotency-Key，返回 200 SourceView，覆盖早期 A055 的 202 草案；实际生成仍由未来 driver 排队。周期 token 沿用 F01 的 32 位十六进制标识。C06 已实现，账号余额/订阅/体力仍未知，真实生成未开放。

实现补充（0.33.0）：新增 `GET /api/backends/{backend_id}/settings-lease`，只读本地 F03 恢复状态，不连接 Launcher，不创建或修改恢复文件，无需打开项目。响应含 blocked、phase、reason、inflight、revision、version、source_id、changed_keys、recovery_adapter_available；不返回设置原值/目标值、身份或路径。未知后端 404，多余查询参数 422；沿用 Bearer/Host/Origin 校验，没有恢复写入或强制清除路由。损坏记录返回 blocked=true、phase=invalid，版本与来源置空，不影响项目查看。细节见 [F03 验收](40-F03-设置租约与恢复日志验收.md)。

实现补充（0.31.0）：A023–A027 已实现，见 [F24 自定义尺寸预设验收](38-F24-自定义尺寸预设验收.md)。本轮尺寸/名称格式错误统一沿用现有严格 DTO 的 `422 schema:invalid`，覆盖下表早期的 `preset:invalid_size` 草案；内置改删仍为 `409 preset:builtin_locked`。创建、更新、删除要求 Idempotency-Key，更新/删除要求 If-Match；请求去重仅在当前进程会话有效，配置及资源版本跨重启持久化。没有增加数据库表或开启真实生成。

已完整阅读的依据：

| 简称 | 文档及本设计使用范围 |
|---|---|
| 05 | [项目结构与数据模型](05-项目结构与数据模型.md)，尤其 §3/4/11；架构、目录、状态机、单项目与安全约束 |
| 04 | [工作流程与架构草案](04-工作流程与架构草案.md) §4 F01–F24；仅作功能编号/交付阶段参考，不继承旧流水线 |
| D | [F01 project.db](F01-project-db-设计.md)，DAL、事务、恢复、迁移、CAS |
| G | [F02–F05 生成后端](F02-F05-生成后端设计.md)，源运行、费用、能力与收据 |
| P | [F06–F07 提示词生产](F06-F07-提示词生产设计.md)，产品字典、提示词任务与完整 seeds |
| V | [F08–F12 视觉与执行器](F08-F12-视觉与执行器设计.md)，LLM、jobs、设备、扫描与重新分级 |
| E | [F09–F10 标注编辑与导入导出](F09-F10-标注编辑与导入导出设计.md)，编辑、规则、方案、导出和标签库 |

优先级为本次用户明确约束 → 05 及其已采纳 D 决议 → 各 F 模块规格 → 04 旧草案。本文统一传输合同，不改领域状态边、8 张业务表/93 列、单写连接或模块算法；未采纳的 C06–C16 不因出现在 API schema 中就启用。

固定约束：Python 包为 `src/stylelora/`；FastAPI 单进程、单 Uvicorn worker、只绑定 `127.0.0.1`；一次打开一个项目；SQLite WAL、线程归属固定的单写连接；长任务走受生命周期管理的后台任务及 SSE。产品配置为 `%APPDATA%/StyleLoraAutom/`；DPAPI 密钥、Launcher 恢复日志和 WD 模型为 `%LOCALAPPDATA%/StyleLoraAutom/`；随机 API token 每次启动生成且不落盘。Tauri 后期接入同一服务接口，当前用浏览器。

API 总表中的 MVP 指完成项目/导入/生成/基础 WD/规则与导出的后端合同；v1 指视觉、审图工作台扩展及辅助管理。前端 F14 本身仍归 v1，MVP API 不等于承诺 MVP 完成界面。F13/F16/F17/F18/F19/F20、clean/clean_nsfw、YOLO、非空导出处理链不增执行入口；F21 仅预留只读返回，F22/F23/F24 在表内明确覆盖。阶段细分建议见 Q07。

## 2. 进程结构、启动与关闭

### 2.1 进程和线程所有权

| 单元 | 所有权与约束 |
|---|---|
| 浏览器 / 开发期 Vite | 浏览器只持 token 内存和不透明项目句柄；Vite 只提供开发静态资源及代理，不拥有 DB |
| stylelora 主进程 | FastAPI、应用服务、任务监管器、事件总线、产品配置写队列；禁止多 worker、应用内热重载 |
| 项目写线程 | 唯一 sqlite3 写连接，F01 WriterQueue FIFO；连接不跨线程、不跨 await 暴露 |
| 只读工作线程 | 各自创建/关闭 read connection；短快照提取数据后释放，不为 SSE 长持事务 |
| 有界工作线程池 | 文件扫描/复制、缩略图、Pillow、WD 推理；线程不能独立提交 SQL，结果交服务层 |
| 外部进程 | Launcher 自己管理生命周期；nvidia-smi 仅有超时的查询子进程；不引入 Celery/Redis/独立写库进程 |

源的 Launcher 设置租约跨源互斥，同实例最多一张生成在途；视觉/prompt 共用全局 LLM 并发 2，WD 本地 worker 1。任务监管器管理取消和资源释放，不把生成源、提示词任务、导出伪装成 jobs。

### 2.2 启动顺序、锁与端口

1. 解析 CLI；拒绝 `--host` 非 `127.0.0.1`、workers≠1、生产 reload。创建产品目录，验证目录权限、配置 schema 与 SQLite 能力，不打印密钥。
2. 获取当前 Windows 用户的产品单实例命名互斥锁 `Local\StyleLoraAutom-<SID哈希>`，服务和独立 CLI 执行模式都必须持有。OS 句柄才是锁依据，PID 文件或锁文件存在不能判存活；abandoned mutex 触发恢复检查。
3. 初始化配置/DPAPI、产品任务恢复、Launcher 恢复日志检查、任务监管器、事件 epoch。恢复日志只记录最小设置与后端身份；当前设置与 applied 不同则保持写屏障，不覆盖外部修改。
4. 用密码学随机源生成 32 字节 API token（base64url），生成独立 instance_id/epoch。启动当前用户 ACL 的命名管道供 CLI 附着；token 不写发现文件、日志、命令行参数或项目。
5. 默认 bind `127.0.0.1:0`，由 OS 分配端口，保留同一已绑定 socket 交 ASGI server，避免“先探测空闲端口再绑定”的竞态。显式 `--port N` 占用即失败，不偷偷换端口；开发期可固定端口给 Vite。
6. ASGI lifespan 初始化完成后才发布 ready；可写不含 token 的实例定位文件到 LOCALAPPDATA runtime，字段仅 PID、instance_id、端口、协议版本、管道名。读取者仍须通过互斥锁/管道握手校验身份。
7. 有 `--project` 时执行 F01 open：先项目 `.project.lock` OS 独占锁，版本检查/可信迁移恢复，running 源/jobs→paused，核查 sent/收据，完成后发布项目句柄。恢复未知不得自动 submit。
8. 生成一次性浏览器引导码并打开页面；`--no-browser` 不生成引导链接，用户可运行 `stylelora ui`。自动恢复任务只恢复记录，不自动继续外部付费调用。

产品单实例锁与项目锁分别防止两份产品配置写入、两个进程打开同一项目。第二次 `serve` 通过管道请求已有实例打开浏览器后退出；不启动第二个 ASGI。项目被其它程序持锁返回 423。锁与管道细节为 Q02 推荐，不能退化成无锁双进程写。

### 2.3 token 下发与浏览器刷新

推荐“片段中的一次性引导码换取内存 token”，见 C18/Q01：主进程生成 32 字节随机 code，存内存哈希、60 秒有效期、一次性消费，绑定 instance_id 和目标前端 Origin。打开 `http://127.0.0.1:<port>/#bootstrap=<code>`；URL 片段不是 API token，不会随 HTTP 或 Referer 发给服务器。

前端最早执行的引导模块读取片段，立即 `history.replaceState` 清除，再 POST `/api/session/bootstrap`，body=`{code}`。服务器校验 Host/Origin、用途/有效期并原子消费 code，返回 `{token, instance_id, epoch, api_base:'/api', expires:'process'}`，`Cache-Control:no-store`。此接口是唯一免 Bearer 的 API，错误统一 401，限制每来源连接的速率；不返回可枚举的 code 状态。静态 HTML 不嵌 token，不提供公开 GET token 接口。

前端仅在 JS 内存持 token；禁止 localStorage/sessionStorage/IndexedDB、cookie、service worker 缓存。刷新后须通过 `stylelora ui` 重新引导；并发打开多个标签可各发一次 code，共享本进程 token。换码响应丢失时重新引导，不重放已消费 code。`ui --print-link` 仅用户明确指定时输出短时一次性链接到终端，默认不打印。

原生 EventSource/img 标签不能任意附加 Authorization：SSE 使用 fetch 流式读取，图片使用带 Bearer 的 fetch→Blob URL，并在视图释放时 revoke。禁止 token 放 query、fragment 图片链接、SSE query 或日志。后期 Tauri 可从受控 IPC 注入内存 token，复用同一认证合同。

### 2.4 静态托管与开发代理

生产期 FastAPI 从安装包只读 `web/dist` 托管 `/`、`/assets/*`；仅非 `/api`、非资源路径且 Accept HTML 的 GET 做 SPA fallback。API 404 不返回 index.html。index.html no-store；带内容哈希的 JS/CSS `public,max-age=31536000,immutable`；不得静态挂载项目根、用户配置或 exports。

开发期 Vite 也只监听 `127.0.0.1`，显式允许一个固定开发 Origin，例如 `http://127.0.0.1:5173`。浏览器访问 Vite 的相对 `/api`，代理指向后端实际端口，保留 Origin，Host 改为真实后端 authority，Authorization/Last-Event-ID 透传，SSE 禁缓冲；不在 Vite env 或源码中保存 token。主进程以 `--dev-origin` 绑定引导码目标，启动链接改为 Vite Origin。Vite HMR 重载丢失内存 token 时重新引导；不以持久 token 换开发便利。

### 2.5 优雅关闭与项目切换

关闭顺序：进入 draining → 拒绝新业务写入/新任务（503 `process:draining`）→ 停止所有派发 → running 源/job 原子转 paused、清费用 ack → 收敛在途 → 排空 F01 已接受写任务 → 释放文件租约/readers → checkpoint/关连接 → 释放项目锁 → 关闭外部客户端/管道/socket/产品锁。close 重入共享同一 Future。

源已 sent 的合法结果仍可登记；未知结果保留 sent 与收据，不重放、不伪造 failed。job/prompt 暂停允许当前代次已返回结果提交；用户 canceled 的 job/prompt 丢弃迟到结果。任务结果提交完成后源/job 可以按既定状态边自然 done，不强行把完成任务写回 paused。提示词任务持久 running→paused；导出没有 paused/resume：停止新导出，未发布目录记 interrupted，已发布 finished 保留。

建议总宽限期 30 秒（Q04），期间源 restore 仅在副作用核实且设置安全时执行；超过期限对异步网络请求尽力取消，留下恢复日志。无法安全中止的原生推理/文件线程不得在释放项目锁后继续写：它们只能返回内存结果；持文件句柄/文件副作用的工作尚未退出则项目关闭保持 draining，不宣称成功。应用最终被强制终止由 F01 崩溃协议恢复。

切换项目串行关闭旧项目再打开新项目；旧 project_id 立即失效，订阅/预览/文件 grant 的项目绑定失效。新项目打开失败时返回失败并保持无打开项目，不偷偷重新运行旧项目。产品提示词任务与项目无关联，切换项目可继续；应用退出才暂停它们。项目关闭等待项目导出/扫描/重新分级停止，不影响无关产品任务。

## 3. 服务分层、模块规格与依赖注入

下列路径均相对 `src/stylelora/`，接口是类型签名规格而非实现代码。DTO 见 §4，领域 DTO 保持所属 F 文档字段和语义；服务无 FastAPI Request/Response 依赖。

| 模块 | 职责与公开接口（含类型） | 依赖 | 错误处理 / 幂等性 | 最小测试 |
|---|---|---|---|---|
| runtime/lifecycle、instance | 启停/互斥/端口；`start(options: ServeOptions) -> Awaitable[Runtime]`；`close(reason: str) -> Awaitable[None]`；`attach() -> Awaitable[InstanceClient]` | Windows 锁/管道、supervisor、F01 | 锁占用附着或报错；close 可重复；无第二写进程 | T01–T04 |
| api/app、dependencies | 应用工厂与注入；`create_app(runtime: Runtime) -> FastAPI`；`require_project(id: UUID) -> ProjectContext` | 下列服务接口 | 项目代次不符 409/410；工厂不自动打开项目 | T05、T31 |
| api/security、bootstrap | token/Host/Origin、换码；`authorize(headers: HeaderMap) -> Principal`；`redeem(code: str, origin: Origin) -> SessionCredential` | runtime 内存、时钟、CSPRNG | 常量时间比较；无效认证不进业务；code 一次消费 | T06–T10 |
| api/contracts、errors、pagination | 严格 DTO、错误、白名单游标；`map_error(error: DomainError, request_id: UUID) -> HttpError`；`decode_cursor(value: str, query: ListQuery) -> Cursor` | F DTO、规范 JSON/HMAC | 验证 422、冲突 409；同输入同语义，不暴露底层异常 | T11–T14 |
| services/product | 配置、端点、roles、preset、密钥写入口；`get_config() -> Versioned[ProductConfig]`；`update(patch: ProductPatch, expected: Version) -> Awaitable[Versioned[ProductConfig]]`；`put_secret(ref: UUID, value: SecretInput, expected: Version) -> Awaitable[SecretStatus]` | 产品单写队列、V LLM/DPAPI | 原子文件替换/CAS；相同值 no-op；明文只入不出 | T15–T17 |
| services/projects、filesystem | 单项目切换、路径授权；`open(grant: UUID) -> Awaitable[ProjectView]`；`close(expected: UUID) -> Awaitable[None]`；`pick(purpose: GrantPurpose) -> Awaitable[PathGrant]` | F01、native picker、supervisor | 高版本拒绝、锁 423；同根 open 复用；create 不覆盖 | T03–T05、T18 |
| services/source_facade | 路由到 nai/import；`start(ctx: ProjectContext, id: int, input: SourceStart, expected: Version) -> Awaitable[Accepted]`；`control(ctx, id: int, action: SourceAction, expected: Version) -> Awaitable[SourceView]` | G runner/cost、P seeds、E importer、D | 同任务无双派发；unknown 只核查；费用 cycle CAS | T19–T20 |
| services/catalog_facade | 字典/标签/规则产品资源；`preview(input: CatalogImport) -> Awaitable[PreviewView]`；`commit(id: UUID, digest: str, expected: Version, key: UUID) -> Awaitable[CommitReport]` | P dictionaries/tasks、E catalog/rules | 产品凭据先于 CAS；同键同摘要重放；不改变项目来源 | T16、T21 |
| services/image_facade、media | 图集投影/审图/文件服务；`list(ctx, query: ImageQuery) -> Awaitable[Page[ImageView]]`；`open_media(ctx, id: int, variant: MediaVariant) -> Awaitable[MediaHandle]` | D、E projection、Pillow、文件租约 | 缺文件 410；ETag 只影响读；缩略图同键单航班 | T22–T24 |
| services/editor_facade | manual、规则与撤销；`edit(ctx, input: ManualEdit) -> Awaitable[ManualResult]`；`apply(ctx, preview: UUID, digest: str) -> Awaitable[RuleApplyResult]` | E 算法、D command | 全读集 CAS；整批回滚；预览重放仅会话保障 | T25–T26 |
| services/task_facade、supervisor | 后台任务受控启动/查询；`submit(spec: TaskSpec, owner: TaskOwner) -> Awaitable[TaskRef]`；`control(ref: TaskRef, action: TaskAction, expected: Version) -> Awaitable[TaskView]` | V jobs、P tasks、E export、G source | 委托各状态机；不新增通用 DB job；代次防迟到 | T27–T30 |
| services/storage | 只读占用/清理确认；`scan(scope: StorageScope) -> Awaitable[StorageReport]`；`preview_cleanup(ctx, kind: CleanupKind) -> Awaitable[CleanupPreview]`；`cleanup(ctx, confirmation: CleanupConfirmation) -> Awaitable[CleanupReport]` | D、文件租约、项目管理 | 二次确认摘要过期拒绝；逐文件凭据/缺失审计；不删业务行 | T32–T33 |
| api/events | 事件汇聚/环形缓冲；`publish(event: DomainEvent) -> None`；`subscribe(filter: EventFilter, after: Optional[EventId]) -> AsyncIterator[SseFrame]` | 提交后回调、supervisor | 缺口 resync，慢读断开；事件无业务副作用 | T34–T37 |
| api/routes/* | 请求验证→服务调用→DTO 投影；各公开签名以 §5 方法/路径及 §4 类型为准 | 注入上述 facade | 无 SQL、无重试循环、无图片算法；统一错误中间件 | T11、T38 |
| cli、runtime/pipe | 子命令解析/附着；`run(args: Sequence[str], transport: ServiceTransport) -> Awaitable[ExitCode]`；`connect_instance() -> Awaitable[ServiceTransport]` | 同一服务容器或本地 HTTP/受控管道 | 不绕过 DAL；未知写不自动重发；输出脱敏 | T39–T42 |

DI：lifespan 构建 Runtime 单例（产品服务、LLM 闸门、EventBus、TaskSupervisor、Clock、随机源、SecretStore、PathGrantRegistry）；每请求注入 Principal、RequestContext、对应服务。ProjectContext 含 project_id、generation、F01 handle，但不含可直接取得连接的字段。请求、后台任务、预览和文件租约均绑定同一项目代次；关闭先封锁代次，再等待已接受工作退出。

路由只校验传输结构，业务服务做状态/能力/权限/原子协调，repository 做数据访问，外部适配器做网络/推理。网络和磁盘长工作不得放入 writer 回调；writer Future 异步桥接。FastAPI `BackgroundTasks` 不作为长任务队列：202 前必须先注册受监管任务或持久实体，不能响应后才可能丢失建任务动作。

## 4. 统一 HTTP 合同与类型

### 4.1 命名、资源和通用响应

根路径 `/api`，初版 API schema version=1，由 `/api/session` 返回；兼容新增可选字段，小版本不改变已有枚举语义，破坏性协议需新主版本。`P=/api/projects/{project_id}`、`U=/api` 仅为 §5 表格缩写，实际 URL 不含 P/U。所有路径采用小写 kebab-case，JSON 字段 snake_case。

项目实体 ID 为正整数；产品/任务/预览/授权 ID 为 UUID 字符串；project_id 为每次打开生成的会话 UUID，不写 project.json，不是路径哈希。整数拒绝 bool；JSON 拒 NaN/Infinity、重复键及未知可写字段，UTC 时间沿 F01 格式。`*_json` 在 HTTP 中仍保留字段名但值为已解码对象/数组，禁止双重编码 JSON 字符串。

成功 GET/PATCH/PUT/action 默认 200；实体创建默认 201 加 Location；后台受理 202 加 Location 和 Retry-After；删除/释放 204；二进制/SSE 单独 content-type。资源返回 `version: str` 与同值强 ETag；列表项各自携带 version。错误不混在成功资源对象中。响应含 `X-Request-ID`；客户端自传仅接受合法 UUID，否则由服务器生成。

| 公共类型 | 字段和语义 |
|---|---|
| Version / UUID / EventId | 非空不透明字符串 / 标准 UUID 字符串 / `epoch:sequence` |
| Versioned[T] | 资源 T 加 version；强 ETag 为双引号包围该 version，不是 DB 新列 |
| Page[T] | `items: list[T], next_cursor: str或null, has_more: bool, limit: int, upper_id: int或null`；不默认 count 全表 |
| Accepted | `task: TaskRef, status_url: str, events_url: str, request_id: UUID` |
| TaskRef | `kind: Literal[source,job,prompt,export,operation], id: int或UUID, project_id: UUID或null`；不承诺统一 status 枚举 |
| OperationView | `operation_id: UUID, kind: str, status: Literal[pending,running,done,failed,canceled,interrupted], completed: int, total: int或null, result: JsonValue或null, error: ApiError或null, version: Version`；非持久通用业务表 |
| ProjectView | `project_id: UUID, metadata: ProjectMetadata, schema_version: int, recovery: RecoveryReport, version: Version`；无绝对 root |
| PathGrant | `grant_id: UUID, purpose: Literal[project_create,project_open,import_root], display_name: str, expires_at: UTC, project_id: UUID或null`；绝对路径仅注册表内存 |
| SourceStart | 判别联合：`kind=nai, seeds: FrozenSeeds`；或 `kind=import, preview_id: UUID, digest: str, root_handle: UUID`；不接 task_id/字典引用进入项目 |
| SourceView | D.SourceRow 加 `counts: ItemCounts, version, runtime: {cycle_id: UUID或null, pause_reason: str或null, restore_pending: bool, in_flight: int}`；runtime 不落库 |
| ImageView | D.ImageRow 的安全投影，加 `width: int>0, height: int>0, has_alpha: bool, annotation_coverage: object, version, original_url: str, thumbnail_url: str`；尺寸/透明通道为入库元数据只读投影，列表与详情均返回，missing仍保留；png_meta 仅详情按需返回 |
| AnnotationView / ManualEdit | E §3/5；完整五层 current/缺失 refs，tags 为 `{tag: str, conf: float或null}[]`；NL 写入由 write_nl 显式控制 |
| JobView | D.JobRow 安全投影、version；列表省略巨大的 progress_json，逐图 outcomes 走分页；详情不含 URL/秘密 |
| PromptTaskView | P.TaskView 去除内联 outcomes，增加计数和 outcomes_url；字段 ready/coverage/revision 保留 |
| ExportView | `export_id: UUID, status: Literal[planned,running,finished,failed,canceled,interrupted], selected/exported/skipped/failed: int, report_url: str或null, relative_dir: str或null, version` |
| PreviewView | `preview_id: UUID, digest: str, expires_at: UTC, issues: Issue[], version: Version` 加所属领域预览；15 分钟会话有效 |
| Issue | `code: str, message: str, severity: Literal[warning,error], field: str或null, row: int或null` |
| ApiError | `{code: str, message: str, retryable: bool, retry_action: Literal[none,read,reconcile,user], outcome: Literal[not_sent,rejected,unknown]或null, details: object, issues: Issue[], request_id: UUID}` |
| ProductConfig | `revision: int, cost_policy: Literal[deny,ask,allow], storage_thresholds: {project_bytes:int,total_bytes:int}, llm_limits: {concurrency:int,timeout_s:int}, wd_enabled: bool, mcp: {discovery_mode: Literal[default,configured]}, capabilities: object`；不接受任意路径/任意环境变量映射 |
| EndpointView / EndpointInput | V.EndpointConfig 的 endpoint_id/base_url/protocol/models/revision；token_refs 改为安全 `{token_ref:UUID,has_secret:bool}` 状态；写配置只引用 token_ref，SecretInput 独立含 `value: str` |
| RoleMap / Preset | 五个已启用角色映射 `{endpoint_id:UUID,model:str}`或null，clean 两角色仅 null；preset 为 `{preset_id:UUID,name:str,builtin:bool,sizes:Mapping[Aspect,PixelSize],revision:int}` |
| Scope / ImageQuery | selected_ids 非空整数数组与 filter 二选一；filter 是白名单 source_ids/review_statuses/rating/file_missing/layer 状态；不接 SQL/任意表达式 |
| CleanupConfirmation | `preview_id: UUID, digest: str, confirm: Literal[true]`，绑定目标项目/清理类型/候选文件身份与显示统计 |
| ImagePage | `Page[ImageView]` 加 `total: int>=0, start_index: int>=0, anchor_index: int或null`；索引从0起，total为本次快照中筛选且不超过upper_id的总数 |
| ReviewResult | `items: list[{image: ImageView, old_review_status: ReviewStatus}]`，按image_id升序，包含no-op项；ReviewStatus沿D既有枚举；image携带提交后的version |
| ReviewPreview | `PreviewView` 加 `total: int>=0`；服务端保存规范化query、成员ID和逐图Version，不向客户端传整批ID；15分钟会话有效 |
| ImageStats | `{total: int>=0, groups: list[{by: Literal[review_status,rating,source_id,annotation_coverage], buckets: list[{key: str或int或null, count: int>=0}]}]}`；rating用有效rating，source_id为整数，coverage键为complete/partial/none |

FrozenSeeds、SourceDraft/Profile、SamplingRequest、Dictionary、RuleSpec、ImportSchemeInput、ExportSchemeInput、BackendCapabilities、CatalogHit 等严格引用 P/G/E/V 对应 schema；表内请求只允许所列业务字段，DB 时间、attempts、is_current、merge_meta、model_name 等由服务生成，不能凭响应 DTO 全量回传取得写权。

### 4.2 分页、过滤、排序

全部 HTTP 列表默认 `limit=50`，范围 1..1000，超界 422，不静默截断；内部 DAL 仍可每页 200，不修改 F01。统一 `cursor`，不接 page/offset；A059另支持下述around_id定位。首批生成 upper_id；后续游标含 project generation/resource/filter digest/sort/last key/upper bound，HMAC 校验，不能跨列表或改过滤复用。

DB 列表默认 `sort=id` 升序；支持 `sort=-id`，annotations 历史默认 -id，source_items 对外按 item_id，内部执行仍按 seq。别的 sort 只有能力清单声明才可用：跨项目 storage 允许 `-total_bytes,known_id`（默认）或 `known_id`，以持久编号而非当前打开句柄打破并列；近似 pair 为 `distance,left_id,right_id`；产品 UUID 列表为 `name,id`，来自冻结排序快照；任务 outcomes 按冻结 index 或 image_id。不得把客户端字段直接插入 SQL。

多值过滤用重复 query 参数，例如 `review_status=keep&review_status=unreviewed`，同字段 OR、字段间 AND；未知 filter/sort 422；`q` 仅用于名称/标签，不作为正则。缺省 images 展示所有审图状态并包含 missing，执行器/规则/导出各按既定 Scope 默认，不混用列表默认。范围、预览和导出都在服务端物化，不能把浏览器当前页误作全选。

普通 DB 分页逐页快照，upper_id 仅排除后来插入，过滤值变化可能改变成员；不保证跨页一致性。产品列表游标绑定产品 revision，变化返回 409 `page:stale`。一致性导出/扫描先冻结元数据，不能长期持读事务等待客户端翻页。需要总计走统计/预览接口，不把 total 默认为准确快照。

A059为total例外：items、total和索引在同一短读事务计算，跨请求仍遵上述逐页快照。`around_id: int>0`与cursor互斥，按当前过滤/排序求该图索引k，返回从`floor(k/limit)*limit`开始的一页，anchor_index=k、start_index为页首索引；普通分页anchor_index=null，start_index为本次快照页首位置，空页为total。目标不存在或不在过滤范围返回404 `resource:not_found`；空集合total=0。定位不新增DAL offset合同，后续仍用next_cursor。

A059/A141/A142共用图片过滤：HTTP重复参数`source_id: int>0`、`review_status: ReviewStatus`、`rating: Rating`映射ImageQuery的source_ids/review_statuses/rating数组；`file_missing: bool`可选；`layer: str`取E既有层名白名单，配`annotation_coverage: Literal[complete,partial,none]`可选。coverage按所选层的current有效内容计算（未指定layer则用ImageView全部适用层）：全部覆盖/部分覆盖/无覆盖；空适用层集归none。同字段OR、字段间AND，未知值422，rating沿D有效rating枚举。query请求体仅含上述规范字段，不含分页/排序；列表sort仍按能力白名单。

### 4.3 CAS、版本与请求幂等

所有更新/删除/控制已有资源须 `If-Match: "<version>"`；缺失 428，格式错误 422，失效统一 409 `concurrency:version_conflict`（不混用 412）。新建资源不需要 If-Match。涉及两资源追加/默认切换在 body 带对应 `expected_versions: {resource_ref:Version}`；大批 manual/review 带逐图前置条件，不用一个含糊的全项目时间戳。

version 是带资源身份及项目代次的服务端令牌：产品 revision、方案 updated_at、image 审图/rating 预期元组、source 状态/锁定配置摘要、job 状态与快照身份、annotation current refs。后台进度计数不使控制令牌每图失效；资源返回的业务数据版本与控制前置条件按 schema 指定同一控制投影。方案使用严格单调 updated_at；manual/规则仍验证所有 current/缺失 refs，不能用摘要替代 F01 全读集检查。

服务在同一 writer 事务或产品写队列中重新比较并执行；仅路由层读一次比较不算 CAS。无法用持久版本表达的实体增加会话内 mutation generation，防同进程 A→B→A 旧编辑覆盖；重启/重新打开后全部旧会话 version 失效。无新增 version 列，不宣称跨进程持久修订历史。

创建/应用/有费用风险的动作要求 `Idempotency-Key: <UUID>`，对应 F 草案 request_key；路由只采用头，不同时接受两份相互矛盾键。记录 `(instance,project generation,principal,method,path,key,body digest)` 的 in-flight/result，重复在校验旧 If-Match 前查成功凭据，同键不同摘要 409。TTL 建议 24 小时且活跃任务不淘汰；过期不保证去重。

持久保证只来自既有机制：jobs 稳定 batch_id、字典 operations、prompt task request_key、source 全量 seeds/config、item+attempt、F01 outcomes；其余创建仅会话去重。相同 key 在进程重启后不得自动重试无持久凭据的创建/规则/导出。客户端遇网络断开先 GET 状态/日志/report；CommitUnknown 返回 outcome=unknown、retry_action=reconcile、retryable=false，绝不指示重发付费请求。

同状态控制可 no-op，但不能把“再次运行一周期”误当重复 resume；成功凭据优先。source confirm-cost 还必须校验 cycle_id 和 ask 策略，普通 resume 不带费用同意。终态 job/prompt 重做创建新实体；source failed item 重试后 paused，需明确 resume。

A062请求为二选一：`{ids: list[int], target: ReviewStatus, expected_versions: Mapping[str,Version]}`，或`{query: ImageQuery, expected_total: int>=0, preview_id: UUID, digest: str, target: ReviewStatus}`。query先经A142冻结逐图前置条件，提交时在同一writer事务重新展开，比较规范query、完整成员集、expected_total及全部旧version，再批量更新；仅数量一致不能替代CAS。差异409 `concurrency:version_conflict`、过期410 `preview:expired`、缺前置条件428；任一失败整批回滚。两模式均最多5000张，超限413 `batch:too_large`，不自动拆批；空query结果可成功返回空items。

撤销仍走A062的ids模式，以互斥的`targets: Mapping[str,ReviewStatus]`替代target：逐张填入上次old_review_status，expected_versions取上次返回image.version，映射键须与ids完全一致；并发改动使整批409，不能覆盖。A062及A142均要求Idempotency-Key；同键同摘要先重放原结果（含旧状态）再检查CAS，不重新展开query，异摘要409；撤销使用新键。只承诺§4.3会话去重，重启/结果未知先核查，预览不新增持久账本。

### 4.4 错误体和 HTTP 映射

统一 JSON 错误信封为 `{"error": ApiError}`。details 只允许安全字段：resource_id、current_version、conflicting_image_ids、reason_code、retry_after_ms、相对路径诊断；不回 SQL、堆栈、供应商正文、环境变量名、密钥或外部绝对目录。

| HTTP | code 类别及含义 | retry_action |
|---|---|---|
| 400 | `request:malformed_json`、`event:invalid_id` | none |
| 401 | `auth:required`、`auth:invalid`、`auth:bootstrap_invalid` | user |
| 403 | `security:origin_denied`、`security:host_denied`、`path:grant_denied` | none |
| 404 | `resource:not_found`、未知路由 | none |
| 409 | `concurrency:version_conflict`、`state:conflict`、`rule:preview_stale`、`undo:conflict`、`idempotency:key_conflict`、`source:locked` | read/user |
| 410 | `session:expired`、`project:handle_expired`、`preview:expired`、`image:file_missing` | read/user |
| 413 | `request:too_large`、`batch:too_large` | user |
| 422 | `input:invalid`、`schema:invalid`、`path:unsafe`、`coverage:unsatisfiable`、`trigger:in_style_prompt` | user |
| 423 | `project:locked` | user |
| 428 | `concurrency:precondition_required` | read |
| 429 | `request:rate_limited`、事件订阅超限 | read；Retry-After |
| 500 | `storage:corrupt`、`internal:error` | none；故障 ID |
| 501 | `feature:unsupported`、`pipeline:unsupported`、`sorter:unsupported` | user |
| 503 | `storage:busy`、`storage:disk_full`、`storage:commit_unknown`、`dependency:unavailable`、`process:draining` | 仅安全读可 read，未知提交 reconcile |

上游 401/403 是配置问题，映射 503 `llm:unauthorized`，不是本地 API 身份失效；上游 429 的令牌轮换归 V，不让浏览器另建重试层。202 后的错误进入实体状态/任务 error/SSE，而不是试图改已经返回的 HTTP 状态。item skipped、job skipped、规则零变化、预览 warning 均不是 HTTP 错误。

## 5. API 总表（142 条）

每行恰为一个方法+路径；A001–A142 连续编号。HTTP 表共 **142 条**，不把 HEAD/OPTIONS 自动处理、静态资源、CLI 管道或未启用的旧路由别名算入条数。所有写入通用 CAS/请求键按 §4.3；所有列表均用 Page（A059扩展为ImagePage），未标分页的能力对象不是列表资源。

错误列为额外领域错误；**每一行均继承**公共错误 B：400/401/403/404/413/422/429/500/503，写已有资源另继承 409/428，带会话句柄另继承 410。代码缩写 `conflict=state:conflict`、`unavailable=dependency:unavailable`、`unsupported=feature:unsupported`；不因列中省略 B 就允许绕过认证。来源“整合”表示本文补齐已有职责缺口，未改变架构。

### 5.1 会话、进程与路径授权

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A001 | GET | U/session | 无 → instance/epoch/API版本、打开项目、能力、draining；认证后读 | — | 05§11 / MVP |
| A002 | POST | U/session/bootstrap | code → 内存 token/instance；免 Bearer、严格 Origin | 401 bootstrap_invalid | 05§13#18、整合 / MVP |
| A003 | POST | U/session/shutdown | grace_seconds（≤配置上限）→202 OperationView，退出意图 | 409 conflict | F11、整合 / MVP |
| A004 | POST | U/filesystem/grants | purpose、project_id? →202 原生选择器操作，结果 PathGrant；不接 path | 409 picker_busy、501 picker_unavailable | 05§3、整合 / MVP |
| A005 | DELETE | U/filesystem/grants/{grant_id} | If-Match →204 撤销；活动使用者释放后清内存 | 409 grant_in_use | 整合 / MVP |
| A006 | GET | U/events | topics、project_id?、resource_id?、Last-Event-ID →SSE | 409 event:filter_conflict | G/P/V、整合 / MVP |
| A007 | GET | U/operations/{operation_id} | 无 →OperationView，产品或绑定项目的辅助任务 | 410 session:expired | V/E、整合 / MVP |
| A008 | POST | U/operations/{operation_id}/cancel | If-Match →202/200，尽力停止 | 409 operation:not_cancelable | V/E、整合 / MVP |

### 5.2 product config、LLM endpoints 与 roles、presets

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A009 | GET | U/config | 无 →Versioned ProductConfig、安全默认/能力 | — | 05§4、整合 / MVP |
| A010 | PATCH | U/config | ProductPatch →新配置；非密钥白名单、影响下一新任务 | 409 config:revision_conflict | 05§4、V / MVP |
| A011 | GET | U/llm/endpoints | filters/page →Page EndpointView | — | V§9 / MVP |
| A012 | POST | U/llm/endpoints | EndpointInput →201 EndpointView；不自动测试/计费 | 409 idempotency:key_conflict | V§6、整合 / MVP |
| A013 | GET | U/llm/endpoints/{endpoint_id} | 无 →EndpointView，base_url 仅去除凭据后的合法配置 | — | V§9 / MVP |
| A014 | PUT | U/llm/endpoints/{endpoint_id} | EndpointInput+If-Match →新 revision | 409 config:revision_conflict | V§9 / MVP |
| A015 | DELETE | U/llm/endpoints/{endpoint_id} | If-Match →204；仍被角色绑定则拒绝 | 409 endpoint:in_use | V、整合 / MVP |
| A016 | POST | U/llm/endpoints/{endpoint_id}/tokens | value →201 token_ref/has_secret/version；DPAPI | 503 secret:unavailable | V§6、整合 / MVP |
| A017 | PUT | U/llm/endpoints/{endpoint_id}/tokens/{token_ref} | SecretInput+If-Match →has_secret/version；不回明文或尾缀 | 503 secret:unavailable | V§9 / MVP |
| A018 | DELETE | U/llm/endpoints/{endpoint_id}/tokens/{token_ref} | If-Match →204，删除存储秘密/关联；环境覆盖仍单列状态 | 409 token:in_use | V、整合 / MVP |
| A019 | GET | U/llm/roles | 无 →RoleMap/revision、每角色可用性 | — | V§9、05§4.2 / MVP |
| A020 | PUT | U/llm/roles | RoleMap+If-Match →revision；图像角色校验能力 | 422 llm:image_unsupported、501 clean_reserved | V§9 / MVP |
| A021 | POST | U/llm/endpoints/{endpoint_id}/models/probe | If-Match →202 operation，候选/明确模态；失败不覆盖手填 | 503 llm:unauthorized | V§9 / MVP |
| A022 | POST | U/llm/endpoints/{endpoint_id}/models/test-image | model:str+If-Match →202 operation；固定小图测试，避免 model 含 / 的路径问题 | 422 llm:image_unsupported | V§9、整合 / v1 |
| A023 | GET | U/presets | page →Page Preset，实际三组像素/内置标记 | — | G§4、F24 / MVP |
| A024 | POST | U/presets | name/sizes →201 Preset；严格整数64–16384且64倍数，符合横/竖/方关系 | 422 schema:invalid（0.31.0） | F24、整合 / MVP |
| A025 | GET | U/presets/{preset_id} | 无 →Preset | — | F24、整合 / MVP |
| A026 | PUT | U/presets/{preset_id} | name/sizes+If-Match →Preset；只影响后续建源取值 | 409 preset:builtin_locked | F24、整合 / MVP |
| A027 | DELETE | U/presets/{preset_id} | If-Match →204；已锁源仍保实际像素 | 409 preset:builtin_locked | F24、整合 / MVP |

### 5.3 dictionaries 与覆盖预览

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A028 | GET | U/dictionaries | kind/q/page →Page 字典摘要 | — | P§9 / MVP |
| A029 | POST | U/dictionaries | FeatureDictionary或PromptDictionary输入 →201 id/revision | 422 dictionary:schema_invalid | P§9 / MVP |
| A030 | GET | U/dictionaries/{dictionary_id} | 无 →信封/维度摘要/revision，条目另分页 | — | P§9、整合 / MVP |
| A031 | PUT | U/dictionaries/{dictionary_id} | 完整字典+If-Match →revision；受10MiB限制 | 409 dictionary:revision_conflict | P§9 / MVP |
| A032 | DELETE | U/dictionaries/{dictionary_id} | If-Match →204；冻结任务和已启源不受影响 | 409 dictionary:revision_conflict | P§9补齐 / v1 |
| A033 | GET | U/dictionaries/{dictionary_id}/entries | dimension?/page →Page 条目；cursor绑定revision | 409 page:stale | P§4、整合 / MVP |
| A034 | POST | U/dictionaries/imports/preview | multipart file/format/kind/dimension? →ImportPreview | 422 schema:invalid、413 request:too_large | P§9 / MVP |
| A035 | POST | U/dictionaries/{dictionary_id}/imports | preview_id/digest+If-Match →AppendResult | 409 dictionary:rating_conflict、410 preview:expired | P§9 / MVP |
| A036 | GET | U/dictionaries/{dictionary_id}/export | format=json/csv/txt →附件；TXT附有损字段头 | 422 input:invalid | P§9 / MVP |
| A037 | POST | U/coverage/preview | SamplingRequest →202 operation→CoverageReport；无精修 LLM | 422 coverage:unsatisfiable、search_exhausted | P§9、整合 / MVP |
| A038 | POST | U/dictionaries/launcher/preview | selection/分组映射 →202 operation→ImportPreview；只读 | 503 launcher:unsupported_schema | P§9 / MVP |

### 5.4 projects

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A039 | GET | U/projects | page →最近项目摘要/known_id/可访问性/当前句柄；不是扫描磁盘 | — | 05§3.5、整合 / MVP |
| A040 | POST | U/projects | project_create grant/name/trigger_word →202 operation→新 ProjectView | 409 project:exists、423 project:locked | D§6.3 / MVP |
| A041 | POST | U/projects/open | project_open grant或known_id（二选一）→202 operation→ProjectView | 409 project:version_mismatch、423 project:locked | D§8 / MVP |
| A042 | GET | U/projects/current | 无 →ProjectView或null，不隐式打开最近项目 | — | 05§11、整合 / MVP |
| A043 | GET | P | 无 →ProjectView/恢复摘要 | 410 project:handle_expired | D / MVP |
| A044 | PATCH | P | name/trigger_word+If-Match →ProjectMetadata/version，允许暂空触发词 | 409 concurrency:version_conflict | D§6.3、F22 / MVP |
| A045 | POST | P/close | If-Match →202 operation；停派发并安全释放 | 409 project:closing | D§7.1、整合 / MVP |
| A046 | GET | P/recovery | page →sent/收据/孤立文件/缺失/待恢复摘要；相对位置 | 503 recovery:unavailable | D/G / MVP |
| A047 | POST | P/recovery/reconcile | item_ids?+If-Match →202 operation，只核查，不 submit | 409 recovery:busy | D/G / MVP |

创建项目默认成为当前项目，若已有项目打开则 409，先明确 close；open 到另一项目是明确切换意图，遵循 §2.5。known_id 为产品最近记录 ID，与每次打开的 project_id 区别明确。A039 只显示 basename/用户名称，不给任意文件列表或绝对路径。

### 5.5 sources 与 source_items

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A048 | GET | P/sources | type/status/q/page →Page SourceView | — | G/D / MVP |
| A049 | POST | P/sources | SourceDraft →201 SourceView；cost_policy默认从产品复制 | 422 profile:invalid | G/D / MVP |
| A050 | GET | P/sources/{source_id} | 无 →SourceView/费用暂停runtime摘要 | — | G§8 / MVP |
| A051 | PATCH | P/sources/{source_id} | draft配置或name/cost_policy+If-Match →SourceView | 409 source:locked | D§5.1、G / MVP |
| A052 | DELETE | P/sources/{source_id} | If-Match →204，仅无items/images的draft | 409 source:not_empty_draft | D§2.2 / MVP |
| A053 | POST | P/sources/{source_id}/start | SourceStart+If-Match →202 Accepted；全 seeds 先冻结、一次写齐 | 409 seeds:not_ready、422 profile:invalid | G§7、P§8 / MVP |
| A054 | POST | P/sources/{source_id}/control | action=pause/resume/cancel+If-Match →SourceView；长恢复202 | 409 conflict、503 unavailable | G§7 / MVP |
| A055 | POST | P/sources/{source_id}/confirm-cost | cycle_id+If-Match →202 Accepted；新周期仅一次授权 | 409 cost:cycle_expired、cost:denied | G§5 / MVP |
| A056 | GET | P/sources/{source_id}/items | status/page →Page SourceItemRow，含attempt/error/image_id | — | D/G / MVP |
| A057 | GET | P/source-items/{item_id} | 无 →SourceItemRow/version | — | D/G / MVP |
| A058 | POST | P/source-items/{item_id}/retry | expected_attempt+If-Match →SourceItemRow；done源原子paused | 409 item:not_failed、source:canceled | D/G / MVP |

### 5.6 images 与 annotations

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A059 | GET | P/images | ImageQuery + limit/cursor或around_id →ImagePage，含total及所在页索引，见§4.2 | 404 resource:not_found（定位目标不匹配） | 05§8.1、D、F14 C31 / MVP |
| A060 | GET | P/images/{image_id} | include=metadata? →ImageView/层状态；含只读width/height/has_alpha（列表同） | — | D/E、F14 C30 / MVP |
| A061 | PATCH | P/images/{image_id} | review_status或manual rating+If-Match →ImageView；四列rating服务生成 | 409 image:file_missing | D§5.4 / v1 |
| A062 | POST | P/images/review | ids或query范围请求+Idempotency-Key →ReviewResult；逐图旧状态/新version供撤销，query预览CAS及targets撤销见§4.3 | 409 image:file_missing、concurrency:version_conflict；410 preview:expired；413 batch:too_large | D§6.5、F14 C32 / v1 |
| A063 | GET | P/images/{image_id}/content | If-None-Match? →200原始图片字节或304；均带ETag/Cache-Control，Bearer，见§7.1 | 410 image:file_missing | 05§3、F14 C34 / MVP |
| A064 | GET | P/images/{image_id}/thumbnail | size=256或512、默认256，If-None-Match? →200 WebP或304；均带ETag/Cache-Control，见§7.1 | 410 image:file_missing、422 image:decode_failed | 05§3、F14 C34 / MVP |
| A065 | POST | P/images/{image_id}/verify-file | If-Match →202 operation；核验真实恢复文件才解除missing | 409 image:hash_mismatch | D§5.4、整合 / v1 |
| A066 | GET | P/annotations/{image_id} | 无 →AnnotationView，无导出阈值编辑视图/翻译/refs | — | E§5/13 / v1 |
| A067 | GET | P/annotations/{image_id}/history | source/type/page →Page AnnotationRow，当前和历史标志 | — | D§6.4、整合 / v1 |
| A068 | PUT | P/annotations/{image_id}/manual | ManualEdit/read_set+If-Match →ManualResult；服务生成merge_meta | 409 manual:current_conflict | E§5/13 / v1 |
| A069 | POST | P/annotations/manual/bulk | ids/add/remove/read_set/expected_versions →BulkEditResult | 409 manual:current_conflict、413 batch:too_large | E§5/13 / v1 |

### 5.7 rules 与 operation_log

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A070 | GET | U/rules | 无 →产品RuleLibrary/revision，10MiB上限 | — | E§6/13 / MVP |
| A071 | PUT | U/rules | RuleLibrary+If-Match →revision；不执行项目规则 | 409 catalog:revision_conflict | E§6/13 / MVP |
| A072 | POST | U/rules/imports/preview | TagCleaner multipart文件 →迁移预览，提示旧语义差异 | 422 rule:schema_invalid | E§4、整合 / MVP |
| A073 | POST | U/rules/imports/commit | preview_id/digest+If-Match →CommitReport | 409 catalog:revision_conflict | E§4、整合 / MVP |
| A074 | POST | P/rules/preview | RuleSpec/Scope →RulePreview；物化所有候选及完整读集 | 413 batch:too_large、422 rule:empty | E§6/13 / MVP |
| A075 | POST | P/rules/apply | preview_id/digest+If-Match预览version →RuleApplyResult | 409 rule:preview_stale、410 preview:expired | E§6/13 / MVP |
| A076 | GET | P/operation-log | status/page →Page OperationRow摘要，不内联巨型changes | — | D§6.4、整合 / MVP |
| A077 | GET | P/operation-log/{op_id} | changes分页 →日志头+Page changes | — | D/E、整合 / MVP |
| A078 | POST | P/operation-log/{op_id}/undo | If-Match →OperationRow，仅最近applied；已undone no-op | 409 undo:not_latest、undo:conflict | D§7.2、E§6 / MVP |

### 5.8 jobs 与 prompt tasks

0.16.0 补充：JobFilter 增加 rating/file_missing/q，和工作台共用过滤语义；mode 与 scope selector 分开，支持 selected+filter 和 all+selected_ids。新增 `GET P/jobs/{job_id}/retry-preview`（只读）及 `POST P/jobs/{job_id}/retry`（batch_id、preview_digest →202 新单阶段批次）。后端决定失败 ID 和冻结配置，客户端不能提交扩大范围或改配置；原批次须全部终结。详见 [0.16.0 验收](23-F14-工作台执行器与失败重试验收.md)。

实现补充（0.15.0）：A079 支持 `{wd, llm_vision}` 组合 config 和可选 `preview_scope_digest`，两阶段同事务冻结，返回 `batch_url`。增加只读 `POST P/jobs/preview`、分页 `GET P/jobs/batches`、聚合 `GET P/jobs/batches/{batch_id}` 与 `POST P/jobs/batches/{batch_id}/control`（pause/resume/cancel + 整批 If-Match）。沿用下表的逐 job 查询/控制；没有新增持久任务种类。详细响应、范围校验与接续合同见 [0.15.0 验收](22-F15-组合批次与覆盖预览验收.md)。

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A079 | POST | P/jobs/batches | batch_id/executors/mode/scope →202 JobView[]；WD先建，LLM后建 | 409 job:batch_conflict、422 input:invalid | V§9、D / MVP；LLM v1 |
| A080 | GET | P/jobs | status/batch_id/executor/page →Page JobView | — | V§9 / MVP |
| A081 | GET | P/jobs/{job_id} | 无 →JobView/计数/冻结摘要 | — | V§9 / MVP |
| A082 | GET | P/jobs/{job_id}/outcomes | kind/page →Page 持久JobOutcome投影 | — | D§5.3、V§9 / MVP |
| A083 | POST | P/jobs/{job_id}/control | action=pause/resume/cancel+If-Match →JobView或202 | 409 job:state_conflict、503 unavailable | V§9 / MVP |
| A084 | GET | U/executors/capabilities | 无 →wd/llm_vision能力与禁用原因；扩展reserved | — | V§8/11、整合 / MVP |
| A085 | POST | P/ratings/reclassify | scope →202 operation；仅清keyword/model/fallback后重判 | 409 rating:conflict、503 unavailable | V§9 / v1 |
| A086 | GET | U/prompt-tasks | status/page →Page PromptTaskView | — | P§7补齐 / MVP |
| A087 | POST | U/prompt-tasks | SamplingRequest →202 PromptTaskView；持久冻结产品输入 | 409 idempotency:key_conflict、422 input:invalid | P§9 / MVP |
| A088 | GET | U/prompt-tasks/{task_id} | 无 →PromptTaskView/ready/coverage | — | P§9 / MVP |
| A089 | GET | U/prompt-tasks/{task_id}/outcomes | status/page →Page TaskOutcome；正文仅认证GET | — | P§9 / MVP |
| A090 | POST | U/prompt-tasks/{task_id}/control | action=pause/resume/cancel+If-Match →PromptTaskView | 409 seeds:state_conflict | P§9 / MVP |
| A091 | POST | U/prompt-tasks/{task_id}/append | dictionary_id/indices/expected_versions →AppendResult | 409 dictionary:revision_conflict | P§9 / MVP |
| A092 | POST | U/prompt-tasks/{task_id}/seeds | If-Match →FrozenSeeds全量，非分页；只允许ready | 409 seeds:not_ready | P§9 / MVP |
| A093 | DELETE | U/prompt-tasks/{task_id} | If-Match →204；仅终态且无未定在途、无活动消费者 | 409 task:in_use | P§7、整合 / v1 |
| A094 | GET | U/prompt-tasks/{task_id}/coverage | 无 →CoverageReport及缺口，不重新调用LLM | — | P§5/7、整合 / MVP |

### 5.9 import/export schemes 与导入

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A095 | GET | P/import-schemes | page →Page ImportScheme | — | E§13 / MVP |
| A096 | POST | P/import-schemes | ImportSchemeInput →201 Scheme | 422 schema:invalid | E§13 / MVP |
| A097 | GET | P/import-schemes/{scheme_id} | 无 →Scheme/version | — | D/E / MVP |
| A098 | PUT | P/import-schemes/{scheme_id} | SchemeInput+If-Match →Scheme；updated_at CAS | 409 concurrency:version_conflict | D/E / MVP |
| A099 | DELETE | P/import-schemes/{scheme_id} | replacement_default_id?/expected_versions →204；默认切换同事务 | 409 scheme:last_or_default | D/E / MVP |
| A100 | POST | P/import-schemes/{scheme_id}/set-default | expected_versions（新/旧）→Scheme；唯一默认 | 409 concurrency:version_conflict | D§6.4、整合 / MVP |
| A101 | GET | P/export-schemes | page →Page ExportScheme | — | E§13 / MVP |
| A102 | POST | P/export-schemes | ExportSchemeInput →201 Scheme | 422 schema:invalid | E§13 / MVP |
| A103 | GET | P/export-schemes/{scheme_id} | 无 →Scheme/version | — | D/E / MVP |
| A104 | PUT | P/export-schemes/{scheme_id} | SchemeInput+If-Match →Scheme；预留链可存不可执行 | 409 concurrency:version_conflict | D/E / MVP |
| A105 | DELETE | P/export-schemes/{scheme_id} | If-Match →204，不删已有导出目录 | 409 scheme:last_scheme | D/E / MVP |
| A106 | GET | U/exchange/capabilities | 无 →reader/exporter/编码/sorter/pipeline启用状态 | — | E§9–11、整合 / MVP |
| A107 | POST | P/imports/preview | scheme_id/root_handle →202 operation→manifest预览，复制前查错 | 409 import:root_unavailable、422 sidecar:invalid_layout | E§9、整合 / MVP |
| A108 | POST | P/imports | preview_id/digest/name →202 SourceView；建import源并启动 | 409 preview:stale、503 unavailable | E§13 / MVP |
| A109 | POST | P/sources/{source_id}/import-root | root_handle+If-Match →202核验operation；核对冻结manifest | 409 import:input_changed | D§2.1、E§9 / MVP |
| A110 | GET | P/imports/{source_id}/summary | page →done/failed/skipped计数和重复明细 | 422 source:not_import | E§9、整合 / MVP |

导入 A108 与 A049+A053 是同一个 ImportService 用例的便捷组合，不能各维护一套状态机；A108 失败返回已创建 source_id 供查询，不暗中创建第二源。根重选 A109 只建立会话授权与核验，不解锁参数，不自动 resume。DELETE A099 的替代默认项放 query（整数），版本映射放专用 `X-Expected-Versions` 编码头，避免 DELETE body 的客户端兼容差异；其结构/大小验证同 JSON expected_versions。

### 5.10 exports 与 storage scan

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A111 | POST | P/exports/preview | scheme_id →202 operation→过滤/样例/trigger警告摘要 | 422 trigger:missing、501 sorter:unsupported | E§13 / MVP |
| A112 | POST | P/exports | scheme_id/preview_id/digest/trigger_override_digest? →202 ExportView；重新冻结 | 409 preview:stale、422 trigger:in_style_prompt、501 pipeline:unsupported | E§13 / MVP |
| A113 | GET | P/exports | page →Page ExportView，活动任务+正式报告/中断收据索引 | — | E§10/12补齐 / MVP |
| A114 | GET | P/exports/{export_id} | 无 →ExportView；重启从正式report/收据恢复只读状态 | 410 export:session_expired | E§13 / MVP |
| A115 | GET | P/exports/{export_id}/report | 无 →ExportReport，finished才可读 | 409 export:not_finished | E§13 / MVP |
| A116 | POST | P/exports/{export_id}/cancel | If-Match →202/200；无resume，正式目录不删 | 409 export:already_finished | E§13 / MVP |
| A117 | POST | P/storage/scans | 无 →202 operation→项目分区/审图/源统计 | 503 storage:busy | 05§3.5、整合 / v1 |
| A118 | POST | U/storage/scans | known_ids? →202 operation→仅已知项目总占用 | 423 project:locked（逐项报告） | 05§3.5、整合 / v1 |
| A119 | GET | U/storage/scans/{scan_id} | page/sort →Page项目统计；scan_id为operation_id | 410 session:expired | 05§3.5、整合 / v1 |
| A120 | POST | P/storage/cleanup/preview | kind=thumbnails/exports/rejected_files →202 operation→CleanupPreview | 409 storage:leased | 05§3.5、整合 / v1 |
| A121 | POST | P/storage/cleanup | CleanupConfirmation →202 operation→逐文件结果；confirm必须true | 409 cleanup:preview_stale、storage:leased | 05§3.5、D§5.5 / v1 |
| A122 | GET | P/storage/summary | 无 →最近扫描摘要/as_of/stale；未扫描null，不伪造实时值 | — | 05§3.5、整合 / v1 |

### 5.11 backends capabilities/account_status、tag search/translation

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A123 | GET | U/backends | 无 →注册后端列表/available/reserved/reasons | — | G§2、F14b / MVP |
| A124 | GET | U/backends/{backend_id}/capabilities | 无 →BackendCapabilities、参数schema/锁定项/只读词库能力 | 503 backend:incompatible_schema | G§4、P§9、F14b / MVP |
| A125 | GET | U/backends/{backend_id}/account-status | project_id/source_id →AccountStatus或null；字段均可空 | 422 source:backend_mismatch | G§5、F21 / v1预留 |
| A126 | GET | U/tags/search | q/provider=local或launcher/page →Page CatalogHit；默认local | 503 launcher:unsupported_schema | E§4/13、V§5 / MVP |
| A127 | POST | U/tags/translations/lookup | tags:str[]、provider →翻译/unknown/版本；不自动入库 | 503 catalog:unavailable | E§4、整合 / v1 |
| A128 | PUT | U/translations | tag/value+If-Match →revision；空值明确删除，tag不用路径段 | 409 catalog:revision_conflict | E§13、整合 / v1 |
| A129 | POST | U/tags/imports/preview | multipart file/kind=catalog或translations/format →ImportPreview | 422 schema:invalid | E§4/13 / MVP |
| A130 | POST | U/tags/imports/commit | preview_id/digest+If-Match →CommitReport | 409 catalog:revision_conflict | E§4/13 / MVP |
| A131 | GET | U/tags/catalog-status | 无 →revision/覆盖完整性/alias能力/翻译数量 | — | E§4、F22补齐 / MVP |
| A132 | POST | U/validation/trigger | trigger/style_prompts或project_id →Issue[]/digest/可强制项 | 422 trigger:in_style_prompt | P§9、E§8、F22 / MVP |

A125 未实现真实账号查询时返回 local_estimate 或 null：绝不把估算当余额；未采纳 G.C06 时 estimate_scope=process_observed，明确累计不完整。A124 聚合 P 的 Launcher 只读词库能力，不猜工具名，不让前端提交任意 MCP 方法。A132 正常校验结果用200 Issue[]（含硬错误），格式非法422；正式启动/导出门禁仍以422拒绝硬错误，existing_tag 仅警告可绑定摘要强制。

### 5.12 近似重复与 WD 安装辅助合同

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A133 | POST | P/duplicates/scans | scope/threshold（必填0..64）→202 scan_id | 413 batch:too_large | V§9 / MVP |
| A134 | GET | P/duplicates/scans/{scan_id} | page →Page唯一pair/进度/跳过计数，固定三键排序 | 410 session:expired | V§9 / MVP |
| A135 | DELETE | P/duplicates/scans/{scan_id} | If-Match →204取消并释放会话结果，不删图片 | 409 conflict | V§9 / MVP |
| A136 | GET | U/tagger/capabilities | 无 →设备/后端/可用性/禁用原因；WD默认关闭 | — | V§9 / MVP |
| A137 | GET | U/tagger/models | page →Page模型清单/固定revision/安装状态 | — | V§9 / MVP |
| A138 | POST | U/tagger/models/install | manifest_id →202 installation_id，明确安装授权 | 422 tagger:manifest_invalid、503 unavailable | V§9 / MVP |
| A139 | GET | U/tagger/installations/{installation_id} | 无 →OperationView/下载字节/校验状态 | 410 session:expired | V§9 / MVP |
| A140 | DELETE | U/tagger/installations/{installation_id} | If-Match →204停止本次安装；不删除已被使用模型 | 409 tagger:in_use | V§9 / MVP |

### 5.13 图片统计与范围审图预览（F14补充）

| ID | 方法 | 路径 | 请求 → 响应要点 | 领域错误 | 来源 / 阶段 |
|---|---|---|---|---|---|
| A141 | GET | P/images/stats | §4.2图片过滤 + 重复group_by（默认review_status、rating；可选source_id、annotation_coverage）→ImageStats；无分页/排序，未知参数422 | — | F14 C33 / v1 |
| A142 | POST | P/images/review/preview | `{query: ImageQuery}`+Idempotency-Key →200 ReviewPreview；冻结query成员和逐图Version供A062，不改图片 | 413 batch:too_large | F14 C32、§4.3 / v1 |

上述接口归现有image_facade：`stats(ctx, query: ImageQuery, group_by: list[StatsGroup]) -> Awaitable[ImageStats]`、`preview_review(ctx, query: ImageQuery, key: UUID) -> Awaitable[ReviewPreview]`；A062为`review(ctx, input: ReviewRequest, key: UUID) -> Awaitable[ReviewResult]`，list返回改为ImagePage。依赖D读事务/单写队列、E覆盖投影及既有会话预览/幂等缓存；错误与重放见§4.3，最小测试T43–T47。A141只读可安全重试，单次快照内按每个group_by分别计数（非交叉分组），每图在每个维度只计一次，bucket省略零计数、null表示未赋值，各维度合计=total，空集groups仍含请求维度但buckets=[]；不受当前页或cursor影响。静态路径stats优先于`{image_id}`匹配。

## 6. SSE 事件合同

### 6.1 通道、信封与事件类型

唯一通道为 A006；源、jobs、prompt、exports 与辅助任务均用 topics/resource_id 筛选，不实现各 F 草案的重复 events 路由。`Content-Type:text/event-stream; charset=utf-8`、`Cache-Control:no-store`、`X-Accel-Buffering:no`；禁压缩/代理缓冲。每15秒注释心跳，客户端空闲45秒重连；网络等待不占 DB reader 或外部服务并发闸门。

每事件字段：SSE `id: <epoch>:<sequence>`、`event: <type>`、`data: <JSON>`。JSON 公共信封为 `{schema_version:1, epoch:str, sequence:int, occurred_at:UTC, project_id:UUID|null, resource:{kind,id}, version:Version|null, payload:object}`。sequence 全进程递增，过滤后序号不连续是正常情况；不能仅凭跳号断言丢失。pause_reason/cycle 等无持久字段仍可用源 GET 当前会话摘要重建。

| 事件类型 | payload 必需字段与客户端含义 |
|---|---|
| stream.ready | `instance_id, current_sequence, replayed:bool`；连接建立，不代表任务已完成 |
| stream.resync_required | `reason: Literal[epoch_changed,buffer_expired,slow_consumer], snapshot_urls:str[], current_sequence:int`；丢弃旧游标并GET重建 |
| project.opened / project.closed | `project_id, generation, recovery_pending:bool`；关闭使项目资源/预览失效 |
| source.started / source.paused / source.done / source.canceled | `source_id, cycle_id:UUID或null, status, counts:{pending,sent,done,failed,skipped}, reason_code:str或null, in_flight:int, restore_pending:bool` |
| source.cost_confirmation_required | 上述源摘要加 `cycle_id, item_id, estimated_anlas:int, cost_policy:'ask'`；deny用source.paused，不能渲染同意动作 |
| source.item_completed | `source_id,item_id,attempt:int,image_id:int或null,outcome:Literal[done,failed,skipped],counts,reason_code`；done才有新图，failed/skipped也更新计数 |
| source.progress | `source_id,current_item_id,phase,counts,estimated_anlas:int或null`；节流摘要，非完成凭据 |
| job.started / job.paused / job.done / job.failed / job.canceled | `job_id,batch_id,status,total,done_count,failed_count,skipped_count,reason_code` |
| job.image_completed | 上述job摘要加 `image_id,outcome:Literal[done,failed,skipped],annotation_ids:int[]`；LLM双层提交后只发一次 |
| image.updated / annotations.changed | `image_ids:int[],fields:str[],annotation_ids:int[]`；只作失效通知，无tags/NL内容 |
| rule.applied / rule.undone | `op_id,affected_count,image_ids:int[]或null,invalidate_scope:bool`；大范围不塞完整changes |
| prompt.started / prompt.paused / prompt.done / prompt.failed / prompt.canceled | `task_id,status,completed,total,ready,coverage_status,reason_code` |
| prompt.item_completed | `task_id,index,outcome,completed,total,ready:false`；正文通过outcomes GET |
| export.started / export.progress / export.finished / export.failed / export.canceled | `export_id,status,selected,exported,skipped,failed,report_url:str或null,reason_code`；finished仅目录正式发布后 |
| operation.progress / operation.done / operation.failed / operation.canceled | `operation_id,kind,completed,total,phase,reason_code,result_url:str或null`；覆盖安装/扫描/重分级/预览 |
| config.changed | `resource_kind,resource_id,revision`；不含URL/秘密/配置正文 |
| server.draining | `deadline:UTC,reason`；停止新提交，查询直至断开 |

事件只含摘要，禁止 token、code、完整 prompt/tags/NL、图片base64、原始工具响应和绝对路径。需要正文必须认证 GET。事件发布在 DB 提交/产品文件原子替换/导出发布确认之后；通知失败不回滚业务。

### 6.2 重连、快照与 Last-Event-ID

进程内环形缓冲建议保留最近10分钟且最多10000条/8MiB，任一上限触发淘汰；不是持久队列。fetch SSE 重连发送 `Last-Event-ID`，指数退避1/2/4至30秒加抖动。必须沿用原过滤器；换 filter 建新订阅并GET快照。

同epoch且游标仍在缓冲内补发匹配事件，再切实时；未知epoch/过期游标发送 resync_required 后继续实时，不伪造补齐。格式错误400；未来sequence也要求resync。进程重启无旧token，401必须重新引导，不无限重试旧凭据。

无缝初始化推荐先连 SSE 并缓存通知，再 GET 资源快照，再应用缓冲通知作为“失效需重取”信号；不把计数事件当增量直接+1。重放按 EventId 去重，GET与SSE竞态用version/最终GET解决。断开期间已完成工作以 source/items、job/outcomes、prompt文件、export report 为事实来源。

### 6.3 背压、节流和每图刷新

每连接有界队列建议256条/1MiB、最多4个订阅；普通 progress 同资源250ms合并，只保留最新累计计数。状态/费用确认/错误保留最新快照；每图提交后的 source.item_completed/job.image_completed 正常负载不得被普通进度合并掉，每完成一张图触发一次列表失效与刷新机会。

前端列表最多一个GET在途：完成事件置dirty，空闲立即刷新；GET在途又来完成事件则结束后再刷新。这满足逐图可见性，又不会让每秒多张产出排出无限GET。事件计数覆盖本地计数，不累加；单图双标注只刷新一次。

慢客户端队列溢出时停止声称完整交付，优先发送resync_required并关闭；若socket已阻塞无法发送，直接断开，重连依据缓冲或GET重建。事件广播绝不阻塞 writer、源或模型调用。大范围 image_ids 超过500改 invalidate_scope=true，单帧上限64KiB。

## 7. 图片、缩略图、文件路径与存储

### 7.1 图片服务

A063/A064 仅按当前 project_id+image_id 查 D.ImageRow，实际文件为 `images/<image_id>.<小写原扩展名>`；不接受 filename/path/url 参数，不暴露 cache 文件路径。缺行404，file_missing或实际文件消失410 `image:file_missing`；实际消失交文件审计服务置missing，保留原review，不由媒体路由直接SQL。

原图按真实格式返回 image/png、jpeg、webp 等，`X-Content-Type-Options:nosniff`、`Content-Disposition:inline`；拒绝SVG/HTML伪装，Pillow解码验证归入库/媒体检查。缩略图等比、EXIF归正、透明白底，size仅256/512，编码WebP，原图不改；缓存键包含sha256、尺寸、编码器版本。写临时文件原子替换，同键单航班；cache删掉仅重建。

媒体200/304均返回 `Cache-Control: private, max-age=0, must-revalidate`、`Vary: Authorization` 和强ETag：原图为带双引号的sha256，缩略图为原图sha256+size+变换/编码器版本的摘要；不使用审图version。允许私有缓存保存Bearer响应，每次复用须重验证；认证、项目句柄及文件存在/身份检查先于条件匹配，missing不能返回304。If-None-Match支持标签列表、弱比较及`*`，命中返回304无body，不命中200完整字节；304保留当前ETag/Cache-Control/Vary。Last-Modified使用已登记资源时间，但ETag优先；不设public/immutable长时裸访问。MVP不实现Range，`Accept-Ranges:none`，Range请求忽略并返200完整图；HEAD若框架启用也须同样鉴权，只不发送body。

文件打开与读取期间持共享图级租约，reject清理拿独占租约；校验目标在可信根内、拒junction/reparse逃逸，打开后核对最终句柄路径和文件身份以缩小TOCTOU窗口。缩略图生成同样持租约，不凭字符串 startswith 判根。

### 7.2 路径参数白名单与浏览器目录选择

浏览器不能直接可信地取得本地绝对目录；推荐后端受控原生目录对话框（A004，Q03）由已认证请求发起，单个在途，显示用途。用户选定后生成内存 PathGrant，绑定用途、当前用户、项目代次和15分钟TTL；活动导入持有租约至本周期结束，paused后重新授权可续跑。对话框取消返回operation canceled，不创建空根授权。

CLI路径是用户明确的本地输入，可通过当前用户命名管道授权为同类型grant，再调用同一服务；不能因此开放HTTP任意path字段。无桌面会话时picker能力禁用，用户通过CLI创建grant/执行；不临时给HTTP加任意磁盘浏览接口。

路径准入仅三类：项目创建目标（不存在且后缀.slproj）、项目打开根（已有合法容器）、外部导入根（只读）。产品配置/MCP发现路径/WD模型路径由固定产品目录或CLI受控配置确定，前端只提交ID。导出只能位于当前项目exports，output subdirectory为版本目录内部相对路径；无HTTP任意导出目标。项目创建授予的是目标本身，不是可遍历父盘权限。

所有相对路径拒绝绝对/盘符/UNC/设备路径、`.`/`..`、反斜杠绕过、ADS冒号、NUL、Windows保留名、尾随点/空格和解码后逃逸；只解码一次并基于最终路径验证。导入递归不跟随junction/目录链接，不进入项目本体；上传文件名仅展示、生成服务器文件名，不能作为落盘目标。外部原目录不写project.db，import_manifest只有根内相对路径和摘要。

### 7.3 扫描与清理合同

项目扫描只读：images按keep/unreviewed/reject/source拆分，cache、exports（含report）、DB/WAL/SHM、迁移备份、`.pending`、其它项目文件分别统计；逻辑分类不重复加总，missing记录与实际文件字节分开。Launcher外部画廊副本不计入。阈值建议20GiB/100GiB暂用可配置值，单位选择待Q05，不把旧文档GB字样悄悄当已定二进制。

跨项目只访问最近记录，目录大小可直接只读扫描；按审图拆分仅当前打开项目提供，别的项目显示总量/不可访问/正在占用等状态。不开第二个可写ProjectHandle，不迁移、不恢复、不扫描全盘。统计有as_of、errors和partial，锁或权限失败逐项报告，不伪造0字节。

清理两阶段：preview给出kind/候选/大小/不可恢复说明与digest；commit必须confirm=true且摘要/项目代次未变。kind仅thumbnails、exports、rejected_files。删reject文件前再核对review=reject和文件身份，禁止并发导出/恢复审图，删除后F01置missing；已missing重复删除no-op。清cache不能触及`.pending`，清exports跳过活动/未结清目录；没有清理DB/WAL/恢复证据的通用路径入口。部分文件失败返回CleanupReport逐项结果，不能谎称跨文件删除事务原子。

## 8. 安全合同

Bearer校验覆盖全部API、图片、缩略图和SSE，A002仅有明确的引导例外；静态壳可公开但不含业务数据。token 32字节CSPRNG、进程内常量时间比较、启动轮换，无长期cookie/session。token泄露时退出/重启失效；日志中Authorization、code、SecretInput、原始请求body统一遮蔽。

Host严格匹配实际 `127.0.0.1:<port>`（开发代理也发送此Host），拒绝任意域名/localhost别名/重复Host/非法authority；不信X-Forwarded-Host/Forwarded，关闭proxy headers自动信任。Origin必须是生产同源或启动显式允许的一个Vite Origin，拒绝null、通配和任意127网段；这与loopback bind共同防DNS rebinding。

非浏览器CLI可无Origin但必须Bearer；带 `Sec-Fetch-Site:cross-site` 的请求拒绝。A002必须Origin且用途匹配，不给无Origin的普通网络客户端换码。CORS默认关闭跨域，开发只精确Origin、allow_credentials=false，允许Authorization/Content-Type/If-Match/Idempotency-Key/Last-Event-ID/X-Expected-Versions等必要头；OPTIONS先校验Host/Origin再回允许头，不执行业务，任何CORS错误不以200吞掉。

生产壳建议CSP `default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'`，Referrer-Policy=no-referrer；开发HMR仅开发模式增加精确ws源，不把宽松CSP带到生产。第三方脚本/遥测不进入bootstrap页；服务端文档OpenAPI按Bearer提供或开发模式关闭，不成为匿名业务探测入口。

建议统一限制（Q05）：普通JSON 16MiB；字典/规则/翻译上传10MiB文件、multipart总11MiB、最多1文件；导入同名txt 1MiB；HTTP不提供原图上传入口，目录导入单图100MiB/解码100MP，超限在预检报告；显式批处理上限5000图或16MiB计划，seeds最多10000条且总JSON仍16MiB。读取Content-Length只能预检，流式累计到阈值立即413，chunked同样限制；压缩请求体不支持（415），防解压后绕过。415也走统一ApiError。

端点base_url仅允许http/https、拒userinfo/query/fragment中的秘密，不跟随携带凭据的重定向；可合法配置本机LLM，不把外部服务连接混成浏览器CORS策略。SecretStore仅DPAPI CurrentUser，环境变量优先；API只返has_secret/effective_available/origin=environment或stored，不返变量名/内容。请求验证异常也不得回显SecretInput。

本威胁边界不承诺防御已控制同一Windows用户的恶意本地进程；命名管道使用当前SID ACL、拒远程客户端并校验连接者SID/服务进程身份，不能仅靠可猜管道名。产品runtime定位文件可删可重建，不含token；同用户以外不可通过管道取得凭据。

## 9. CLI（F11）合同

命令名 `stylelora`（等价 `python -m stylelora`）；不保留旧S0–S6子命令。serve为长驻进程；其它命令默认附着已有实例，通过ACL管道获取当次内存凭据/路径grant，再调用本地HTTP或同服务分派。无实例时可选择独立服务容器模式持产品/项目锁直调服务；`project open`/`ui`需启动长驻serve才有持续打开语义，不能打开后随CLI退出却声称常驻。

| 子命令 | 参数 / 输出 | 共享服务与完成语义 |
|---|---|---|
| serve | `--port 0 --project PATH? --no-browser --dev-origin ORIGIN?` | Runtime；单实例，当前用户后台任务持续存在 |
| ui | `--print-link?` | 请求新一次性引导码并打开浏览器；不输出API token |
| project init | `PATH --name NAME --trigger WORD?` | ProjectService.create；不覆盖；可独立运行，完成后安全关闭 |
| project open | `PATH` | 开启/附着serve后显式切换，输出project_id；旧项目安全关闭 |
| project close / status | 无 | close等待释放；status无副作用，不扫描磁盘 |
| source create | `--type {nai,import} --spec FILE` | SourceService；spec严格schema，禁止秘密/产品引用写项目 |
| source run | `SOURCE_ID --seeds FILE? --wait/--detach` | draft需完整seeds或已核验import输入；paused走resume；默认不授权非零费用 |
| source pause / resume / cancel | `SOURCE_ID` | 同HTTP控制；resume不继承旧费用确认 |
| source confirm-cost | `SOURCE_ID --cycle-id HEX32` | 明确当前周期同意；deny仍拒绝；无隐式yes-to-all |
| source retry-item | `ITEM_ID --expected-attempt N` | 同F01人工重试；只加一次attempt，不自动run |
| import | `ROOT --scheme ID --name NAME --wait/--detach` | 授权根→预览→同ImportService；报告重复汇总，原文件只读 |
| job run | `--executor wd[,llm_vision] --mode {selected,uncovered,all} --ids ...或--filter FILE` | 建batch且冻结；不直接调用模型/写annotations |
| job pause / resume / cancel / status | `JOB_ID` | 与HTTP同状态机/版本/CAS；终态重跑建新job |
| prompt run / status / pause / resume / cancel / seeds | `--spec FILE`或`TASK_ID` | 产品PromptTaskService；seeds输出内容投影，不写项目引用 |
| export | `--scheme ID --wait/--detach --accept-trigger-warning DIGEST?` | 先预览再提交；只允许现有tag警告摘要强制，style冲突不可强制 |
| storage scan / cleanup | `--known?`或`--kind ... --confirm-digest DIGEST` | 同只读扫描/两阶段清理，无任意rm路径参数 |
| doctor | `--project PATH? --json --probe-network?` | 配置schema、SQLite、锁、目录可写性、DPAPI可用、模型清单、恢复日志诊断；默认无付费/安装/修复动作 |

通用 `--project PATH` 在需要项目的独立命令中通过ProjectService打开，不能直接sqlite3连接；附着时先验证当前项目身份，未经明确open不自动切换。`--json` stdout仅结果JSON，进度/提示stderr；密钥输入用交互无回显或标准输入，不接受命令行明文参数。

默认长命令 `--wait`，附着服务下 Ctrl+C 只停止本客户端等待并打印实体ID，任务继续；明确pause/cancel命令才改变服务任务。独立模式不能detach，Ctrl+C触发服务优雅关闭、running→paused；提示外部已发送操作可能未结清。非交互模式遇费用ask输出paused/确认需求并退出，不自动回答。

退出码：0请求成功且目标完成；2参数/预检错误；3认证/运行实例不可用；4版本/状态/项目锁冲突；5依赖/I/O失败；6任务部分失败；7已暂停、需用户确认或结果未知；130客户端中断。`--detach`受理成功返回0及task/status_url，不把202伪称done。doctor默认只读检查，不执行迁移/清理/费用探测。

## 10. unittest/httpx 验收清单

以下仅测试规格，不编写或运行实现测试。使用unittest/IsolatedAsyncioTestCase、httpx.AsyncClient+ASGITransport、显式lifespan上下文、TemporaryDirectory真实SQLite/WAL、假Clock/随机源/DPAPI/Picker/MCP/LLM/模型；不调用真实收费服务。HTTP流式断连、Host、管道及OS锁另用本机真实socket/Windows进程集成fixture，不能只靠ASGITransport证明网络行为。

| 编号 | 最小场景与断言 |
|---|---|
| T01 | 两次serve只一个实例；abandoned mutex恢复；仅有残留定位文件不误判存活 |
| T02 | 127.0.0.1:0保留绑定socket；指定占用端口失败；0.0.0.0/多worker/reload拒绝 |
| T03 | 同根重复open复用；项目进程锁423；切换关闭前不开放新项目writer |
| T04 | 关闭时running源/job→paused、清费用ack；sent unknown保留；在途合法结果可落库，关连接后零写入 |
| T05 | 旧project_id/cursor/grant/后台回调不能读写新项目；新项目打开失败保持无项目 |
| T06 | token每进程不同且32字节随机；缺/错Bearer401；图片/SSE也必须认证 |
| T07 | bootstrap code仅一次且60秒到期，绑定Origin；并发兑换仅一成功；响应/日志/URL请求无明文token |
| T08 | 错Host/DNS域名/null Origin/跨站Origin拒绝；代理头不能改信任对象；CLI无Origin仍需Bearer |
| T09 | Vite准确Origin/Host代理可用；OPTIONS不执行业务；CORS无*无credentials；生产禁开发Origin |
| T10 | 静态index不含秘密，API404不变HTML；恶意assets路径不可读项目/用户目录；CSP不带生产HMR通配 |
| T11 | 全部A路由OpenAPI方法/路径/响应类型与表一致；未知字段/重复JSON键/NaN/bool整数拒绝；错误体一致 |
| T12 | 每类HTTP列表默认50，1/1000边界，1001拒绝；篡改游标/跨过滤/跨项目失败；内部DAL200不泄漏 |
| T13 | upper_id排除新插入；DB过滤成员可变明示；产品revision改变cursor409；排序注入不能进SQL |
| T14 | If-Match缺428、旧409；两客户端并发仅一写入；A→B→A旧会话version失效；全读集仍逐项验证 |
| T15 | DPAPI/环境覆盖优先级，秘密写成功只返has_secret；校验失败日志、错误details、OpenAPI示例均无秘密 |
| T16 | 同键同摘要返回原结果、不同摘要409；持久字典/prompt凭据跨重启可核验；无凭据创建未知不能重发 |
| T17 | 改角色/端点不改已冻结job；删除仍绑定endpoint409；clean非空拒绝；上游401映射503不退出本地会话 |
| T18 | Picker授权用途/TTL/project绑定；CLI路径只通过ACL管道；UNC/ADS/junction/尾随点/目录穿越拒绝 |
| T19 | 源首次启动全量seed一次落库，重复start不多items、不双submit；续跑不查字典；cost_policy独立可改 |
| T20 | ask过期cycle409、deny不能同意；普通resume重新确认；retry-item只加一次attempt，sent不可retry |
| T21 | 字典导入10MiB边界、TXT有损提示、P任务不写project.db jobs；seeds不足ready=false不得启源 |
| T22 | 图片仅ID定位，missing410；If-None-Match命中仍先鉴权；换项目同image_id不复用旧URL权限 |
| T23 | 缩略图同hash/尺寸单航班、删cache可重建；拒SVG伪图、100MP超限；原图sha不变 |
| T24 | 媒体/导出共享租约与reject清理互斥；reparse替换逃逸拒绝；恢复文件哈希错不解除missing |
| T25 | manual完整refs含缺失层，后台新层导致整批409；merge_meta不能由客户端注入；NL/tags同成同败 |
| T26 | 规则预览过期410、stale409、零变更无日志；最近撤销及后续标注冲突；重复apply仅会话结果复用 |
| T27 | 202前实体/任务已注册；请求断开任务仍受监管；WD/LLM拆批串行，双层结果计一图 |
| T28 | prompt恢复paused；外部未知结果不自动重发；导出无pause/resume且中断不发布正式目录 |
| T29 | 重新分级不注册非法executor、不覆盖manual/refusal；duplicates不删图；安装无显式动作不下载 |
| T30 | 导出默认keep、reject/missing恒跳过；trigger已有tag警告摘要可强制，style冲突不可强制；非空pipeline501 |
| T31 | 注入fake服务验证路由不访问SQL/连接；读线程/WAL、全局LLM并发≤2、单写线程归属不变 |
| T32 | storage只扫最近记录，跨项目不打开第二writer；WAL/收据/report分类不重复；不可访问不算0 |
| T33 | cleanup必须preview+digest+confirm；状态变keep或文件身份变就拒绝；删后崩溃审计补missing，历史不丢 |
| T34 | DB提交失败不发成功SSE；提交后事件丢失可GET恢复；job双层只一个image_completed |
| T35 | Last-Event-ID同epoch补发、过期/重启resync；过滤跳号不误判丢失；重复事件不重复加计数 |
| T36 | 慢客户端队列溢出resync/断开，writer不阻塞；progress250ms合并；正常每图都有完成通知 |
| T37 | fetch SSE Bearer、15秒心跳、代理不缓冲；无token query/prompt/图片/绝对路径；订阅上限429 |
| T38 | Content-Length和chunked均按实读计数；multipart/JSON/计划分别413；压缩请求415；非法model/tag不拼路径 |
| T39 | CLI和API用同fake服务同事务；已有实例附着不二次打开DB；独立模式持同产品/项目锁 |
| T40 | source run非交互遇费用ask退出7不submit；--detach仅附着可用；Ctrl+C语义与模式一致 |
| T41 | 管道跨SID/远程客户端拒绝；不含token的定位文件被篡改不能绕过身份检查 |
| T42 | doctor默认无外部调用/安装/迁移；JSON stdout纯结果、秘密输入不出现在参数/日志/诊断 |
| T43 | A059/A060对不透明/透明图片返回准确width/height/has_alpha，missing保留元数据；写入这些只读字段422 |
| T44 | A059过滤后total与同快照items一致；around_id在id/-id排序及页边界均定位正确，过滤外404、与cursor并用422；空集total=0，续页排除upper_id后的新增 |
| T45 | A142→A062 query批量返回逐张旧状态与新version；同数量成员替换、版本改变、expected_total错均409且零写入；预览过期410、5001张413；targets撤销还原不同旧状态，并发改动整批拒绝 |
| T46 | A062同键重放保留原旧状态且不再次展开query，异摘要409；撤销新键成功、重复撤销重放；重启旧预览失效，未知提交只核查；A142同键返回同预览 |
| T47 | A141重复过滤OR/跨字段AND与A059一致；四维度分别计数、coverage三态及null桶、空集形状正确，合计均为total；分页/未知分组422，stats不误匹配image_id |
| T48 | A063/A064首次200含ETag/Cache-Control/Vary；标签列表/弱标签/*命中304无body且头齐全，不命中200；缩略图尺寸/编码器改变ETag改变、仅审图不变；命中但无权限401/403、missing410 |

## 11. 跨文档冲突与统一方案

| 冲突 | 原文位置与差异 | 本文统一合同 / 保留边界 |
|---|---|---|
| X01 路由作用域 | G§8使用/api/sources；V§9/E§13项目带project_id；P§9把字典/tasks放prompt-production | 项目统一P，产品统一U；dictionary/prompt-task拆资源；旧草案路径不注册兼容别名，尚无实现用户无需迁移 |
| X02 默认分页 | D内部PageRequest及P/V/E HTTP草案200/1000；本任务要求50 | HTTP统一50/1000、不透明keyset cursor；D内部200不改；不把offset引入DAL |
| X03 错误DTO | P使用message/issues；V使用safe_message；E使用details/request_id | 统一error信封/message/issues/details/retry_action/outcome；领域原因码保留，不透传供应商异常 |
| X04 并发前置条件 | 产品revision、方案updated_at、manual read_set、源expected状态/attempt分散 | HTTP统一If-Match/expected_versions；服务转换为现有F01 CAS，完整读集仍强制，不新增版本列 |
| X05 SSE | G按源events/sequence，P按任务events，V按jobs/events且epoch | 单U/events、epoch:sequence与topics；快照恢复统一；每图提交事件保留，SSE非持久队列 |
| X06 幂等承诺 | P产品文件可持久request_key；D创建source/方案明确无持久请求幂等；E导出/规则仅会话 | 请求头统一但保证不一刀切：会话缓存+既有领域凭据；跨重启未知创建只核查，不承诺全API exactly-once |
| X07 长任务命名 | V重分级/扫描会话任务；P产品task；E导出planned/finished；D jobs仅wd/llm_vision | TaskRef只做传输定位，各自状态/存储不变；通用OperationView不新增DB表/executor |
| X08 token与文件选择缺口 | 05§13#18未定token；V/E禁止绝对路径却没有浏览器root_handle获取接口 | 一次code换内存Bearer、fetch SSE/图片；受控picker/CLI管道创建用途grant，见Q01–Q03 |
| X09 F22强制 | P§10概括existing_tag为422；05§10.4/E§8允许已有tag警告强制 | 校验200 Issue[]；正式导出existing_tag允许摘要强制，style冲突硬422；无完整索引仍unverifiable |
| X10 标签规范化 | G/P折叠连续空白/alias，D/E合并键严格不折叠 | API原样使用E.C11分层建议：历史合并遵D，新输入解析策略独立；不静默重写历史或去重键 |
| X11 预留能力与schema | E方案schema列auto/hardlink；C14/C16未采纳，pipeline非空可存 | capability标支持状态；auto/hardlink执行入口禁用，pipeline仅保存执行501，不把可存等同可跑 |
| X12 费用与账号 | 04旧uc_preset可编辑、F21像完整源累计；05/G要求固定None，G.C06未采纳 | 能力固定uc_preset=null/quality_toggle=false；账户估算仅process_observed，null余额，绝不参与费用决策 |
| X13 blocklist/导入段落 | 05§10.3待确认剔除；E.C12建议warn；05§8.2 unknown→NL与§10.5无第二段不建NL冲突 | 不在API层裁决算法；blocklist显式选方案，歧义sidecar按E保守拒绝；沿C12/C15待原负责人确认 |
| X14 默认导入/导出方案 | D默认导入唯一、导出无is_default；E泛称方案默认切换 | 仅import有set-default；删除最后export拒绝，绝不为export虚构is_default字段 |
| X15 目录与恢复 | 05产品路径未决，G/V产品恢复/WD位置是推荐；本任务已明确路径 | 直接采用APPDATA配置、LOCALAPPDATA秘密/恢复/WD；项目收据仍images/.pending，不当cache清除 |

未解决的领域问题不重编号为新的业务提案：G的真实Launcher契约、P的nsfw模板、V的R2/R5及模型清单、E的C11–C16继续由各自文档负责。本文只在capabilities给出unavailable/reason，不能伪称这些适配已验证。

## 12. 建议变更 C17–C23

以下仅建议同步的合同，未修改任何参考文档，也不宣称已获架构裁决；沿既有C01–C16编号。已经明确的单进程/单项目/DAL/状态边不进入重决策。

| 编号 | 建议变更 | 推荐与未采纳边界 |
|---|---|---|
| C17 | 各F草案统一资源路径、50分页、错误体和版本前置条件 | 采用§4–5并由后续维护任务同步来源；当前仅本文是汇总提案，不同时实现两套路由 |
| C18 | 补齐05§11.3 token下发 | 采用一次性fragment code→POST换内存token；fetch SSE/图片；未裁决前不开放匿名token或持久浏览器秘密 |
| C19 | 统一SSE为单事件总线和明确补发/背压合同 | 采用§6；进程有界缓存+事实GET，每图完成通知；不新增持久事件表 |
| C20 | 明确HTTP幂等键分层保证和CAS适配 | 采用§4.3，会话去重与既有产品/DAL凭据组合；不新增通用请求账本，不承诺跨重启exactly-once |
| C21 | 补齐浏览器路径授权及CLI附着通道 | 采用用途绑定PathGrant、原生picker、当前用户ACL管道；未裁决时仅CLI受控路径入口，不放开HTTP任意path |
| C22 | 明确辅助任务/导出资源合同与清理两阶段协议 | 采用OperationView+各领域原状态机、preview/digest/confirm；不把辅助任务塞jobs，不暗删missing历史 |
| C23 | 对齐安全基线、阶段标注和配置/恢复目录说明 | 固定loopback/Host/Origin/上传边界，APPDATA与LOCALAPPDATA分工；保留各模块未定能力，不扩大MVP模型/插件范围 |

**C30–C34（来自 F14）已并入**：C30见§4.1 ImageView、A059/A060；C31见§4.2 ImagePage/around_id、A059；C32见§4.3及A062/A142（会话预览保存逐图CAS，仍限5000张）；C33见§4.2过滤及§5.13 A141；C34见A063/A064与§7.1缓存头；验收追加T43–T48。待确认：超过5000张的单事务全选需要另议容量边界，本次不扩大既定批量上限。

## 13. 待确认 Q01–Q08（本文局部编号）

| 编号 | 待确认 | 推荐及实施边界 |
|---|---|---|
| Q01 | 浏览器token下发与刷新体验 | 采用60秒单次code、内存token、刷新需ui重新引导；不落盘优先。可评估内存cookie替代需另审CSRF，本文不引入 |
| Q02 | 产品单实例锁/管道的Windows实现与独立CLI模式 | SID命名mutex+当前用户ACL命名管道，校验对端身份；无实例短命令可同服务容器，open/ui启动serve；不靠PID文件锁 |
| Q03 | 浏览器选择本地目录所用原生对话框依赖 | 使用Windows原生目录/目标选择器适配，picker单在途，返回用途grant；无桌面时CLI授权，不开放任意HTTP路径 |
| Q04 | 优雅关闭30秒宽限期与不可取消原生工作 | 推荐可配置30秒，外部未知保收据/日志；文件副作用线程未退出不得释放项目锁，UI显示draining，强退走恢复 |
| Q05 | 限额、阈值和媒体参数 | 推荐JSON16MiB/上传10MiB/单图100MiB与100MP/缩略图256或512；存储提醒20GiB/100GiB且界面明示单位，均须性能验证 |
| Q06 | 无DB持久请求账本时是否接受跨重启幂等边界 | 推荐接受会话24小时缓存+现有自然键/产品凭据；未知创建人工核查，不加表。若需完整持久去重另提架构变更 |
| Q07 | 04旧F清单与新编辑器定位的MVP/v1拆分 | 推荐本文§1/§5：基础API/WD/导出MVP，审图编辑UI配套、视觉与辅助管理v1；F14界面仍独立设计，预留项不提前启用 |
| Q08 | 内置预设不可变、blocklist默认值及领域未决选项如何呈现 | 推荐内置预设只读、自建可改；blocklist在源裁决前显式选择；auto/hardlink/nsfw模板等未采纳能力禁用并给reason，不自动采纳C06–C16 |

交付验收：本文仅新增一个设计文件；140条方法/路径、42个最小测试规格、C17–C23七条提案、Q01–Q08八项待确认。无实现代码、无测试执行、无提交，也未改动其它文档。
