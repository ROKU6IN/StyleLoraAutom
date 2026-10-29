# F01 project.db 模块详细设计

> 0.35.0 实现补充：`images/.operations/<item>/<attempt>.json` 是调度辅助证据，保存 prepare 起的未知操作屏障关联及结清状态，不增加 runs/jobs 或数据库表列。图片归属 SHA-256 在写完成收据前校验，生成失败仍只能人工授权新 attempt。详见 [调度验收](42-F02-F05-串行调度与未知结果对账验收.md)。

> 0.34.0 C06 实现补充：费用服务在 mark_sent 前持久保存项目 `images/.costs/<item>/<attempt>.json`；未知提交不授权远端重发，缺失历史凭据不能视作零费用。凭据属于辅助恢复/估算证据，不改变 DDL、attempt 分配或原有图片收据协议。confirm_cost_and_resume 新增可选 validate 回调，在同一写事务内执行来源版本前置校验；原接口调用保持兼容。详见 [费用验收](41-F04-F21-费用策略与持久估算验收.md)。

> 0.32.0 C07 实现补充：ImageArtifact 增加可选 registration_check（schema_version/review_status/summary），完整收据冻结该值；prepare_artifact 可核对 F05 解码时的 SHA-256/大小，并先保存 `images/.checks/<item>/<attempt>.json` 检查信封。complete_item 同事务设置初始 unreviewed/reject，重放不覆盖后续人工审核。无 original 时摘要也存在，恢复缺失/损坏检查证据的产物保持未知。旧无检查 v1 收据的字段与摘要不变，无数据库迁移。详见 [F05 验收](39-F05-产物检查与原子入库验收.md)。

版本：设计稿 v0.2；日期：2026-10-12；状态：已并入 C01—C05 / Q01—Q13 裁决，仅剩明确列出的外部契约待实测。

> 本地实现更新（2026-10-25）：项目骨架和 F01 已落地到 src/stylelora/project_db，验收测试已执行通过。本文保留设计规格与历史说明；当前交付范围和验证边界见 [08-F01-本地实现与验收](08-F01-本地实现与验收.md)。

## 1. 范围、权威来源与交付边界

权威来源为 `docs/05-项目结构与数据模型.md`，重点 §2、§3、§5、§7—§10、§13。`docs/04` §4 只用于 F 编号映射；不继承旧 S0—S6、manifest 表、runs、parent_id 或旧分层结构。本文件仅含设计、DDL 规格与接口声明，不包含实现代码；本次裁决同步回写 docs/05 及 docs/04 F04。

F01 负责项目容器生命周期、数据库结构、数据访问、状态约束、原子业务写入、迁移与启动恢复。调用外部服务、图像解析、规则计算、caption 组合属于调用方；F01 接收这些模块已经校验的结果并保证持久化一致性。

固定使用 Python 3.11+、标准库 sqlite3、FastAPI 单进程、WAL、每个打开的项目一个写连接。不引入 ORM。连接留在其所属线程；对外请求并发 2 不等于并行写库。

| 功能 | 与 F01 的接口边界 |
|---|---|
| F02 / F03 | MCP 不重放写操作；源配置快照由 F03 构造，F01 锁定保存 |
| F04 | 每张非零估算按 cost_policy 执行 deny/ask/allow；确认仅在工作周期内有效 |
| F05 | 文件暂存、落盘证据、图片元数据；F01 完成图片与 item 的原子登记 |
| F06 / F07 | 提供启动时完整 prompt/aspect 列表；不在项目保存字典引用或提示词生产模型信息 |
| F08 | 提供 phash；F01 支持候选读取，不在 SQL 内实现感知距离 |
| F09 / F10 / F22 | 读取当前标注、方案、触发词；导出不回写图片与标注 |
| F12 / F15 | job 快照、分层结果、缓存键、rating 回写与断点恢复 |
| F14 | 图片筛选、详情、三档审图、存储统计、规则预览与撤销 |
| F23 / F24 | item.aspect 与源内实际尺寸快照；不保存尺寸预设外部引用 |

外部导入、项目管理、规则撤销等在旧清单尚无独立准确编号，按 docs/05 实现范围设计，不自行重编 F 编号。

## 2. 存储与字段解释

### 2.1 项目文件

项目根为 `<名>.slproj/`，必有 `project.json`、`project.db`、`images/`、`cache/`、`exports/`。`project.json` 仅含 `format_version: int`、`name: str`、`trigger_word: str`、`created_at: str`。产品端点、密钥、字典、模型文件和预设库不得进入项目。

数据库路径字符串统一使用 `/`。`images.filename` 是相对 `images/` 的单个文件名，例如 `123.png`，不是 `images/123.png`。方案输出目录相对 `exports/`。调用方传入的项目根 Path 是运行时绝对路径，不写入业务数据。路径验证同时拒绝盘符、UNC、绝对路径、`..`、反斜杠绕过及解析后逃出根目录的符号链接。

Q01/Q04 已采纳：保留并统一小写原扩展名，不转码原图；导出使用 `exports/<scheme_id>/<UTC时间>-<随机短码>/`。不持久化导入原绝对目录；续跑未复制项时由用户重新选择根目录。原目录展示仅在会话内保留；锁定 config.import_manifest 保存 item.seq 对应的根内相对位置，prompt 仍存原文件名。

### 2.2 类型、空值与验证分工

所有 8 个主键严格为 `INTEGER PRIMARY KEY`；不加 `AUTOINCREMENT`，也没有 sqlite_sequence 表。应用不删除历史行，主键正常使用不会复用已提交记录；回滚事务内分配过的 ID 不构成持久身份，文件协议不得仅凭 ID 判定归属。

时间由统一时钟显式绑定 UTC `YYYY-MM-DDTHH:MM:SS.ffffffZ`，不依赖本地时区。下文 `created_at` / `updated_at` 没有 SQL 默认值，遗漏即失败。可空列没有 DEFAULT 时，SQLite 默认 NULL；业务默认值全部显式写在 DDL 中。

JSON 使用 UTF-8 文本、确定性键顺序、禁止 NaN/Infinity。对象与数组的外形在 SQL 约束，内部 schema 在 DTO 验证器约束。运行要求 SQLite ≥3.38 且 JSON 函数可用；Python 版本本身不保证 SQLite 功能，打开项目时检查，失败报 `UnsupportedSQLite`。SQL 不用 STRICT 表，整数类型与浮点有限性由 DTO 严格验证，避免 SQLite 动态类型隐式转换混入业务。

外键全部 `ON UPDATE RESTRICT ON DELETE RESTRICT`，不使用级联或 SET NULL；保留溯源。公开 API 不提供 images、annotations、source_items、jobs、operation_log 的物理删除。删除源仅允许未启动且无 item/图片的 draft；方案可删除，但默认导入方案需先在同一事务中切换默认项。

扩展名、枚举扩展及 JSON 语义版本均须迁移或版本检查。预留其它 source type / executor / reader / exporter 不是允许随意写入未知字符串；本版 CHECK 仅列已支持值，新增值时迁移 CHECK。`rating_source=detector` 按已定决议现在就允许保存，但本版写 API 拒绝主动产生它。`pipeline_json` 允许数组存储，执行层对非空链报“不支持的处理器链”，不得静默忽略。

### 2.3 字段逐表对应

下文 DDL 顺序与更新后的 docs/05 §5.2—§5.9 相同，合计 93 列：裁决新增 sources.cost_policy、jobs.progress_json，不增加 runtime_json。附加 CHECK、索引和空值规则是本模块的实现细化。

| 表 | 字段数 | 与权威表逐一对应的字段（同顺序） |
|---|---:|---|
| sources | 13 | source_id, type, name, config_json, cost_policy, style_prompt, negative_prompt, target_count, status, locked, created_at, started_at, finished_at |
| source_items | 10 | item_id, source_id, seq, prompt, aspect, status, image_id, attempts, error, updated_at |
| images | 16 | image_id, filename, width, height, phash, sha256, file_size, source_id, file_missing, review_status, rating, rating_source, rating_verified, rated_at, png_meta_json, created_at |
| annotations | 12 | annotation_id, image_id, source, type, content, confidence, model_name, params_json, job_id, cache_key, created_at, is_current |
| jobs | 17 | job_id, executor, batch_id, mode, scope_json, config_snapshot_json, progress_json, status, total, done_count, failed_count, skipped_count, error, created_at, started_at, finished_at, updated_at |
| operation_log | 9 | op_id, rule_json, scope_json, affected_count, changes_json, status, created_at, undone_at, note |
| export_schemes | 9 | scheme_id, name, exporter, filter_json, caption_config_json, output_config_json, pipeline_json, created_at, updated_at |
| import_schemes | 7 | scheme_id, name, reader, params_json, is_default, created_at, updated_at |

## 3. 完整建表 DDL

以下是基线 schema v1 的规范 SQL；全部在初始化事务内执行。source_items 引用稍后创建的 images，SQLite 允许该建表顺序；必须所有表创建完成后再插入业务记录。CHECK 不表达跨表状态机，相关写入必须经过 §6 的事务服务。

### 3.1 sources

```sql
CREATE TABLE sources (
    source_id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN ('import', 'nai')),
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    config_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(config_json) AND json_type(config_json) = 'object'),
    cost_policy TEXT NOT NULL DEFAULT 'ask'
        CHECK (cost_policy IN ('deny', 'ask', 'allow')),
    style_prompt TEXT NOT NULL DEFAULT '',
    negative_prompt TEXT NOT NULL DEFAULT '',
    target_count INTEGER CHECK (target_count IS NULL OR target_count > 0),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'running', 'paused', 'done', 'canceled')),
    locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    CHECK (type <> 'import' OR (style_prompt = '' AND negative_prompt = '')),
    CHECK (status <> 'draft' OR (locked = 0 AND started_at IS NULL)),
    CHECK (status NOT IN ('running', 'paused', 'done')
           OR (locked = 1 AND started_at IS NOT NULL)),
    CHECK ((status IN ('done', 'canceled') AND finished_at IS NOT NULL)
           OR (status NOT IN ('done', 'canceled') AND finished_at IS NULL)),
    CHECK (type <> 'nai' OR locked = 0 OR target_count IS NOT NULL)
);
```

draft 可以暂缺有效配置，启动验证必须通过：NAI 模型/参数、三组实际像素、默认 aspect、quality_toggle=false、uc_preset=null、目标数匹配 item 数；import 配置必须有 reader 与参数快照。`locked` 只能从 0 变 1，不能解锁；启动后四个配置列及 type 不可修改，name 与独立运行策略 cost_policy 可编辑。cost_policy 不重复塞入锁定 config_json，修改生效于下一张；产品默认只在建源时复制。旧值/新值比较由写服务执行，并通过 CAS 验证。

### 3.2 source_items

