import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { request } from '../../api/client';
import { AppError, errorText } from '../../api/errors';
import type { Accepted, ImageItem, Operation } from '../../api/types';
import { queryClient, projectKey } from '../../state/queryClient';
import { canWrite, useSession } from '../../state/session';
import { Button, ErrorBox, ui } from '../../ui';

type Result = { image: ImageItem; changed: boolean; as_of: string; verified_bytes: number };
type Attempt = {
  key: string;
  version: string;
  accepted?: Accepted;
  uncertain?: boolean;
  reconciled?: boolean;
};

export function VerifyImageFile({ pid, image }: { pid: string; image: ImageItem }) {
  const connection = useSession((s) => s.connection);
  const [submitting, setSubmitting] = useState(false),
    [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false),
    completed = useRef('');
  const key = projectKey(pid, 'file-verification', image.image_id);
  // Keep unknown request identity across selection/navigation within this session.
  const attempt = useQuery<Attempt | null>({
    queryKey: key,
    queryFn: () => queryClient.getQueryData<Attempt | null>(key) ?? null,
    enabled: false,
    initialData: null,
    gcTime: Infinity,
  });
  const accepted = attempt.data?.accepted;
  const operation = useQuery({
    queryKey: ['operations', 'file-verification', pid, accepted?.task.id],
    queryFn: ({ signal }) => request<Operation<Result>>('GET', accepted!.status_url, undefined, { signal }),
    enabled: !!accepted && !['reauth', 'offline'].includes(connection),
    retry: false,
    refetchInterval: (q) =>
      ['pending', 'running'].includes(q.state.data?.status ?? 'pending') ? 500 : false,
  });
  const running = !!accepted && (!operation.data || ['pending', 'running'].includes(operation.data.status));
  const commitUnknown =
    operation.data?.status === 'failed' &&
    operation.data.error?.outcome === 'unknown' &&
    !attempt.data?.reconciled;
  async function reconcile() {
    if (inFlight.current) return;
    inFlight.current = true;
    setSubmitting(true);
    try {
      const fresh = await request<ImageItem>('GET', `/api/projects/${pid}/images/${image.image_id}`);
      queryClient.setQueryData(projectKey(pid, 'image', image.image_id), fresh);
      queryClient.setQueryData(key, { ...attempt.data, reconciled: true });
      void queryClient.invalidateQueries({ queryKey: ['project', pid] });
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }
  useEffect(() => {
    if (
      !accepted ||
      !operation.data ||
      ['pending', 'running'].includes(operation.data.status) ||
      completed.current === accepted.task.id
    )
      return;
    completed.current = accepted.task.id;
    if (operation.data.status === 'done') {
      queryClient.setQueryData(projectKey(pid, 'image', image.image_id), operation.data.result.image);
    }
    void queryClient.invalidateQueries({ queryKey: ['project', pid] });
  }, [accepted, operation.data, pid, image.image_id]);
  async function start(recover = false) {
    if (inFlight.current || !canWrite()) return;
    const original = recover ? attempt.data : { key: crypto.randomUUID(), version: image.version };
    if (!original) return;
    inFlight.current = true;
    setSubmitting(true);
    setError(null);
    queryClient.setQueryData(key, original);
    try {
      const value = await request<Accepted>(
        'POST',
        `/api/projects/${pid}/images/${image.image_id}/verify-file`,
        {},
        { version: original.version, key: original.key },
      );
      queryClient.setQueryData(key, { ...original, accepted: value, uncertain: false });
    } catch (e) {
      setError(e);
      queryClient.setQueryData(key, {
        ...original,
        uncertain: original.uncertain || (e instanceof AppError && e.outcome === 'unknown'),
      });
    } finally {
      inFlight.current = false;
      setSubmitting(false);
    }
  }
  if (!image.file_missing && !attempt.data) return null;
  const failure =
    error ?? operation.error ?? (operation.data?.error ? AppError.from(409, operation.data.error) : null);
  return (
    <section className={ui.section} aria-label="恢复原图核验">
      <h3>恢复原图核验</h3>
      <p>
        请先从备份把原文件放回当前项目的 <code>images/{image.filename}</code>。核对大小和 SHA-256
        通过后解除文件缺失；审图决定与标注保持原样。
      </p>
      <ErrorBox message={failure ? errorText(failure) : undefined} />
      {operation.data?.status === 'done' && (
        <p role="status">
          上次原图核验通过（{operation.data.result.as_of}）。审图状态保持原样；当前文件状态以最新记录为准。
        </p>
      )}
      {operation.data?.status === 'canceled' && <p role="status">核验已取消，请查看当前图片状态。</p>}
      {running && <p role="status">后台正在核验原图，离开页面不会取消任务。</p>}
      {attempt.data?.uncertain && (
        <p role="status">提交结果未知，已停止新核验。可用原请求找回任务，或前往任务记录核对。</p>
      )}
      {commitUnknown && <p role="status">核验提交尚未确认，请先读取图片状态再决定后续操作。</p>}
      <div className={ui.actions}>
        <Button
          disabled={
            submitting ||
            running ||
            commitUnknown ||
            !!attempt.data?.uncertain ||
            !image.file_missing ||
            !canWrite()
          }
          onClick={() => void start()}
        >
          {submitting ? '正在提交…' : '核验已放回的原图'}
        </Button>
        {attempt.data?.uncertain && (
          <Button disabled={submitting || !canWrite()} onClick={() => void start(true)}>
            找回原核验任务
          </Button>
        )}
        {accepted && (
          <Button
            disabled={submitting || ['reauth', 'offline'].includes(connection)}
            onClick={() => void operation.refetch()}
          >
            查询核验结果
          </Button>
        )}
        {commitUnknown && (
          <Button
            disabled={submitting || ['reauth', 'offline'].includes(connection)}
            onClick={() => void reconcile()}
          >
            核对图片状态
          </Button>
        )}
        <Link to="/tasks">查看任务记录</Link>
      </div>
    </section>
  );
}
