import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useNavigate, useSearchParams } from 'react-router';
import { request } from '../api/client';
import { AppError, errorText } from '../api/errors';
import type { Job, JobOutcome, ModelEntry, Page, TaggerCapabilities } from '../api/types';
import { useAction } from '../hooks/useAction';
import { useSelection } from '../state/selection';
import { canWrite, useSession } from '../state/session';
import { projectKey, queryClient } from '../state/queryClient';
import { Button, EmptyState, ErrorBox, Field, TaskProgress, ui } from '../ui';
import RetryFailed from './executors/RetryFailed';

const statuses: Record<string, string> = {
  pending: '等待',
  running: '运行中',
  paused: '已暂停',
  done: '已完成',
  canceled: '已取消',
  failed: '任务失败',
};
const outcomeNames: Record<string, string> = { done: '已写入', failed: '失败', skipped: '跳过' };
const reason = (code: string | null) =>
  !code
    ? '—'
    : code === 'cache_hit'
      ? '当前标注缓存命中'
      : code === 'file_missing'
        ? '原图缺失'
        : errorText(new AppError(503, code));
export const validThreshold = (value: string) =>
  value.trim() !== '' && Number.isFinite(Number(value)) && Number(value) >= 0 && Number(value) <= 1;

export function JobDetails({
  pid,
  id,
  controls = true,
  onRetryCreated,
}: {
  pid: string;
  id: number;
  controls?: boolean;
  onRetryCreated?: (id: string) => void;
}) {
  const navigate = useNavigate(),
    action = useAction();
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [failedOnly, setFailedOnly] = useState(false);
  const connection = useSession((s) => s.connection);
  const base = `/api/projects/${pid}/jobs/${id}`;
  const job = useQuery({
    queryKey: projectKey(pid, 'job', id),
    queryFn: ({ signal }) => request<Job>('GET', base, undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 1000,
  });
  const cursor = cursors[cursors.length - 1];
  const outcomes = useQuery({
    queryKey: projectKey(pid, 'job-outcomes', id, failedOnly, cursor),
    queryFn: ({ signal }) =>
      request<Page<JobOutcome>>(
        'GET',
        `${base}/outcomes?limit=20${failedOnly ? '&kind=failed' : ''}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
        undefined,
        { signal },
      ),
    refetchInterval: connection === 'reauth' ? false : 1500,
  });
  const row = job.data;
  async function control(actionName: 'pause' | 'resume' | 'cancel') {
    const current = await request<Job>('GET', base);
    await request('POST', base + '/control', { action: actionName }, { version: current.version });
    await queryClient.invalidateQueries({ queryKey: ['project', pid] });
  }
  return (
    <section className={ui.section} aria-label={`标注任务 ${id}`}>
      <div className={ui.between}>
        <h2>
          任务 #{id} · {row ? statuses[row.status] : '读取中'}
        </h2>
        <div className={ui.row}>
          {controls && row?.status === 'running' && (
            <Button
              quiet
              disabled={!canWrite() || action.busy}
              onClick={() => void action.run(() => control('pause'))}
            >
              暂停标注
            </Button>
          )}
          {controls && row?.status === 'paused' && (
            <Button
              disabled={!canWrite() || action.busy}
              onClick={() => void action.run(() => control('resume'))}
            >
              恢复标注
            </Button>
          )}
          {controls && row && ['pending', 'running', 'paused'].includes(row.status) && (
            <Button
              quiet
              disabled={!canWrite() || action.busy}
              onClick={() => void action.run(() => control('cancel'))}
            >
              取消标注
            </Button>
          )}
        </div>
      </div>
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : job.error
              ? errorText(job.error)
              : outcomes.error
                ? errorText(outcomes.error)
                : row?.runtime_error
                  ? reason(row.runtime_error)
                  : undefined
        }
      />
      {controls && row?.batch_id && (
        <Link to={`/p/${pid}/batches?batch=${row.batch_id}`}>查看与控制整个批次</Link>
      )}
      {row && (
        <>
          <TaskProgress
            done={row.done_count + row.failed_count + row.skipped_count}
            total={row.total}
            label="已处理图片"
          />
          <p role="status">
            已写入 {row.done_count} · 失败 {row.failed_count} · 跳过 {row.skipped_count}
          </p>
          {row.failed_count > 0 && (
            <div className={ui.row}>
              <Button
                quiet
                aria-pressed={failedOnly}
                onClick={() => {
                  setFailedOnly(!failedOnly);
                  setCursors([null]);
                }}
              >
                {failedOnly ? '查看全部结果' : `查看失败图片（${row.failed_count}）`}
              </Button>
              {['done', 'failed', 'canceled'].includes(row.status) && (
                <RetryFailed pid={pid} id={id} onCreated={onRetryCreated} />
              )}
            </div>
          )}
          {row.error && <p className={ui.notice}>{reason(row.error)}</p>}
          <details>
            <summary>冻结的执行配置</summary>
            <p className={ui.caption}>{row.config_snapshot_json.model ?? '尚未冻结模型'}</p>
            {row.executor === 'llm_vision' ? (
              <p>已冻结评级策略、角色模型、模板及词表摘要；恢复时校验相同配置。标签与自然语言成组写入。</p>
            ) : row.config_snapshot_json.params?.config ? (
              <p>
                通用阈值 {row.config_snapshot_json.params.config.general_threshold} · 角色阈值{' '}
                {row.config_snapshot_json.params.config.character_threshold} · 设备策略{' '}
                {row.config_snapshot_json.params.config.provider === 'auto' ? '自动' : 'CPU'}
                {' · 请求批大小上限 '}
                {row.config_snapshot_json.params.config.batch_size ?? 1}
              </p>
            ) : (
              <p>此任务没有当前版本可恢复的执行配置。</p>
            )}
            <p>任务范围和参数不会跟随当前表单改变。</p>
          </details>
        </>
      )}
      <div className={ui.tableWrap}>
        <table className={ui.table}>
          <caption>逐图处理结果</caption>
          <thead>
            <tr>
              <th>图片</th>
              <th>结果</th>
              <th>说明</th>
              <th>标注版本</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {outcomes.data?.items.map((item) => (
              <tr key={item.image_id}>
                <td>#{item.image_id}</td>
                <td>{outcomeNames[item.kind]}</td>
                <td>{reason(item.reason)}</td>
                <td>{item.annotation_ids.join(', ') || '—'}</td>
                <td>
                  <Button
                    quiet
                    onClick={() => {
                      useSelection.getState().focusOn(item.image_id);
                      void navigate(`/p/${pid}/workbench`);
                    }}
                  >
                    查看图片 {item.image_id}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {outcomes.data?.items.length === 0 && <p className={ui.muted}>尚无已提交的图片结果。</p>}
      {(cursors.length > 1 || outcomes.data?.has_more) && (
        <div className={ui.row}>
          <Button quiet disabled={cursors.length === 1} onClick={() => setCursors((v) => v.slice(0, -1))}>
            上一页结果
          </Button>
          <span>第 {cursors.length} 页</span>
          <Button
            quiet
            disabled={!outcomes.data?.has_more}
            onClick={() => setCursors((v) => [...v, outcomes.data!.next_cursor])}
          >
            下一页结果
          </Button>
        </div>
      )}
    </section>
  );
}

export default function Jobs({ pid }: { pid: string }) {
  const action = useAction(),
    [params, setParams] = useSearchParams();
  const selected = useSelection((s) => s.selection),
    selectionPid = useSelection((s) => s.pid);
  const ids = selectionPid === pid && selected.kind === 'ids' ? [...selected.ids].sort((a, b) => a - b) : [];
  const connection = useSession((s) => s.connection);
  const [manifest, setManifest] = useState(''),
    [general, setGeneral] = useState(''),
    [character, setCharacter] = useState('');
  const [mode, setMode] = useState('uncovered'),
    [provider, setProvider] = useState('cpu');
  const [batchSize, setBatchSize] = useState('1');
  const [includeRejected, setIncludeRejected] = useState(false);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const attempt = useRef<{ fingerprint: string; batch: string } | null>(null);
  const caps = useQuery({
    queryKey: ['tagger-capabilities'],
    queryFn: ({ signal }) =>
      request<TaggerCapabilities>('GET', '/api/tagger/capabilities', undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  const models = useQuery({
    queryKey: ['tagger-models', 'jobs'],
    queryFn: ({ signal }) =>
      request<Page<ModelEntry>>('GET', '/api/tagger/models?limit=1000', undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  const cursor = cursors[cursors.length - 1];
  const list = useQuery({
    queryKey: projectKey(pid, 'jobs', cursor),
    queryFn: ({ signal }) =>
      request<Page<Job>>(
        'GET',
        `/api/projects/${pid}/jobs?executor=wd&limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
        undefined,
        { signal },
      ),
    refetchInterval: connection === 'reauth' ? false : 1500,
  });
  const ready = models.data?.items.some((m) => m.manifest_id === manifest && m.state === 'ready');
  const canStart =
    canWrite() &&
    !action.busy &&
    caps.data?.available &&
    ready &&
    validThreshold(general) &&
    validThreshold(character) &&
    (mode !== 'selected' || (ids.length > 0 && ids.length <= 5000));
  const jobId = Number(params.get('job'));
  const chosen = Number.isSafeInteger(jobId) && jobId > 0 ? jobId : null;
  async function start() {
    const body = {
      executors: ['wd'],
      mode,
      scope:
        mode === 'selected'
          ? { selected_ids: ids }
          : {
              filter: {
                review_statuses: includeRejected ? ['keep', 'unreviewed', 'reject'] : null,
                include_missing: true,
              },
            },
      config: {
        manifest_id: manifest,
        general_threshold: Number(general),
        character_threshold: Number(character),
        provider,
        batch_size: provider === 'cpu' ? 1 : Number(batchSize),
        backend: 'onnx',
      },
    };
    const fingerprint = JSON.stringify(body);
    if (attempt.current?.fingerprint !== fingerprint)
      attempt.current = { fingerprint, batch: crypto.randomUUID() };
    const accepted = await request<{ items: Job[] }>('POST', `/api/projects/${pid}/jobs/batches`, {
      ...body,
      batch_id: attempt.current.batch,
    });
    attempt.current = null;
    setParams({ job: String(accepted.items[0].job_id) });
    setCursors([null]);
    await queryClient.invalidateQueries({ queryKey: ['project', pid] });
  }
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>ANNOTATIONS / WD</div>
          <h1>WD 标注任务</h1>
          <p>本地推理结果保存到 WD 层，原始标注和人工编辑保持独立。</p>
        </div>
        <Link className={ui.button} to="/settings/models">
          管理模型
        </Link>
      </div>
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : caps.error
              ? errorText(caps.error)
              : models.error
                ? errorText(models.error)
                : list.error
                  ? errorText(list.error)
                  : undefined
        }
      />
      {!caps.data?.available && (
        <p className={ui.notice}>请先在模型管理中安装并校验可信模型，再启动标注。已有任务记录仍可查看。</p>
      )}
      <section className={ui.section} aria-label="新建 WD 标注任务">
        <h2>新建标注任务</h2>
        <div className={ui.formGrid}>
          <Field id="job-model" label="已校验的模型">
            <select id="job-model" value={manifest} onChange={(e) => setManifest(e.target.value)}>
              <option value="">请选择模型</option>
              {models.data?.items.map((m) => (
                <option key={m.manifest_id} value={m.manifest_id} disabled={m.state !== 'ready'}>
                  {m.model_id} · {m.revision.slice(0, 8)}
                  {m.state !== 'ready' ? '（需安装或校验）' : ''}
                </option>
              ))}
            </select>
          </Field>
          <Field id="job-mode" label="执行范围">
            <select id="job-mode" value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="uncovered">项目范围，仅补全当前未覆盖</option>
              <option value="all">项目范围，全部重新标注</option>
              <option value="selected" disabled={!ids.length}>
                工作台选中图片（{ids.length} 张）
              </option>
            </select>
          </Field>
          <Field id="job-general" label="通用标签阈值（0–1，必填）">
            <input
              id="job-general"
              type="number"
              min="0"
              max="1"
              step="0.01"
              value={general}
              onChange={(e) => setGeneral(e.target.value)}
            />
          </Field>
          <Field id="job-character" label="角色标签阈值（0–1，必填）">
            <input
              id="job-character"
              type="number"
              min="0"
              max="1"
              step="0.01"
              value={character}
              onChange={(e) => setCharacter(e.target.value)}
            />
          </Field>
          <Field id="job-provider" label="设备策略">
            <select id="job-provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
              <option value="cpu">CPU</option>
              <option value="auto">自动（CUDA 不可用时使用 CPU）</option>
            </select>
          </Field>
          <Field id="job-batch" label="批大小上限">
            <select
              id="job-batch"
              value={provider === 'cpu' ? '1' : batchSize}
              disabled={provider === 'cpu'}
              onChange={(e) => setBatchSize(e.target.value)}
            >
              <option value="1">1 张</option>
              <option value="2">2 张</option>
              <option value="4">4 张</option>
            </select>
          </Field>
        </div>
        {mode !== 'selected' && (
          <label className={ui.row}>
            <input
              type="checkbox"
              checked={includeRejected}
              onChange={(e) => setIncludeRejected(e.target.checked)}
            />
            包含已剔除图片
          </label>
        )}
        <p className={ui.muted}>
          单次最多 5000 张。CPU 每次一图；GPU 按显存和已验收档案调整，无档案时每次一图。
          暂停后当前批次可以完成；取消会丢弃尚未提交的结果。任务恢复沿用冻结范围和参数。
        </p>
        {mode === 'all' && (
          <p className={ui.notice}>全部重新标注会再次执行推理并追加 WD 标注版本，历史版本保留。</p>
        )}
        <Button disabled={!canStart} onClick={() => void action.run(start)}>
          开始标注
        </Button>
      </section>
      {chosen && <JobDetails key={chosen} pid={pid} id={chosen} />}
      <section className={ui.section} aria-label="标注任务记录">
        <h2>任务记录</h2>
        {list.data?.items.length === 0 && (
          <EmptyState title="尚无标注任务" description="运行记录保存在项目中，重新打开项目后仍可查看。" />
        )}
        <div className={ui.stack}>
          {list.data?.items.map((job) => (
            <div className={ui.between} key={job.job_id}>
              <div>
                #{job.job_id} · {statuses[job.status]} ·{' '}
                {job.done_count + job.failed_count + job.skipped_count}/{job.total}
              </div>
              <Button quiet onClick={() => setParams({ job: String(job.job_id) })}>
                查看任务 {job.job_id}
              </Button>
            </div>
          ))}
        </div>
        {(cursors.length > 1 || list.data?.has_more) && (
          <div className={ui.row}>
            <Button quiet disabled={cursors.length === 1} onClick={() => setCursors((v) => v.slice(0, -1))}>
              上一页任务
            </Button>
            <span>第 {cursors.length} 页</span>
            <Button
              quiet
              disabled={!list.data?.has_more}
              onClick={() => setCursors((v) => [...v, list.data!.next_cursor])}
            >
              下一页任务
            </Button>
          </div>
        )}
      </section>
    </div>
  );
}