```sql
CREATE TABLE source_items (
    item_id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    prompt TEXT NOT NULL,
    aspect TEXT CHECK (aspect IS NULL OR aspect IN ('landscape', 'portrait', 'square')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'done', 'failed', 'skipped')),
    image_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, seq),
    FOREIGN KEY (source_id) REFERENCES sources(source_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (image_id) REFERENCES images(image_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK ((status = 'done' AND image_id IS NOT NULL)
           OR (status <> 'done' AND image_id IS NULL)),
    CHECK (status <> 'skipped' OR (error IS NOT NULL AND error = 'duplicate_sha256'))
);
```

seq 从 1 开始连续分配；连续性由启动服务验证。import 的 aspect 必须为空；image_id 指向的 images.source_id 必须与 item.source_id 一致，跨表规则由完成事务验证。prompt/aspect/seq/source_id 在源启动后不可修改。attempts 解释见 §5.2；仅 import 可进入 skipped，error=duplicate_sha256、image_id=NULL，导入结束汇总跳过项，不冒认其它源图片。

### 3.3 images

```sql
CREATE TABLE images (
    image_id INTEGER PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE
        CHECK (length(filename) > 0 AND instr(filename, '/') = 0
               AND instr(filename, char(92)) = 0 AND instr(filename, ':') = 0
               AND filename NOT IN ('.', '..')),
    width INTEGER NOT NULL CHECK (width > 0),
    height INTEGER NOT NULL CHECK (height > 0),
    phash TEXT,
    sha256 TEXT NOT NULL
        CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
    file_size INTEGER NOT NULL CHECK (file_size > 0),
    source_id INTEGER NOT NULL,
    file_missing INTEGER NOT NULL DEFAULT 0 CHECK (file_missing IN (0, 1)),
    review_status TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (review_status IN ('unreviewed', 'keep', 'reject')),
    rating TEXT CHECK (rating IS NULL OR rating IN
        ('general', 'sensitive', 'questionable', 'explicit')),
    rating_source TEXT CHECK (rating_source IS NULL OR rating_source IN
        ('keyword', 'model', 'fallback', 'refusal', 'manual', 'detector')),
    rating_verified INTEGER NOT NULL DEFAULT 0 CHECK (rating_verified IN (0, 1)),
    rated_at TEXT,
    png_meta_json TEXT CHECK (png_meta_json IS NULL OR
        (json_valid(png_meta_json) AND json_type(png_meta_json) = 'object')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES sources(source_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK ((rating IS NULL AND rating_source IS NULL
            AND rating_verified = 0 AND rated_at IS NULL)
           OR (rating IS NOT NULL AND rating_source IS NOT NULL AND rated_at IS NOT NULL)),
    CHECK (rating_source IS NULL OR rating_source <> 'fallback' OR rating_verified = 0),
    CHECK (rating_source IS NULL OR rating_source NOT IN ('manual', 'refusal')
           OR rating_verified = 1)
);
```

phash 可空，允许指纹组件暂不可用；图片尺寸、sha256 和字节数完成后才能登记。不设 sha256 唯一约束，也不因重复内容合并来源。filename 与 image_id 的精确主干匹配由 DTO 验证。没有 `file_missing=1 ⇒ review_status=reject` 的 SQL CHECK：磁盘外部丢失也可能发生在 keep 图片；服务禁止缺失图片被新切换为 keep/unreviewed，但不伪造已存审图决定。

### 3.4 annotations

```sql
CREATE TABLE annotations (
    annotation_id INTEGER PRIMARY KEY,
    image_id INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('original', 'wd', 'llm', 'manual', 'rule')),
    type TEXT NOT NULL CHECK (type IN ('tags', 'nl')),
    content TEXT NOT NULL,
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0.0 AND 1.0),
    model_name TEXT,
    params_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(params_json) AND json_type(params_json) = 'object'),
    job_id INTEGER,
    cache_key TEXT,
    created_at TEXT NOT NULL,
    is_current INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    FOREIGN KEY (image_id) REFERENCES images(image_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
    CHECK (source NOT IN ('original', 'manual', 'rule') OR model_name IS NULL),
    CHECK (source NOT IN ('original', 'manual') OR job_id IS NULL),
    CHECK (source NOT IN ('wd', 'rule') OR type = 'tags'),
    CHECK (type <> 'tags' OR CASE WHEN json_valid(content)
           THEN json_type(content) = 'array' ELSE 0 END)
);
```

tags 每个元素严格为 `{tag: str, conf: float | null}`，tag 非空、conf 在 [0,1]；拒绝其它形状，由 DTO 验证。wd/llm 结果必须提交非空 model_name、job_id、cache_key 及实际参数；rule 必须由规则事务写入且 job_id 为空。original 只允许入库事务写，手改走 manual；空 tags 数组是明确空结果，和没有该层不同。历史行只允许切换 is_current，禁止覆盖 content/params/model。

### 3.5 jobs

```sql
CREATE TABLE jobs (
    job_id INTEGER PRIMARY KEY,
    executor TEXT NOT NULL CHECK (executor IN ('wd', 'llm_vision')),
    batch_id TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('selected', 'uncovered', 'all')),
    scope_json TEXT NOT NULL
        CHECK (json_valid(scope_json) AND json_type(scope_json) = 'object'),
    config_snapshot_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(config_snapshot_json) AND json_type(config_snapshot_json) = 'object'),
    progress_json TEXT NOT NULL DEFAULT '{"schema_version":1,"image_ids":[],"outcomes":{}}'
        CHECK (json_valid(progress_json) AND json_type(progress_json) = 'object'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'paused', 'done', 'failed', 'canceled')),
    total INTEGER NOT NULL DEFAULT 0 CHECK (total >= 0),
    done_count INTEGER NOT NULL DEFAULT 0 CHECK (done_count >= 0),
    failed_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    skipped_count INTEGER NOT NULL DEFAULT 0 CHECK (skipped_count >= 0),
    error TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK (done_count + failed_count + skipped_count <= total),
    CHECK (status <> 'pending' OR started_at IS NULL),
    CHECK (status NOT IN ('running', 'paused', 'done') OR started_at IS NOT NULL),
    CHECK ((status IN ('done', 'failed', 'canceled') AND finished_at IS NOT NULL)
           OR (status NOT IN ('done', 'failed', 'canceled') AND finished_at IS NULL)),
    CHECK (status <> 'done' OR done_count + failed_count + skipped_count = total)
);
```

job 的 done 指整个范围处理结束，允许 failed_count>0；failed 指批次级无法继续。snapshot 在首次启动冻结；恢复不重新读产品配置。progress_json 存冻结的有序 image_ids 和按 image_id 字符串键索引的 outcomes（kind/reason/annotation_ids/cache_keys），每图至多一条终态，与标注和计数同事务提交。total=len(image_ids)，三类计数从 outcomes 重算。恢复后模型不可用应保持 paused 或显式 failed，不悄悄换模型。

### 3.6 operation_log

```sql
CREATE TABLE operation_log (
    op_id INTEGER PRIMARY KEY,
    rule_json TEXT NOT NULL
        CHECK (json_valid(rule_json) AND json_type(rule_json) = 'object'),
    scope_json TEXT NOT NULL
        CHECK (json_valid(scope_json) AND json_type(scope_json) = 'object'),
    affected_count INTEGER NOT NULL DEFAULT 0 CHECK (affected_count >= 0),
    changes_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(changes_json) AND json_type(changes_json) = 'array'),
    status TEXT NOT NULL DEFAULT 'applied' CHECK (status IN ('applied', 'undone')),
    created_at TEXT NOT NULL,
    undone_at TEXT,
    note TEXT,
    CHECK ((status = 'applied' AND undone_at IS NULL)
           OR (status = 'undone' AND undone_at IS NOT NULL)),
    CHECK (affected_count = json_array_length(changes_json))
);
```

每个 changes 元素对应一张实际变化图片，含 `image_id`、`new_annotation_id`、`previous_annotation_id: int | null`。这些 JSON 内的 ID 不是 SQL 外键；写服务必须验证引用存在、归属一致、source=rule/type=tags，并在启动审计中检查。零变化不新增日志，返回 `op_id=None, affected_count=0`。

### 3.7 export_schemes

```sql
CREATE TABLE export_schemes (
    scheme_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    exporter TEXT NOT NULL DEFAULT 'image_txt' CHECK (exporter IN ('image_txt')),
    filter_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(filter_json) AND json_type(filter_json) = 'object'),
    caption_config_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(caption_config_json) AND json_type(caption_config_json) = 'object'),
    output_config_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(output_config_json) AND json_type(output_config_json) = 'object'),
    pipeline_json TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(pipeline_json) AND json_type(pipeline_json) = 'array'),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

项目初始化显式写一条默认方案，不靠 `{}` 推断产品默认策略。默认仅 keep、全部 tags 层合并、自然语言关闭（启用时取最高非空层）、触发词第一位、分隔符 `, `、新时间目录、pipeline=[]。合并及阈值配置放现有 caption_config_json，不新增含糊的 params_json 列；具体语义见 §7.4。方案名不唯一，以 scheme_id 定位；本表没有 is_default，默认方案不等于存在全局唯一默认标记。

### 3.8 import_schemes

```sql
CREATE TABLE import_schemes (
    scheme_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    reader TEXT NOT NULL DEFAULT 'txt_sidecar' CHECK (reader IN ('txt_sidecar')),
    params_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(params_json) AND json_type(params_json) = 'object'),
    is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

项目初始化写一条 `is_default=1` 的方案，参数明确 `encoding=utf-8-sig`（接受 UTF-8 BOM）、tags 逗号分隔、空行分段；严格解码，失败不静默替换字符。SQL 保证至多一个默认，服务保证正常项目至少一个；启动导入时复制 reader 与参数到源快照，此后方案编辑/删除不改变源。

## 4. 索引与查询合同

共有 **15 条显式 CREATE INDEX**（其中 3 条部分唯一），另有 **2 条 UNIQUE 自动索引**，合计 **17 条二级索引**。8 个 INTEGER PRIMARY KEY 使用 rowid，不额外计索引；无冗余的 source_items(source_id) 或 annotations(image_id) 单列索引。

