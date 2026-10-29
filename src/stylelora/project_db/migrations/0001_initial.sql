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
