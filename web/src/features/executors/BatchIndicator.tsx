import { useQuery } from '@tanstack/react-query';
import { request } from '../../api/client';
import type { JobBatchView } from '../../api/types';
import { projectKey } from '../../state/queryClient';
import { useSession } from '../../state/session';
import { Button } from '../../ui';

export default function BatchIndicator({ pid, id, onOpen }: { pid: string; id: string; onOpen: () => void }) {
  const connection = useSession((s) => s.connection);
  const query = useQuery({
    queryKey: projectKey(pid, 'job-batch', id),
    queryFn: ({ signal }) =>
      request<JobBatchView>('GET', `/api/projects/${pid}/jobs/batches/${id}`, undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 1500,
  });
  const row = query.data;
  const statuses: Record<string, string> = {
    pending: '等待',
    running: '运行中',
    paused: '已暂停',
    done: '已完成',
    failed: '失败',
    canceled: '已取消',
    blocked: '前序阻塞',
  };
  return (
    <Button small quiet onClick={onOpen} aria-label="查看当前标注批次">
      标注批次 ·{' '}
      {row
        ? `${statuses[row.status]} ${row.done_count + row.failed_count + row.skipped_count}/${row.total}`
        : query.error
          ? '状态待核查'
          : '读取中'}
    </Button>
  );
}
