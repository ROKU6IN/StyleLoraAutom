import { useEffect, useRef, useState } from 'react';
import { useQueries, useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router';
import { request } from '../api/client';
import { AppError, errorText } from '../api/errors';
import type {
  ImageQuery,
  JobBatchView,
  JobPreviewView,
  ModelEntry,
  Page,
  TaggerCapabilities,
} from '../api/types';
import { useAction } from '../hooks/useAction';
import { useSelection } from '../state/selection';
import { canWrite, useSession } from '../state/session';
import { projectKey, queryClient } from '../state/queryClient';
import { Button, ErrorBox, Field, HoldButton, Modal, TaskProgress, ui } from '../ui';
import { JobDetails, validThreshold } from './Jobs';
import { executorScope, filterSummary } from './executors/scope';

const names: Record<string, string> = { wd: 'WD 标签', llm_vision: 'LLM 视觉描述' };
const statuses: Record<string, string> = {
  pending: '等待启动',
  running: '运行中',
  paused: '已暂停',
  done: '已完成',
  failed: '失败',
  canceled: '已取消',
  blocked: '前序阶段阻塞',
};
export function needsBatchConfirmation(preview: JobPreviewView) {
  return (
    preview.image_count > 500 ||
    preview.items.some((v) => v.executor === 'llm_vision' && v.needs_execution + v.waiting_for_wd > 50) ||
    preview.overlapping_jobs.length > 0
  );
}

function BatchDetails({ pid, id, onCreated }: { pid: string; id: string; onCreated?: (id: string) => void }) {
  const action = useAction(),
    connection = useSession((s) => s.connection);
  const base = `/api/projects/${pid}/jobs/batches/${id}`;
  const detail = useQuery({
    queryKey: projectKey(pid, 'job-batch', id),
    queryFn: ({ signal }) => request<JobBatchView>('GET', base, undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 1000,
  });
  const row = detail.data;
  async function control(actionName: string) {
    const current = await request<JobBatchView>('GET', base);
    await request('POST', base + '/control', { action: actionName }, { version: current.version });
    await queryClient.invalidateQueries({ queryKey: ['project', pid] });
  }
  return (
    <section className={ui.section} aria-label="批次详情">
      <div className={ui.between}>
        <h2>批次 · {row ? statuses[row.status] : '读取中'}</h2>
        <div className={ui.row}>
          {row && ['running', 'pending'].includes(row.status) && (
            <Button
              disabled={action.busy || !canWrite()}
              onClick={() => void action.run(() => control('pause'))}
            >
              暂停批次
            </Button>
          )}
          {row && ['paused', 'pending'].includes(row.status) && (
            <Button
              disabled={action.busy || !canWrite()}
              onClick={() => void action.run(() => control('resume'))}
            >
              继续批次
            </Button>
          )}
          {row && !['done', 'failed', 'canceled'].includes(row.status) && (
            <Button
              quiet
              disabled={action.busy || !canWrite()}
              onClick={() => void action.run(() => control('cancel'))}
            >
              取消批次
            </Button>
          )}
        </div>
      </div>
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : detail.error
              ? errorText(detail.error)
              : row?.reason && row.reason !== 'job:batch_paused'
                ? errorText(new AppError(409, row.reason))
                : undefined
        }
      />
      <p className={ui.mono}>{id}</p>
      {row && (
        <>
          <p>
            {row.image_count} 张图片 · {row.items.length} 个阶段；同一图片在每个阶段各计一次。
          </p>
          <TaskProgress
            done={row.done_count + row.failed_count + row.skipped_count}
            total={row.total}
            label="已处理阶段图片项"
          />
          <p role="status">
            写入 {row.done_count} · 失败 {row.failed_count} · 跳过 {row.skipped_count}
          </p>
          <div className={ui.stack}>
            {row.items.map((job, index) => (
              <div key={job.job_id}>
                <h3>
                  {index + 1}. {names[job.executor]} · {statuses[job.status]}
                </h3>
                {job.status === 'pending' && (
                  <p className={ui.muted}>
                    配置和范围已冻结。前序完成且批次未暂停后才能启动；重开项目需显式继续。
                  </p>
                )}
                <JobDetails pid={pid} id={job.job_id} controls={false} onRetryCreated={onCreated} />
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

export default function JobBatches({
  pid,
  embedded = false,
  active = true,
  currentFilter,
  batchId,
  onCreated,
  scopePending = false,
}: {
  pid: string;
  embedded?: boolean;
  active?: boolean;
  currentFilter?: ImageQuery;
  batchId?: string | null;
  onCreated?: (id: string) => void;
  scopePending?: boolean;
}) {
  const action = useAction(),
    [params, setParams] = useSearchParams();
  const connection = useSession((s) => s.connection);
  const selected = useSelection((s) => s.selection),
    selectionPid = useSelection((s) => s.pid);
  const ids = selectionPid === pid && selected.kind === 'ids' ? [...selected.ids].sort((a, b) => a - b) : [];
  const selection = selectionPid === pid ? selected : { kind: 'ids' as const, ids: new Set<number>() };
  const selectedCount = selection.kind === 'filter' ? selection.total : ids.length;
  const [showForm, setShowForm] = useState(!batchId);
  const viewedBatch = embedded ? batchId : params.get('batch');
  const [wdChoice, setWd] = useState<boolean | null>(null),
    [vision, setVision] = useState(true);
  const [mode, setMode] = useState('uncovered'),
    [manifest, setManifest] = useState('');
  const [general, setGeneral] = useState(''),
    [character, setCharacter] = useState('');
  const [provider, setProvider] = useState('cpu'),
    [batchSize, setBatchSize] = useState('1');
  const [rating, setRating] = useState(false),
    [fallback, setFallback] = useState('normal'),
    [rejected, setRejected] = useState(false);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [confirmed, setConfirmed] = useState<{ fingerprint: string; scope: string } | null>(null);
  const attempt = useRef<{ fingerprint: string; batch: string } | null>(null);
  const models = useQuery({
    queryKey: ['tagger-models', 'batches'],
    queryFn: ({ signal }) =>
      request<Page<ModelEntry>>('GET', '/api/tagger/models?limit=1000', undefined, { signal }),
    enabled: active,
    refetchInterval: !active || connection === 'reauth' ? false : 3000,
  });
  const caps = useQuery({
    queryKey: ['llm', 'batch-capabilities'],
    queryFn: ({ signal }) =>
      request<{ wd: { available: boolean; default_enabled?: boolean }; llm_vision: { available: boolean } }>(
        'GET',
        '/api/executors/capabilities',
        undefined,
        { signal },
      ),
    enabled: active,
    refetchInterval: !active || connection === 'reauth' ? false : 3000,
  });
  const tagger = useQuery({
    queryKey: ['tagger-capabilities'],
    queryFn: ({ signal }) =>
      request<TaggerCapabilities>('GET', '/api/tagger/capabilities', undefined, { signal }),
    enabled: active,
  });
  const wd = wdChoice ?? caps.data?.wd.default_enabled ?? false;
  const wdConfig = {
    manifest_id: manifest,
    general_threshold: Number(general),
    character_threshold: Number(character),
    provider,
    batch_size: provider === 'cpu' ? 1 : Number(batchSize),
    backend: 'onnx',
  };
  const visionConfig = { dataset_mode_policy: 'style', use_rating_model: rating, fallback };
  const body = {
    executors: [...(wd ? ['wd'] : []), ...(vision ? ['llm_vision'] : [])],
    mode,
    scope: executorScope(mode, selection, currentFilter, rejected),
    config: wd && vision ? { wd: wdConfig, llm_vision: visionConfig } : wd ? wdConfig : visionConfig,
  };
  const fingerprint = JSON.stringify(body);
  const [settled, setSettled] = useState('');
  useEffect(() => {
    const timer = setTimeout(() => setSettled(fingerprint), 300);
    return () => clearTimeout(timer);
  }, [fingerprint]);
  const valid =
    !scopePending &&
    (wd || vision) &&
    (!wd ||
      (caps.data?.wd.available &&
        tagger.data?.available &&
        models.data?.items.some((m) => m.manifest_id === manifest && m.state === 'ready') &&
        validThreshold(general) &&
        validThreshold(character))) &&
    (!vision || caps.data?.llm_vision.available) &&
    (mode !== 'selected' || (selectedCount > 0 && (selection.kind === 'filter' || ids.length <= 5000)));
  const preview = useQuery({
    queryKey: projectKey(pid, 'batch-preview', fingerprint),
    queryFn: ({ signal }) =>
      request<JobPreviewView>('POST', `/api/projects/${pid}/jobs/preview`, JSON.parse(fingerprint), {
        signal,
      }),
    enabled: Boolean(
      active && (!embedded || showForm) && valid && settled === fingerprint && connection !== 'reauth',
    ),
  });
  const rangeCounts = useQueries({
    queries: ['selected', 'uncovered', 'all'].map((option) => {
      const estimate = {
        ...body,
        mode: option,
        scope: executorScope(option, selection, currentFilter, rejected),
      };
      return {
        queryKey: projectKey(pid, 'batch-preview', JSON.stringify(estimate)),
        queryFn: ({ signal }: { signal: AbortSignal }) =>
          request<JobPreviewView>('POST', `/api/projects/${pid}/jobs/preview`, estimate, { signal }),
        enabled: Boolean(
          active &&
            (!embedded || showForm) &&
            valid &&
            settled === fingerprint &&
            connection !== 'reauth' &&
            (option !== 'selected' || selectedCount > 0),
        ),
      };
    }),
  });
  const rangeCount = (index: number) => rangeCounts[index].data?.image_count ?? '—';
  const usable = Boolean(
    valid &&
      settled === fingerprint &&
      preview.data &&
      !preview.isFetching &&
      !preview.error &&
      preview.data.image_count > 0 &&
      canWrite() &&
      !action.busy,
  );
  const cursor = cursors[cursors.length - 1];
  const list = useQuery({
    queryKey: projectKey(pid, 'job-batches', cursor),
    queryFn: ({ signal }) =>
      request<Page<JobBatchView>>(
        'GET',
        `/api/projects/${pid}/jobs/batches?limit=10${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
        undefined,
        { signal },
      ),
    enabled: !embedded && active,
    refetchInterval: embedded || !active || connection === 'reauth' ? false : 1500,
  });
  function selectBatch(id: string) {
    if (onCreated) onCreated(id);
    else setParams({ batch: id });
    setShowForm(false);
  }
  async function start(scope: string) {
    const currentBody = { ...body, preview_scope_digest: scope };
    const identity = JSON.stringify(currentBody);
    if (attempt.current?.fingerprint !== identity)
      attempt.current = { fingerprint: identity, batch: crypto.randomUUID() };
    let result: { batch_id: string };
    try {
      result = await request<{ batch_id: string }>('POST', `/api/projects/${pid}/jobs/batches`, {
        ...currentBody,
        batch_id: attempt.current.batch,
      });
    } catch (error) {
      if (error instanceof AppError && error.code === 'job:preview_stale') {
        setConfirmed(null);
        await preview.refetch();
      }
      throw error;
    }
    attempt.current = null;
    setConfirmed(null);
    selectBatch(result.batch_id);
    setCursors([null]);
    await queryClient.invalidateQueries({ queryKey: ['project', pid] });
  }
  function begin() {
    if (!usable || !preview.data) return;
    if (mode === 'all' || needsBatchConfirmation(preview.data))
      setConfirmed({ fingerprint, scope: preview.data.scope_digest });
    else void action.run(() => start(preview.data!.scope_digest));
  }
  const confirmAllowed =
    usable && confirmed?.fingerprint === fingerprint && confirmed?.scope === preview.data?.scope_digest;
  return (
    <div className={embedded ? ui.stack : 'page'}>
      {embedded && viewedBatch && (
        <div className={ui.row}>
          <Button onClick={() => setShowForm(!showForm)}>{showForm ? '返回批次进度' : '新建标注批次'}</Button>
          <Link to={`/p/${pid}/batches?batch=${viewedBatch}`}>打开完整批次页</Link>
        </div>
      )}
      {!embedded && (
        <div className="pageHeading">
          <div>
            <div className={ui.eyebrow}>ANNOTATIONS / BATCHES</div>
            <h1>标注批次</h1>
            <p>选择一个或两个阶段。组合批次按 WD → LLM 顺序执行，视觉阶段读取最新 WD 证据。</p>
          </div>
          <div className={ui.row}>
            <Link to="/settings/models">模型管理</Link>
            <Link to="/settings/llm">LLM 配置</Link>
          </div>
        </div>
      )}
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : preview.error
              ? errorText(preview.error)
              : caps.error
                ? errorText(caps.error)
                : models.error
                  ? errorText(models.error)
                  : list.error
                    ? errorText(list.error)
                    : undefined
        }
      />
      {(!embedded || showForm || !viewedBatch) && (
        <section className={ui.section} aria-label="新建标注批次">
          <h2>执行范围与阶段</h2>
          {scopePending && <p role="status">正在应用工作台搜索条件，请稍候再执行。</p>}
          <div className={ui.row}>
            <label>
              <input
                type="checkbox"
                checked={wd}
                disabled={!wd && !caps.data?.wd.available}
                onChange={(e) => setWd(e.target.checked)}
              />
              WD 标签阶段
            </label>
            <label>
              <input
                type="checkbox"
                checked={vision}
                disabled={!vision && !caps.data?.llm_vision.available}
                onChange={(e) => setVision(e.target.checked)}
              />
              LLM 视觉阶段
            </label>
          </div>
          {((wd && !caps.data?.wd.available) || (vision && !caps.data?.llm_vision.available)) && (
            <p className={ui.notice}>所选阶段尚不可用，请先配置模型和角色，或取消勾选该阶段。</p>
          )}
          {embedded && (
            <div className={ui.row}>
              <Link to="/settings/models">模型管理</Link>
              <Link to="/settings/llm">LLM 配置</Link>
            </div>
          )}
          {currentFilter && <p className={ui.caption}>当前筛选：{filterSummary(currentFilter, rejected)}</p>}
          <Field id="batch-mode" label="批次范围">
            <select id="batch-mode" value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="uncovered">
                {currentFilter ? '当前筛选' : '项目范围'}，仅执行未覆盖（候选 {rangeCount(1)} 张）
              </option>
              <option value="all">
                {currentFilter ? '当前筛选' : '项目范围'}，全部重新执行（候选 {rangeCount(2)} 张）
              </option>
              <option value="selected" disabled={!selectedCount}>
                工作台选中{selection.kind === 'filter' ? '筛选全集' : ''}（候选 {rangeCount(0)} 张）
              </option>
            </select>
          </Field>
          {mode === 'selected' && selection.kind === 'filter' && (
            <p className={ui.notice}>
              使用全选时保存的筛选条件，由服务端解析完整范围；后续改变当前筛选不会改变这份选择。
              <br />
              已选筛选：{filterSummary(selection.query, true)}
            </p>
          )}
          {mode !== 'selected' && !currentFilter?.review_statuses?.length && (
            <label className={ui.row}>
              <input type="checkbox" checked={rejected} onChange={(e) => setRejected(e.target.checked)} />
              包含已剔除图片
            </label>
          )}
          {wd && (
            <div className={ui.formGrid}>
              <Field id="batch-model" label="批次 WD 模型">
                <select id="batch-model" value={manifest} onChange={(e) => setManifest(e.target.value)}>
                  <option value="">请选择已校验模型</option>
                  {models.data?.items.map((m) => (
                    <option key={m.manifest_id} value={m.manifest_id} disabled={m.state !== 'ready'}>
                      {m.model_id} · {m.revision.slice(0, 8)}
                    </option>
                  ))}
                </select>
              </Field>
              <Field id="batch-general" label="批次通用标签阈值">
                <input
                  id="batch-general"
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  value={general}
                  onChange={(e) => setGeneral(e.target.value)}
                />
              </Field>
              <Field id="batch-character" label="批次角色标签阈值">
                <input
                  id="batch-character"
                  type="number"
                  min="0"
                  max="1"
                  step="0.01"
                  value={character}
                  onChange={(e) => setCharacter(e.target.value)}
                />
              </Field>
              <Field id="batch-provider" label="批次设备策略">
                <select id="batch-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
                  <option value="cpu">CPU</option>
                  <option value="auto">自动（CUDA / CPU）</option>
                </select>
              </Field>
              <Field id="batch-size" label="批次 WD 每批上限">
                <select
                  id="batch-size"
                  disabled={provider === 'cpu'}
                  value={provider === 'cpu' ? '1' : batchSize}
                  onChange={(e) => setBatchSize(e.target.value)}
                >
                  <option value="1">1 张</option>
                  <option value="2">2 张</option>
                  <option value="4">4 张</option>
                </select>
              </Field>
            </div>
          )}
          {vision && (
            <>
              <Field id="batch-fallback" label="批次评级兜底">
                <select id="batch-fallback" value={fallback} onChange={(e) => setFallback(e.target.value)}>
                  <option value="normal">普通角色，未验证</option>
                  <option value="nsfwfallback">NSFW 角色，未验证</option>
                </select>
              </Field>
              <label className={ui.row}>
                <input type="checkbox" checked={rating} onChange={(e) => setRating(e.target.checked)} />
                启用分级模型
              </label>
              <p className={ui.notice}>
                开始后，LLM 阶段会向配置的服务发送压缩图片和原始/WD
                标签；分级模型还会收到原始自然语言，可能产生费用。
              </p>
            </>
          )}
          <h3>本地覆盖预览</h3>
          {!valid ? (
            <p>请补齐所选阶段配置后查看数量。</p>
          ) : preview.isFetching || settled !== fingerprint ? (
            <p role="status">正在计算覆盖数量…</p>
          ) : (
            preview.data && (
              <>
                <div className={ui.tableWrap}>
                  <table className={ui.table}>
                    <caption>阶段与预计处理数量</caption>
                    <thead>
                      <tr>
                        <th>阶段</th>
                        <th>候选</th>
                        <th>缓存命中</th>
                        <th>待执行</th>
                        <th>等待 WD 决定</th>
                        <th>文件缺失</th>
                        <th>角色缺失</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.data.items.map((item) => (
                        <tr key={item.executor}>
                          <td>{names[item.executor]}</td>
                          <td>{item.total}</td>
                          <td>{item.cache_hit}</td>
                          <td>{item.needs_execution}</td>
                          <td>{item.waiting_for_wd}</td>
                          <td>{item.file_missing}</td>
                          <td>{item.role_empty}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {preview.data.overlapping_jobs.length > 0 && (
                  <p className={ui.notice}>
                    与 {preview.data.overlapping_jobs.length}{' '}
                    个未结束任务范围重叠。继续将新建批次，可能重复推理；请先核对已有记录。
                  </p>
                )}
              </>
            )
          )}
          <p className={ui.muted}>
            预览不推理、不调用分级模型，也不写评级。数量是当前状态估算，实际执行会重新核验文件与缓存；WD
            待执行图片的视觉覆盖需等 WD 完成再确定。单次最多 5000 张。
          </p>
          <Button primary disabled={!usable} onClick={begin}>
            开始标注批次
          </Button>
        </section>
      )}
      {viewedBatch && active && (!embedded || !showForm) && (
        <BatchDetails key={viewedBatch} pid={pid} id={viewedBatch} onCreated={selectBatch} />
      )}
      {!embedded && (
        <section className={ui.section}>
          <h2>批次记录</h2>
          <p className={ui.caption}>包含已有单阶段任务。查看批次可统一控制所有未完成阶段。</p>
          <div className={ui.stack}>
            {list.data?.items.map((row) => (
              <div className={ui.between} key={row.batch_id}>
                <span>
                  {row.items.map((item) => names[item.executor]).join(' → ')} · {statuses[row.status]} ·{' '}
                  {row.done_count + row.failed_count + row.skipped_count}/{row.total}
                </span>
                <Button quiet onClick={() => setParams({ batch: row.batch_id })}>
                  查看批次 {row.items[0].job_id}
                </Button>
              </div>
            ))}
          </div>
          {(cursors.length > 1 || list.data?.has_more) && (
            <div className={ui.row}>
              <Button disabled={cursors.length === 1} onClick={() => setCursors((v) => v.slice(0, -1))}>
                上一页批次
              </Button>
              <Button
                disabled={!list.data?.has_more}
                onClick={() => setCursors((v) => [...v, list.data!.next_cursor])}
              >
                下一页批次
              </Button>
            </div>
          )}
        </section>
      )}
      <Modal
        open={Boolean(confirmed)}
        onOpenChange={(open) => {
          if (!open && !action.busy) setConfirmed(null);
        }}
        title="确认执行标注批次"
        description="核对范围与预计数量。大批量或重复执行可能增加模型调用及费用。"
      >
        <p>
          {body.executors.map((name) => names[name]).join(' → ')} ·{' '}
          {mode === 'all' ? '全部重新执行' : mode === 'selected' ? '选中图片' : '未覆盖图片'} ·{' '}
          {preview.data?.image_count ?? 0} 张候选图片
        </p>
        {mode === 'all' && <p>全部重新执行会追加标注版本。请按住按钮 1 秒确认。</p>}
        {mode === 'selected' && selection.kind === 'filter' ? (
          <p className={ui.caption}>{filterSummary(selection.query, true)}</p>
        ) : mode !== 'selected' && currentFilter ? (
          <p className={ui.caption}>{filterSummary(currentFilter, rejected)}</p>
        ) : null}
        {preview.data?.overlapping_jobs.length ? <p>存在范围重叠的未结束任务。</p> : null}
        <ErrorBox
          message={
            action.error
              ? errorText(action.error)
              : !confirmAllowed && !action.busy
                ? '预览或范围已变化，请返回重新核对。'
                : undefined
          }
        />
        <div className={ui.actions}>
          <Button disabled={action.busy} onClick={() => setConfirmed(null)}>
            返回检查
          </Button>
          {mode === 'all' ? (
            <HoldButton
              disabled={!confirmAllowed}
              onConfirm={() => void action.run(() => start(confirmed!.scope))}
            >
              按住执行全部
            </HoldButton>
          ) : (
            <Button
              primary
              disabled={!confirmAllowed}
              onClick={() => void action.run(() => start(confirmed!.scope))}
            >
              确认启动批次
            </Button>
          )}
        </div>
      </Modal>
    </div>
  );
}
