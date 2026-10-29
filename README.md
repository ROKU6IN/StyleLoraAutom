# StyleLoraAutom

当前版本 **0.59.0** 补齐产品默认配置、前台独立 CLI 写入会话、中断导出人工恢复和 NSFW 精修模板，并完成万图真实文件导入/导出性能验收。操作入口、边界与综合验证记录见 [本地功能收尾与综合验收](docs/66-本地功能收尾与综合验收.md)。真实 NovelAI 小批量生成/扣费、真实 LLM 质量/费用按用户决定暂缓。

设计覆盖复核：核心闭环可用，但完整设计仍有覆盖筛选、标注详情对照、工作台交互、API 响应类型及标签索引/导出门禁尾项，见 [完成情况与范围审计](docs/67-完成情况与范围审计.md)。测试通过不表示全部规划功能已实现。

源码净化导出与新环境重建步骤见 [源码导出与文件范围](docs/68-源码导出与文件范围.md)。净化副本不包含本机虚拟环境、模型、测试数据和已编译界面，需要先安装依赖并构建前端。

**0.58.0** 增加 F11 独立 CLI 容器第一阶段：来源查询、标注任务查询、导入汇总、导出状态/报告和当前项目存储扫描可使用 `--standalone --project PATH`，无需启动工作台。命令持有产品与项目锁，完成或中断后关闭自有 Runtime；不监听端口、不发布管道、不启动推理或生成。列表在同一容器内读完分页，不留下可重用的会话游标。见 [独立查询与扫描验收](docs/65-F11-独立CLI查询与扫描验收.md)。

**0.57.0** 增加 NovelAI 直连未知请求的人工结束流程：在“来源与导入 → 生成恢复与对账 → 未知请求人工处置”核对摘要，双重勾选并长按后结束来源、持久保存决定。原请求保持 `sent/unknown`，费用与现场文件保留；不会重发、确认未执行或推断退款。记录中断后可重新读取并明确完成。见 [未知请求人工处置验收](docs/64-F02-直连未知请求人工处置验收.md)。

**0.56.0** 增加 F11 显式网络诊断：`doctor --probe-network` 使用已配置凭据，只读检查 LLM 模型列表和 NovelAI 直连账户接口，不执行推理或生成、不保存账户内容。支持总超时预算，拒绝重定向且不自动重试；默认仍为离线诊断。见 [显式网络诊断验收](docs/63-F11-显式网络诊断验收.md)。

**0.55.0** 增加 F11 离线内容核验：`doctor --verify-content` 完整校验已安装 WD 模型的 SHA-256，配合 `--project` 检查空闲项目的 SQLite 完整性、表结构、CHECK 约束和外键。默认诊断仍只检查元数据；占用、WAL/恢复痕迹与超时会明确报告，不自动修复或运行模型。见 [离线内容核验验收](docs/62-F11-离线内容核验验收.md)。

**0.54.0** 增加 F11 显式按需启动：`ui --start-service`、`project init/open --start-service` 在无实例时后台启动本地服务，已有实例时附着；命令退出后服务保持运行。先验证管道身份和 HTTP 就绪，再按原有流程创建/打开项目或打开浏览器；未知请求不自动重发。见 [按需启动服务验收](docs/61-F11-按需启动服务验收.md)。

**0.53.0** 增加 F14 生成参数表单：模型、默认值、参数范围和固定值均来自本地后端能力描述，可直接调整参数和尺寸，保留高级 JSON。保存时后端再次校验并返回具体字段错误；原有 Launcher 草稿和未知字段不会被自动改写。读取表单及保存草稿不需要 Token，也不调用真实生成服务。见 [生成参数表单与能力校验验收](docs/60-F14-生成参数表单与能力校验验收.md)。

**0.52.0** 实现 NovelAI 直连：在“NovelAI 直连”设置 Token，新建 `nai_direct` 生成源即可通过既有交接、费用确认、串行生成、图片入库与导出流程运行，无需 Aaalice Launcher。首版支持 V4.5 Full 与 V5 Full high 单图文生图；单次费用未知，按 deny/ask/allow 授权，超时保留未知且不重发。完整响应回执支持重启后本地核查入库。已用本机模拟 HTTP 验证，尚未进行真实账户生成/扣费验收。见 [直连实现与验收](docs/59-F02-NovelAI直连实现与验收.md)。

**0.51.0** 增加生成来源的 Launcher 参数预检：在“来源与导入”打开并保存生成来源，点击“读取并比较 Launcher 参数”，后台读取 v4.3.1 当前设置，逐项比较模型、Effort、采样与固定参数，显示 UC None→3、V5 Medium 模型映射、负面词冲突及尚不能确认的生成条件。检查绑定来源版本，编辑使旧结果失效，取消保留上次结果，关闭项目会终止检查。预检不改参数、不调用 prepare/submit，也不授权生成。详见 [来源参数预检验收](docs/58-F03-来源参数预检验收.md)。

