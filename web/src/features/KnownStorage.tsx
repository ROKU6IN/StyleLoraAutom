import { useState } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { AppError, errorText } from '../api/errors';
import type { Accepted, Operation, Page } from '../api/types';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, Field, ui } from '../ui';
import { useRecentProjects, recentStatus } from './RecentProjects';
import { storageSize } from './storageFormat';

interface Summary {
  scan_id: string;
  as_of: string;
  project_count: number;
  scanned_count: number;
  unavailable_count: number;
  observed_total_bytes: number;
  partial: boolean;
  exceeds_threshold: boolean;
  project_threshold_bytes: number;
  total_threshold_bytes: number;
}
interface Row {
  known_id: string;
  name: string;
  display_name: string;
  total_bytes: number | null;
  partial: boolean;
  reason: string | null;
  error_count: number;
  hardlinked_files: number | null;
  exceeds_threshold: boolean | null;
}
export default function KnownStorage() {
  const connection = useSession((s) => s.connection);
  const [recentCursor, setRecentCursor] = useState<string | null>(null),
    recent = useRecentProjects(recentCursor);
  const [all, setAll] = useState(true),
    [selected, setSelected] = useState<string[]>([]);
  const [accepted, setAccepted] = useState<Accepted | null>(null);
  const [submitting, setSubmitting] = useState(false),
    [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<unknown>(null),
    [cursor, setCursor] = useState<string | null>(null);
  const [sort, setSort] = useState('-total_bytes,known_id');
  const operation = useQuery({
    queryKey: ['operations', 'known-storage', accepted?.task.id],
    queryFn: ({ signal }) => request<Operation<Summary>>('GET', accepted!.status_url, undefined, { signal }),
    enabled: !!accepted && connection !== 'reauth',
    refetchInterval: (q) =>
      ['pending', 'running'].includes(q.state.data?.status ?? 'pending') ? 500 : false,
  });
  const running = !!accepted && (!operation.data || ['pending', 'running'].includes(operation.data.status));
  const busy = submitting || running || uncertain || !canWrite();
  const results = useQuery({
    queryKey: ['known-storage-results', accepted?.task.id, sort, cursor],
    queryFn: ({ signal }) =>
      request<Page<Row> & { summary: Summary }>(
        'GET',
        '/api/storage/scans/' +
          accepted!.task.id +
          '?limit=20&sort=' +
          encodeURIComponent(sort) +
          (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''),
        undefined,
        { signal },
      ),
    enabled: !!accepted && operation.data?.status === 'done' && connection !== 'reauth',
  });
  async function start() {
    setSubmitting(true);
    setError(null);
    setAccepted(null);
    setCursor(null);
    try {
      setAccepted(await request<Accepted>('POST', '/api/storage/scans', all ? {} : { known_ids: selected }));
    } catch (e) {
      setError(e);
      if (e instanceof AppError && e.outcome === 'unknown') setUncertain(true);
    } finally {
      setSubmitting(false);
    }
  }
  async function cancel() {
    setSubmitting(true);
    setError(null);
    try {
      await request('POST', accepted!.status_url + '/cancel', {}, { version: operation.data!.version });
      await queryClient.invalidateQueries({ queryKey: ['operations', 'known-storage'] });
    } catch (e) {
      setError(e);
      if (e instanceof AppError && e.outcome === 'unknown') setUncertain(true);
    } finally {
      setSubmitting(false);
    }
  }
  const failure =
    error ||
    recent.error ||
    operation.error ||
    results.error ||
    (operation.data?.error ? AppError.from(409, operation.data.error) : null);
  const summary = results.data?.summary;
  return (
    <div className="page">
      <section className={ui.section} aria-label="跨项目存储统计">
        <div className={ui.between}>
          <h2>跨项目存储统计</h2>
          <Button
            disabled={
              busy || recent.isPending || !!recent.error || (all ? !recent.data?.total : !selected.length)
            }
            onClick={() => void start()}
          >
            扫描最近项目
          </Button>
        </div>
        <p className={ui.muted}>
          仅统计最近项目中已授权的位置。范围在提交时固定；目录移走、被替换或被占用会单独显示。此处只读，不清理文件。
        </p>
        <ErrorBox message={failure ? errorText(failure) : undefined} />
        <label>
          <input type="checkbox" checked={all} disabled={busy} onChange={(e) => setAll(e.target.checked)} />
          扫描全部最近项目
        </label>
        {!all && (
          <>
            <p>已选 {selected.length} 个项目（跨页保留）。</p>
            {recent.data?.items?.map((row) => (
              <div key={row.known_id}>
                <label>
                  <input
                    type="checkbox"
                    checked={selected.includes(row.known_id)}
                    disabled={busy}
                    onChange={(e) =>
                      setSelected((ids) =>
                        e.target.checked ? [...ids, row.known_id] : ids.filter((id) => id !== row.known_id),
                      )
                    }
                  />
                  {row.name} · {row.display_name} · {recentStatus(row)}
                </label>
              </div>
            ))}
            <Button
              disabled={!recent.data?.next_cursor || recent.isFetching}
              onClick={() => setRecentCursor(recent.data!.next_cursor)}
            >
              更多可选项目
            </Button>
          </>
        )}
        <Button
          disabled={busy || recent.isFetching}
          onClick={() => {
            setRecentCursor(null);
            setSelected([]);
            void queryClient.invalidateQueries({ queryKey: ['recent-projects'] });
          }}
        >
          刷新项目范围
        </Button>
        {recent.data?.total === 0 && <p>尚无最近项目，请先打开一个项目。</p>}
        {(running || uncertain) && (
          <p role="status">
            {uncertain
              ? '请求结果未知，已停止新扫描，请先核对任务记录。'
              : `正在扫描项目 ${operation.data?.completed ?? 0} / ${operation.data?.total ?? '…'}。`}{' '}
            <Link to="/tasks">查看扫描任务</Link>
          </p>
        )}
        {running && (
          <Button
            disabled={submitting || uncertain || !operation.data || !canWrite()}
            onClick={() => void cancel()}
          >
            取消跨项目扫描
          </Button>
        )}
        {operation.data?.status === 'canceled' && <p>扫描已取消，未发布不完整结果。</p>}
        {operation.data?.status === 'done' && (
          <Field id="known-storage-sort" label="项目占用排序">
            <select
              id="known-storage-sort"
              value={sort}
              onChange={(e) => {
                setSort(e.target.value);
                setCursor(null);
              }}
            >
              <option value="-total_bytes,known_id">占用从大到小</option>
              <option value="known_id">项目编号</option>
            </select>
          </Field>
        )}
        {summary && (
          <>
            <h3>
              已统计 {storageSize(summary.observed_total_bytes)}
              {summary.partial ? '（不完整统计）' : ''}
            </h3>
            <p>
              扫描时间 {summary.as_of}；{summary.scanned_count} / {summary.project_count} 个项目已读取，
              {summary.unavailable_count} 个不可访问。
            </p>
            <p className={ui.muted}>
              按逻辑字节统计，硬链接可能重复计数；不等于可释放磁盘空间。快照保留 15 分钟，最多 3
              次。当时提醒阈值：单项目 {storageSize(summary.project_threshold_bytes)}，扫描范围总计{' '}
              {storageSize(summary.total_threshold_bytes)}。
            </p>
            {summary.partial && <p role="alert">总量仅表示已读取部分；不可访问的项目未按零占用计算。</p>}
            {summary.exceeds_threshold && (
              <p className={ui.notice}>已读取总量超过当时阈值，可打开具体项目核对占用。</p>
            )}
            <div className={ui.tableWrap}>
              <table className={ui.table}>
                <thead>
                  <tr>
                    <th>项目</th>
                    <th>逻辑占用</th>
                    <th>结果</th>
                  </tr>
                </thead>
                <tbody>
                  {results.data!.items.map((row) => (
                    <tr key={row.known_id}>
                      <td>
                        {row.name}
                        <div className={ui.muted}>{row.display_name}</div>
                      </td>
                      <td>
                        {row.total_bytes === null ? '—' : storageSize(row.total_bytes)}
                        {row.exceeds_threshold && ' · 超过当时单项目阈值'}
                      </td>
                      <td>
                        {row.reason
                          ? errorText(new AppError(409, row.reason))
                          : row.partial
                            ? `统计不完整（${row.error_count} 处）`
                            : '已扫描'}
                        {row.hardlinked_files ? ` · ${row.hardlinked_files} 个硬链接文件` : ''}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
        {operation.data?.status === 'done' && (
          <>
            <Button disabled={!cursor} onClick={() => setCursor(null)}>
              占用结果首页
            </Button>
            <Button
              disabled={!results.data?.next_cursor || results.isFetching}
              onClick={() => setCursor(results.data!.next_cursor)}
            >
              更多占用结果
            </Button>
            {results.error && <p>快照可能已过期；可回到首页核对，或重新扫描。</p>}
          </>
        )}
      </section>
    </div>
  );
}
