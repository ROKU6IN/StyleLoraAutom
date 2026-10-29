import type { components } from './schema';
export interface BackendDiagnostic {
  backend_id: string;
  name: string;
  available: boolean;
  reserved: boolean;
  reachable: boolean;
  transport_verified: boolean;
  status: 'not_checked' | 'unavailable' | 'incompatible' | 'verified';
  reason: string;
  checked_at: string | null;
  app_version: string | null;
  server_version: string | null;
  protocol_version: string | null;
  schema_hash: string | null;
  full_schema_hash: string | null;
  tool_count: number;
  core_tools: string[];
  missing_core_tools: string[];
  features: { generation: boolean; tag_search: boolean; interrogate: boolean };
  warnings: string[];
}
export interface BackendRegistry {
  items: BackendDiagnostic[];
  probe_operation_id: string | null;
  inspection?: LauncherInspectionResult | null;
  inspection_operation_id?: string | null;
}
export interface LauncherInspectionResult {
  status: 'ready' | 'unavailable';
  reason: string | null;
  generation_enabled: false;
  checked_at: string;
  warnings: string[];
  version?: string;
  settings?: {
    model: string;
    effort: string | null;
    model_mode: string;
    sampler: string;
    noise_schedule: string;
    steps: number;
    width: number;
    height: number;
    scale: number;
    cfg_rescale: number;
    uc_preset: number;
    quality_toggle: boolean;
    variety_plus: boolean;
    decrisp: boolean;
    smea: boolean;
    smea_dyn: boolean;
    locked_by_effort: string[];
    available_models: { id: string; efforts: string[] }[];
  };
  activity?: {
    generation: { status: string; progress: number };
    queue: {
      pending: number;
      completed: number;
      failed: number;
      execution: string;
      session_progress: number;
    };
  };
}
export type Review = 'unreviewed' | 'keep' | 'reject';
export type Layer = 'original' | 'wd' | 'llm' | 'rule' | 'manual';
export interface Session {
  instance_id: string;
  epoch: string;
  project_id: string | null;
  draining: boolean;
  version: string;
  capabilities: Record<string, boolean | string>;
}
export interface Project {
  project_id: string;
  known_id?: string | null;
  recent_warning?: string | null;
  version: string;
  metadata: { name: string; trigger_word: string };
  recovery: Record<string, unknown>;
}
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
  limit: number;
  upper_id: number | null;
}
export interface ImageItem {
  image_id: number;
  source_id: number;
  filename: string;
  width: number;
  height: number;
  file_size: number;
  review_status: Review;
  rating: string | null;
  file_missing: boolean;
  has_alpha: boolean | null;
  original_url: string;
  thumbnail_url: string;
  version: string;
  annotation_coverage: Record<Layer, boolean>;
}
export interface ImagePage extends Page<ImageItem> {
  total: number;
  start_index: number;
  anchor_index: number | null;
}
export interface ImageQuery {
  source_ids?: number[];
  review_statuses?: Review[];
  rating?: string[];
  file_missing?: boolean | null;
  q?: string | null;
}
export interface ImageStats {
  total: number;
  groups: { by: string; buckets: { key: string | number | null; count: number }[] }[];
}
export interface Tag {
  tag: string;
  conf: number | null;
}
export interface Ref {
  image_id: number;
  source: Layer;
  type: 'tags' | 'nl';
  annotation_id: number | null;
}
export interface AnnotationView {
  image_id: number;
  filename: string;
  tags: Tag[];
  natural_language: string | null;
  version: string;
  refs: Ref[];
  missing_original: boolean;
  wd_overlap: Record<string, boolean>;
  layers: Record<string, { content: Tag[] | string; annotation_id: number } | null>;
}
export type ManualInput = components['schemas']['ManualEdit'];
export interface ReviewResult {
  items: { image: ImageItem; old_review_status: Review; old_version?: string }[];
}
export interface Accepted {
  status_url: string;
  task: { kind: string; id: string; project_id: string | null };
}
export interface ApiErrorBody {
  code: string;
  retry_action: string;
  outcome: string;
  details: Record<string, unknown>;
  issues: { field: string; code: string }[];
}
export interface Operation<T = unknown> {
  operation_id: string;
  project_id: string | null;
  kind: string;
  status: 'pending' | 'running' | 'done' | 'failed' | 'canceled' | 'interrupted';
  completed: number;
  total: number | null;
  result: T;
  error: ApiErrorBody | null;
  version: string;
}
export interface ModelEntry {
  provenance?: {
    reviewed_at: string;
    license: string;
    model_sha256_source: string;
    csv_sha256_source: string;
    csv_git_blob: string;
  } | null;
  manifest_id: string;
  model_id: string;
  repo_id: string;
  revision: string;
  total_bytes: number;
  files: { name: string; size: number; sha256: string }[];
  state: 'missing' | 'unverified' | 'ready' | 'invalid' | 'installing';
  reason: string | null;
  installation_id: string | null;
  preprocess_version: string;
  verified_at: string | null;
}
export interface Installation extends Operation {
  installation_id: string;
  manifest_id: string;
  phase: string;
}
export interface TaggerCapabilities {
  available: boolean;
  reason: string | null;
  trusted_manifest_count: number;
  max_batch_size?: number;
  cuda_profiles?: { manifest_id: string; max_batch_size: number; profile_id: string }[];
  runtime: { available: boolean; reason: string | null; version: string | null };
  device: {
    provider: 'cpu' | 'cuda';
    reason: string;
    total_bytes: number | null;
    free_bytes: number | null;
    cuda_available?: boolean;
  };
}
export interface Job {
  job_id: number;
  executor: string;
  batch_id: string;
  mode: string;
  status: 'pending' | 'running' | 'paused' | 'done' | 'failed' | 'canceled';
  total: number;
  done_count: number;
  failed_count: number;
  skipped_count: number;
  version: string;
  error: string | null;
  runtime_error: string | null;
  status_url: string;
  config_snapshot_json: {
    model?: string;
    params?: {
      config?: {
        general_threshold: number;
        character_threshold: number;
        provider: string;
        batch_size?: number;
      };
    };
  };
}