**0.50.0** 增加 Aaalice NAI Launcher **v4.3.1** 的只读设置与运行状态检查。“Launcher / MCP”页面点击“读取 Launcher 状态”，在后台核对版本与两个只读工具的结构后读取模型、采样设置和队列概况；支持取消，页面加载不调用 Launcher。官方 [源码](https://github.com/Aaalice233/Aaalice_NAI_Launcher) 与 [下载发布页](https://github.com/Aaalice233/Aaalice_NAI_Launcher/releases/tag/v4.3.1) 已确认，Windows 便携版/安装版链接亦在页面提供。真实生成仍未启用，未进行 Launcher 程序实机验收。源码审阅、接口差异与测试见 [官方接口与只读接入验收](docs/57-F02-Launcher官方接口与只读接入验收.md)。

**0.49.0** 增加 F11 来源草稿编辑与失败项重试：`source update` 必须携带原草稿版本，`source item` 查询单条状态，`source retry-item --expected-attempt` 显式授权失败项进入下一次尝试。同请求重放不重复修改或累加次数，完成来源重试后暂停，需另行继续。详见 [来源编辑与失败项重试验收](docs/56-F11-来源编辑与失败项重试验收.md)。

**0.48.0** 增加 F11 服务化导出 CLI：`export preview/run/list/status/report/cancel`。通过本地 HTTP 审核并提交导出，分别确认触发词警告与硬链接摘要，查询持久报告、明确取消后台工作；重复运行原计划不重复发布，旧离线导出入口保留。详见 [服务化导出 CLI 验收](docs/55-F11-服务化导出CLI验收.md)。

**0.44.0** 增加 F11 标注任务 CLI：先用 `job preview` 生成可审核计划，再用 `job run --preview FILE --confirm-digest DIGEST` 创建 WD/视觉标注批次；支持状态、分页结果及单任务/整批暂停、继续和取消。相同计划保持原批次与请求键，防止响应丢失后的重复建批。详见 [标注任务 CLI 验收](docs/51-F11-标注任务CLI验收.md)。

**0.43.0** 增加 F11 项目管理 CLI：`project init/open/close/status` 默认附着已有本地服务，支持等待或受理后退出、查询任务、版本冲突保护和原请求核对。`project init --standalone` 可在无服务时独立创建并安全关闭项目。详见 [项目管理 CLI 验收](docs/50-F11-项目管理CLI验收.md)。

**0.42.0** 增加离线诊断命令：`./scripts/run.ps1 doctor --json`，也可传入 `--project "项目路径.slproj"`。检查配置、SQLite 能力、访问权限、锁、DPAPI、模型元数据及恢复记录；不启动或附着服务，不修复、迁移、下载或调用模型。项目占用时明确报告，未安装 WD 模型不影响其它诊断。详见 [本地 doctor 验收](docs/49-F11-本地诊断命令验收.md)。

**0.41.0** 增加缺失原图恢复核验：先从备份把原文件放回项目 `images/原文件名`，再在工作台选择缺失图片，点击“核验已放回的原图”。后台核对大小和 SHA-256，通过后解除文件缺失，审图状态与标注保持原样；错误文件不会被采纳。未知请求可用原请求找回任务。详见 [原图恢复核验验收](docs/48-缺失原图恢复核验与工作台验收.md)。

**0.40.0** 增加存储专用 CLI：附着已有本地服务，扫描当前/最近项目、读取历史摘要和分页报告，并以预览编号、项目编号和摘要明确确认清理。默认等待完成，支持受理后退出；中断等待或请求结果未知时保留查询与请求标识，不自动重发。详见 [存储 CLI 验收](docs/47-存储CLI与后台任务验收.md)。

**0.39.0** 增加存储提醒阈值配置和跨页历史提醒：设置保存到产品目录，扫描固定当时阈值；超限、部分统计、阈值变化和明细过期在各页明确提示。不会自动扫描或删除。详见 [存储阈值与提醒验收](docs/46-存储阈值配置与跨页提醒验收.md)。

**0.38.0** 完成最近项目与跨项目存储统计：成功打开后记住已授权项目，首页可直接重开；在“存储占用与清理”中选择全部或部分最近项目，后台只读统计大小。持久编号与临时打开句柄分离，路径不返回浏览器；移走、替换或占用的项目明确报错。详见 [最近项目与跨项目存储验收](docs/45-最近项目与跨项目存储验收.md)。

**0.37.0** 的当前项目存储扫描与安全清理继续保留：分类、审核状态和来源统计；预览并确认后清理缩略图、已完成导出或已剔除图片文件。跨项目视图不提供删除。详见 [项目存储验收](docs/44-项目存储扫描与安全清理验收.md)。

本地数据集清洗编辑器的开发项目。上一阶段 **0.36.0** 完成 **F02–F05 恢复工作台与 HTTP 闭环**：生成源可分页查看恢复证据、显式只读对账，并在证据与适配器齐全时单独确认恢复设置；后台任务绑定当前证据版本，未知请求不自动重发。真实 Launcher 仍未启用。详见 [恢复工作台验收](docs/43-F02-F05-恢复工作台与HTTP验收.md)。此前的 [串行调度与未知结果对账](docs/42-F02-F05-串行调度与未知结果对账验收.md)、[F04/F21 费用控制与持久估算](docs/41-F04-F21-费用策略与持久估算验收.md)、[F03 设置租约与恢复日志](docs/40-F03-设置租约与恢复日志验收.md)、[F05 产物检查与原子入库](docs/39-F05-产物检查与原子入库验收.md)、[自定义尺寸预设](docs/38-F24-自定义尺寸预设验收.md)、[词典 TXT 与选择导入](docs/37-F06-词典TXT与选择导入验收.md)、[本地标签排序](docs/36-F09-F10-R4-本地排序验收.md)、[硬链接导出](docs/35-F10-硬链接导出验收.md)、[导入编码兼容](docs/34-F10-导入编码兼容验收.md)、[库资源删除](docs/33-F06-词典与覆盖方案删除验收.md)、[图形化覆盖方案编辑](docs/32-F06-图形化覆盖方案编辑验收.md)、[覆盖矩阵与格子明细](docs/31-F06-F14-覆盖矩阵与格子明细验收.md)、[词典条目编辑器](docs/30-F06-词典条目编辑器验收.md)、[提示词人工复验](docs/29-F06-F07-提示词人工复验验收.md)、[覆盖方案保存与 JSON/YAML 交换](docs/28-F06-覆盖方案与模板交换验收.md)、[任务管理与部分结果导出](docs/27-F06-F07-任务管理与结果导出验收.md)、[生成源交接](docs/26-F06-F07-生成源交接验收.md)、[精修与词典回写](docs/25-F06-F07-精修与词典回写验收.md) 保留。以上为历史阶段记录。0.52.0 已采用 NovelAI 直连替代 Launcher 生成依赖；0.59.0 已补齐 NSFW 精修模板。真实 LLM 供应商质量与费用验收由用户暂缓。

“LLM 连接与角色”提供三种协议、Windows 加密密钥存储、模型与角色配置，并可显式查询模型或检查固定图片。视觉任务会向配置的模型服务发送压缩图片及原始/WD 标签，可能产生费用；分级模型还接收原始自然语言。页面加载、导入和重新打开项目不会自动调用模型。默认分级关键词为空，分级模型默认关闭；普通线路未验证内容按 R0 处理，NSFW 角色缺失时跳过。仅支持 style 模式。“视觉关键词”管理本地分级词表与输出禁用词，独立任务入口继续保留。见 [LLM 基础能力验收](docs/20-全局LLM配置与协议适配验收.md) 与 [视觉路由验收](docs/21-F15-视觉描述与评级路由验收.md)。

既有图片导入、WD 标注、编辑、审核、导出，以及缓存、暂停/恢复/取消和持久任务记录均保留。内置已审核的 `SmilingWolf/wd-eva02-large-tagger-v3`；CPU 单图与本机 RTX 5060 Ti 的 GPU 1/2/4 图批次已通过固定参考输出和 API 闭环验收，这不是独立数据集准确率评估。见 [GPU 与动态批处理验收](docs/19-F12-GPU与动态批处理验收.md)，摘要来源见 [真实模型与 CPU 验收](docs/18-F12-真实模型接入与CPU验收.md)。

尺寸预设：侧栏“尺寸预设”可维护自定义横/竖/方像素，宽高须为 64–16384 的整数且是 64 的倍数。到“来源与导入”显式选用并保存草稿；预设校验不代表具体生成模型支持该尺寸。既有来源保留自身像素，内置小尺寸仍为默认。

## 本地运行

需要 Python 3.11+、SQLite 3.38+（含 JSON 函数），以及 pyproject.toml 声明的 Pillow、FastAPI、Uvicorn、httpx、python-multipart、jsonschema、PyYAML。Pillow 固定为 12.3.0，以保持 phash64-v1 的预处理一致；DCT 使用标准库实现，无需安装 SciPy。PyYAML 固定为 6.0.3，仅用于模板交换。原开发目录的依赖已配置在 .venv 与 .venv-gpu，已构建的 wheel 带前端资源，正常运行无需 Node；净化源码副本不带这些生成产物，须按 [重建说明](docs/68-源码导出与文件范围.md) 使用 Node >=22.22.2 构建前端。真实 WD 权重另行安装，不随源码或 Python 包分发。F01 数据库本身仍只依赖标准库。

直接打开工作台：

```powershell
./scripts/run.ps1 serve
```

保持这个终端运行。浏览器中创建项目时选择一个尚不存在的 `.slproj` 位置；打开项目时选择已有 `.slproj` 文件夹。先在“来源与导入”扫描并导入，再到“数据工作台”编辑和审核，最后在“导出数据集”预览、确认导出。标注和触发词中的下划线按现有规则规范化为空格。

提示词生产不要求打开项目。侧栏“提示词生产”新建并保存词典，勾选输入词典，设置数量/随机种子，预览后创建本地任务。默认模板为 scene×lighting 六格、无人物至少 15%、远景至少 10%；规则在表单中显式可编辑。选择现有词典可预览并追加 JSON/CSV/TXT 文件；JSON/CSV 保留结构，TXT 提示词需本地标签索引。任务只有数量、去重、围栏和覆盖全部满足才可导出冻结 JSON；输出仅含完整的 seq/prompt/aspect 列表及校验摘要。

词典编辑：选择特征维度或提示词条目，直接编辑候选标签、有序标签、描述、图幅与评级。每页显示 20 条，检索只改变显示范围，保存包含全部条目；清空检索后可排序。新增内容的评级默认未知。删除需确认，可立即撤销最近一次删除；继续修改后撤销失效。全部修改先保留在草稿，点击“保存词典”才整体写入；重新载入、切换词典或离页时处理未保存草稿。高级 JSON 保留，格式错误或未知字段不被表单丢弃。保存使用已有版本校验与原子写入；冲突后可继续编辑或明确重新载入，不能覆盖新版。特征重复标签按既有规则取较高评级并报告；提示词相同内容的不同评级需用户修正后保存。修改词典维度不会自动改写覆盖方案或已创建任务。

词典交换：保存后可下载 JSON、CSV 或 TXT。特征 TXT 按维度生成压缩包，保留评级并附文件与维度对照；提示词 TXT 会丢失图幅、评级和段落结构，须确认后下载。TXT 下载绑定已保存版本，冲突时重新载入。导入文件有逐行错误时，可在“选择有效行并重新预览”中跨页勾选有效记录，再生成新预览并确认追加；整体格式损坏须先修正文件，原有错误不会被自动跳过。详见 [词典交换验收](docs/37-F06-词典TXT与选择导入验收.md)。

已打开项目时，就绪任务提供“交接到当前项目的生成源”。也可直接到“来源与导入”填写生成源名称、目标数量、画风、负面词和费用策略，并使用后端能力驱动的表单调整模型、步数、引导强度和实际尺寸；固定参数会显示原因。高级 JSON 与表单共用同一份配置。保存时由后端最终校验，错误显示到具体字段；无兼容能力描述的旧 Launcher 草稿仍保留 JSON 编辑入口。预览必须使用已保存且数量匹配的草稿，会读取完整任务结果而不是当前分页，展示最多 100 条样例。预览不会发送模型请求、写入 source_items 或锁源。适配未开放、触发词校验未通过时禁止启动；预计费用显示未知，不代表免费。

生成费用：在“来源与导入”打开已保存生成源，可独立修改运行费用策略，锁定来源也适用。选择允许非零费用须勾选并长按确认；询问策略的同意覆盖整个当前运行周期，暂停、改策略或重启后失效。页面显示按 attempt 保存的本地估算，未知结果单列、明确拒绝排除、缺失凭据提示不完整。估算不是实际账单，0 也不代表免费。NovelAI 直连支持未知费用的明确授权；Launcher 真实生成适配仍未开放，不能通过费用确认绕过。

任务详情中的“导出合格结果”只读取已保存、通过内容与围栏检查的条目，并做标签集合去重；不重新抽样或调用模型。JSON 包含结构与源任务报告，CSV 保留结构化条目，TXT 需确认丢失图幅、评级等信息。未就绪任务可导出已有合格部分，但文件不能替代冻结 seeds。任务列表下方可选择最多 200 个已停止任务，确认后按住清理。未知清理结果保留原请求供核对和继续，损坏文件不自动删除；清理不影响词典或已锁定生成源。

覆盖方案：从“提示词生产 → 管理覆盖方案”进入，点击“使用表单编辑”维护名称、数量、种子、2–3 个主维度及取值、整体比例/数量约束、排除谓词和无人物兼容规则。选择格子调整相对权重，点击“预览计划配额”；实际词典是否可满足仍须在采样页检查。未固定种子时不预测随机余数分配。维度重命名保留权重，删除维度/修改取值后的失效权重需核对并手动清理。保存才写入方案库，离页或切换时处理未保存草稿；文本模式仍可编辑 JSON/YAML，切换时由后端严格转换，非法内容保留原草稿。可导出 JSON/YAML，也可载入 UTF-8 本地文件为新草稿再保存。文件上限 1 MiB，禁止重复键、YAML 锚点/别名、合并键和自定义对象。返回提示词生产后，选择方案并点击“载入并替换覆盖设置”，会复制规则、数量、种子并启用格子配额，必须重新采样预览。已有任务不跟随方案变更。API、模板和方案表单完整保存 uint64 种子；浏览器采样只能使用不超过 9007199254740991 的整数，超出时明确拒绝载入，原方案仍可编辑/导出。

删除库资源：打开已保存词典或覆盖方案，先保存或明确放弃草稿，再点击“删除整份词典”/“删除覆盖方案”。核对名称、编号、修订，勾选确认并按住按钮一秒。版本变化会拒绝删除；未知结果可用同一请求重试。清理中断或重新打开页面后，点击“检查未完成删除”继续清理。删除不会删除已有任务、冻结结果或项目；已删除词典不能再用于新采样、导入或追加，运行中任务自动回写该词典会报错，结果仍保留。删除不支持撤销，请先导出需要保留的内容；重新导入会创建新编号。

覆盖矩阵：采样预览与任务报告展示每格目标/实际及缺口；三维方案可任选两维作行列，剩余一维筛选。被规则排除的格子明确标出，不作为“目标 0 的满足格”。大矩阵每页最多 10×10 格；点击格子或 Tab 聚焦后按 Enter/空格查看已选条目，每页 50 条，覆盖完整集合。修改采样条件后需重新预览；任务版本变化时旧明细关闭，重新选择格子即可读取新版。预览明细最多保留 3 次、15 分钟，可主动释放；释放不影响报告、创建任务或导出。方案内容仍在原模板编辑区修改，矩阵切片不会改动任何约束。

提示词人工复验：在任务结果表点击待检查条目的“编辑并复验”。任务必须已完成且后台已退出。查看原始输入和待审核内容，编辑后点击“复验修改并预览结果”；通过后明确勾选内容与评级确认，再保存。原有禁用规则不提供强制放行；评级不能低于冻结证据，覆盖格子与已确定图幅保持。保存后按数量、去重、比例与格子配额重新选择结果，就绪时可导出冻结 seeds；仍有缺口时只能使用合格部分。人工提交不自动调用模型、补位或追加词典，也不回写已导出文件和已交接来源。已采纳条目保留只读记录；旧任务没有保存的模型输出不会补造。

会话凭据只保存在内存。刷新页面或关闭标签后，运行 `./scripts/run.ps1 ui` 取得新的授权页面。

模型管理：侧栏“WD 模型管理”查看 ONNX Runtime、设备信息和安装状态。只有经过开发审核的固定清单才能安装，浏览器不能提交下载 URL、路径或自行信任的摘要。当前内置 EVA02-Large v3，权重约 1.26 GB；首次使用点击“下载并安装”，启动程序不会自动下载。模型目录为 `%LOCALAPPDATA%/StyleLoraAutom/models/wd`，与项目和标签资料分开。本机已安装该版本；服务重启后显示待校验，点击“重新校验文件”通过后即可创建 WD 任务。取消不删除已发布版本。

模型校验和推理使用可选依赖：CPU 环境安装 `python -m pip install -e ".[wd-cpu]"`；独立 GPU 环境安装 `python -m pip install -e ".[wd-gpu]"`。同一环境不能同时安装 CPU/GPU 两种 ORT 发行包。本机 `.venv` 保留 CPU 运行时，新增 `.venv-gpu` 已配置 GPU 运行时和 CUDA/cuDNN。先退出已运行的服务，再用 GPU 环境启动：

```powershell
./scripts/run.ps1 -Python ./.venv-gpu/Scripts/python.exe serve
```

在“WD 模型管理”重新校验模型，然后到“WD 标注任务”选择“自动”设备策略和 1/2/4 张批大小上限。CPU 固定一图；动态模型仅在模型、运行时、CUDA 库、GPU、驱动和会话策略匹配已审核显存档案时合批，未知组合取一图。每批检查空闲显存并保留 max(1 GiB, 20%)；OOM 按 4→2→1 缩小，再回退同模型 CPU，一图一次 CPU 尝试。

本机 RTX 5060 Ti / 驱动 610.88 / ORT 1.31.0，固定六例实测 GPU 暖运行纯推理约 0.08–0.09 秒/图（不含文件校验、解码、提交及加载时间）。批大小增加没有明显提高本样例的每图速度；时延和显存不是其它设备上的保证。其它硬件与驱动组合仍需独立验收。

任务入口支持明确填写通用/角色阈值，选择未覆盖、选中图片或全部重新标注。默认排除已剔除图片，一次最多 5000 张。暂停允许当前已接受批次完成，各图仍独立提交；取消丢弃尚未提交结果。库忙保留整批未提交结果，恢复不重复推理；重开项目后的中断任务保持暂停。历史 v1 任务仍按一图处理，恢复沿用原范围与参数。

组合批次入口：“标注批次”或工作台顶部“执行器批次”。WD 与 LLM 均勾选时，LLM 等 WD 完成后读取最新证据；WD 需要重新推理的图片会显示“等待 WD 决定”，不会提前声称命中视觉缓存。预览不调用模型、不修改评级。候选超过 500 张、潜在视觉调用超过 50 张或与未结束任务重叠时需确认；全部重跑还需按住 1 秒。整批暂停会阻止后一阶段接续；整批取消保留已提交结果。前序取消或批次级失败不会跳过依赖。历史单阶段任务也可从批次页查看。

工作台面板保留网格交互，覆盖右侧详情区域；Escape 或关闭按钮收起并返回原入口。当前筛选支持来源、审核、评级、标签搜索和文件缺失；全选筛选结果由服务端解析全部匹配图片，不只取已加载的 200 张。批次创建时仍校验预览范围摘要，最多 5000 张。原组合批次全部结束后可“仅重跑失败项”：沿用原阶段的冻结配置和依赖，忽略旧缓存、创建新单阶段任务；成功、跳过及未执行图片不包含在内，也不自动重跑其它阶段。原记录保留。组合调度合同见 [0.15.0 验收](docs/22-F15-组合批次与覆盖预览验收.md)。

近似重复扫描：侧栏“近似重复扫描”选择未剔除图片或工作台选中范围，明确填写 0–64 的最大汉明距离，然后开始。距离为 0 也不证明图片相同；扫描不自动删除、剔除或合并图片。每次最多 10,000 张、结果最多 64 MiB，超过结果上限整次失败并提示缩范围，不返回截断结果。候选保留 15 分钟，最多保留 3 次，可在会话下拉框切换或主动释放；关闭项目会取消任务并清理结果。旧版导入的空指纹暂不回填，页面显示跳过计数。

标签清洗：从“本地标签库”导入自己的规范标签 JSON、翻译 JSON/TSV；编辑器可按标签、别名或中文查询补全，只插入规范标签。“替换规则库”可新建/编辑/删除规则，或预览迁入 TagCleaner 规则文件。在工作台选中图片后使用“批量标签”或“替换规则”，先生成预览，再确认整批应用。规则可在“规则操作日志”中撤销，批量人工编辑不属于规则撤销历史。一次最多 5000 张，预览有效 15 分钟。

产品标签资料保存在 `%APPDATA%/StyleLoraAutom/tagging/tagging-library.json`，跨项目复用，单文件原子替换；不会增加项目数据库表或改写原始标注。需要独立测试库时可运行 `./scripts/run.ps1 serve --product-dir ./local-tagging`。示例文件格式、范围语义及限制见 [标签清洗验收](docs/12-F09-F10-标签清洗实现与验收.md)。

方案管理：侧栏“导入导出方案”可新建、编辑、删除方案并切换默认导入方案。新建导入方案默认使用词库识别，需先导入本地词库；已有默认方案保持首行全部作为标签的兼容模式。权重/转义语法或不明确的未知文本会在扫描时阻止导入，修正 TXT 后重新扫描即可。暂停导入使用产品目录 `catalog-versions` 中的旧词库快照，恢复时不会改用新版词库。

导出预览会检查触发词。与已有标签或别名重合时，需要勾选本次确认；与来源风格描述冲突时拒绝执行。词库缺失、不完整或风格语法无法判断时显示“无法完整验证”，保留兼容导出，不声称已通过完整唯一性校验。此门禁策略及预留能力详见 [方案解析验收](docs/13-F09-F10-方案解析实现与验收.md)。

## HTTP 服务与示例

没有运行服务时，可用 `./scripts/run.ps1 source list --standalone --project "E:\datasets\demo.slproj"` 查询来源，用 `storage scan --standalone --project "E:\datasets\demo.slproj"` 完成存储扫描。独立模式通过正式项目打开流程执行既有恢复，并在退出时关闭；会更新最近项目记录。它不能与正在运行的服务共存，也不能 detach 或复用旧会话的请求键、游标、句柄。单命令独立查询范围见 [独立查询与扫描验收](docs/65-F11-独立CLI查询与扫描验收.md)。0.59.0 新增 `./scripts/run.ps1 session --project "E:\datasets\demo.slproj"` 前台会话，可在同一 Runtime 内完成预览确认、导入/导出提交、生成、标注执行和清理；用法与边界见 [综合验收](docs/66-本地功能收尾与综合验收.md)。这些写入也可继续使用下面的长驻服务流程。

提示词 CLI 不要求打开项目。先 `prompt preview --spec sampling.json`，审核并将完整输出保存为 UTF-8 JSON，再 `prompt run --spec prompt-plan.json --confirm-digest <confirmation_digest>`。用 `prompt status <task_id>` 查看 ready 和 revision；`prompt seeds <task_id> --expected-revision <revision>` 输出仅含内容的 seeds。完整输入示例、控制命令和恢复说明见 [提示词任务 CLI 验收](docs/52-F11-提示词任务CLI验收.md)。

生成来源使用 `source create --type nai --spec draft.json --project-id <project_id>` 创建草稿，再用 `source preview <source_id> --project-id <project_id> --task-id <task_id> --expected-task-revision <revision>` 保存交接计划；审核后 `source run --preview source-plan.json --confirm-digest <confirmation_digest>`。费用暂停需明确 `source confirm-cost`，不会自动确认；`source reconcile` 只核对已冻结内容，不启动工作。详细参数、恢复与当前限制见 [生成来源 CLI 验收](docs/53-F11-生成来源CLI验收.md)。

编辑未启动的生成草稿使用 `source update <source_id> --project-id <project_id> --spec draft.json --if-match <原version>`，文件是完整 Draft；费用策略为 allow 时额外提供 `--confirm-allow`。对导入或生成的失败条目，先 `source item <item_id> --project-id <project_id>`，再 `source retry-item <item_id> --project-id <project_id> --expected-attempt <原attempts> --if-match <原version>`。命令只授权该次重试，不主动启动来源；完成来源转为暂停后用 `source resume` 显式继续。详见 [来源编辑与失败项重试验收](docs/56-F11-来源编辑与失败项重试验收.md)。

服务化导入先 `import preview INPUT_ROOT --project-id <project_id> --name NAME`，审核并保存输出为 UTF-8 JSON，再 `import run --preview import-plan.json --confirm-digest <confirmation_digest>`。用 `import status <operation_id> --project-id <project_id> --wait` 查看后台完成情况，`import summary <source_id> --project-id <project_id>` 查看持久汇总。服务重启后恢复导入前，先 `source import-root <source_id> INPUT_ROOT --project-id <project_id>` 重新验证目录。旧 `import PROJECT ROOT` 入口保留，详见 [服务化导入 CLI 验收](docs/54-F11-服务化导入CLI验收.md)。

服务化导出先 `export preview --project-id <project_id> --scheme <scheme_id>`，审核并保存完整输出后，执行 `export run --preview export-plan.json --confirm-digest <confirmation_digest>`。有触发词警告或使用硬链接时，还需分别传入对应的 `--accept-trigger-warning` / `--accept-hardlink` 摘要。可用 list/status/report 查询、cancel 取消；旧 `export PROJECT` 入口保留。完整示例与恢复说明见 [服务化导出 CLI 验收](docs/55-F11-服务化导出CLI验收.md)。

标注任务 CLI 同样连接已有服务。`job preview --executor wd --config wd.json` 只估算当前非剔除图片的覆盖，输出运行计划和确认摘要；审核后保存为 UTF-8 JSON，再执行 `job run --preview job-preview.json --confirm-digest <confirmation_digest>`。组合执行器使用 `--executor wd,llm_vision`。`job list` 查看批次，`job status/pause/resume/cancel --batch-id <batch_id> --project-id <project_id>` 操作整批；单阶段使用数字 JOB_ID。配置格式、保存预览的 PowerShell 示例及失败恢复步骤见 [标注任务 CLI 验收](docs/51-F11-标注任务CLI验收.md)。

项目管理命令先连接已运行的 `serve`，创建后项目保持打开；`open` 明确切换项目，`close` 等待后台使用者排空并释放锁。所有命令 stdout 为单个 JSON，进度写 stderr。默认无服务时返回退出码 3；`init/open --start-service` 可显式后台拉起服务并保持项目打开。`init --standalone` 仍为独立创建并关闭，不能与启动或重放选项混用。

```powershell
# 可直接按需启动：已有服务时附着，无服务时后台启动
./scripts/run.ps1 project open "E:\datasets\example.slproj" --start-service
./scripts/run.ps1 ui --start-service
# 也可保持原来的双终端方式：终端一启动服务
./scripts/run.ps1 serve --no-browser
# 终端二：管理项目
./scripts/run.ps1 project init "E:\datasets\example.slproj" --name "样例" --trigger "example_style"
./scripts/run.ps1 project status
./scripts/run.ps1 project close
./scripts/run.ps1 project open "E:\datasets\example.slproj" --detach
./scripts/run.ps1 project status --operation-id <operation_id> --wait
# 无服务时使用的显式独立创建方式
./scripts/run.ps1 project init "E:\datasets\offline.slproj" --name "离线项目" --standalone
```

响应丢失或中断时，先保留 JSON 中的 `cli` 凭据并核对任务；同会话原请求重放需要原 grant、请求键及版本等字段，具体步骤见 [验收文档](docs/50-F11-项目管理CLI验收.md)。服务重启后旧凭据失效，不自动重新执行。

存储 CLI 先在一个终端运行 `./scripts/run.ps1 serve --no-browser`，再在另一个终端执行以下命令。项目可从工作台打开；命令不会自动切换项目。所有输出默认为 JSON，进度写到 stderr。

```powershell
# 只读取历史摘要；未扫描为 null
./scripts/run.ps1 storage summary --json
# 当前项目扫描，默认等待完成
./scripts/run.ps1 storage scan
# 最近项目扫描；受理后退出，再用返回的任务编号查询
./scripts/run.ps1 storage scan --known --detach
./scripts/run.ps1 storage status <operation_id> --wait
./scripts/run.ps1 storage results <scan_id> --limit 50
# 可用 api GET /api/projects 取得 known_id，再用 --known-id 重复选择部分最近项目
./scripts/run.ps1 storage scan --known-id <known_id>
# 清理先预览，不删除；另外两种 kind 为 exports、rejected_files
./scripts/run.ps1 storage cleanup --kind thumbnails
# 核对预览候选后，用返回的 project_id/result.preview_id/result.digest 明确确认
./scripts/run.ps1 storage cleanup --project-id <project_id> --preview-id <preview_id> --confirm-digest <digest>
```

确认命令不重新生成预览；候选改变、预览过期或项目重开后均需重新预览并核对。Ctrl+C 只停止等待，不取消后台任务；输出的 `cli.operation_id` / `cli.status_url` 可用于查询。写请求未取得回包时，`cli.request` 保留原方法、相对 API 路径、白名单 body 和请求键；须先核对状态，仅可在同一 `cli.instance_id` 会话内用相同参数和 `--idempotency-key` 继续原请求。程序不会自动重试。未运行服务时退出 3，不启动独立模式。完整参数、退出码和期限见验收文档。

Launcher 诊断：侧栏“Launcher / MCP”点击“检查连接”，程序从 `%APPDATA%/nai-launcher/mcp-server.json` 读取本机发现信息。HTTP 查询状态不自动连接；检查为可取消后台任务，只执行握手和工具清单查询，结束后关闭诊断会话。页面显示最近检查时间、版本、工具数量和兼容性摘要；未知工具结构不会启用生成。当前生产兼容清单尚无经过真实 Launcher 核验的条目，本机也未发现 Launcher 运行信息，因此该 Launcher 后端仍不能生成图片；0.52.0 起可使用独立的 NovelAI 直连后端。

设置恢复状态：同页显示本地租约、未知写入、在途生成、外部修改冲突或日志损坏；也可通过 `GET /api/backends/nai_launcher/settings-lease` 查看。查询不连接 Launcher、不修改设置。Launcher 恢复记录未结清会阻止对应后端的新生成源启动，项目查看和标注仍可用。真实恢复适配尚未开放，页面没有强制解锁入口；不要手工删除恢复日志。

可用本地 CLI 执行同一检查：`./scripts/run.ps1 api POST /api/backends/nai_launcher/probe --wait`；查询缓存用 `./scripts/run.ps1 api GET /api/backends`。凭据不会返回浏览器、写入项目或诊断报告。账号接口指定当前项目和 NAI 来源时返回本地持久估算，未指定来源时返回 null；不查询真实余额。

F11 进程入口目前支持 Windows。在项目根目录的第一个 PowerShell 窗口运行：

```powershell
./scripts/run.ps1 serve --no-browser
```

服务绑定 `127.0.0.1` 的空闲端口并输出就绪信息。在第二个窗口执行：

```powershell
# 自动通过当前用户管道取得内存凭据，不需要复制 token
./scripts/run.ps1 api GET /api/session

# 在当前目录新建独立演示目录，用两张合成图片跑完 HTTP 闭环
# 服务需处于没有打开项目的状态；不会关闭已有项目
./scripts/http-demo.ps1 -Output .

# 打开数据工作台；已有项目时默认进入图片工作台
./scripts/run.ps1 ui

# 退出服务；先拒绝新写入，再等待已接受的文件操作结束
$session = ./scripts/run.ps1 api GET /api/session | ConvertFrom-Json
./scripts/run.ps1 api POST /api/session/shutdown --if-match $session.version
```

演示输出项目路径、导出目录和数量，生成的演示数据保留在本地。实际图片目录通过 `grant import_root <目录> --project-id <ID>` 授权；HTTP 不接受任意磁盘路径。详细调用、接口范围和测试映射见 [F11 核心实现与验收](docs/10-F11-核心API与进程验收.md)。

重复启动 `serve` 会连接现有实例。`--workers 2`、`--reload` 和非回环地址会被拒绝。下方旧离线命令除 `info/check` 外，需要先停止服务，以免出现第二个可写项目进程。

## 离线命令行

在项目根目录运行 PowerShell：

```powershell
# 运行全部数据库、文件闭环与 HTTP/进程测试；先退出手动启动的服务
./scripts/test.ps1

# 创建项目；目标目录必须不存在，父目录必须存在
./scripts/run.ps1 init ./demo.slproj --name "本地示例" --trigger-word "my_style"

# 只读查看与完整性检查
./scripts/run.ps1 info ./demo.slproj
./scripts/run.ps1 check ./demo.slproj

# 显式执行本地恢复：暂停中断任务，核查已有文件收据，不发起外部请求
./scripts/run.ps1 recover ./demo.slproj
```

脚本依次寻找仓库的 .venv、当前账户的 Codex 内置 Python、PATH 中的独立 Python。也可通过 `-Python "绝对路径/python.exe"` 指定。脚本只在运行期间设置进程环境变量，不修改系统配置。

跨平台或已安装 Python 的环境：

```bash
PYTHONPATH=src python -m unittest discover -s tests -t . -v
PYTHONPATH=src python -m stylelora --help
```

可选：使用本地已有 setuptools 构建/安装该 Python 包；pyproject.toml 声明了 `stylelora` 命令、初始化 SQL 和完整工作台静态资源。运行源码不需要安装包，但需要安装声明的依赖。

## 前端开发与验证

```powershell
cd web
npm ci
npm run lint
npm test
npm run build
npm run test:e2e
```

`build` 会将产物复制到 `src/stylelora/api/web_dist`，由同一个本地 HTTP 服务托管。E2E 使用本机 Edge 无头浏览器及测试专属服务、临时项目；发现用户已有服务运行时测试拒绝接管，须先停止该服务。浏览器测试中的系统目录选择器由测试模块替代，应用其余操作使用真实 HTTP 与磁盘文件。详细开发模式、测试映射和仍未实现的设计项见 [F14 验收文档](docs/11-F14-工作台实现与验收.md)。

## 图片、标注和导出的最小闭环

将真实素材放在项目容器之外，例如工作目录下的 `input`，使用上面创建的 `demo.slproj`：

```powershell
# 递归导入；默认严格 UTF-8，支持 BOM。GBK 文件须显式加 --encoding gbk
./scripts/run.ps1 import ./demo.slproj ./input

# 查看图片 ID、原始与合并标注。以下以图片 ID 1 为例
./scripts/run.ps1 images ./demo.slproj
./scripts/run.ps1 show ./demo.slproj 1

# 替换完整手工标签集合；标签、描述在一个事务中保存
./scripts/run.ps1 edit ./demo.slproj 1 --tags "blue_hair, smile" --nl "人物站在花园中。"

# 批量追加/删除标签；审核支持 keep、unreviewed、reject
./scripts/run.ps1 edit-many ./demo.slproj 1 --add "outdoors" --remove "smile"
./scripts/run.ps1 review ./demo.slproj 1 --status keep

# 预览不写文件；正式导出每次生成新的版本目录
./scripts/run.ps1 export ./demo.slproj --include-nl --dry-run
./scripts/run.ps1 export ./demo.slproj --include-nl
./scripts/run.ps1 check ./demo.slproj
```

默认只导出 `keep`，`--include-unreviewed` 可额外选择未审核图片；`reject` 和已标记文件缺失的图片始终跳过。导出目录为 `demo.slproj/exports/<方案 ID>/<UTC 时间>-<随机后缀>/`，含原图副本、同名 UTF-8 txt 与 `export_report.json`。`--include-json` 另外输出图像身份和 PNG 文本元数据。不会修改原图或历史标注。

同名 txt 使用显式格式，首行是逗号分隔标签，空行后是可选描述：

```text
blue_hair, smile, outdoors

人物站在花园中。
阳光落在肩上。
```

默认解析器 `literal-tags-v1` 按该格式将首行明确视为标签，不猜测 Danbooru 类别。原始层保留拼写；编辑与导出按 F01 规则转小写、将下划线换为空格并去重。缺少 txt 与空 txt 分别表示“没有原始标注”与“显式空标签”；普通多行 txt 没有空行分隔时会报错。权重、`BREAK`、`AND` 等提示词语法不在此最小格式内。

导入支持 PNG、JPEG、WebP、BMP、单帧 GIF/TIFF；解码验证后直接复制原始字节，不缩放或重新编码。SHA-256 相同的图片跳过，不覆盖既有标注。默认上限为每图 100 MiB、1 亿像素、每 txt 1 MiB、每批 10,000 张；多帧图片明确报错。扫描跳过符号链接、目录联接和 `.slproj` 子目录。

TXT 默认严格 UTF-8（兼容 BOM）。遇到混合 UTF-8/GBK 素材，可在导入方案或“来源与导入 → TXT 编码”选择“UTF-8 优先，失败后尝试 GBK”，CLI 使用 `--encoding auto`。带 BOM 的损坏 UTF-8 不回退，两个解码器均失败则报错。GBK 解码成功并不保证识别正确，确认前核对中文；预览显示实际编码、回退提示和全部条目的回退计数。导入后在来源“查看明细”读取持久编码记录；原 txt 不变，暂停续导仍用已冻结的编码配置。

命令返回 JSON；退出码 `0` 为成功，`2` 为部分失败或导入未完成，`1` 为参数、项目或整体操作错误，`130` 为用户中断。以报告中的 ID、数量和原因码判断结果。

## 编辑冲突、恢复和导出边界

- `show` 返回 `revision`；`edit --expected-revision <revision>` 可拒绝过期编辑。`--clear-tags` 清空当前手工期望标签；`--clear-nl` 清空人工描述，使显示/导出回退到模型或原始描述。仅改描述不会生成手工标签版本。
- 导入清单冻结相对路径、图像/txt 哈希和解析参数。中断后运行 `import <项目> <素材目录> --source-id <ID>` 继续；失败项只在显式增加 `--retry-failed` 时重试。素材目录可整体搬迁，内容必须与冻结清单相同；内容变化会停止本源继续处理，应另建导入源。
- 导出先冻结数据库快照并核对原图哈希，完整图片/txt 文件对写成后才发布新目录。中断留下 `.partial-*` 目录及 `.export-state.json`，不会当成成品；再次导出新建目录。当前不自动续写或清理历史临时目录。0.59.0 可在导出页明确扫描，完整复制产物通过核验后手动发布，其余现场可整目录归档保留；这不等于自动补全中断导出，见 [综合验收](docs/66-本地功能收尾与综合验收.md)。
- trigger 必须非空且满足普通标签语法，不能与任意源的风格提示词冲突。`config <项目> --trigger-word "my_style"` 可修改。默认没有全量词库，报告明确写 `unverified_no_catalog`，不会声称已经通过全量 Danbooru 唯一性校验。
- `--block "artist_name, other_tag"` 额外排除标签；默认也排除源的显式风格标签及提供的词库中 `artist` 类标签。仅修改导出副本；描述命中时默认要求先编辑。`--blocklist-mode warn` 保留并报告命中。该规则是确定性字面匹配，未实现完整风格语义识别。
- 默认复制导出，支持在方案中选择硬链接；仅同卷、空处理链、目标不存在且确认共享内容风险后执行，失败不回退。修改硬链接导出图也会影响项目原图，删除链接不会删除其它链接。预览确认绑定当前计划，重新预览后需重新勾选；报告保留方式和确认摘要。CLI 对硬链接方案需显式传 `--acknowledge-hardlink`。取消和写入故障不发布正式目录；若系统阻止临时链接清理，收据保留 `hardlink_cleanup_pending`，中断目录可能仍有共享图片。
- 排序默认 `layer_first`；方案可选择 `prompt_pyramid` 本地纯排序，按主体、动作表情、服装、构图、场景、光照分组，同组和未分类项保持原相对顺序，触发词仍在最前。未知标签完整保留并在预览/报告提示数量；只调整导出顺序，不改项目标注或自然语言。规则与词库摘要冻结进导出计划，变更后须重新预览；排序资源不可用或违反标签集合契约时明确报错。非空图片处理链仍未开放。

可选词库为本地 JSON，通过 `--catalog <路径>` 提供。导入显式指定 `--parser catalog-comma-v1` 才使用别名映射；该模式下未知标签仅在明确存在描述段分隔空行时转入描述，否则报歧义。词库内容哈希也会冻结。导出使用提供的词库检查 trigger，已存在时需显式 `--allow-existing-trigger`；该选项不会解除风格冲突。

```json
{"schema_version":1,"kind":"tag_catalog","complete":false,"tags":[
  {"tag":"blue_hair","category":"general","aliases":["azure_hair"]},
  {"tag":"artist_name","category":"artist","aliases":[]}
]}
```

`complete` 只记录提供者的声明；程序不下载词库，也不保证所提供文件覆盖全量标签。进一步的实现与验收记录见 [本地图片与标注闭环](docs/09-本地图片与标注闭环.md)。

## 当前目录

```text
src/stylelora/
  cli.py                       项目、导入、图片、编辑、审核与导出命令
  api/                         严格 HTTP 合同、鉴权、路由与工作台静态资源
  runtime/                     单实例、当前用户管道、生命周期、后台任务、SSE
  services/                    项目服务、持久标注任务、导入导出与鉴权媒体
  backends/                    NovelAI 直连、Launcher 发现、MCP 传输与兼容性门禁
  imaging/                     phash64-v1 指纹、汉明距离与候选算法
  exchange/                    清单、txt 解析、图片解码、导入与版本化导出
  annotations/editing.py       手工标签/描述、批量编辑与审核事务
  tagging/                     标签资料、可信模型管理、ONNX 预处理与推理
  llm/                         全局连接、加密密钥、三协议调用与运行模板
  rules/engine.py              三组 all/any 规则规范化与纯计算
  project_db/
    types.py, validation.py    DTO、输入和相对路径校验
    connection.py, locking.py 单写队列、WAL 快照、进程文件锁
    project.py                创建、打开、关闭、元数据更新
    repositories/             八张表的数据访问接口
    commands.py               状态转换与跨表原子操作
    artifacts.py, recovery.py 图片收据、入库与中断恢复
    rules.py, merge.py         标注快照、撤销与证据边界合并
    cache.py                  版本化标注缓存键
    migrations/               schema v1、迁移、备份与恢复
tests/                        标准库 unittest 验收测试
web/                          React 工作台、单元测试与浏览器验收
scripts/                      本地运行和测试入口
docs/                         设计与本地实现记录
skills/                       产品提示词模板
```

运行产生的测试数据放在仓库内的 .test-tmp；Python 临时项目在测试后清理，浏览器截图、测试项目与包验证产物保留供检查。不读取外部服务凭据，也不调用 NAI 或 LLM。普通测试使用固定合成小模型，不下载真实权重；真实 WD 另用显式 `scripts/accept_wd_cpu.py` 验收，参考概率和标签表保存在 `tests/fixtures/wd-v3`。Python 进程测试发现用户服务时跳过，浏览器测试拒绝接管已有服务；完整验收应先退出用户服务。

最新测试结果与包验证记录见 [F11 服务化导出 CLI 验收](docs/55-F11-服务化导出CLI验收.md)；此前为 [服务化导入 CLI 验收](docs/54-F11-服务化导入CLI验收.md)、[生成来源 CLI 验收](docs/53-F11-生成来源CLI验收.md)、[提示词任务 CLI 验收](docs/52-F11-提示词任务CLI验收.md)、[标注任务 CLI 验收](docs/51-F11-标注任务CLI验收.md)、 [项目管理 CLI 验收](docs/50-F11-项目管理CLI验收.md) 和 [本地诊断命令验收](docs/49-F11-本地诊断命令验收.md)。此前阶段分别见 [最近项目与跨项目存储](docs/45-最近项目与跨项目存储验收.md)、[项目存储扫描与安全清理](docs/44-项目存储扫描与安全清理验收.md)、[F12 GPU 与动态批处理](docs/19-F12-GPU与动态批处理验收.md)、[F12 持久任务](docs/17-F12-WD推理与持久任务实现与验收.md)、[F12 模型管理](docs/16-F12-模型管理实现与验收.md)、[F08 近似重复扫描](docs/15-F08-近似重复扫描实现与验收.md)、[F02 连接诊断](docs/14-F02-MCP连接诊断实现与验收.md)、[方案解析](docs/13-F09-F10-方案解析实现与验收.md)、[标签清洗](docs/12-F09-F10-标签清洗实现与验收.md) 和 [F14 工作台](docs/11-F14-工作台实现与验收.md)。

## 开发接口

```python
from pathlib import Path
from stylelora.project_db.project import create_project
from stylelora.project_db.connection import submit_write, read_snapshot
from stylelora.project_db.repositories.sources import create_source, list_sources
from stylelora.project_db.types import SourceDraft, SourceFilter, PageRequest

with create_project(Path("demo.slproj"), name="示例", trigger_word="my_style") as project:
    source = submit_write(
        project,
        lambda db: create_source(db, SourceDraft("import", "待配置的导入源")),
    ).result()
    with read_snapshot(project) as db:
        page = list_sources(db, SourceFilter(), PageRequest(limit=50))
```

写操作必须经 submit_write 排队；repository/command 使用调用方提供的事务，不自行提交。ReadSession/WriteSession 只在所属线程和上下文内有效。一次进程仅打开一个项目；同项目重复打开返回现有 handle，切换项目前先关闭。

文件复制、图片解码和 txt 解析由 exchange 层在数据库事务外完成。F01 接收已经解码验证的尺寸、指纹和原始标注；prepare_artifact 创建可信收据，complete_item 完成文件登记和数据库原子写入。导出固定快照后释放数据库事务，再复制文件。

详细实现范围、验收映射和边界见 [F01 本地实现与验收](docs/08-F01-本地实现与验收.md)。