```sql
CREATE UNIQUE INDEX ux_source_items_image
    ON source_items(image_id) WHERE image_id IS NOT NULL;
CREATE UNIQUE INDEX ux_annotations_current
    ON annotations(image_id, source, type) WHERE is_current = 1;
CREATE UNIQUE INDEX ux_import_schemes_default
    ON import_schemes(is_default) WHERE is_default = 1;
CREATE INDEX ix_sources_status_id ON sources(status, source_id);
CREATE INDEX ix_source_items_source_status_seq ON source_items(source_id, status, seq);
CREATE INDEX ix_images_source_review_id ON images(source_id, review_status, image_id);
CREATE INDEX ix_images_review_id ON images(review_status, image_id);
CREATE INDEX ix_images_sha256 ON images(sha256);
CREATE INDEX ix_images_phash ON images(phash) WHERE phash IS NOT NULL;
CREATE INDEX ix_annotations_history ON annotations(image_id, source, type, annotation_id);
CREATE INDEX ix_annotations_job_image ON annotations(job_id, image_id) WHERE job_id IS NOT NULL;
CREATE INDEX ix_annotations_current_cache
    ON annotations(source, type, cache_key, image_id) WHERE is_current = 1;
CREATE INDEX ix_jobs_status_id ON jobs(status, job_id);
CREATE INDEX ix_jobs_batch_id ON jobs(batch_id, job_id) WHERE batch_id IS NOT NULL;
CREATE INDEX ix_operation_log_status_id ON operation_log(status, op_id);
```

| 索引 | 服务查询 / 必须匹配的条件 |
|---|---|
| UNIQUE(source_id,seq) 自动索引 | 启动 upsert 冲突检查；`WHERE source_id=? AND seq>? ORDER BY seq LIMIT ?` |
| UNIQUE(filename) 自动索引 | 文件登记冲突检测、根据文件名查归属 |
| ux_source_items_image | 一个 image 至多归属一个 item；按 image_id 查生成索引 |
| ux_annotations_current | 详情当前层；`WHERE image_id=? AND source=? AND type=? AND is_current=1` |
| ux_import_schemes_default | 默认方案查找与切换时的唯一性 |
| ix_sources_status_id | 启动扫描 running/paused；按状态分页源 |
| ix_source_items_source_status_seq | 源的 pending 队列、sent 核查、done/failed/skipped 分组计数 |
| ix_images_source_review_id | 按源与审图状态的图集筛选、占用汇总；仅 source 查询可利用前缀 |
| ix_images_review_id | 全项目按审图状态筛选；reject 清理候选 |
| ix_images_sha256 | 导入前精确查重；允许多条命中 |
| ix_images_phash | 相同 pHash 快速候选；近似汉明距离仍需 F08 扫描候选，不承诺 B-tree 加速距离 |
| ix_annotations_history | 单层历史按 annotation_id 倒序分页；判断撤销依赖 |
| ix_annotations_job_image | 同 job 已提交结果与计数核对；FK 删除检查可利用非空索引 |
| ix_annotations_current_cache | 按目标层与 cache_key 检查覆盖；多图键不同可用 current 唯一索引逐图关联 |
| ix_jobs_status_id | 重启 running→paused；调度 pending，job_id 升序 |
| ix_jobs_batch_id | batch 进度聚合与 WD/LLM 展示排序 |
| ix_operation_log_status_id | `WHERE status='applied' ORDER BY op_id DESC LIMIT 1` |

不为低频方案名、rating、file_missing 单独建索引；按 image_id 扫描后过滤。实际统计量与 EXPLAIN QUERY PLAN 验证后才能增加索引，不声称所有过滤组合均无临时排序。

## 5. 状态机与恢复规则

所有迁移使用“读当前行并验证 + 条件 UPDATE + 检查 rowcount”的 CAS 语义。重复请求若已经达到完全相同的目标结果，返回现有结果；不同目标或失效前置条件报 StateConflict。数据库 CHECK 仅约束状态值及静态形状；下表是服务必须实现的合法边集，其余迁移全部拒绝。

### 5.1 sources

| 起点 → 终点 | 触发者 | 前置条件与同事务效果 |
|---|---|---|
| draft → running | 用户启动 / 源服务 | 校验全部参数与完整 item 列表；一次性写 items；locked=1；首次 started_at；finished_at=NULL |
| running → paused | 用户、F04、启动恢复 | 停止取新 pending；F04 当前项仍 pending；保留已发 sent，允许其结果落库 |
| paused → running | 用户继续 | 核查完遗留 sent、外部依赖可用；配置不变；不重抽 prompt |
| running → done | 完成协调器 | 全部 item 为 done/failed/skipped，且无未定落盘结果；设置 finished_at |
| running → canceled | 用户取消 | 停止派发，设置 finished_at；已发请求无法撤回，仍接收其合法结果 |
| paused → canceled | 用户取消 | 允许，不隐式恢复生成 |
| paused → done | 完成协调器 | 暂停时在途结果恰好全部终结，允许结束 |
| draft → canceled | 用户取消 | 允许；locked=0/started_at=NULL，设置 finished_at |
| done → paused | 用户重试失败项 | 与 failed→pending、attempts+1 同事务，清空 finished_at；配置仍锁定 |

done/canceled 均不自动恢复。生成配置锁定，cost_policy 可修改；canceled 中未发 pending 仅用于溯源，不再派发。done 源仅在用户明确重试 failed 时原子重开 paused，随后由用户继续；skipped 不重试，若确需导入重复文件应走另行明确的导入动作。canceled 需要创建新源。

源第一次启动不接受空列表；target_count import 可空。启动请求重试须比较全量配置及 `(seq,prompt,aspect)`，完全相同才返回现状；不能用 INSERT OR REPLACE 重建源。

### 5.2 source_items

| 起点 → 终点 | 触发者 | 条件与动作 |
|---|---|---|
| pending → sent | 源执行器 | source=running；提交本地发送意图后才调用 submit/复制；清空旧 error |
| pending / sent → skipped | 导入查重器 | 仅 import，确认 sha256 命中；image_id=NULL、error=duplicate_sha256；结束后汇总 |
| sent → done | F05 / 导入服务 / 恢复器 | 核实完整文件与归属；同事务登记 image、original 标注、回填 image_id |
| sent → failed | 明确失败或核查器 | 有明确错误或核查结束无完整产物；保存脱敏错误；不自动重新发送 |
| failed → pending | 用户明确重试 | 源可恢复且配置未变；attempts+1、清空 error；事务提交后仍需正常调度 |

done/skipped 不可迁出；sent 不可直接退回 pending。attempts 是尝试序号：初始化 0，首次 pending→sent 置 1；failed→pending 加 1（预留下一次尝试），重试 pending→sent 不再加。pending 查重直接 skipped 不增加次数；sent 查重跳过保留当前次数。不能在授权重试和发送时重复加一。

费用决策发生在 prepare 之后、sent 之前；暂停不增加 attempts，该项保持 pending。estimated_anlas 非零（包括本周期第一张）按 sources.cost_policy 执行：deny 暂停并说明原因，不提供本周期同意按钮；ask 暂停并询问；allow 直接提交，但 Launcher 自身的付费确认仍可能弹出。零估算直接继续。

工作周期是一次 running，从启动/继续到暂停、停止、完成或应用退出。ask 同意按钮将“paused→running + 为新周期授权”作为同一服务动作，旧周期确认已失效；该新周期内后续非零不再问。任意再次暂停即清除授权，普通继续没有隐含同意。内存保存 cycle_id/ack，过期确认请求因 cycle_id 不匹配被拒；不存在跨重启确认。

cost_policy 可随时修改，写库后下一张取新值；已 sent 不撤回。修改策略同时清除内存 ack，切回 ask 时重新确认；deny 下只能改策略后继续，不能用同意按钮绕过。无 sources.runtime_json：pause_reason 由当前会话事件展示，重启统一显示“中断后暂停”，无需持久化。网络超时不证明未生成，保留 sent 核查。

### 5.3 jobs

| 起点 → 终点 | 触发者 | 条件与效果 |
|---|---|---|
| pending → running | job 调度器 | 冻结范围、配置、模型、Skill 哈希，计算 total，设置 started_at |
| running → paused | 用户 / 启动恢复 | 停止派发；重启一律暂停，由用户选择继续 |
| paused → running | 用户 | 沿用快照；重新核查缓存覆盖与已提交结果，不清空进度 |
| running → done | job 协调器 | 全部候选均成功/失败/跳过；计数之和等于 total |
| running → failed | job 协调器 | 批次级不可恢复错误；保留已有结果与进度 |
| running → canceled | 用户 | 不派发新图；保留历史结果；设置 finished_at |
| pending / paused → canceled | 用户 | 允许取消尚未启动或已暂停任务 |
| pending → failed | 配置验证器 | 角色缺失等启动前错误，started_at 可空 |
| paused → done / failed | 协调器 | 在途结果已结清或依赖确认不可用 |

failed/canceled/done 不改回 pending；手动重做创建新 job。多选在一个事务建多个 job，共用调用方稳定 batch_id，按 WD 后 LLM 顺序分配 job_id，调度串行。running→paused 后收到当前进程在途结果可完成本图；canceled 后迟到结果默认丢弃、不新增标注。结果提交时检查 job 状态和运行内存代次；旧进程连接不可跨恢复复用。

0.15.0 组合调度细化：`freeze_job(..., start=False)` 可在 pending 保存不可变快照、有序 IDs 与 total，保留空 started_at、不登记运行代次。建批时冻结两阶段，只启动 WD；后续 pending→running 必须已有快照且前序全部 done，首次设置 started_at 并登记代次。默认 start=True 保持单阶段兼容。pending 的 error 可保存 `job:batch_paused` 或依赖等待原因，状态仍是 pending；整批控制与恢复语义见 [组合批次验收](22-F15-组合批次与覆盖预览验收.md)。

`mode=all` 新 job 首轮必须忽略历史缓存并重新执行，不能被普通 cache-hit 短路。同一个 job 的重复结果、恢复则按已提交证据跳过；“其它旧 job 有相同键”与“本 job 已完成”必须区分。LLM 同时输出 tags/nl 时，两层共同提交才算一张成功，不能把单层缓存算整张覆盖。

progress_json 是逐图持久凭据，不增加子表。启动物化筛选范围并冻结 image_ids；有终态 outcome 的图片恢复时不重做、不重复计数，即使当前标注后来换版。未有 outcome 的图按快照覆盖判定：all 只承认本 job 证据，其余模式允许当前缓存命中记 skipped。失败/跳过也是终态；手动重试创建新 job。只能保证已提交结果精确记账，不能保证外部 LLM 返回而未落库时不再次付费。

### 5.4 images.review_status 与 rating

| 迁移 | 触发者 | 条件 |
|---|---|---|
| unreviewed ↔ keep；unreviewed ↔ reject；keep ↔ reject | 用户审图 | 文件存在时任意互转；同状态写入返回 unchanged |
| 任意状态 → reject | 用户 | 文件缺失也允许；仅写标记，不删文件 |
| 缺失图片 → keep / unreviewed | 用户 | 拒绝 FileMissing；恢复真实文件并核验后才可解除 missing |
| reject 的 file_missing: 0→1 | 已确认的清理动作 | 只删文件，保留图片行、全部标注及历史 |

