# 存储 CLI 与后台任务验收

后续更新（0.58.0）：当前项目 `storage scan` 已支持 `--standalone --project PATH`，命令完成后关闭自有容器；最近项目扫描和清理仍使用本文的附着流程。见 [独立查询与扫描验收](65-F11-独立CLI查询与扫描验收.md)。下文保留 0.40.0 历史范围。

日期：2026-10-28；版本：0.40.0。仅本地开发，不初始化、提交或上传 Git，不调用真实生成服务。

## 本轮范围

接续 0.39.0，完成 F11 §9 的存储 CLI 附着模式。命令名为 `stylelora storage`，等价 `python -m stylelora storage` 或仓库 `./scripts/run.ps1 storage`。使用现有当前用户 ACL 管道取得本次服务内存凭据，随后调用原 HTTP 接口；不直接打开数据库或执行文件删除，不建立第二套扫描/清理状态机。

服务必须已通过 `serve` 启动。未运行服务返回退出码 3，不自动启动短命服务、不自动打开或切换项目。文档允许的独立服务容器模式仍待后续；其它旧命令保持原有行为。未新增 HTTP 路由、DDL 或产品持久文件。

## 命令合同

所有子命令均输出一个 JSON 对象到 stdout；`--json` 为显式写法，也可省略。受理进度和等待中断提示写 stderr。`--help` 列出参数，非法参数退出 2，解析阶段不附着服务。UUID 必须为规范形式；摘要为 64 位小写十六进制；没有任意文件删除路径或 token 参数。

| 命令 | 行为 |
|---|---|
| `storage scan [--project-id ID]` | 读取会话，捕获当前项目句柄，再 POST 当前项目 storage/scans；给定 ID 必须匹配当前句柄 |
| `storage scan --known` | POST 产品 storage/scans，范围为受理时全部最近项目，不要求打开当前项目 |
| `storage scan --known-id ID [--known-id ID...]` | 仅扫描选定的最近项目，1–200 个不同编号；不能与 --known 或 --project-id 混用 |
| `storage summary [--project-id ID]` | GET 产品历史摘要，或指定当前项目摘要；不启动盘点，未扫描仍为 null |
| `storage cleanup --kind thumbnails\|exports\|rejected_files [--project-id ID]` | 只创建并等待清理预览，返回候选、大小、不可恢复说明、期限、preview_id 和 digest；不删除 |
| `storage cleanup --project-id ID --preview-id ID --confirm-digest DIGEST` | 明确确认之前已核对的预览；不接受 --kind，不重新预览，不从当前文件重新计算摘要 |
| `storage status OPERATION_ID [--wait]` | 只读指定存储后台任务；默认单次 GET，--wait 等待终态；拒绝其它类型任务 |
| `storage results SCAN_ID [--limit 50] [--cursor CURSOR] [--sort SORT]` | 一页最近项目扫描明细；limit 1–200，sort 为 known_id 或默认 -total_bytes,known_id，游标按查询参数编码 |

scan/cleanup 默认 `--wait`，也可 `--detach` 受理后退出，两者互斥；支持 `--idempotency-key UUID`。未传请求键时生成一次，整个命令不再换键。选择最近项目所用 known_id 从 `api GET /api/projects` 获取，它与临时 project_id 不可混用。

## 受理、等待与未知结果

结果保留原 API 业务字段，并增加 `cli`：当前 instance_id、捕获到的 project_id、operation_id/status_url，以及 POST 的 method、相对 path、白名单 body 和 idempotency_key。没有凭据、绝对项目路径或自由输入文件内容。HTTP 错误对象移出 error 外壳后保留原 code/outcome/request_id 等字段；后台失败仍保留完整 OperationView.error，便于区分请求拒绝和任务执行失败。

`--detach` 返回 status=accepted 及 task/status_url；退出 0 只表示任务已受理，不代表清理或扫描完成。等待只轮询 GET，不重发 POST。Ctrl+C 返回 130，保留已有任务标识；未收到受理回包时至少保留原请求和请求键。它不调用取消接口，服务器继续管理任务。