export interface JobBatchView {
  batch_id: string;
  version: string;
  status: 'pending' | 'running' | 'paused' | 'done' | 'failed' | 'canceled' | 'blocked';
  reason: string | null;
  image_count: number;
  total: number;
  done_count: number;
  failed_count: number;
  skipped_count: number;
  items: Job[];
}
export interface JobPreviewView {
  scope_digest: string;
  image_count: number;
  items: {
    executor: string;
    total: number;
    cache_hit: number;
    needs_execution: number;
    file_missing: number;
    waiting_for_wd: number;
    role_empty: number;
    rating_unresolved: number;
  }[];
  overlapping_jobs: { job_id: number; batch_id: string; executor: string; status: string }[];
}
export interface JobOutcome {
  image_id: number;
  kind: 'done' | 'failed' | 'skipped';
  reason: string | null;
  annotation_ids: number[];
  cache_keys: string[];
}
export interface Grant {
  grant_id: string;
  purpose: string;
  display_name: string;
  project_id: string | null;
  version: string;
  expires_at: string;
}
export interface Preview {
  preview_id: string;
  digest: string;
  expires_at: string;
  version: string;
  issues: { code: string; severity: string }[];
}
export interface ReviewPreview extends Preview {
  total: number;
  query: ImageQuery;
}
export interface ImportPreview extends Preview {
  can_import: boolean;
  blocking_errors: number;
  encoding_fallback_count: number;
  catalog_revision: string | null;
  diagnostics: {
    path: string;
    error: string | null;
    warnings?: string[];
    encoding?: string | null;
    encoding_fallback?: boolean;
    segments?: { raw: string; lookup: string; reason: string | null }[];
    originals?: { type: string; content: Tag[] | string }[];
  }[];
  selected: number;
  manifest: {
    entries: { path: string; preflight_error: string | null; warnings: string[] }[];
    warnings: string[];
  };
}
export interface ImportReport {
  source_id: number;
  imported: number;
  skipped: number;
  failed: number;
  pending: number;
  status: string;
  items: {
    item_id: number;
    path: string;
    warnings: string[];
    encoding: string | null;
    encoding_fallback: boolean;
  }[];
}
export interface Source {
  source_id: number;
  name: string;
  type: string;
  status: string;
  version: string;
  counts: Record<'pending' | 'sent' | 'done' | 'failed' | 'skipped', number>;
}
export interface SourceItem {
  item_id: number;
  seq: number;
  status: string;
  image_id: number | null;
  error: string | null;
  attempts: number;
}
export interface ExportPreview extends Preview {
  sorter?: { name: string; version: string } | null;
  output_config: { image_mode: 'copy' | 'hardlink' };
  hardlink_ack_digest: string | null;
  trigger_validation: TriggerValidation;
  scheme: { scheme_id: number };
  trigger_word: string;
  trigger_check: string;
  warnings: string[];
  images: {
    image_id: number;
    filename: string;
    status: string;
    reason: string | null;
    tags: string[];
    natural_language: string | null;
    removed_tags: string[];
    output_path: string | null;
    sorting_counts?: Record<string, number> | null;
  }[];
}
export interface TriggerValidation {
  status: 'valid' | 'existing_tag' | 'unverifiable' | 'invalid';
  issues: { code: string; severity: string; canonical?: string }[];
  catalog_revision: string | null;
  override_digest: string | null;
  can_override: boolean;
}
export type ImportConfiguration = Omit<Required<components['schemas']['ImportScheme']>, 'params_json'> & {
  params_json: Required<components['schemas']['ImportParams']>;
};
export type ExportConfiguration = Omit<
  Required<components['schemas']['ExportScheme']>,
  'filter_json' | 'caption_config_json' | 'output_config_json'
> & {
  filter_json: Required<components['schemas']['ExportFilter']>;
  caption_config_json: Required<components['schemas']['CaptionConfig']>;
  output_config_json: Required<components['schemas']['OutputConfig']>;
};
export interface Scheme<T = ImportConfiguration | ExportConfiguration> {
  scheme_id: number;
  name: string;
  version: string;
  is_default?: boolean;
  configuration: T;
  executable: boolean;
  unavailable_reasons: string[];
}
export interface ExportView {
  image_mode?: 'copy' | 'hardlink';
  export_id: string;
  status: string;
  selected: number;
  exported: number;
  skipped: number;
  failed: number;
  report_url: string | null;
  relative_dir: string | null;
  version: string;
  status_url?: string;
  operation_id: string | null;
}
export interface ExportReport {
  sorter?: { name: string; version: string } | null;
  sort_summary?: { images_with_unclassified: number; unclassified_tag_occurrences: number } | null;
  output_config?: { image_mode: 'copy' | 'hardlink' };
  hardlink_ack_digest?: string | null;
  counts: { selected: number; exported: number; skipped: number; failed: number };
  directory: string;
  trigger_word: string;
  reason_counts: Record<string, number>;
}
