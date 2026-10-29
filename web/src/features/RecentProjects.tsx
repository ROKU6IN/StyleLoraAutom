import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { AppError, errorText } from '../api/errors';
import type { Accepted, Operation, Page, Project } from '../api/types';
import { useDraftGuard } from '../hooks/drafts';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, ui } from '../ui';

export interface RecentProject {
  known_id: string;
  name: string;
  display_name: string;
  last_opened_at: string;
  project_id: string | null;
  status: 'available' | 'current' | 'closing' | 'unavailable';
  reason: string | null;
}
export function useRecentProjects(cursor: string | null) {
  return useQuery({
    queryKey: ['recent-projects', cursor],
    queryFn: ({ signal }) =>
      request<Page<RecentProject> & { total: number }>(
        'GET',
        '/api/projects?limit=20' + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''),
        undefined,
        { signal },
      ),
  });
}
export function recentStatus(row: RecentProject) {
  if (row.reason) return errorText(new AppError(409, row.reason));
  return { available: '可打开', current: '当前项目', closing: '正在关闭', unavailable: '不可访问' }[
    row.status
  ];
}
export default function RecentProjects() {
  const [cursor, setCursor] = useState<string | null>(null);
  const recent = useRecentProjects(cursor);
  const [accepted, setAccepted] = useState<Accepted | null>(null);
  const [submitting, setSubmitting] = useState(false),
    [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const connection = useSession((s) => s.connection),
    guard = useDraftGuard(),
    navigate = useNavigate();
  const completed = useRef('');
  const operation = useQuery({
    queryKey: ['operations', 'recent-open', accepted?.task.id],
    queryFn: ({ signal }) => request<Operation<Project>>('GET', accepted!.status_url, undefined, { signal }),
    enabled: !!accepted && connection !== 'reauth',
    refetchInterval: (q) =>
      ['pending', 'running'].includes(q.state.data?.status ?? 'pending') ? 500 : false,
  });
  const running = !!accepted && (!operation.data || ['pending', 'running'].includes(operation.data.status));
  const busy = submitting || running || uncertain || !canWrite();
  useEffect(() => {
    if (operation.data?.status !== 'done' || completed.current === accepted?.task.id) return;
    completed.current = accepted!.task.id;
    void queryClient.invalidateQueries({ queryKey: ['current'] });
    void queryClient.invalidateQueries({ queryKey: ['session'] });
    void queryClient.invalidateQueries({ queryKey: ['recent-projects'] });
    navigate(`/p/${operation.data.result.project_id}/workbench`);
  }, [accepted, operation.data, navigate]);
  async function open(known_id: string) {
    setSubmitting(true);
    setError(null);
    setAccepted(null);
    try {
      setAccepted(await request<Accepted>('POST', '/api/projects/open', { known_id }));
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
    (operation.data?.error ? AppError.from(409, operation.data.error) : null);
  return (
    <section className={ui.section} aria-label="最近项目">
      <div className={ui.between}>
        <h2>最近项目</h2>
        <Button
          disabled={recent.isFetching}
          onClick={() => {
            setCursor(null);
            void queryClient.invalidateQueries({ queryKey: ['recent-projects'] });
          }}
        >
          刷新最近项目
        </Button>
      </div>
      <p className={ui.muted}>
        仅显示曾成功打开的项目，最多保留 200 条。移动后的项目请通过“打开已有项目”重新选择。
      </p>
      <ErrorBox message={failure ? errorText(failure) : undefined} />
      {(running || uncertain) && (
        <p role="status">
          {uncertain ? '打开请求结果未知，已停止重复提交。' : '正在打开项目。'}{' '}
          <Link to="/tasks">查看打开任务</Link>
        </p>
      )}
      {recent.isPending && <p role="status">正在读取最近项目…</p>}
      {recent.data?.total === 0 && <p>尚无最近项目。</p>}
      <div className={ui.tableWrap}>
        <table className={ui.table}>
          <thead>
            <tr>
              <th>项目</th>
              <th>最近打开</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {recent.data?.items?.map((row) => (
              <tr key={row.known_id}>
                <td>
                  {row.name}
                  <div className={ui.muted}>{row.display_name}</div>
                </td>
                <td>{row.last_opened_at}</td>
                <td>{recentStatus(row)}</td>
                <td>
                  <Button
                    disabled={busy || !['available', 'current'].includes(row.status)}
                    onClick={() => guard.run(() => void open(row.known_id))}
                  >
                    打开 {row.name}
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Button
        disabled={!recent.data?.next_cursor || recent.isFetching}
        onClick={() => setCursor(recent.data!.next_cursor)}
      >
        更多最近项目
      </Button>
    </section>
  );
}
