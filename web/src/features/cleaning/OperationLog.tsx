import { useState } from 'react';
import { useQuery, useInfiniteQuery } from '@tanstack/react-query';
import { request, allPages } from '../../api/client';
import { projectPath } from '../../api/endpoints';
import type { Page } from '../../api/types';
import { errorText } from '../../api/errors';
import { useAction } from '../../hooks/useAction';
import { queryClient, projectKey } from '../../state/queryClient';
import { canWrite, useSession } from '../../state/session';
import { Button, ErrorBox, HoldButton, ui } from '../../ui';
import type { LogItem } from './types';

export default function OperationLog({ pid }: { pid: string }) {
  const [selected, setSelected] = useState<number | null>(null);
  const path = `${projectPath(pid)}/operation-log`;
  const logs = useQuery({
    queryKey: projectKey(pid, 'rule-log'),
    queryFn: ({ signal }) => allPages<LogItem>(path, signal),
  });
  const details = useInfiniteQuery({
    queryKey: projectKey(pid, 'rule-changes', selected),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) =>
      request<Page<{ image_id: number; new_annotation_id: number; previous_annotation_id: number | null }>>(
        'GET',
        `${path}/${selected}?limit=100${pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ''}`,
        undefined,
        { signal },
      ),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: selected !== null,
  });
  const latest = Math.max(0, ...(logs.data?.filter((r) => r.status === 'applied').map((r) => r.op_id) ?? []));
  const action = useAction();
  useSession((s) => s.connection);
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>OPERATION HISTORY</div>
          <h1>规则操作日志</h1>
          <p>仅最近一次未撤销的规则可以撤销。后续人工或模型标注发生变化时，整批撤销会被拒绝。</p>
        </div>
      </div>
      <ErrorBox
        message={action.error ? errorText(action.error) : logs.error ? errorText(logs.error) : undefined}
      />
      <section className={ui.section}>
        {!logs.data?.length && <p>尚无规则变更。</p>}
        {[...(logs.data ?? [])].reverse().map((row) => (
          <div key={row.op_id} className={ui.between}>
            <div>
              <h2>
                操作 #{row.op_id} · {row.status === 'applied' ? '已应用' : '已撤销'}
              </h2>
              <p>
                {row.affected_count} 张 · {row.created_at}
              </p>
            </div>
            <div className={ui.row}>
              <Button onClick={() => setSelected(row.op_id)}>查看变更 #{row.op_id}</Button>
              <HoldButton
                disabled={row.op_id !== latest || action.busy || !canWrite()}
                onConfirm={() =>
                  void action.run(async () => {
                    await request('POST', `${path}/${row.op_id}/undo`, {}, { version: row.version });
                    await queryClient.invalidateQueries({ queryKey: ['project', pid] });
                  })
                }
              >
                撤销操作 #{row.op_id}
              </HoldButton>
            </div>
          </div>
        ))}
      </section>
      {selected !== null && (
        <section className={ui.section}>
          <h2>操作 #{selected} 的图片变更</h2>
          <ErrorBox message={details.error ? errorText(details.error) : undefined} />
          <div className={ui.scroll}>
            <table>
              <thead>
                <tr>
                  <th>图片</th>
                  <th>之前的规则层</th>
                  <th>此次规则层</th>
                </tr>
              </thead>
              <tbody>
                {details.data?.pages
                  .flatMap((p) => p.items)
                  .map((row) => (
                    <tr key={row.image_id}>
                      <td>#{row.image_id}</td>
                      <td>{row.previous_annotation_id ?? '无'}</td>
                      <td>{row.new_annotation_id}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {details.hasNextPage && (
            <Button disabled={details.isFetchingNextPage} onClick={() => void details.fetchNextPage()}>
              加载更多变更
            </Button>
          )}
        </section>
      )}
    </div>
  );
}