rating 更新四列为一个原子单元；普通未知不写入，fallback 写入但 verified=0；manual/refusal 置 verified=1，refusal 固定 questionable。后台分级结果须比对提交前 rating 快照，不能覆盖用户并发手改。重新分级仅清空 keyword/model/fallback 的四列；manual/refusal 不动。detector 保留值不在本版执行。

### 5.5 启动恢复顺序与文件证据

打开时先取得项目独占锁，完成版本/迁移与完整性检查，启动写线程但暂不调度。一次事务把 running jobs→paused、running sources→paused，保持 started_at/快照，不重算或清空历史计数。再分页核查 sent，核查结果写库串行。仅用户继续后恢复派发。

F05 必须提供可持久恢复的 `item_id + 尝试序号 + 文件指纹 + 完整落盘标志` 证据；仅存在同名 PNG 或相似 prompt 不足以认定归属。已采纳在 `images/.pending/<item_id>/<attempts>/` 存临时产物和收据，不能放可随时清空的 cache。这是文件登记协议，不是第九张业务表，不得含外部绝对路径/密钥。

Launcher save_path 实测结论（2026-10-12，v4.3.0）：
- 可写任意本地目录（实测 `D:/IdeaProjects/sp_probe/`），父目录不存在时自动创建；返回 `saved: true`、`saved_path_source: "caller"`、`resource_ref.resourceId`、`metadata_stripped: false`。
- 目标已存在时 **prepare 阶段**即返回 `{"ok":false,"code":"save_path_exists"}`，从不覆盖。
- Launcher 无论是否传 save_path 都会在画廊 `NAIGEN/<日期>/` 另存一份（双写）。画廊副本归 Launcher 管，产品不得移动/删除，存储占用扫描不计入项目。
- 因此 F05 直接把 save_path 设为 `<项目>/images/.pending/<item_id>/<attempts>/out.png`：路径每次尝试唯一，`save_path_exists` 等价于「该 attempt 已有产物」，转入收据核查而非重试；resourceId 写入收据作为归属证据。

已采纳的文件协议如下；无法获得完整证据时按 unknown 保持暂停：

1. pending→sent 提交后请求外部操作，结果写入该 item/attempt 的 staging。网络请求与大文件复制均在数据库事务外。
2. 完整解码校验、尺寸、大小、sha256、original 解析准备完成，写收据并刷新文件。收据中的外部 save_path 只能运行时使用，持久化的是项目相对路径。
3. 单写连接 BEGIN IMMEDIATE，分配 image_id；先把候选 ID、目标文件名、哈希写入可恢复收据并刷新，再原子改名到 images/<image_id>.<ext>；目标已存在则核查，禁止覆盖无关文件。
4. 同一数据库事务插入 images、original 标注、sent→done；COMMIT 后清理收据。重命名与 SQLite 无跨介质原子性，故必须保留收据直到提交已证实。
5. 改名后提交前崩溃：启动恢复优先处理收据，核对 item/attempt/hash，补登记后才允许分配新 image_id；若候选 ID 被回滚后占用，分配新 ID 并改名，不能复用错误归属。
6. 提交后清理前崩溃：发现 done 与相同 hash/归属，返回原 image_id，仅清理收据，不新增图片。

无收据但可从 Launcher 明确查询到可信 item/attempt 对应文件时，允许适配器构造等价证据。未找到完整产物且核查已结束：sent→failed，标记 `recovery:no_complete_artifact`；无法访问外部路径、仍可能在途等“未知”情形保持 sent、源保持 paused，提示人工核查，绝不自动 submit。

磁盘上孤立文件只报告，不自动删或冒认。数据库指向的文件丢失则置 file_missing=1 并报告；不会删 DB 行。清理 reject 文件采用“检查状态→短暂禁止该图审图/导出→删文件→置 missing”协议；删后提交前崩溃由文件审计补写 missing。不得用“DB 已写 missing”作为文件已实际删除的证明。

## 6. 数据访问层模块与公开接口

### 6.1 公共类型和约定

拟放置 `src/stylelora/project_db/`；这是模块建议，不继承 docs/04 的旧 stages 架构。以下仅接口声明，`...` 表示无实现。统一以不可变 dataclass/TypedDict 表示 DTO；SQL Row 不泄漏到 API。

```python
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Literal, TypeAlias, TypeVar

JsonValue: TypeAlias = None | bool | int | float | str | list['JsonValue'] | dict[str, 'JsonValue']
JsonObject: TypeAlias = dict[str, JsonValue]
SourceStatus: TypeAlias = Literal['draft', 'running', 'paused', 'done', 'canceled']
ItemStatus: TypeAlias = Literal['pending', 'sent', 'done', 'failed', 'skipped']
JobStatus: TypeAlias = Literal['pending', 'running', 'paused', 'done', 'failed', 'canceled']
ReviewStatus: TypeAlias = Literal['unreviewed', 'keep', 'reject']
Layer: TypeAlias = Literal['original', 'wd', 'llm', 'manual', 'rule']
AnnotationType: TypeAlias = Literal['tags', 'nl']
Aspect: TypeAlias = Literal['landscape', 'portrait', 'square']
T = TypeVar('T')
```

下列类型在 `types.py` 定义，字段合同如下；不使用 Any 规避接口约束。Row 类型的列名与 §2.3 完全相同，INTEGER 映射 int（0/1 语义列映射 bool）、REAL 映射 float、TEXT 映射 str、可空列增加 `| None`；JSON 列在 Row 映射为 JsonObject/tuple 元素 DTO，数据库编码只在 repository 内进行。

| 类型 | 必备字段 / 语义 |
|---|---|
| ProjectHandle | root: Path, metadata: ProjectMetadata, schema_version: int, writer: WriterQueue；不公开 sqlite3.Connection |
| ProjectMetadata | format_version: int, name: str, trigger_word: str, created_at: str |
| ReadSession / WriteSession | 内部连接包装；WriteSession 只在写线程、事务作用域有效，不可保存或跨 await |
| PageRequest | after_id: int=0, limit: int=200；上限 1000；必要时 upper_id: int 指定快照上界 |
| Page[T] | items: tuple[T, ...], next_after_id: int \| None, upper_id: int；空页 next=None |
| SourceDraft | type: Literal['import','nai'], name: str, config: JsonObject, cost_policy: Literal['deny','ask','allow'], style_prompt: str, negative_prompt: str, target_count: int \| None |
| SourcePatch | name: str \| None, cost_policy: Literal['deny','ask','allow'] \| None；锁定配置字段使用 UNSET，不含 cost_policy |
| ItemSeed | seq: int, prompt: str, aspect: Aspect \| None |
| SourceFilter / ItemFilter | statuses: tuple[相应状态, ...]；ItemFilter.source_id: int 必填 |
| ImageFilter | source_ids: tuple[int,...] \| None, review_statuses: tuple[ReviewStatus,...] \| None, include_missing: bool；0.16.0 增加可选 rating、file_missing、q，复用工作台 SQL 语义；不接受 SQL 字符串 |
| ImageArtifact | item_id: int, attempt: int, relative_staging_path: str, extension: str, width: int, height: int, phash: str \| None, sha256: str, file_size: int, png_meta: JsonObject \| None, receipt_digest: str |
| Tag | tag: str, conf: float \| None |
| AnnotationInput | image_id: int, source: Layer, type: AnnotationType, content: tuple[Tag,...] \| str, confidence: float \| None, model_name: str \| None, params: JsonObject, job_id: int \| None, cache_key: str \| None |
| OriginalInput | type: AnnotationType, content: tuple[Tag,...] \| str, confidence: float \| None, params: JsonObject, cache_key: str \| None；入库命令绑定 image_id/source，model_name/job_id 固定为空 |
| CurrentRef | image_id: int, source: Layer, type: AnnotationType, annotation_id: int \| None；None 表示期望尚无当前行 |
| RatingValue | rating: Literal['general','sensitive','questionable','explicit'], source: Literal['keyword','model','fallback','refusal','manual','detector'], verified: bool, rated_at: str |
| JobRequest | executor: Literal['wd','llm_vision'], mode: Literal['selected','uncovered','all'], scope: Scope |
| Scope | selected_ids: tuple[int,...] \| None, filter: ImageFilter \| None；二者恰有一个；JSON 用对象包装 |
| JobSnapshot | role/model/protocol/params、目标层类型、Skill 哈希、并发/超时/重试和路由配置，内部结构由执行器版本验证；不得含 URL/token |
| JobOutcome | image_id: int, kind: Literal['done','failed','skipped'], annotations: tuple[AnnotationInput,...], reason: str \| None, annotation_ids: tuple[int,...]；progress_json 持久化此结果 |
| RulePlan | rule: JsonObject, scope: Scope, changes: tuple[RuleImageChange,...], read_set: tuple[CurrentRef,...], preview_digest: str |
| RuleImageChange | image_id: int, tags: tuple[Tag,...], previous_rule_id: int \| None, merge_meta: JsonObject；schema 与引用按 §7.4 验证 |
| RuleApplyResult | op_id: int \| None, affected_count: int |
| WriteResult[T] | value: T, changed: bool；重复提交可返回 changed=False |
| RecoveryEvidence | item_id: int, attempt: int, verdict: Literal['complete','absent','unknown'], artifact: ImageArtifact \| None, reason: str |
| RecoveryReport | paused_source_ids/job_ids、recovered/failed/unresolved item_ids、missing image_ids、orphan 相对文件名，均为不可变 tuple |
| ExportSchemeInput / ImportSchemeInput | 与对应表除 scheme_id/时间之外各列一一对应；JSON 以验证后的对象/数组传入 |
| MigrationInfo | from_version: int, to_version: int, name: str, checksum: str |

0.16.0 范围合同：mode 决定缓存执行策略，Scope 决定图片成员，二者正交。selected 可使用筛选条件表达“全选筛选结果”，all 可使用明确 IDs 表达失败重试；仍只允许一个 selector，建批事务中冻结完整有序 IDs。扩展过滤字段省略默认 null 时，scope_json 保持旧版本形状和请求重放身份；不增加 SQL 列或迁移。失败重试创建新的 all 单阶段 job，在 params.retry_origin 中保存来源 job ID 与预览摘要，不修改旧 outcome。

Row DTO 名称为 SourceRow、SourceItemRow、ImageRow、AnnotationRow、JobRow、OperationRow、ExportSchemeRow、ImportSchemeRow。JSON 的业务 schema 必须带内部 `schema_version`（新对象），旧版本解释由迁移统一处理；默认空对象仅用于 draft，不允许启动/执行时跳过验证。

### 6.2 模块职责、依赖、错误、幂等与最小测试

