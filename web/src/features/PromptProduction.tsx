import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router';
import { allPages, request } from '../api/client';
import { errorText } from '../api/errors';
import { useDraftGuard } from '../hooks/drafts';
import { canWrite, useSession } from '../state/session';
import type { Project } from '../api/types';
import { queryClient } from '../state/queryClient';
import { Button, EmptyState, ErrorBox, Field, ui } from '../ui';
import PromptTaskManagement, { TaskResultsExport } from './PromptTaskManagement';
import { CoveragePlanPicker } from './CoveragePlans';
import { defaultCoverage } from './coveragePlanData';
import DictionaryEditor from './DictionaryEditor';
import { DictionaryDownload, ImportRows, type ImportPreview } from './DictionaryExchange';
import { DeleteLibrary, PendingLibraryDeletions } from './LibraryDeletion';
import { dictionaryDraftIssues } from './dictionaryEditorData';
import CoverageMatrix, { type Matrix, type MatrixSource, type CoverageViewRef } from './CoverageMatrix';
export { defaultCoverage } from './coveragePlanData';

const P = '/api/prompt-production';
type Dictionary = {
  dictionary_id: string;
  name: string;
  kind: 'features' | 'prompts';
  revision: number;
  version: string;
  dimensions?: Record<string, { tag: string; rating: string | null }[]>;
  entries?: {
    tags: string[];
    natural_language: string | null;
    aspect: string | null;
    rating: string | null;
  }[];
};
type Report = {
  matrix?: Matrix;
  status: string;
  ready: boolean;
  requested: number;
  available: number;
  count: number;
  cell_targets: Record<string, number>;
  cell_counts: Record<string, number>;
  constraint_counts: Record<string, number>;
  constraint_bounds: Record<string, number[]>;
  diagnostics: Record<string, number>;
  warnings: Record<string, number>;
  source_counts: Record<string, number>;
  complete_pool: boolean;
  search_nodes: number;
};
type Outcome = {
  index: number;
  entry: { tags: string[]; natural_language: string | null; aspect: string | null };
  verified: boolean;
  rating: string | null;
  status?: string;
  reason?: string | null;
  input?: Outcome;
  result?: Outcome | null;
  review_candidate?: Outcome;
  review?: { reviewed_at: string };
};
type Preview = Report & {
  coverage_view?: CoverageViewRef | null;
  coverage_view_reason?: string | null;
  request: unknown;
  preview_digest: string;
  examples: Outcome[];
  examples_truncated: boolean;
  refinement?: {
    roles: Record<string, { model: string; protocol: string } | null>;
    max_candidates: number;
    max_semantic_calls_per_candidate: number;
  };
};
type Task = {
  task_id: string;
  revision: number;
  status: string;
  ready: boolean;
  reason: string | null;
  requested: number;
  random_seed: number;
  coverage: Report | null;
  progress?: { round: number; counts: Record<string, number>; deficits_reason: string | null } | null;
  appends?: { target_id: string; result: { inserted: number; duplicates: number } }[];
  append_error?: string | null;
  can_export_results?: boolean;
};
type Page<T> = { items: T[]; total: number; next_cursor: string | null };
type DictionarySave = { revision: number; duplicates: number; issues: { code: string; field?: string }[] };

