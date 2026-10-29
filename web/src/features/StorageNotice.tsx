import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { request } from '../api/client';
import { useSession } from '../state/session';
import { ui } from '../ui';
import { storageSize } from './storageFormat';

interface Observation {
  as_of: string;
  config_changed: boolean;
  partial: boolean;
  exceeds_threshold: boolean;
  project_threshold_bytes: number;
}
interface StorageSummary {
  current_project: (Observation & { project_id: string; total_bytes: number }) | null;
  known_projects:
    | (Observation & {
        scope: string;
        project_count: number;
        unavailable_count: number;
        observed_total_bytes: number;
        total_threshold_bytes: number;
        over_threshold_count: number;
        expired: boolean;
        history_changed: boolean;
      })
    | null;
}
export default function StorageNotice({ pid }: { pid?: string }) {
  const connection = useSession((s) => s.connection);
  const summary = useQuery({
    queryKey: ['storage-notices', pid],
    queryFn: ({ signal }) => request<StorageSummary>('GET', '/api/storage/summary', undefined, { signal }),
    enabled: !['reauth', 'offline'].includes(connection),
    refetchInterval: ['reauth', 'offline'].includes(connection) ? false : 15000,
  });
  if (summary.error)
    return (
      <div role="status" className={ui.notice}>
        暂时无法读取存储提醒，请核对本地设置。 <Link to="/settings/storage">查看存储</Link>
      </div>
    );
  const candidate = summary.data?.current_project;
  const current = candidate && candidate.project_id === pid ? candidate : null;
  const known = summary.data?.known_projects;
  const showCurrent = current && (current.exceeds_threshold || current.partial || current.config_changed);
  const showKnown =
    known &&
    (known.exceeds_threshold ||
      known.over_threshold_count ||
      known.partial ||
      known.config_changed ||
      known.expired ||
      known.history_changed);
  if (!showCurrent && !showKnown) return null;
  return (
    <section className={ui.notice} aria-label="存储占用提醒" role="status">
      <strong>存储占用提醒</strong> <Link to="/settings/storage">查看存储与重新扫描</Link>
      {showCurrent && (
        <p>
          当前项目上次扫描 {storageSize(current.total_bytes)}；当时阈值{' '}
          {storageSize(current.project_threshold_bytes)}
          {current.exceeds_threshold ? '，已超过阈值' : ''}。扫描时间 {current.as_of}。
          {current.partial && '统计不完整，仅为已读取部分。'}
          {current.config_changed && '阈值已修改，请重新扫描。'}
        </p>
      )}
      {showKnown && (
        <p>
          {known.scope === 'all_known' ? '当时全部最近项目' : '选定项目范围'}（{known.project_count}{' '}
          个）上次已统计 {storageSize(known.observed_total_bytes)}，当时总量阈值{' '}
          {storageSize(known.total_threshold_bytes)}
          {known.exceeds_threshold ? '，已超过阈值' : ''}；{known.over_threshold_count}{' '}
          个项目超过当时单项目阈值。扫描时间 {known.as_of}。
          {known.partial && `统计不完整，${known.unavailable_count} 个项目不可访问，未按零占用计算。`}
          {known.config_changed && '阈值已修改，请重新扫描。'}
          {known.history_changed && '最近项目记录已变化，请重新扫描。'}
          {known.expired && '明细快照已过期，请重新扫描。'}
        </p>
      )}
      <p>这是历史统计，当前占用可能已变化；不会自动扫描或清理文件。</p>
    </section>
  );
}