| 模块 | 职责 / 依赖 | 错误处理 | 幂等合同 | 最小测试 |
|---|---|---|---|---|
| types.py / validation.py | DTO、路径/枚举/JSON/时间验证；仅标准库 | ValidationError、UnsafePath，入队前失败 | 规范化同一输入产生相同 JSON | 非法 tag/conf、盘符、NaN 被拒 |
| connection.py | 连接配置、单写队列、读快照、事务；sqlite3/threading | Busy、DiskFull、Corrupt；回滚与连接失效判别 | 重复 close 无害；不盲重试未知提交 | 双线程写串行、FK 每连接开启 |
| project.py | 创建/打开/关闭、项目锁、元数据；connection/migrations/recovery | ProjectExists、ProjectLocked、VersionMismatch | open 进程内同根返回现有 handle；create 不覆盖 | 创建含默认方案，二次创建拒绝 |
| repositories/sources.py | 源与 items 查询、draft 编辑、自然键 seed；types/session | NotFound、LockedSource、StateConflict | (source_id,seq) 相同值 no-op，不同值冲突 | seed 重放不多行，锁定不能改参数 |
| repositories/images.py | 图片读取、筛选、审图、rating；validation/session | FileMissing、StateConflict | 相同审图值 no-op，sha256 非唯一 | reject 不删文件，手改 rating 不被迟到结果覆盖 |
| repositories/annotations.py | 当前层/历史/覆盖、版本追加；session | CurrentConflict、ConstraintViolation | cache 匹配当前行返回已有；all 新 job 例外 | 唯一当前层、换版失败回滚 |
| repositories/jobs.py | 批次创建、查询、冻结、进度；session | SnapshotMismatch、StateConflict | batch 相同请求 no-op；progress_json 按图去重 | WD/LLM 分 job；多层计一图 |
| repositories/operations.py | 日志读取与撤销记录访问；session | UndoConflict、InvalidChangeReference | undone 重撤返回原记录 | JSON 引用必须匹配真实标注 |
| repositories/schemes.py | 导入/导出方案 CRUD 与默认切换；session | UnsupportedScheme、StateConflict | 对同 ID 同内容 update 不改时间 | 默认切换失败仍只有原默认 |
| commands.py | 跨表原子用例、状态边验证；各 repo | 领域异常使整事务回滚 | 同结果返回旧值；请求键不支持时明确非幂等 | item/image/original 同成同败 |
| recovery.py | 启动暂停、核查证据、落盘补记；F05 只读 probe/commands | UnresolvedArtifact 保持 paused/sent | 多次恢复不重复登记、不重放 submit | 四个文件崩溃窗口注入 |
| migrations.py | 版本映射、备份、升级恢复；connection/project | MigrationFailed、NewerProject | 每步版本 CAS；已完成不重复迁移 | DB 已升级 JSON 未改仍能恢复 |

规则算法、模型调用、哈希计算不放 repository。所有 SQL 使用绑定参数；表名、排序键只从内部白名单选择。

### 6.3 项目与连接接口

```python
# project.py
def create_project(root: Path, *, name: str, trigger_word: str) -> ProjectHandle: ...
def open_project(root: Path, *, migrate: bool = True) -> ProjectHandle: ...
def close_project(project: ProjectHandle) -> None: ...
def update_metadata(project: ProjectHandle, *, name: str, trigger_word: str) -> ProjectMetadata: ...

# connection.py（WriterQueue 对外只提交纯数据库短任务）
def submit_write(project: ProjectHandle, work: Callable[[WriteSession], T]) -> Future[T]: ...
def read_snapshot(project: ProjectHandle) -> AbstractContextManager[ReadSession]: ...
def write_transaction(project: ProjectHandle) -> AbstractContextManager[WriteSession]: ...
```

write_transaction 只供写线程内部调用，外部调用报 WrongThread。FastAPI 用异步桥接 Future 等待，不直接在事件循环执行 sqlite3 阻塞操作。repository 函数是同步的，传入 ReadSession 的地方可以使用其子类型 WriteSession；只有最外层 command 提交事务，repo 禁止自行 commit/rollback。

create_project 要求目标不存在，在同卷临时目录构建全部文件、建表/索引/默认方案、写 metadata、校验并关闭连接后改名发布，再打开正式目录。失败清理仅本次临时目录，不覆盖用户目录。trigger_word 允许暂空供编辑，F22/导出前才要求有效。

### 6.4 每表 repository 接口

```python
# repositories/sources.py
def get_source(db: ReadSession, source_id: int) -> SourceRow: ...
def list_sources(db: ReadSession, query: SourceFilter, page: PageRequest) -> Page[SourceRow]: ...
def create_source(db: WriteSession, draft: SourceDraft) -> SourceRow: ...
def update_source(db: WriteSession, source_id: int, patch: SourcePatch) -> WriteResult[SourceRow]: ...
def delete_empty_draft(db: WriteSession, source_id: int) -> None: ...
def seed_items(db: WriteSession, source_id: int, items: Sequence[ItemSeed]) -> tuple[SourceItemRow, ...]: ...
def get_item(db: ReadSession, item_id: int) -> SourceItemRow: ...
def list_items(db: ReadSession, query: ItemFilter, page: PageRequest) -> Page[SourceItemRow]: ...
def count_item_states(db: ReadSession, source_id: int) -> Mapping[ItemStatus, int]: ...

# repositories/images.py
def get_image(db: ReadSession, image_id: int) -> ImageRow: ...
def list_images(db: ReadSession, query: ImageFilter, page: PageRequest) -> Page[ImageRow]: ...
def find_by_sha256(db: ReadSession, sha256: str) -> tuple[ImageRow, ...]: ...
def set_review(db: WriteSession, image_id: int, *, expected: ReviewStatus,
               target: ReviewStatus) -> WriteResult[ImageRow]: ...
def set_file_missing(db: WriteSession, image_id: int, *, missing: bool,
                     verified_sha256: str | None) -> WriteResult[ImageRow]: ...
def set_rating(db: WriteSession, image_id: int, *, expected: RatingValue | None,
               value: RatingValue | None) -> WriteResult[ImageRow]: ...
def clear_recomputable_ratings(db: WriteSession, image_ids: Sequence[int]) -> int: ...

# repositories/annotations.py
def get_current(db: ReadSession, image_id: int, source: Layer,
                type: AnnotationType) -> AnnotationRow | None: ...
def list_history(db: ReadSession, image_id: int, source: Layer,
                 type: AnnotationType, page: PageRequest) -> Page[AnnotationRow]: ...
def is_covered(db: ReadSession, image_id: int, keys: Mapping[tuple[Layer, AnnotationType], str]) -> bool: ...
def append_version(db: WriteSession, value: AnnotationInput, *, expected: CurrentRef,
                   policy: Literal['cache', 'content', 'force_new_job']) -> WriteResult[AnnotationRow]: ...

# repositories/jobs.py
def get_job(db: ReadSession, job_id: int) -> JobRow: ...
def list_jobs(db: ReadSession, *, status: JobStatus | None,
              batch_id: str | None, page: PageRequest) -> Page[JobRow]: ...
def create_batch(db: WriteSession, batch_id: str, requests: Sequence[JobRequest]) -> tuple[JobRow, ...]: ...
def freeze_job(db: WriteSession, job_id: int, snapshot: JobSnapshot,
               *, image_ids: Sequence[int], start: bool = True) -> WriteResult[JobRow]: ...

# repositories/operations.py
def get_operation(db: ReadSession, op_id: int) -> OperationRow: ...
def latest_applied(db: ReadSession) -> OperationRow | None: ...
def list_operations(db: ReadSession, page: PageRequest) -> Page[OperationRow]: ...

# repositories/schemes.py
def get_export_scheme(db: ReadSession, scheme_id: int) -> ExportSchemeRow: ...
def list_export_schemes(db: ReadSession, page: PageRequest) -> Page[ExportSchemeRow]: ...
def create_export_scheme(db: WriteSession, value: ExportSchemeInput) -> ExportSchemeRow: ...
def update_export_scheme(db: WriteSession, scheme_id: int, value: ExportSchemeInput,
                         *, expected_updated_at: str) -> WriteResult[ExportSchemeRow]: ...
def delete_export_scheme(db: WriteSession, scheme_id: int) -> None: ...
def get_import_scheme(db: ReadSession, scheme_id: int) -> ImportSchemeRow: ...
def get_default_import_scheme(db: ReadSession) -> ImportSchemeRow: ...
def list_import_schemes(db: ReadSession, page: PageRequest) -> Page[ImportSchemeRow]: ...
def create_import_scheme(db: WriteSession, value: ImportSchemeInput) -> ImportSchemeRow: ...
def update_import_scheme(db: WriteSession, scheme_id: int, value: ImportSchemeInput,
                         *, expected_updated_at: str) -> WriteResult[ImportSchemeRow]: ...
def set_default_import_scheme(db: WriteSession, scheme_id: int) -> WriteResult[ImportSchemeRow]: ...
def delete_import_scheme(db: WriteSession, scheme_id: int) -> None: ...
```

expected_updated_at 是同事务比较的编辑令牌，写时生成严格晚于旧值的时间，避免同微秒冲突。删除最后一个导出方案也拒绝；不增加字段即可保持项目始终有可用方案。create_source/create_scheme 本身不是跨进程请求幂等接口，重复请求会创建新实体；调用方不得自动重放未知成功的创建请求，可先查询确定结果。需要持久创建请求去重时另行确认请求键，不把名字当唯一键。

### 6.5 跨表 command、恢复、迁移接口

```python
# commands.py：由 submit_write 调用，每次一个完整事务
def start_source(db: WriteSession, source_id: int, items: Sequence[ItemSeed]) -> SourceRow: ...
def transition_source(db: WriteSession, source_id: int, *, expected: SourceStatus,
                      target: SourceStatus, reason: str) -> WriteResult[SourceRow]: ...
def mark_sent(db: WriteSession, item_id: int) -> WriteResult[SourceItemRow]: ...
def skip_duplicate(db: WriteSession, item_id: int, *, sha256: str) -> WriteResult[SourceItemRow]: ...
def set_cost_policy(db: WriteSession, source_id: int, *,
                    policy: Literal['deny', 'ask', 'allow']) -> WriteResult[SourceRow]: ...
def fail_item(db: WriteSession, item_id: int, *, attempt: int, error: str) -> WriteResult[SourceItemRow]: ...
def retry_failed_item(db: WriteSession, item_id: int, *, expected_attempt: int) -> WriteResult[SourceItemRow]: ...
def complete_item(db: WriteSession, artifact: ImageArtifact,
                  originals: Sequence[OriginalInput]) -> WriteResult[ImageRow]: ...
def transition_job(db: WriteSession, job_id: int, *, expected: JobStatus,
                   target: JobStatus, error: str | None = None) -> WriteResult[JobRow]: ...
def commit_job_outcome(db: WriteSession, job_id: int, outcome: JobOutcome) -> WriteResult[JobRow]: ...
def apply_rule(db: WriteSession, plan: RulePlan) -> RuleApplyResult: ...
def undo_rule(db: WriteSession, op_id: int) -> WriteResult[OperationRow]: ...
def bulk_review(db: WriteSession, image_ids: Sequence[int], *,
                 expected: Mapping[int, ReviewStatus], target: ReviewStatus) -> tuple[ImageRow, ...]: ...
def edit_manual_tags(db: WriteSession, image_id: int, *, tags: Sequence[Tag],
                     read_set: Sequence[CurrentRef]) -> WriteResult[AnnotationRow]: ...

# 费用协调服务：串行执行状态写入及周期内存更新，非 repository 持久字段
def confirm_cost_and_resume(project: ProjectHandle, source_id: int, *,
                            expected_cycle_id: str) -> SourceRow: ...
def evaluate_cost(project: ProjectHandle, source_id: int, *,
                   estimated_anlas: int) -> Literal['submit', 'paused']: ...

# recovery.py：probe 只核查，绝不 submit
def pause_interrupted(db: WriteSession) -> RecoveryReport: ...
def inspect_sent(project: ProjectHandle, item: SourceItemRow) -> RecoveryEvidence: ...
def reconcile_item(db: WriteSession, evidence: RecoveryEvidence) -> WriteResult[SourceItemRow]: ...
def recover_project(project: ProjectHandle) -> RecoveryReport: ...

# migrations.py
def plan_migrations(format_version: int, user_version: int) -> tuple[MigrationInfo, ...]: ...
def migrate_project(root: Path, *, target_version: int) -> ProjectMetadata: ...
def verify_integrity(db: ReadSession, *, full: bool = False) -> tuple[str, ...]: ...
```