export const exampleFeatures = {
  scene: ['forest', 'beach', 'city street'],
  lighting: ['sunlight', 'night'],
  count: ['no humans', '1girl', '1boy', '2girls'],
  view: ['wide shot', 'full body', 'upper body'],
  angle: ['from side', 'from above'],
  season: ['spring', 'summer', 'autumn', 'winter'],
};
const featureText = JSON.stringify(
  Object.fromEntries(
    Object.entries(exampleFeatures).map(([key, values]) => [
      key,
      values.map((tag) => ({ tag, rating: 'general' })),
    ]),
  ),
  null,
  2,
);
const promptText = JSON.stringify(
  [
    {
      tags: ['forest', 'sunlight', 'no humans', 'wide shot'],
      natural_language: null,
      aspect: 'landscape',
      rating: 'general',
    },
  ],
  null,
  2,
);
const initialCoverage = JSON.stringify(defaultCoverage, null, 2);
const statusText: Record<string, string> = {
  pending: '等待中',
  running: '正在处理',
  paused: '已暂停',
  done: '已完成',
  failed: '失败',
  canceled: '已取消',
  satisfied: '覆盖满足',
  unsatisfiable: '约束无解',
  search_exhausted: '搜索预算耗尽',
  ok: '通过',
  skipped: '保留原值',
  fenced: '围栏排除',
  review_required: '待检查',
};
const reasonText: Record<string, string> = {
  canceled: '任务已取消，未采用迟到结果',
  fenced: '围栏排除',
  forbidden_content: '禁用内容',
  review_required: '需人工检查内容',
  duplicate: '标签集合重复',
  excluded: '规则排除',
  excluded_cell: '已排除格子',
  unclassified: '缺少主维标签',
  ambiguous: '主维标签歧义',
  candidate_limit: '候选预算截断',
  rating_unverified: '评级未验证',
  unknown_tag_may_parse_as_nl: '未知标签可能被解析为描述',
  nl_fragment_may_parse_as_tag: '描述片段可能被解析为标签',
  disabled: '未启用精修',
  role_empty: '精修角色为空',
  nsfw_contract_missing: 'NSFW 精修模板尚未提供',
  upstream_refused: '模型拒绝处理，已停止路由切换',
  coverage_drift: '精修改变覆盖格子',
  invalid_input: '模型判定输入不可用',
  invalid_contract: '模型输出不符合约定',
  llm_outcome_unknown: '请求结果未知；不会自动重发',
  refill_exhausted: '有限补位结束，仍有缺口',
  manual_review: '已人工修改并通过复验',
  manual_review_deficits: '人工复验后仍有数量或覆盖缺口',
};
function download(name: string, value: unknown) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2) + '\n'], { type: 'application/json' }),
  );
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function message(error: unknown) {
  return error instanceof Error && !('code' in error) ? error.message : errorText(error);
}
export function dictionaryPayload(kind: 'features' | 'prompts', name: string, content: string) {
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    throw new Error('词典内容不是有效的 JSON，请检查逗号、引号和括号。');
  }
  if (
    kind === 'features'
      ? !parsed || typeof parsed !== 'object' || Array.isArray(parsed)
      : !Array.isArray(parsed)
  )
    throw new Error(kind === 'features' ? '特征词典需填写维度对象。' : '提示词词典需填写条目数组。');
  return { schema_version: 1, kind, name, [kind === 'features' ? 'dimensions' : 'entries']: parsed };
}
export function samplingPayload(
  ids: string[],
  dictionaries: Dictionary[],
  count: string,
  seed: string,
  enabled: boolean,
  coverage: string,
) {
  const total = Number(count),
    random = seed.trim() ? Number(seed) : null;
  if (
    !Number.isSafeInteger(total) ||
    total < 1 ||
    total > 10000 ||
    (random !== null && (!Number.isSafeInteger(random) || random < 0))
  )
    throw new Error('数量需为 1–10000 的整数；随机种子需为空或 0–9007199254740991 的整数。');
  let rules: unknown;
  try {
    rules = JSON.parse(coverage);
  } catch {
    throw new Error('覆盖规则不是有效的 JSON。');
  }
  if (!rules || typeof rules !== 'object' || Array.isArray(rules)) throw new Error('覆盖规则需填写对象。');
  return {
    prompt_dictionary_ids: dictionaries
      .filter((d) => ids.includes(d.dictionary_id) && d.kind === 'prompts')
      .map((d) => d.dictionary_id)
      .sort(),
    feature_dictionary_ids: dictionaries
      .filter((d) => ids.includes(d.dictionary_id) && d.kind === 'features')
      .map((d) => d.dictionary_id)
      .sort(),
    coverage_enabled: enabled,
    plan: { ...rules, target_count: total, random_seed: random },
    optimize: false,
  };
}