没有代理、重定向或写重试。POST 超时、连接中断、非 JSON 或无效受理回包按结果未知返回 7，不伪造失败后重新执行。之后可查询任务；确需复查原请求时，先核对 `api GET /api/session` 的 instance_id 与输出一致，再用原参数和同一 --idempotency-key 重发，沿用服务端会话幂等凭据。跨重启的旧请求键不构成幂等保证；旧项目句柄/预览也不可复用。轮询失败保留查询标识，不重新附着到其它会话。

清理继续使用既有 15 分钟预览、项目代次、候选身份/摘要、文件租约和逐项审计。内容改变、审图变更、关闭重开、过期或摘要不匹配均拒绝。已经执行的有效预览返回原任务，不能重复删除。三类清理不接受任意磁盘路径；已剔除图片清理保留业务行及标注，只标记文件缺失。跨项目报告仍只读，没有跨项目删除命令。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 请求成功且目标完成；detach 仅受理成功；summary 为历史摘要读取成功，不代表占用健康或实时 |
| 2 | 参数/预检错误，包括无效编号、范围、分页或非存储任务 |
| 3 | 无运行实例、管道不可用或认证失败 |
| 4 | 版本、句柄、预览或锁冲突；后台候选变化/文件租约冲突也归此类 |
| 5 | 依赖、I/O、轮询失败或其它任务失败 |
| 6 | 扫描/结果页的部分统计、不可访问项目或清理部分失败 |
| 7 | 任务仍运行/取消/中断，或写入结果未知；不是成功完成 |
| 130 | 客户端 Ctrl+C，只停止等待 |

单纯超过阈值不作为任务失败；已有摘要携带 stale/as_of/partial/config_changed 等字段，CLI 不重新解释为实时值。最近项目明细仍有 3 次/15 分钟保留限制，失效返回 410/退出 4；历史摘要可继续查看至服务退出。清理预览中的 skipped 为明确保留项，不能当作可删除项。

## 验证记录

新增 `tests/test_storage_cli.py` 共 29 项（17 个传输/参数测试、12 个真实 API 服务测试），新增 `tests/test_runtime_process.py` 2 项真实 Windows 命名管道与 HTTP 子进程闭环。新增共 31 项。定向连同原 4 个进程用例共 35 项通过（22.614 秒），日志 `.test-tmp/storage-cli-targeted.log`。

验证包括：JSON/进度分流、默认等待与 detach、当前句柄匹配、选定范围、摘要只读、分页与游标错配、三类清理、候选变化、预览过期、错误摘要、会话幂等与键冲突、部分失败、不可访问项目、Ctrl+C、未知写入不重试、轮询失败、无服务、真实重启后的旧句柄和预览拒绝。检查 token 和测试目录路径未进入 CLI 输出。测试项目、缓存、导出均在临时目录内；真实进程测试不附着或关闭用户已有服务。

Python/API 全量 935 项全部通过（199.379 秒），日志 `.test-tmp/storage-cli-python-full.log`；前端 191 项全部通过（29 个文件、6.91 秒），日志 `.test-tmp/storage-cli-web-full.log`。ESLint、设计边界检查、TypeScript/Vite 构建均通过，日志 `.test-tmp/storage-cli-lint.log`、`.test-tmp/storage-cli-build.log`。本轮未修改浏览器业务流程，未重跑 Edge 用例；上一阶段的 57 项结果不计入本轮执行数。

最终 wheel 隔离复验 778 项全部通过（164.063 秒），包含全部 29 项新增 CLI/API 测试；日志 `.test-tmp/storage-cli-wheel-full.log`。确认 152 个 Python 源文件、45 个前端资源、SQL、运行时规则、版本和隔离导入与当前源码一致。包内复验与源码测试重叠，不重复计入独立测试数量。

交付包 `.test-tmp/wheels/stylelora-0.40.0-py3-none-any.whl`，大小 652021 字节，SHA-256：`afddb8568edf3347a9225395eec1d2ba1966ce8a6b8be605309773d377bba3d9`。本轮源码与前端共执行 1126 项独立测试（935 + 191），全部通过；另有上述包内复验。未初始化 Git，未提交或上传。

## 后续边界

文件恢复/核验入口、项目迁移、完整统一产品配置以及其它领域专用 CLI 仍待后续；本轮仅完成存储 CLI 的已有服务附着模式。真实 Launcher 生成适配依旧受已审核契约门控，未启用真实生成，也不代表真实 LLM 供应商质量或费用验收。