complete_item 的 originals 使用不含 image_id 的 OriginalInput，由命令统一绑定新图片及 original 层。commit_job_outcome 按 progress_json 的 image_id 去重：相同结果返回 unchanged，不同终态报 OutcomeConflict；不得仅 count+1。append_version 对 manual/rule 的 params 必须由编辑/规则服务构造，校验 §7.4 元数据，不能接受客户端任意伪造 cutoff/来源。

### 6.6 事务与 upsert 合同

| 用例 | 一个事务中必须完成 | 事务外行为 |
|---|---|---|
| 创建项目 | 8 表/索引、2 默认方案、user_version | 临时目录发布、metadata 原子替换 |
| 启动源 | seed 全部 item、锁定配置、状态/时间 | 提示词抽取先完成；不得事务内调用 LLM |
| 发送意图 | pending→sent、attempts | commit 后才能 submit；不由事务重试器重发 |
| 完成 item | images + original tags/nl + item.done/image_id | 文件复制、指纹及解析先完成；仅最终改名在短事务协同 |
| 标注换版 | 验证 expected/current/cache；旧行置 0；插新行置 1 | 模型调用与规则计算在外 |
| job 逐图完成 | 多目标层换版 + 成功计数 + C02 逐图 outcome | 一张图一次计数；失败/跳过不伪造 annotation |
| 审图批量 | 整批前置条件验证与写入 | 不触碰图片文件 |
| 规则应用 | 验证预览读集 + 所有 rule 快照 + 一条 operation_log | 预览/规则算法在外 |
| 规则撤销 | 全部引用验证 + 当前位回退 + 日志 undone | 无文件或外部调用 |
| 默认方案切换 | 原默认置 0 + 目标置 1 | 源快照不变 |

upsert 不使用 INSERT OR REPLACE（它会删除旧行破坏外键/历史）。seed_items 对自然键 `(source_id,seq)` 先查重，值相同 no-op，值不同报 SeedConflict；只允许源启动事务写入，不允许启动后追加。complete_item 对 item_id+attempt 判断，done 且 hash/来源相同返回原图，不同结果报 ArtifactConflict。annotations 对当前 tuple+cache_key 命中 no-op；不同 key 新增历史，不对所有历史设置 cache_key 唯一，因为 A→B→A 可以合法再生新当前版本。manual/rule 无 cache_key，比较规范化 content 与实际 params；强制 all 仅新 job 初次允许绕过旧缓存，同 job 重交仍去重。

### 6.7 分页与批量约定

列表统一按主键递增 keyset 分页，不用 OFFSET；history 可提供明确的反向主键游标，不能把正向游标混用。item 队列按 seq 的专用内部游标执行，对外 list_items 主键排序以符合 PageRequest。首批记录 upper_id，后续排除新插入 ID，避免导出/统计无限追随新增行。

普通列表是逐页独立快照，过滤属性变化可能引起成员变化，接口不保证跨页全局快照。导出及一致性统计使用同一个 ReadSession 快照，尽快读取所需元数据后释放；文件读另行检查缺失。长 job 范围必须在启动时物化并冻结（C02），不能只靠 upper_id 保证 review_status 筛选不变。

IN 参数按连接实际 `SQLITE_LIMIT_VARIABLE_NUMBER` 分块，默认每块最多 500，预留其它绑定参数；不是按 Python 列表长度直接拼 SQL。大批只读每页 200，最多 1000。source 启动虽分块 executemany，仍一个事务；不能半批启动。用户批量审图允许显式拆成独立请求并报告每批结果。规则操作不能暗中分多个事务：计划超过内存/日志预算时，执行前报 BatchTooLarge 并要求调用方显式拆成多个可独立撤销的操作；推荐初始上限 5000 图或 16 MiB changes JSON，可配置后压测调整。

## 7. 一致性、并发与撤销

### 7.1 连接与写队列

本版一次打开一个项目，切换先安全关闭；写连接在线程内部创建，保留 `check_same_thread=True`，Python 3.11 用 `isolation_level=None` 加显式事务。初始化回读 WAL、foreign_keys=ON、synchronous=FULL、busy_timeout=5000；read 连接同样启用外键/超时，另设 query_only=ON，每个连接只由所属线程使用。

写任务 FIFO 串行、短事务 BEGIN IMMEDIATE；网络、长时 CPU、文件复制在事务外。禁止嵌套 BEGIN，repo 复用现有 session；需要局部回滚时只有外层 command 可以显式 SAVEPOINT，规则事务默认整批回滚。读写并行依赖 WAL 快照，不允许两个 Uvicorn worker 打开同一项目写入。项目目录使用 Windows 可释放的进程文件锁（推荐隐藏 `.project.lock`，锁文件无业务数据）；崩溃 OS 释放锁，不能仅凭锁文件存在判锁。

busy 在开始事务且尚无副作用时可有限重试；COMMIT 失败或连接丢失时向上返回 CommitUnknown，先按持久身份查证，不能重放文件操作或外部提交。IntegrityError 映射 ConstraintViolation/StateConflict；OperationalError 按 busy、只读、磁盘满、I/O 分别映射。SQL、凭据及外部原始响应不直接写 error 或返回前端。建议 HTTP 映射：不存在 404、状态/锁冲突 409、输入 422、项目被锁/暂忙 423/503、损坏或 I/O 500；由 API 层决定响应，不在 DAL 引入 FastAPI 依赖。

运行中允许 WAL 自动 checkpoint；显式 PASSIVE 在空闲执行，TRUNCATE 仅关闭且无活跃 reader 时。关闭先停派发、排空已接受写任务、关闭读连接，再 checkpoint 和关闭写连接。不要在打开项目时直接复制 project.db 作备份；必须 sqlite3 backup 或关闭全部连接并确认 checkpoint 完成。活跃 WAL/SHM 不能单独删。

### 7.2 规则快照与撤销

规则预览记录所有实际读取的 current annotation_id，不只 rule 层，还包含条件使用的 original/wd、合并读取的其它层及“该层不存在”的证据。执行在写事务内重新验证完整 read_set；有变更报 PreviewStale，要求重新预览。条件全量/任意及合并优先级由规则模块提供，DAL 不发明规则语义。

每张真正变化图：把旧 rule 当前行置 0，插入完整 tags 快照，写 new/previous ID；其它层不改。一条日志覆盖本次事务全部变化；失败全回滚。新 WD/LLM 行不会自动触发规则，导出也不能把 rule 快照解释成持续生效的替换程序。

Q08 已采纳：本版仅允许撤销全局最近一条 applied 操作，再加逐图 current 身份校验。算法合同：

1. op.status=undone 则直接返回 unchanged；否则要求 latest_applied.op_id 等于请求。
2. 遍历 changes，验证新旧行存在且归属同图 rule/tags，new 当前仍为 1；previous 若存在必须为 0。
3. 如果后续手工或 WD/LLM 对这些图有变更，报 UndoConflict；用 annotation_id 与该操作新行 ID 比较作后续变更证据，不依赖时间戳。审图和 rating 变化不影响 rule 当前位回退。
4. 所有图验证成功后先把 new 置 0，再恢复 previous 为 1；previous=None 时恢复到“无 rule 层”。最后日志置 undone、写 undone_at。
5. 任一冲突整批回滚，不部分撤销；不删除新旧行。没有 redo API，重新应用规则创建新操作。

连续操作 A/B 的撤销：先撤 B，再撤 A。已撤销 B 的历史行不应被“后续变更”误拦；后续变更检查排除明确属于已 undone 操作的新 rule 行，仍检查其它层新增及无法归属日志的 rule 行。基础 current 身份检查始终保留。更早的不相交撤销需额外依赖图，本版不支持。

### 7.3 快照与缓存键

annotations.cache_key 为规范化快照相关参数、实际模型名、Skill 内容哈希、图片 sha256 的带版本 SHA-256；包含目标层/type、路由、关键词内容哈希，排除纯调度参数的规则版本化。params 同时保存 requested 与 actual，降档实际参数入键；覆盖检查枚举冻结快照允许的降档档位所得键，匹配其中一个才算覆盖，不以原档键冒充降档结果。Q10 已采纳。

冻结 job 不意味着保存端点地址/密钥。运行时按角色解析连接，仅允许匹配已冻结模型及协议，否则暂停。产品关键词表不嵌入项目；仅保存内容哈希，在运行内存保留本批输入，跨重启哈希已变则暂停，要求恢复对应版本或新建 job。缓存失效与 rating 缓存分开：新关键词版本使相关标注覆盖失效，已有可靠 rating 不自动清空，需用户重新分级。

### 7.4 全层合并、显式移除与时间边界（Q07 已采纳）

用户提出的移除集放在 annotations.params_json，operation_log 仍只引用新旧版本以便撤销。不增列/索引；现有 current 与 history 索引足够。相比只记操作时间，采用同一写连接生成的 annotation_id 作为全序边界，created_at 仅用于展示；这样同微秒两次操作也有确定先后。

caption_config_json 配置：`tag_layers` 默认五层全部；`layer_priority` 默认 manual/rule/llm/wd/original；`wd_min_confidence` 默认 0.0、允许 [0,1]；`tag_sort` 默认 layer_first，可选 prompt_pyramid；`include_nl` 默认 false，`nl_priority` 固定默认 manual/llm/original。不存在 export_schemes.params_json 字段。prompt_pyramid 是 F09 的纯排序适配器，不能增删 tag；不可用时明确报 UnsupportedSorter，不静默退化。