export function CoverageReport({ value, source }: { value: Report; source?: MatrixSource }) {
  return (
    <div aria-label="覆盖报告">
      <p role="status">
        <strong>{statusText[value.status] ?? value.status}</strong> · 已选 {value.count} / {value.requested}{' '}
        条 · 合规候选 {value.available} 条
      </p>
      <p className={ui.muted}>
        搜索节点 {value.search_nodes}；
        {value.complete_pool ? '候选池已完整枚举' : '候选池未完整枚举，缺口不代表数学无解'}。
      </p>
      {value.matrix && (
        <CoverageMatrix
          key={JSON.stringify([value.matrix, value.cell_targets, value.cell_counts, source])}
          matrix={value.matrix}
          counts={value}
          source={source}
        />
      )}
      {!value.matrix && (
        <div className={ui.tableWrap}>
          <table className={ui.table}>
            <caption>格子配额与实际数量</caption>
            <thead>
              <tr>
                <th>格子</th>
                <th>目标</th>
                <th>实际</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(value.cell_targets).map(([key, count]) => (
                <tr key={key}>
                  <td>
                    {key === 'random'
                      ? '随机池'
                      : (JSON.parse(key) as string[][]).map(([name, tag]) => `${name}: ${tag}`).join(' · ')}
                  </td>
                  <td>{count}</td>
                  <td>{value.cell_counts[key] ?? 0}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {Object.keys(value.constraint_counts).length > 0 && (
        <p>
          整体比例约束：
          {Object.entries(value.constraint_counts)
            .map(([tag, count]) => `${tag} ${count} 条（要求 ${value.constraint_bounds[tag]?.join('–')}）`)
            .join('；')}
        </p>
      )}
      {[...Object.entries(value.diagnostics), ...Object.entries(value.warnings)]
        .filter(([, n]) => n > 0)
        .map(([key, n]) => (
          <p className={ui.warning} key={key}>
            {reasonText[key] ?? key}：{n} 条
          </p>
        ))}
    </div>
  );
}

export default function PromptProduction() {
  const [params, setParams] = useSearchParams();
  const { register, run } = useDraftGuard();
  useSession((s) => s.connection);
  const dictionaries = useQuery({
    queryKey: ['prompt', 'dictionaries'],
    queryFn: ({ signal }) => allPages<Dictionary>(P + '/dictionaries', signal),
  });
  const tasks = useQuery({
    queryKey: ['prompt', 'tasks'],
    queryFn: ({ signal }) => allPages<Task>(P + '/tasks', signal),
    refetchInterval: 2000,
  });
  const [base, setBase] = useState<Dictionary | null>(null),
    [newId, setNewId] = useState(() => crypto.randomUUID());
  const [kind, setKind] = useState<'features' | 'prompts'>('features'),
    [name, setName] = useState(''),
    [content, setContent] = useState(featureText);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const [saveReport, setSaveReport] = useState<DictionarySave | null>(null);
  const [format, setFormat] = useState('json'),
    [dimension, setDimension] = useState('scene'),
    [file, setFile] = useState<File | null>(null),
    [imported, setImported] = useState<ImportPreview | null>(null);
  const [ids, setIds] = useState<string[]>([]),
    [count, setCount] = useState('100'),
    [seed, setSeed] = useState('42'),
    [enabled, setEnabled] = useState(true),
    [coverage, setCoverage] = useState(initialCoverage);
  const [preview, setPreview] = useState<{ for: string; value: Preview } | null>(null);
  const [optimize, setOptimize] = useState(false),
    [ratingModel, setRatingModel] = useState(false),
    [autoTarget, setAutoTarget] = useState(''),
    [confirmed, setConfirmed] = useState(false),
    [options, setOptions] = useState(
      JSON.stringify(
        { nl_enabled: true, composition_mode: 'normal', tag_range: [15, 40], aspect_hint: null },
        null,
        2,
      ),
    );
  const saveIdentity = useRef({ signature: '', key: '' }),
    createIdentity = useRef({ digest: '', task_id: '', key: '' }),
    importIdentity = useRef({ digest: '', key: '' });
  const baseContent = base
    ? JSON.stringify(base.kind === 'features' ? base.dimensions : base.entries, null, 2)
    : kind === 'features'
      ? featureText
      : promptText;
  const dirty = name !== (base?.name ?? '') || content !== baseContent;
  const reset = useCallback(() => {
    setName(base?.name ?? '');
    setContent(baseContent);
  }, [base, baseContent]);
  const save = useCallback(async () => {
    if (busy || !canWrite()) return false;
    setBusy(true);
    setError(null);
    try {
      const issues = dictionaryDraftIssues(kind, content);
      if (issues.length) throw new Error(issues.join('\n'));
      const body = dictionaryPayload(kind, name, content),
        identifier = base?.dictionary_id ?? newId;
      const signature = JSON.stringify([identifier, base?.version, body]);
      if (saveIdentity.current.signature !== signature)
        saveIdentity.current = { signature, key: crypto.randomUUID() };
      const result = await request<DictionarySave>(
        base ? 'PUT' : 'POST',
        P + '/dictionaries' + (base ? '/' + identifier : ''),
        base ? body : { ...body, dictionary_id: identifier },
        { version: base?.version, key: saveIdentity.current.key },
      );
      const next = await request<Dictionary>('GET', P + '/dictionaries/' + identifier);
      setBase(next);
      setName(next.name);
      setContent(JSON.stringify(next.kind === 'features' ? next.dimensions : next.entries, null, 2));
      setSaveReport(result);
      void queryClient.invalidateQueries({ queryKey: ['prompt', 'dictionaries'] });
      useSession.getState().notify('词典已保存在本地');
      return true;
    } catch (failure) {
      setError(failure);
      return false;
    } finally {
      setBusy(false);
    }
  }, [base, busy, content, kind, name, newId]);
  useEffect(() => {
    register({ dirty, label: '提示词词典', save, discard: reset });
    return () => register(null);
  }, [dirty, register, reset, save]);
  const action = async (fn: () => Promise<void>) => {
    if (busy || !canWrite()) return;
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  const open = (identifier: string) =>
    run(
      () =>
        void action(async () => {
          const next = await request<Dictionary>('GET', P + '/dictionaries/' + identifier);
          setBase(next);
          setKind(next.kind);
          setName(next.name);
          setContent(JSON.stringify(next.kind === 'features' ? next.dimensions : next.entries, null, 2));
          setImported(null);
          setSaveReport(null);
        }),
    );
  const fresh = (next: 'features' | 'prompts') =>
    run(() => {
      setBase(null);
      setNewId(crypto.randomUUID());
      setKind(next);
      setName('');
      setContent(next === 'features' ? featureText : promptText);
      setImported(null);
      setSaveReport(null);
    });
  const signature = JSON.stringify([
    ids.slice().sort(),
    count,
    seed,
    enabled,
    coverage,
    optimize,
    ratingModel,
    autoTarget,
    options,
  ]);
  const validPreview = preview?.for === signature ? preview.value : null;
  const deletedDictionary = (identifier: string) => {
    setIds((previous) => previous.filter((id) => id !== identifier));
    setAutoTarget((previous) => (previous === identifier ? '' : previous));
    setPreview(null);
    setConfirmed(false);
    if (base?.dictionary_id === identifier) {
      setBase(null);
      setNewId(crypto.randomUUID());
      setName('');
      setContent(kind === 'features' ? featureText : promptText);
      setImported(null);
      setSaveReport(null);
      setError(null);
    }
  };
  const taskId = params.get('task');
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>PROMPT / LOCAL PRODUCTION</div>
          <h1>提示词生产</h1>
          <p>维护内容词典，按覆盖条件采样并导出提示词。词典和任务跨项目使用。</p>
        </div>
      </div>
      <ErrorBox
        message={
          error
            ? message(error)
            : dictionaries.error
              ? errorText(dictionaries.error)
              : tasks.error
                ? errorText(tasks.error)
                : undefined
        }
      />
      <section className={ui.section}>
        <h2>1. 本地词典</h2>
        <PendingLibraryDeletions
          kind="dictionary"
          disabled={busy || dirty}
          onDeleted={deletedDictionary}
          onBusy={setBusy}
        />
        <div className={ui.actions}>
          <Button disabled={busy} onClick={() => fresh('features')}>
            新建特征词典
          </Button>
          <Button disabled={busy} onClick={() => fresh('prompts')}>
            新建提示词词典
          </Button>
        </div>
        <Field id="prompt-dictionary" label="打开已有词典">
          <select
            id="prompt-dictionary"
            value={base?.dictionary_id ?? ''}
            disabled={busy}
            onChange={(e) => {
              if (e.target.value) open(e.target.value);
            }}
          >
            <option value="">新词典</option>
            {dictionaries.data?.map((d) => (
              <option key={d.dictionary_id} value={d.dictionary_id}>
                {d.name} · {d.kind === 'features' ? '特征' : '提示词'}
              </option>
            ))}
          </select>
        </Field>
        <Field id="prompt-name" label="词典名称">
          <input id="prompt-name" value={name} disabled={busy} onChange={(e) => setName(e.target.value)} />
        </Field>
        <DictionaryEditor
          key={(base?.dictionary_id ?? newId) + ':' + (base?.version ?? '') + ':' + kind}
          kind={kind}
          content={content}
          disabled={busy || !canWrite()}
          onChange={setContent}
        />
        <p className={ui.muted}>
          只填写内容标签与描述。画风、作者、触发词和权重不属于内容段；明确禁用词会在采样时拦截。
        </p>
        <div className={ui.actions}>
          <Button
            primary
            disabled={busy || !canWrite() || !name.trim() || !dirty}
            onClick={() => void save()}
          >
            保存词典
          </Button>
          {base && (
            <Button disabled={busy} onClick={() => open(base.dictionary_id)}>
              重新载入已保存词典
            </Button>
          )}
          {base && (
            <DeleteLibrary
              resource={{
                kind: 'dictionary',
                resource_id: base.dictionary_id,
                name: base.name,
                revision: base.revision,
                version: base.version,
              }}
              disabled={busy || dirty}
              onDeleted={deletedDictionary}
              onBusy={setBusy}
            />
          )}
          {base && (
            <DictionaryDownload
              key={base.dictionary_id + ':' + base.version}
              base={base}
              disabled={busy || dirty}
              busy={busy}
              action={action}
            />
          )}
        </div>
        {base && dirty && (
          <p className={ui.caption}>删除词典前，请先保存草稿，或重新载入已保存词典并明确放弃草稿。</p>
        )}
        {saveReport && !dirty && (
          <div role="status" className={ui.notice}>
            <p>
              已保存版本 {saveReport.revision}；合并重复条目 {saveReport.duplicates} 条。
            </p>
            {saveReport.issues?.map((issue, index) => (
              <p key={index}>
                {issue.code === 'dictionary:rating_merged'
                  ? `维度「${issue.field}」的重复标签存在评级差异，已按规则取较高评级。`
                  : issue.code}
              </p>
            ))}
            <p className={ui.caption}>当前表单显示本次保存后读取的内容；已有生产任务仍使用原先冻结的词典。</p>
          </div>
        )}
        {base && (
          <details>
            <summary>从文件追加内容</summary>
            <Field id="prompt-import-format" label="导入格式">
              <select
                id="prompt-import-format"
                value={format}
                disabled={busy}
                onChange={(e) => {
                  setFormat(e.target.value);
                  setImported(null);
                }}
              >
                <option value="json">JSON 完整词典</option>
                <option value="csv">CSV 表格</option>
                <option value="txt">TXT 文本</option>
              </select>
            </Field>
            {kind === 'features' && format === 'txt' && (
              <Field id="prompt-import-dimension" label="TXT 对应维度">
                <input
                  id="prompt-import-dimension"
                  value={dimension}
                  disabled={busy}
                  onChange={(e) => {
                    setDimension(e.target.value);
                    setImported(null);
                  }}
                />
              </Field>
            )}
            <Field id="prompt-import-file" label="选择本地词典文件">
              <input
                id="prompt-import-file"
                type="file"
                accept=".json,.csv,.txt"
                disabled={busy}
                onChange={(e) => {
                  setFile(e.target.files?.[0] ?? null);
                  setImported(null);
                }}
              />
            </Field>
            <p className={ui.caption}>
              UTF-8，最多 10 MiB。CSV 提示词列：tags、natural_language、aspect、rating；tags 为 JSON 数组。TXT
              提示词需本地标签库参与识别。
            </p>
            <Button
              disabled={busy || dirty || !file || !canWrite()}
              onClick={() =>
                void action(async () => {
                  const data = new FormData();
                  data.set('file', file!);
                  data.set('format', format);
                  data.set('kind', kind);
                  if (kind === 'features' && format === 'txt') data.set('dimension', dimension);
                  setImported(await request<ImportPreview>('POST', P + '/imports/preview', data));
                })
              }
            >
              预览导入
            </Button>
            {imported && (
              <>
                <p role="status">
                  识别 {imported.count} 条；{imported.valid ? '可追加' : '存在错误，未写入词典'}。诊断{' '}
                  {imported.issue_count} 项。
                </p>
                <details>
                  <summary>查看导入样例（最多 100 条）</summary>
                  <ol>
                    {imported.examples.map((row, index) => (
                      <li key={index}>
                        {row.dimension ? row.dimension + ': ' : ''}
                        {row.tag ?? row.tags?.join(', ')}
                        {row.natural_language ? ' · ' + row.natural_language : ''} ·{' '}
                        {row.rating ?? '评级未知'}
                      </li>
                    ))}
                  </ol>
                </details>
                {imported.issues.map((issue, index) => (
                  <p key={index} className={ui.warning}>
                    第 {issue.row ?? '—'} 行 · {issue.code}
                  </p>
                ))}
                {imported.selection && (
                  <p role="status">
                    已按选择生成新预览：保留 {imported.selection.rows.length} 条，排除{' '}
                    {imported.selection.excluded_count} 条。确认追加仅使用本预览。
                  </p>
                )}
                {imported.selection_available && (
                  <ImportRows
                    key={imported.preview_id}
                    preview={imported}
                    disabled={busy || dirty || !canWrite()}
                    action={action}
                    onPreview={setImported}
                  />
                )}
                <Button
                  disabled={busy || dirty || !imported.valid || !canWrite()}
                  onClick={() =>
                    void action(async () => {
                      const identity = imported.preview_id + ':' + imported.digest;
                      if (importIdentity.current.digest !== identity)
                        importIdentity.current = { digest: identity, key: crypto.randomUUID() };
                      await request(
                        'POST',
                        P + '/dictionaries/' + base.dictionary_id + '/imports',
                        { preview_id: imported.preview_id, digest: imported.digest },
                        { version: base.version, key: importIdentity.current.key },
                      );
                      const next = await request<Dictionary>(
                        'GET',
                        P + '/dictionaries/' + base.dictionary_id,
                      );
                      setBase(next);
                      setContent(
                        JSON.stringify(next.kind === 'features' ? next.dimensions : next.entries, null, 2),
                      );
                      setImported(null);
                      setSaveReport(null);
                      void queryClient.invalidateQueries({ queryKey: ['prompt', 'dictionaries'] });
                    })
                  }
                >
                  确认追加到词典
                </Button>
              </>
            )}
          </details>
        )}
      </section>
      <section className={ui.section}>
        <h2>2. 覆盖与采样</h2>
        {dictionaries.data?.length ? (
          <fieldset>
            <legend>选择输入词典</legend>
            {dictionaries.data.map((d) => (
              <label className={ui.check} key={d.dictionary_id}>
                <input
                  type="checkbox"
                  checked={ids.includes(d.dictionary_id)}
                  disabled={busy}
                  onChange={(e) =>
                    setIds((old) =>
                      e.target.checked
                        ? [...old, d.dictionary_id]
                        : old.filter((id) => id !== d.dictionary_id),
                    )
                  }
                />
                {d.name}
              </label>
            ))}
          </fieldset>
        ) : (
          <EmptyState title="先保存一个词典" description="特征词典用于组合，提示词词典用于抽取完整条目。" />
        )}
        <CoveragePlanPicker
          disabled={busy}
          onBusy={setBusy}
          onApply={(value) => {
            setCount(value.count);
            setSeed(value.seed);
            setCoverage(value.rules);
            setEnabled(true);
            setPreview(null);
            setConfirmed(false);
          }}
        />
        <div className={ui.formGrid}>
          <Field id="prompt-count" label="目标数量">
            <input
              id="prompt-count"
              type="number"
              min="1"
              max="10000"
              value={count}
              disabled={busy}
              onChange={(e) => setCount(e.target.value)}
            />
          </Field>
          <Field id="prompt-seed" label="随机种子" hint="相同快照与种子可复现；留空由服务端生成。">
            <input id="prompt-seed" value={seed} disabled={busy} onChange={(e) => setSeed(e.target.value)} />
          </Field>
        </div>
        <label className={ui.check}>
          <input
            type="checkbox"
            checked={enabled}
            disabled={busy}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          启用覆盖格子配额
        </label>
        <Field
          id="prompt-coverage"
          label="覆盖与约束规则（JSON）"
          hint="默认 scene × lighting 六格；无人物至少 15%，远景至少 10%。关闭格子配额后，整体比例、排除条件和围栏仍生效。"
        >
          <textarea
            id="prompt-coverage"
            rows={9}
            value={coverage}
            disabled={busy}
            onChange={(e) => setCoverage(e.target.value)}
          />
        </Field>
        <fieldset disabled={busy}>
          <legend>精修与结果保存</legend>
          <label className={ui.check}>
            <input type="checkbox" checked={optimize} onChange={(e) => setOptimize(e.target.checked)} />
            启用 LLM 精修
          </label>
          <label className={ui.check}>
            <input type="checkbox" checked={ratingModel} onChange={(e) => setRatingModel(e.target.checked)} />
            使用评级模型判断未知评级
          </label>
          {optimize && (
            <Field
              id="prompt-options"
              label="精修选项（JSON）"
              hint="标签数范围、自然语言开关、构图模式和图幅提示。已有词条图幅优先。"
            >
              <textarea
                id="prompt-options"
                rows={6}
                value={options}
                onChange={(e) => setOptions(e.target.value)}
              />
            </Field>
          )}
          <Field id="prompt-auto-target" label="自动追加合格优化结果">
            <select
              id="prompt-auto-target"
              value={autoTarget}
              onChange={(e) => setAutoTarget(e.target.value)}
            >
              <option value="">不自动追加</option>
              {dictionaries.data
                ?.filter((d) => d.kind === 'prompts')
                .map((d) => (
                  <option key={d.dictionary_id} value={d.dictionary_id}>
                    {d.name}
                  </option>
                ))}
            </select>
          </Field>
        </fieldset>
        <p className={ui.notice}>
          预览仅在本地计算。默认围栏排除高于 sensitive 的已知内容；评级未知会明确标记。NSFW
          模板尚未提供，符合围栏的候选保留原值。
        </p>
        <Button
          disabled={busy || !canWrite() || ids.length === 0 || dirty}
          onClick={() =>
            void action(async () => {
              const body = {
                ...samplingPayload(ids, dictionaries.data ?? [], count, seed, enabled, coverage),
                optimize,
                fence: { use_rating_model: ratingModel },
                options: JSON.parse(options),
                append_target_id: autoTarget || null,
              };
              const value = await request<Preview>('POST', P + '/coverage/preview', body);
              setPreview({ for: signature, value });
              setConfirmed(false);
            })
          }
        >
          预览覆盖与样例
        </Button>
        {dirty && <p className={ui.caption}>请先保存词典修改，再预览采样。</p>}
        {preview && !validPreview && <p className={ui.warning}>采样条件已改变，请重新预览。</p>}
        {validPreview && (
          <>
            <CoverageReport
              value={validPreview}
              source={
                validPreview.coverage_view ? { kind: 'preview', ...validPreview.coverage_view } : undefined
              }
            />
            {validPreview.coverage_view_reason && (
              <p className={ui.warning}>格子明细超过内存上限，未保留明细；报告与采样功能仍可使用。</p>
            )}
            {validPreview.refinement && (
              <div className={ui.notice}>
                <p>
                  冻结角色：
                  {Object.entries(validPreview.refinement.roles)
                    .map(
                      ([role, binding]) =>
                        `${role}: ${binding ? binding.model + ' (' + binding.protocol + ')' : '未配置'}`,
                    )
                    .join('；')}
                </p>
                <p>
                  最多处理 {validPreview.refinement.max_candidates} 个候选（含两轮补位），每候选至多{' '}
                  {validPreview.refinement.max_semantic_calls_per_candidate} 次模型调用。传输层每次最多尝试 3
                  次。结果未知的请求不会自动重发。
                </p>
                {(optimize || ratingModel) && (
                  <label className={ui.check}>
                    <input
                      type="checkbox"
                      checked={confirmed}
                      onChange={(e) => setConfirmed(e.target.checked)}
                    />
                    确认创建任务后调用已配置的外部模型，可能产生费用
                  </label>
                )}
                {autoTarget && (
                  <p>任务就绪后自动追加成功精修的结果到所选词典；版本冲突时保留任务结果并停止追加。</p>
                )}
              </div>
            )}
            <details>
              <summary>预览提示词（最多 100 条）</summary>
              <OutcomeTable rows={validPreview.examples} />
            </details>
            <Button
              primary
              disabled={
                busy ||
                dirty ||
                !canWrite() ||
                !validPreview.ready ||
                ((optimize || ratingModel) && !confirmed)
              }
              onClick={() =>
                void action(async () => {
                  if (createIdentity.current.digest !== validPreview.preview_digest)
                    createIdentity.current = {
                      digest: validPreview.preview_digest,
                      task_id: crypto.randomUUID(),
                      key: crypto.randomUUID(),
                    };
                  const value = await request<Task>(
                    'POST',
                    P + '/tasks',
                    {
                      task_id: createIdentity.current.task_id,
                      request: validPreview.request,
                      preview_digest: validPreview.preview_digest,
                    },
                    { key: createIdentity.current.key },
                  );
                  setParams({ task: value.task_id });
                  void queryClient.invalidateQueries({ queryKey: ['prompt', 'tasks'] });
                })
              }
            >
              {optimize || ratingModel ? '确认创建精修任务' : '创建本地采样任务'}
            </Button>
          </>
        )}
      </section>
      <section className={ui.section}>
        <h2>3. 本地任务与结果</h2>
        <p className={ui.muted}>
          任务冻结输入词典、规则与种子，后续修改词典不改变结果。数量不足或约束不满足时禁止导出可启动的提示词集合。
        </p>
        <Field id="prompt-task" label="查看任务">
          <select
            id="prompt-task"
            value={taskId ?? ''}
            onChange={(e) => setParams(e.target.value ? { task: e.target.value } : {})}
          >
            <option value="">选择本地任务</option>
            {tasks.data?.map((task) => (
              <option key={task.task_id} value={task.task_id}>
                {task.task_id.slice(0, 8)} · {statusText[task.status]} · {task.requested} 条
              </option>
            ))}
          </select>
        </Field>
        {taskId && <PromptTask key={taskId} identifier={taskId} />}
      </section>
      <PromptTaskManagement
        onCleaned={(ids) => {
          if (taskId && ids.includes(taskId)) setParams({});
        }}
      />
    </div>
  );
}

function OutcomeTable({
  rows,
  selected,
  onSelect,
  taskId,
}: {
  rows: Outcome[];
  selected?: number[];
  onSelect?: (index: number, checked: boolean) => void;
  taskId?: string;
}) {
  return (
    <div className={ui.tableWrap}>
      <table className={ui.table}>
        <caption>内容段结果</caption>
        <thead>
          <tr>
            <th>序号</th>
            <th>内容标签与描述</th>
            <th>图幅</th>
            <th>评级</th>
            <th>处理结果</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((outcome) => {
            const row = outcome.result ?? outcome.review_candidate ?? outcome.input ?? outcome;
            return (
              <tr key={outcome.index}>
                <td>
                  {outcome.index + 1}
                  {onSelect && (
                    <input
                      aria-label={`选择结果 ${outcome.index + 1}`}
                      type="checkbox"
                      checked={selected?.includes(outcome.index) ?? false}
                      disabled={!['ok', 'skipped'].includes(outcome.status ?? 'ok')}
                      onChange={(e) => onSelect(outcome.index, e.target.checked)}
                    />
                  )}
                </td>
                <td>
                  {row.entry.tags.join(', ')}
                  {row.entry.natural_language && <p>{row.entry.natural_language}</p>}
                  {outcome.result && outcome.input && (
                    <details>
                      <summary>查看原值</summary>
                      {outcome.input.entry.tags.join(', ')}
                      <p>{outcome.input.entry.natural_language}</p>
                    </details>
                  )}
                </td>
                <td>{row.entry.aspect ?? '未指定'}</td>
                <td>
                  {row.rating ?? '未知'}
                  {!row.verified && ' · 未验证'}
                </td>
                <td>
                  {statusText[outcome.status ?? 'ok'] ?? outcome.status}
                  {outcome.reason && <p>{reasonText[outcome.reason] ?? outcome.reason}</p>}
                  {taskId && (outcome.status === 'review_required' || outcome.review) && (
                    <Link to={`/prompt-review/${taskId}/${outcome.index}`}>
                      {outcome.review ? '查看人工复验' : '编辑并复验'} 第 {outcome.index + 1} 项
                    </Link>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function PromptTask({ identifier }: { identifier: string }) {
  useSession((s) => s.connection);
  const current = useQuery({
    queryKey: ['current'],
    queryFn: ({ signal }) => request<Project | null>('GET', '/api/projects/current', undefined, { signal }),
  });
  const projectId = current.data?.project_id;
  const path = P + '/tasks/' + identifier;
  const task = useQuery({
    queryKey: ['prompt', 'task', identifier],
    queryFn: ({ signal }) => request<Task>('GET', path, undefined, { signal }),
    refetchInterval: 1000,
  });
  const [cursor, setCursor] = useState<string | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const outcomes = useQuery({
    queryKey: ['prompt', 'outcomes', identifier, cursor, task.data?.revision],
    queryFn: ({ signal }) =>
      request<Page<Outcome>>(
        'GET',
        path + '/outcomes?limit=50' + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''),
        undefined,
        { signal },
      ),
    enabled: !!task.data,
  });
  const perform = async (action: string) => {
    if (!task.data || busy || !canWrite()) return;
    setBusy(true);
    setError(null);
    try {
      if (action === 'seeds')
        download(
          'prompt-seeds-' + identifier + '.json',
          await request('POST', path + '/seeds', { expected_revision: task.data.revision }),
        );
      else await request('POST', path + '/control', { action, expected_revision: task.data.revision });
      await queryClient.invalidateQueries({ queryKey: ['prompt'] });
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div aria-label="提示词任务详情">
      <ErrorBox
        message={
          error
            ? message(error)
            : task.error
              ? errorText(task.error)
              : outcomes.error
                ? errorText(outcomes.error)
                : undefined
        }
      />
      {task.data && (
        <>
          <h3>任务：{statusText[task.data.status]}</h3>
          <p>
            随机种子 {task.data.random_seed} · {task.data.ready ? '已就绪，可导出' : '尚未就绪'}
          </p>
          {task.data.reason && <p className={ui.warning}>{task.data.reason}</p>}
          {task.data.progress && (
            <p role="status">
              处理轮次 {task.data.progress.round} / 3 ·{' '}
              {Object.entries(task.data.progress.counts)
                .map(([status, count]) => `${statusText[status] ?? status} ${count}`)
                .join('，')}
              {task.data.progress.deficits_reason &&
                ` · ${reasonText[task.data.progress.deficits_reason] ?? task.data.progress.deficits_reason}`}
            </p>
          )}
          {task.data.append_error && (
            <p className={ui.warning}>自动追加未完成：{task.data.append_error}。可重新预览后手动追加。</p>
          )}
          {task.data.appends?.map((append, i) => (
            <p key={i}>
              词典追加已完成：新增 {append.result.inserted} 条，重复 {append.result.duplicates} 条。
            </p>
          ))}
          <div className={ui.actions}>
            {['running', 'paused'].includes(task.data.status) && (
              <>
                <Button
                  disabled={busy || !canWrite()}
                  onClick={() => void perform(task.data?.status === 'paused' ? 'resume' : 'pause')}
                >
                  {task.data.status === 'paused' ? '继续采样' : '暂停采样'}
                </Button>
                <Button danger disabled={busy || !canWrite()} onClick={() => void perform('cancel')}>
                  取消采样
                </Button>
              </>
            )}
            <Button disabled={busy || !task.data.ready || !canWrite()} onClick={() => void perform('seeds')}>
              导出冻结提示词 JSON
            </Button>
          </div>
          {task.data.coverage && (
            <CoverageReport
              value={task.data.coverage}
              source={{ kind: 'task', task_id: identifier, revision: task.data.revision }}
            />
          )}
          {task.data.ready && projectId && (
            <p>
              <Link to={`/p/${projectId}/sources?task=${identifier}`}>交接到当前项目的生成源</Link>
            </p>
          )}
          {outcomes.data && (
            <>
              <OutcomeTable rows={outcomes.data.items} taskId={identifier} />
              <div className={ui.actions}>
                <Button disabled={!cursor} onClick={() => setCursor(null)}>
                  返回第一页
                </Button>
                <Button
                  disabled={!outcomes.data.next_cursor}
                  onClick={() => setCursor(outcomes.data!.next_cursor)}
                >
                  下一页结果
                </Button>
                <span>共 {outcomes.data.total} 条</span>
              </div>
            </>
          )}
          <TaskResultsExport task={task.data} />
          {task.data.status === 'done' && (
            <AppendResults identifier={identifier} revision={task.data.revision} />
          )}
        </>
      )}
    </div>
  );
}

function AppendResults({ identifier, revision }: { identifier: string; revision: number }) {
  const [target, setTarget] = useState(''),
    [indices, setIndices] = useState<number[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  type AppendPreview = {
    target_id: string;
    indices: number[];
    expected_revision: number;
    target_version: string;
    selected: number;
    inserted: number;
    duplicates: number;
  };
  const [preview, setPreview] = useState<AppendPreview | null>(null);
  const dictionaries = useQuery({
    queryKey: ['prompt', 'dictionaries', 'append'],
    queryFn: ({ signal }) => allPages<Dictionary>(P + '/dictionaries?kind=prompts', signal),
  });
  const outcomes = useQuery({
    queryKey: ['prompt', 'append-outcomes', identifier],
    queryFn: ({ signal }) => allPages<Outcome>(P + '/tasks/' + identifier + '/outcomes', signal),
  });
  const act = async (commit: boolean) => {
    if (busy || !canWrite()) return;
    setBusy(true);
    setError(null);
    try {
      if (commit && preview) {
        await request('POST', P + '/tasks/' + identifier + '/append', {
          target_id: preview.target_id,
          indices: preview.indices,
          expected_revision: preview.expected_revision,
          target_version: preview.target_version,
        });
        setPreview(null);
        await queryClient.invalidateQueries({ queryKey: ['prompt'] });
      } else
        setPreview(
          await request<AppendPreview>('POST', P + '/tasks/' + identifier + '/append-preview', {
            target_id: target,
            indices,
          }),
        );
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  };
  const valid =
    preview &&
    preview.expected_revision === revision &&
    preview.target_id === target &&
    JSON.stringify(preview.indices) === JSON.stringify([...indices].sort((a, b) => a - b));
  return (
    <details>
      <summary>将结果追加到提示词词典</summary>
      <ErrorBox
        message={
          error
            ? message(error)
            : dictionaries.error
              ? errorText(dictionaries.error)
              : outcomes.error
                ? errorText(outcomes.error)
                : undefined
        }
      />
      <Field id="append-target" label="追加目标词典">
        <select id="append-target" value={target} disabled={busy} onChange={(e) => setTarget(e.target.value)}>
          <option value="">选择词典</option>
          {dictionaries.data?.map((d) => (
            <option key={d.dictionary_id} value={d.dictionary_id}>
              {d.name}
            </option>
          ))}
        </select>
      </Field>
      <Button
        disabled={busy || !outcomes.data}
        onClick={() =>
          setIndices(
            outcomes.data!.filter((r) => ['ok', 'skipped'].includes(r.status ?? 'ok')).map((r) => r.index),
          )
        }
      >
        选择全部合格结果
      </Button>
      <Button disabled={busy} onClick={() => setIndices([])}>
        清空选择
      </Button>
      <p>已选 {indices.length} 条。成功精修及通过检查的原值可以手动追加。</p>
      <div className={ui.tableWrap}>
        <OutcomeTable
          rows={(outcomes.data ?? []).slice(0, 100)}
          selected={indices}
          onSelect={(index, checked) => {
            if (!busy) setIndices((old) => (checked ? [...old, index] : old.filter((i) => i !== index)));
          }}
        />
      </div>
      {(outcomes.data?.length ?? 0) > 100 && <p>此处显示前 100 条；“选择全部”涵盖完整结果。</p>}
      <Button disabled={busy || !target || !indices.length || !canWrite()} onClick={() => void act(false)}>
        预览追加结果
      </Button>
      {valid && (
        <>
          <p>
            选中 {preview.selected} 条；预计新增 {preview.inserted} 条，去重 {preview.duplicates} 条。
          </p>
          <Button disabled={busy || !canWrite()} onClick={() => void act(true)}>
            确认追加任务结果
          </Button>
        </>
      )}
    </details>
  );
}
