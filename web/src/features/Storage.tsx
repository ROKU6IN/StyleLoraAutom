import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { AppError, errorText } from '../api/errors';
import type { Accepted, Operation } from '../api/types';
import { queryClient, projectKey } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, EmptyState, ErrorBox, Field, HoldButton, Modal, ui } from '../ui';
import KnownStorage from './KnownStorage';
import { storageSize } from './storageFormat';
import StorageThresholds from './StorageThresholds';

type Kind = 'thumbnails' | 'exports' | 'rejected_files';
type Issue = { path: string; code: string };
type Scan = {
  as_of: string;
  total_bytes: number;
  categories: Record<string, number>;
  reviews: Record<string, { bytes: number; files: number; missing_records: number }>;
  sources: { source_id: number; bytes: number; files: number; missing_records: number }[];
  partial: boolean;
  errors: Issue[];
  error_count: number;
  missing_records: number;
  not_observed_records: number;
  hardlinked_files: number;
  exceeds_threshold: boolean;
  project_threshold_bytes: number;
};
type Preview = {
  preview_id: string;
  digest: string;
  kind: Kind;
  count: number;
  bytes: number;
  hardlinked_files: number;
  expires_at: string;
  skipped: Issue[];
  candidates: { path: string; bytes: number; image_id: number | null }[];
};
type Cleanup = {
  status: string;
  deleted: number;
  failed: number;
  unlinked_bytes: number;
  results: { path: string; status: string; code?: string }[];
};
const labels: Record<string, string> = {
  images: '已登记图片',
  image_other: '其它图片目录文件',
  pending: '待登记产物与收据',
  evidence: '生成与清理证据',
  cache: '缓存',
  exports: '导出产物',
  database: '数据库 / WAL / SHM',
  backups: '迁移备份',
  other: '其它项目文件',
  keep: '保留',
  unreviewed: '未审',
  reject: '剔除',
};
const kinds: Record<Kind, string> = {
  thumbnails: '缩略图缓存',
  exports: '已完成导出产物',
  rejected_files: '已剔除图片文件',
};
export default function Storage({ pid }: { pid?: string }) {
  return (
    <>
      <StorageThresholds />
      {pid ? (
        <ProjectStorage key={pid} pid={pid} />
      ) : (
        <EmptyState
          title="请先打开项目"
          description="打开项目后可使用当前项目的分类统计与清理。下方可扫描最近项目。"
        />
      )}
      <KnownStorage />
    </>
  );
}
function ProjectStorage({ pid }: { pid: string }) {
  const connection = useSession((s) => s.connection);
  const [kind, setKind] = useState<Kind>('thumbnails');
  const [action, setAction] = useState<'scans' | 'cleanup/preview' | 'cleanup'>('scans');
  const [accepted, setAccepted] = useState<Accepted | null>(null);
  const [submitting, setSubmitting] = useState(false),
    [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [checked, setChecked] = useState(false),
    [dismissed, setDismissed] = useState(false);
  const [page, setPage] = useState(0);
  const completed = useRef('');
  const path = `/api/projects/${pid}/storage`;
  const summary = useQuery({
    queryKey: projectKey(pid, 'storage'),
    queryFn: ({ signal }) =>
      request<{ scan: Scan | null; stale: boolean; config_changed: boolean }>(
        'GET',
        path + '/summary',
        undefined,
        { signal },
      ),
  });
  const operation = useQuery({
    queryKey: ['operations', 'storage', accepted?.task.id],
    queryFn: ({ signal }) =>
      request<Operation<Scan | Preview | Cleanup>>('GET', accepted!.status_url, undefined, { signal }),
    enabled: !!accepted && connection !== 'reauth',
    refetchInterval: (q) =>
      ['pending', 'running'].includes(q.state.data?.status ?? 'pending') ? 500 : false,
  });
  const running = !!accepted && (!operation.data || ['pending', 'running'].includes(operation.data.status));
  const busy = submitting || running || uncertain || !canWrite();
  useEffect(() => {
    if (
      !accepted ||
      !operation.data ||
      ['pending', 'running'].includes(operation.data.status) ||
      completed.current === accepted.task.id
    )
      return;
    completed.current = accepted.task.id;
    void queryClient.invalidateQueries({ queryKey: ['project', pid] });
  }, [accepted, operation.data, pid]);
  const preview =
    action === 'cleanup/preview' && operation.data?.status === 'done'
      ? (operation.data.result as Preview)
      : null;
  const cleaned =
    action === 'cleanup' && operation.data?.status === 'done' ? (operation.data.result as Cleanup) : null;
  const scan = summary.data?.scan;
  const expired = !!preview && Date.parse(preview.expires_at) <= Date.now();
  async function start(next: typeof action, body: unknown) {
    setSubmitting(true);
    setError(null);
    setAccepted(null);
    setChecked(false);
    setDismissed(false);
    setPage(0);
    setAction(next);
    try {
      setAccepted(await request<Accepted>('POST', path + '/' + next, body));
    } catch (e) {
      setError(e);
      if (e instanceof AppError && e.outcome === 'unknown') setUncertain(true);
    } finally {
      setSubmitting(false);
    }
  }
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>PROJECT STORAGE</div>
          <h1>存储占用与清理</h1>
          <p>
            只统计当前项目；扫描不会修改文件或审核状态。大小按文件逻辑字节累计，硬链接不代表可释放同等磁盘空间。
          </p>
        </div>
        <Button disabled={busy} onClick={() => void start('scans', {})}>
          扫描当前项目
        </Button>
      </div>
      <ErrorBox
        message={
          error
            ? errorText(error)
            : summary.error
              ? errorText(summary.error)
              : operation.error
                ? errorText(operation.error)
                : operation.data?.error
                  ? errorText(AppError.from(409, operation.data.error))
                  : undefined
        }
      />
      {(running || uncertain) && (
        <p role="status" className={ui.notice}>
          {uncertain
            ? '请求结果未知，已停止新操作。请先核对任务记录，再重新进入此页。'
            : '后台任务正在处理，离开页面不会自动重发或撤销操作。'}{' '}
          <Link to="/tasks">查看任务记录</Link>
        </p>
      )}
      {!scan && !summary.isPending && <p>尚未扫描，没有当前占用数据。</p>}
      {scan && (
        <section className={ui.section} aria-label="扫描结果">
          <h2>
            已扫描 {storageSize(scan.total_bytes)}
            {scan.partial ? '（不完整统计）' : ''}
          </h2>
          <p className={ui.muted}>
            扫描时间 {scan.as_of}；这是历史快照，清理或项目变化后请重新扫描。当时提醒阈值{' '}
            {storageSize(scan.project_threshold_bytes)}。
          </p>
          {summary.data?.config_changed && (
            <p className={ui.notice}>提醒阈值已修改；旧报告保留当时阈值，请重新扫描。</p>
          )}
          {scan.exceeds_threshold && <p className={ui.notice}>项目占用超过当时阈值，可预览下方清理候选。</p>}
          {scan.partial && (
            <p role="alert">有 {scan.error_count} 处无法完整统计，当前总量仅表示已读取部分。</p>
          )}
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>分类</th>
                  <th>逻辑大小</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(scan.categories).map(([key, value]) => (
                  <tr key={key}>
                    <td>{labels[key]}</td>
                    <td>{storageSize(value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3>已登记图片按审核状态</h3>
          <div className={ui.tableWrap}>
            <table className={ui.table}>
              <thead>
                <tr>
                  <th>审核状态</th>
                  <th>现存文件</th>
                  <th>大小</th>
                  <th>已标记缺失</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(scan.reviews).map(([key, row]) => (
                  <tr key={key}>
                    <td>{labels[key]}</td>
                    <td>{row.files}</td>
                    <td>{storageSize(row.bytes)}</td>
                    <td>{row.missing_records}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p>
            未观察到文件的记录 {scan.not_observed_records} 条；已标记缺失 {scan.missing_records}{' '}
            条；硬链接文件 {scan.hardlinked_files} 个。
          </p>
          <details>
            <summary>按来源查看</summary>
            <div className={ui.tableWrap}>
              <table className={ui.table}>
                <thead>
                  <tr>
                    <th>来源</th>
                    <th>文件</th>
                    <th>大小</th>
                    <th>缺失记录</th>
                  </tr>
                </thead>
                <tbody>
                  {scan.sources.map((row) => (
                    <tr key={row.source_id}>
                      <td>#{row.source_id}</td>
                      <td>{row.files}</td>
                      <td>{storageSize(row.bytes)}</td>
                      <td>{row.missing_records}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
          {scan.errors.length > 0 && (
            <details>
              <summary>统计问题（显示最多 200 条）</summary>
              {scan.errors.map((e, i) => (
                <p key={i}>
                  {e.path} · {e.code}
                </p>
              ))}
            </details>
          )}
        </section>
      )}
      <section className={ui.section}>
        <h2>预览清理范围</h2>
        <p>
          缩略图可重新生成；只清理已完成且凭据完整的导出目录。删除剔除图片不可撤销，保留标注与历史，图片将标记为缺失。
        </p>
        <div className={ui.row}>
          <Field id="cleanup-kind" label="清理类型">
            <select
              id="cleanup-kind"
              disabled={busy}
              value={kind}
              onChange={(e) => setKind(e.target.value as Kind)}
            >
              {Object.entries(kinds).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
          <Button disabled={busy} onClick={() => void start('cleanup/preview', { kind })}>
            预览候选文件
          </Button>
        </div>
        <p className={ui.muted}>
          每次最多 5000 个候选文件。数据库、待登记产物、生成与恢复证据不在清理范围内。
        </p>
      </section>
      {cleaned && (
        <section className={ui.section} aria-label="清理结果">
          <h2>清理{cleaned.failed ? '部分完成' : '完成'}</h2>
          <p>
            已删除 {cleaned.deleted} 个文件，未完成 {cleaned.failed} 个；解除链接的逻辑大小{' '}
            {storageSize(cleaned.unlinked_bytes)}。
          </p>
          <p>可重新扫描核对占用。失败文件需重新预览后再决定是否清理。</p>
          {cleaned.results
            .filter((r) => r.status !== 'deleted')
            .map((r) => (
              <p key={r.path}>
                {r.path} · {r.code}
              </p>
            ))}
        </section>
      )}
      <Modal
        open={!!preview && !dismissed}
        onOpenChange={(open) => {
          if (!open) setDismissed(true);
        }}
        title="确认清理候选文件"
        description="核对本次冻结的候选文件；勾选并长按后才开始清理。"
        wide
      >
        {preview && (
          <div className={ui.stack}>
            <p>
              {kinds[preview.kind]}：{preview.count} 个文件，{storageSize(preview.bytes)}
              。仅删除本次预览列出的文件。
            </p>
            <p>
              {preview.kind === 'rejected_files'
                ? '图片文件删除后不可撤销，数据库和标注历史仍保留。'
                : '清理后可重新生成缩略图或重新导出；重新导出采用届时配置。'}
            </p>
            {preview.hardlinked_files > 0 && (
              <p>含 {preview.hardlinked_files} 个硬链接，删除此链接不保证释放对应磁盘空间。</p>
            )}
            {preview.skipped.length > 0 && (
              <details>
                <summary>跳过 {preview.skipped.length} 个未结清或缺少凭据的导出目录</summary>
                {preview.skipped.map((e) => (
                  <p key={e.path}>{e.path}</p>
                ))}
              </details>
            )}
            <div className={ui.tableWrap}>
              <table className={ui.table}>
                <thead>
                  <tr>
                    <th>项目内文件</th>
                    <th>大小</th>
                  </tr>
                </thead>
                <tbody>
                  {preview.candidates.slice(page * 50, (page + 1) * 50).map((f) => (
                    <tr key={f.path}>
                      <td>{f.path}</td>
                      <td>{storageSize(f.bytes)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {preview.count > 50 && (
              <div className={ui.row}>
                <Button disabled={page === 0} onClick={() => setPage(page - 1)}>
                  上一页
                </Button>
                <span>
                  {page + 1} / {Math.ceil(preview.count / 50)}
                </span>
                <Button disabled={(page + 1) * 50 >= preview.count} onClick={() => setPage(page + 1)}>
                  下一页
                </Button>
              </div>
            )}
            <p className={ui.muted}>预览有效至 {preview.expires_at}；文件或审核状态变化会拒绝过期操作。</p>
            <label className={ui.row}>
              <input
                type="checkbox"
                checked={checked}
                disabled={busy || expired || preview.count === 0}
                onChange={(e) => setChecked(e.target.checked)}
              />
              我已核对文件范围并理解清理后果
            </label>
            <HoldButton
              disabled={busy || expired || !checked || preview.count === 0}
              onConfirm={() =>
                void start('cleanup', {
                  preview_id: preview.preview_id,
                  digest: preview.digest,
                  confirm: true,
                })
              }
            >
              按住清理这些文件
            </HoldButton>
            <Button quiet disabled={submitting} onClick={() => setDismissed(true)}>
              关闭预览
            </Button>
          </div>
        )}
      </Modal>
    </div>
  );
}