合并按以下顺序执行：

1. 取参与层各自 current tags，使用 `strip().lower().replace('_', ' ')` 的规范化键比较（转下划线后再 strip）；不改原始 prompt。空键拒绝；导出写规范化键，同层重复保留第一次位置。
2. wd 候选先按 conf≥wd_min_confidence 过滤；conf=NULL 不通过（可在方案明确配置 `wd_null_confidence=include`，默认 exclude）。规则/手工快照中复制自 wd 的候选保留原 conf 和原始层，照样受此阈值约束。
3. rule/manual 的 params_json 增加 `merge_meta`：`schema_version=1`、`tag_origins`、`removals`。tag_origins 按规范化键记录证据列表 `{layer, annotation_id, conf}`；它是复制前的原始证据，不能拿容器快照的新 ID 冒充新证据。
4. removals 为累计有效移除集：`{tag, cutoff_annotation_id, removed_at}`。本次真正显式移除的 tag 使用本次新 annotation_id 作 cutoff；携带过去仍有效的移除时保留原 cutoff，不能因再次保存快照而刷新旧边界。
5. 仅参与合并的 current rule/manual 行贡献 removals；每个 tag 取最大 cutoff。候选至少有一条通过阈值且 origin.annotation_id>cutoff 的证据才可保留；无移除记录则照常保留。禁用某操作层也禁用它的移除作用，符合可配置参与层集合。
6. 普通 original/wd/llm 的证据是自身 annotation_id。rule/manual 复制已有 tag 必须继承证据；用户明确新增/重新加入的 tag 则产生本次 annotation_id 的新证据。最终仍保留的 tag 不生成本次移除记录；显式重新加入只越过更旧的移除，不覆盖未来移除。
7. 候选过滤后按层优先级及层内第一次位置并集去重。快照中的复制 tag 仍按快照层的显示优先级排序，证据年龄仅决定能否保留。触发词先规范化去重再固定第一；其它内容不得再次重复触发词。
8. 开启自然语言时取 manual>llm>original 最高非空 current nl，一段输出，不拼接；为空则无第二段。可选 prompt_pyramid 仅重排触发词之后的 tags。

每次 rule/manual 新版本必须携带有效移除与来源信息；未改变 tag 文本但改变移除/来源也是真实语义变化，不能以 content 相同跳过。manual 全量编辑以用户读到的有效合并视图为基准，差集是显式移除；未触及 tag 保留原证据，不将所有复制 tag 刷新为本次新产生。服务校验 read_set 防止过期全量编辑误删新 tag。

规则执行输入使用同一套有效合并视图（无导出阈值筛选），条件仍只查 original/无 original 时 wd；结果是完整快照，不是动态规则。新 rule 快照承接前一 rule 的累计 removals 和 tag_origins，并记录本次实际删除；再次触发规则删除新 WD A 时，新的 cutoff 覆盖旧值。跨层当前移除仍分别保留，合并时取最大值。

示例：original#10 含 A；rule#20 把 A→B，存 A cutoff=20、B origin=20，导出为 B；wd#30 又产出 A，则 A 的新证据 30>20，导出为 B、A。再次触发 rule#40 删除 A，cutoff=40，导出只剩 B。若 rule#40 只改 C 而未实际删除 A，不刷新 A 的旧 cutoff=20，WD 的 A 仍保留。

撤销只回退 current rule 版本，移除集和来源随该版本一起恢复，不扫描日志临时推导。已撤销行的 removals 不再生效。默认最近操作与后续标注冲突检查不变。移除元数据中引用的 annotation_id 由 DAL 校验同图、存在、不指向未来；自引用仅用于本次新增/移除边界，并在分配 ID 后同事务写定。

## 8. 版本与迁移机制

### 8.1 版本映射与兼容

Q02 已采纳：初版 `project.json.format_version=1` 与 `PRAGMA user_version=1` 一一对应。前者代表整个项目格式，后者校验数据库对应 schema；即使只改文件格式也提升二者。当前仍未实现，93 列直接作为初版基线；若存在旧草案试验库则显式迁移，不冒认同版本兼容。初建内部 user_version=0 只在未发布临时目录存在，不把任意用户 DB 的 0 当可导入旧格式。

两者均等于支持版本才正常打开；任何一方高于程序支持，拒绝业务打开和修改，包括不执行恢复、不设置 journal_mode，不提供“试着降级”路径。未来可独立设计只读兼容查看器，本版不假定旧程序理解新 CHECK/JSON。旧版本沿已登记的连续迁移链升级；缺步骤拒绝。无受信迁移标记的版本不一致报 VersionMismatch，不自动选择较大/较小值。

### 8.2 脚本组织

设计目录为 `project_db/migrations/0001_initial.sql`、`0002_<主题>.py` 等，包内只读 registry 记录 from/to/name/checksum。不增加 migration_history 表。每步函数合同 `upgrade(db: WriteSession) -> None`，只能数据库变更；文件格式迁移由外层协调器描述 prepare/publish/recover 步骤。不得从项目目录加载可执行迁移脚本。

初建执行 §3、§4 DDL 并设置 user_version=1。新增枚举用新表/复制数据/校验/替换/重建索引迁移，不能仅更新 Python Literal；重建有外键的表必须在专用迁移连接、无业务访问时处理引用。若需要临时关闭 foreign_keys，必须事务开始前切换，事务结束后重新开启并全量 foreign_key_check，禁止在事务内假装 PRAGMA 已生效。迁移器逐条执行语句，避免 sqlite3.executescript 隐式提交破坏外层事务。

### 8.3 SQLite 与 project.json 的双文件提交

1. 取得项目独占锁，拒绝新作业；先以只读连接检查版本，再创建 sqlite3 backup 与原 project.json 副本到项目内 `.migration-backup/<from>-<id>/`。
2. 写并刷新 `.migration-state.json`：from/to、备份相对路径、migration checksum、原/目标 metadata 哈希、阶段。原子替换标记；不放 cache，未完成时禁止清理。
3. 在独立写事务执行完整升级链或单步，校验数据/外键/预期索引，最后同事务设 user_version；成功提交。任一步失败则回滚，不发布新 JSON。
4. 写 project.json.tmp、刷新后同卷 replace 为新版本；同步更新迁移标记阶段，再删除已完成标记。保留备份供用户处理，归项目目录所有，不产生外部残留。
5. 恢复：DB 仍旧版本且标记可信，重做未提交步骤；DB 已新版本而 JSON 仍旧，验证 schema/metadata 哈希后只补发 JSON；二者均新只清标记。不能依据一份可疑标记覆盖手工改过的 project.json。

断电时文件系统刷新仍有平台边界；保证能检测不一致、从完整备份恢复，不声称 JSON 与 DB 是同一原子提交。未知混合状态报 MigrationRecoveryRequired，保留所有证据，禁用业务写入。恢复旧备份必须同时恢复 DB 与 JSON；不能仅将 user_version 改回去。大型迁移预检磁盘空间（至少旧 DB 备份 + 重建空间），失败不删除原项目。

## 9. 最小测试与验收清单

选择 **unittest**：随 Python 自带，当前 F01 无需增加 pytest 依赖；使用 TemporaryDirectory、真实 sqlite3 文件、固定时钟、假 F05 probe 和可控故障点。WAL、断电窗口、外键不能只用 `:memory:` 测试。下面是实现阶段必须写的测试规格，本次只写文档，未执行这些测试。

| ID | 场景与最小断言 |
|---|---|
| D01 | 新建项目：恰好 8 业务表、93 列，列名/类型与 §2.3 相符，15 显式索引、2 UNIQUE 自动索引、user_version=1 |
| D02 | 每个 NOT NULL 列插 NULL 失败；每个枚举接受全部规定值、拒绝未知值；detector 可存但本版 command 不产生 |
| D03 | foreign_keys 开启；孤立 image/source、item/image、annotation/job 均失败；被引用父行删除 RESTRICT |
| D04 | 同源重复 seq 失败；不同源相同 seq 成功；同 image 被第二 item 引用失败；两个默认 import 失败 |
| D04a | import sha256 命中写 skipped/duplicate_sha256，image_id=NULL；非 import 或其它 error 被拒；源完成统计含 skipped |
| D05 | 同图同层同 type 两条 current 失败；不同层/type、任意历史行可并存；错误 content shape/JSON/conf 被拒 |
| D06 | sha256 相同可有两张图；filename 唯一；非法 sha256/尺寸/计数/路径拒绝；默认布尔及空链正确 |
| D07 | rating 四列必须一致；fallback verified=1 失败；manual/refusal verified=0 失败；清空为全空+0 |
| S01 | 遍历 sources/items/jobs/review 合法边表和所有其余边；其余边均 StateConflict；包含 done 源手动重试的原子重开与 skipped 不可迁出 |
| S02 | 源启动原子写齐 items 并锁定；中途 seed 冲突全回滚；锁定后四参数、type、item prompt/aspect 改动失败 |
| S03 | cost_policy=deny/ask/allow 分别暂停拒绝、暂停询问、直接提交；ask 同意仅当前 running 周期有效，修改策略下一张生效，allow 明确保留 Launcher 自身确认 |
| S03a | 首张非零也触发策略；ask 同意新周期内仅询问一次；手动暂停、停止、完成、退出均清 ack；普通继续重新问，过期 cycle_id 拒绝；deny 不能用同意按钮绕过 |
| S03b | 锁定源仍可改 cost_policy，下一张使用新值而不撤回已 sent；切回 ask 不复用旧 ack；重启无 runtime_json/确认残留 |
| S04 | sent 先提交再请求；超时不自动 pending；显式重试 attempts 只加一次，重复 retry CAS 不加两次 |
| S05 | 源含 failed 项仍可 done；job 按图计数，多层 tags/nl 不重复加；批次错误和逐图错误区分 |
| S06 | reject 不删文件；缺失图不能转 keep/unreviewed；外部丢失 keep 图可标 missing 而不篡改 review |
| I01 | 重放完整 seed/start 返回原 IDs；同自然键不同 prompt 报冲突，不覆盖 |
| I02 | complete_item 同证据重放只一 image/一套 original；不同 attempt/hash 拒绝冒认 |
| I03 | cache 命中不新增历史；A→B→A 合法追加；同 job 重交不重复；all 新 job 仍会重做相同键 |
| I04 | llm tags/nl 一层插入失败时，两层及 done_count 全部回滚；没有半成功整图 |
| I05 | progress_json：done/failed/skipped 每图事件重放均不重复计数，恢复范围不随新增图片/改 review 漂移；all 新 job 不被旧 job 结果短路 |
| I06 | 手工 rating 与后台分级竞态，后台 CAS 失败；重新分级仅清 keyword/model/fallback |
| U01 | 规则新增快照与 operation_log 中途任意失败，所有 current 位及日志均恢复原样 |
| U02 | 相同规则结果再次执行 affected=0、不新增行；新 WD 结果不自动更新 rule |
| U03 | 撤销恢复 previous；previous=NULL 恢复无 rule；第二次撤销 no-op；所有历史行保留 |
| U04 | 撤销非 latest/新 rule 已被替换/读集有后续变更，整批拒绝；A/B 先撤 B 后撤 A 成功 |
| U05 | 预览后 original/wd/manual 任一变化或原缺失层被新增，执行报 PreviewStale |
| U06 | original A→rule B 记录 removal cutoff；旧 A 被排除；规则后 wd A 的 annotation_id 更晚则 A 恢复；再次规则删除 A 后重新排除；撤销恢复前一版移除集 |
| U07 | 五层 tags 全部合并去重、规范化下划线/大小写、WD 置信度过滤；manual>llm>original 取一条自然语言；tag_sort/prompt_pyramid 配置生效 |
| U08 | 快照复制旧 tag 不刷新 origin；只改 C 不刷新 A cutoff；同一时间戳靠 ID 区分先后；manual 显式重加产生新证据；禁用 rule 层同时禁用其移除 |
| U09 | 复制 WD tag 仍按原 conf 过滤；NULL conf 默认排除；多证据任一有效即可保留；撤销恢复移除集，标签文本相同但 merge_meta 改变仍新增版本 |
| C01 | 两个 worker 结果同时到达写队列，仅一个线程访问写连接，无并发事务或错误 current 双行 |
| C02 | 长 reader 看到一致旧快照，writer 可提交；reader 关闭后 checkpoint 正常；第二进程锁拒绝 |
| C03 | begin busy 可限次重试；commit 未知不能重放 submit；磁盘满回滚不出现单独日志/半个结果 |
| P01 | keyset 多页无重复，upper_id 排除后插入；页大小和 SQL 变量上限受控；大规则提前拒绝不部分执行 |
| R01 | 重启把 running job/source 置 paused，二次恢复 no-op；paused 不自动开始外部请求 |
| R02 | sent 有可信完整文件补 done；明确无文件转 failed；unknown 保持 sent；probe 从不调用 submit |
| R03 | 复制未完、改名前、改名后提交前、提交后清收据前分别崩溃；恢复无重复图、无错误归属、不覆盖孤立文件 |
| R04 | 删除 reject 后写 missing 前崩溃，审计补 missing；历史/标注/撤销记录仍在 |
| M01 | 支持的旧格式完整迁移到新版本，记录和历史保留；校验 index/foreign_key_check/integrity_check |
| M02 | 迁移 SQL 中间失败回滚；DB 提交后 JSON 发布前失败，启动只补 JSON、不重复 SQL |
| M03 | 高版本两种不一致组合均拒绝，数据库与文件校验和不变；无标记混合版本不能自动修复 |
| M04 | 迁移恢复备份同时恢复 DB/JSON；checksum 不匹配、缺链、空间不足均安全失败 |
| E01 | 项目整体搬目录可打开；JSON/DB 不含产品 endpoint/token/绝对导入根路径；cache 删除不影响恢复证据 |
| E02 | 默认方案初始化与默认切换；导出读取前后 images/annotations 完全不变；非空 pipeline 不被静默执行 |

查询性能测试使用代表性多源数据，EXPLAIN 验证 §4 的核心前缀及部分索引可用；不硬编码 SQLite 每版完整计划字符串，也不把小数据全表扫描直接判失败。压力测试重点是队列延迟、规则事务时长、WAL 增长，参数由测量调整。

## 10. 建议变更（已采纳项并入基线）

以下决议已并入 DDL、状态机、接口和测试，并同步 docs/05。保留编号用于追踪，不再是待批准的提案。

| 编号 | 建议变更 | 原因与推荐方案 | 字段/结构影响 |
|---|---|---|---|
| C01「已采纳」 | 导入跳过终态 | skipped、image_id=NULL、error=duplicate_sha256；源完成含 skipped，导入后汇总 | 修改枚举，不增表 |
| C02「已采纳」 | 固定 job 范围并保存逐图 outcome | progress_json 保存冻结 image_ids 与逐图结果，和标注同事务提交，计数由 outcome 重算 | 新增 jobs.progress_json，仍 8 表 |
| C03「已采纳，按裁决修改」 | 费用运行策略 | cost_policy=deny/ask/allow，默认 ask，可改且下一张生效；确认仅本次 running 周期内存有效 | 新增 sources.cost_policy，不新增 runtime_json；pause_reason 无持久需求 |
| C04「已采纳」 | 落盘收据 | 采用 §5.5 item/attempt 收据及暂存目录；unknown 不重放 | 不改字段；save_path 已实测（§5.5） |
| C05「已采纳」 | attempts 与终态重试 | attempts 为尝试序号；用户重试 failed 时 done→paused 同事务发生 | 不增字段 |

## 11. 耦合点与待确认决策

### 11.1 裁决记录与剩余待确认

| 编号 | 问题与推荐答案 | 影响/对应提案 |
|---|---|---|
| Q01 已采纳 | 小写原扩展名、新时间导出目录、不存外部绝对路径 | §2.1 |
| Q02 已采纳 | format_version/user_version 对应，高版本拒绝 | §8 |
| Q03 已定 | 收据方案已采纳；save_path 已实测 | §5.5 |
| Q04 已采纳 | 重复 skipped 并汇总；清单存根内相对位置 | C01、§2.1 |
| Q05/Q06 已采纳 | 补充状态边、终态重试、attempts 尝试序号 | §5 |
| Q07 按裁决修改 | 全层并集+有版本边界的移除集；配置在 caption_config_json | §7.4 |
| Q08 已采纳 | 最近 applied 撤销与依赖检查，规则预览必需 | §7.2 |
| Q09 已采纳 | reject 默认跳过可明确选入；missing 一律跳过且禁止恢复审图状态 | §5.4 |
| Q10 已采纳 | 关键词哈希、实际降档键，rating 不自动清空 | §7.3 |
| Q11 已采纳 | progress_json 精确记账，外部调用未提交窗口不保证精确一次 | C02 |
| Q12 按裁决修改 | deny/ask/allow；ask 确认仅一个 running 工作周期 | §5.2 |
| Q13 已采纳 | 一次开一个项目，独占项目锁，切换先关闭 | §7.1 |

### 11.2 docs/05 第 13 章逐项对照与 F01 推荐答案

| 原编号（保留历史编号） | F01 对照；本节未被裁决覆盖的推荐仍须所属模块确认 |
|---|---|
| #1 路径 | 采用 Q01；产品配置具体目录交产品配置模块，F01 不存它。项目占用统计应包含 WAL、迁移备份及暂存目录，另行展示为数据库/临时占用 |
| #2 迁移 | 采用 Q02 与 §8；不支持降级写入 |
| #3 崩溃恢复 | running job→paused 已由本任务明确；sent 核查采用 Q03，核查不明不重放 |
| #4 导入去重 | 推荐默认跳过并给出命中汇总，不逐张弹窗；采用 C01 表达跳过，原图归属不改 |
| #5 标注层 | 手改只写 manual，与 §8.2 一致；original 入库后不可改内容，保留溯源 |
| #6 批量规则 | 条件全量=全部存在、任意=至少一个存在；无 original 则 WD、仍无则不满足；空条件组视为无条件，空列表具体模式由规则模块确认；必须预览，撤销采用 Q08 |
| #7 导出 | 采用 Q07；默认稳定保留层内 tag 顺序，语义分类排序留给 F09 配置；新时间目录；统计报告可做 exports 文件，不增业务表 |
| #8 视觉请求 | F15 推荐默认 2MP/JPEG 92，1.6MP/88、1MP/84；实际值进 params，键策略采用 Q10；本模块不强制图像质量常量 |
| #9 执行器 | 采用 Q09，file_missing 跳过有逐图原因与 skipped_count，依赖 C02 |
| #10 WD | 显存未知先禁自动 GPU 调度，允许用户选择受支持的 CPU/设备；不可用写批次级错误，不造成功 annotation；设备策略归 F12 |
| #11 NSFW | 采用 Q10；本地 detector 不实现。未来仅二值 nsfw 推荐映射 questionable 并记录实际模型/阈值，需启用时再确认，不影响现有 detector 枚举 |
| #12 存储 | 暂沿用 20GB/100GB 可配置提示，阈值在产品层；missing 状态规则见 Q09；清理需二次确认由上层保证 |
| #13 提示词生产 | 过滤/词典其余字段不进 project.db；词条 rating 先用于产品层筛选，不能冒充图像已验证分级；source_items 不增加生产溯源字段 |
| #14 费用策略 | 已定：每张非零估算按 deny/ask/allow，含首张非零；ask 确认仅工作周期有效，独立 cost_policy 可改 |
| #15 尺寸预设 | 采用 512×768；内置不可覆写/删除；F01 仅校验启动快照三组 64 倍数与默认 aspect，已启动配置不随产品变化 |
| #16 标签库 | 合并比较已定小写、下划线转空格、去首尾空白；词库选型及原始权重语法仍由解析模块确认，F01 不改原始 prompt |
| #17 导入方案 | 默认严格 UTF-8（接受 BOM），不自动猜编码；失败允许用户选择明确编码后新建/重试未锁源；锁定源不能修改 reader 参数 |
| #18 前端 | 采用 Q13；token 传递属于启动/API，不进入 project.json 或任何表 |
| #19 预留 | 本版不增加 parent_id、资产库或派生类型；未来迁移明确扩展 |
| #20 docs/04 | 重编由架构文档维护任务处理；本文件仅保留 §1 的 F 编号接口映射，不继承旧模型 |

### 11.3 实现验收阻塞与明确限制

全部裁决已并入基线，可按本文实施；Launcher save_path 落盘行为已于 2026-10-12 实测（见 §5.5），F01 无剩余外部验证项。未知证据保持 sent/paused，不阻塞基础 DAL 实现，也不提前宣称生成恢复适配器已验证。其它产品模块原有未决事项保留在 docs/05 第 13 章。

SQL CHECK 不阻止绕过 DAL 直接手工 UPDATE 合法枚举之间的非法迁移；本项目所有业务写入必须经过受控 commands。若未来允许第三方直接改 DB，再评估触发器或独立写协议，不在本版引入第二套状态机实现。

本设计不承诺 SQLite 事务覆盖网络付费请求或文件系统；承诺的是数据库原子性、可检测的跨文件中间态、按证据恢复以及未知生成操作不自动重放。设计文档的 DDL 尚未在真实 sqlite3 执行，落地时以 §9 的约束、恢复与迁移测试作为验收依据。
