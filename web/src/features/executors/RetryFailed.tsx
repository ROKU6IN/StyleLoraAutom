import { useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { request } from '../../api/client';
import { AppError, errorText } from '../../api/errors';
import { projectKey, queryClient } from '../../state/queryClient';
import { canWrite } from '../../state/session';
import { useAction } from '../../hooks/useAction';
import { Button, ErrorBox, Modal, ui } from '../../ui';

export interface RetryPreview {
  source_job_id: number;
  executor: string;
  image_count: number;
  model: string | null;
  preview_digest: string;
  available: boolean;
  reason: string | null;
}
export default function RetryFailed({
  pid,
  id,
  onCreated,
}: {
  pid: string;
  id: number;
  onCreated?: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const action = useAction(),
    navigate = useNavigate();
  const attempt = useRef<{ digest: string; batch: string } | null>(null);
  const base = `/api/projects/${pid}/jobs/${id}`;
  const preview = useQuery({
    queryKey: projectKey(pid, 'job-retry-preview', id),
    queryFn: ({ signal }) => request<RetryPreview>('GET', base + '/retry-preview', undefined, { signal }),
    enabled: open,
    staleTime: 0,
  });
  async function submit() {
    const digest = preview.data!.preview_digest;
    if (attempt.current?.digest !== digest) attempt.current = { digest, batch: crypto.randomUUID() };
    const result = await request<{ batch_id: string }>('POST', base + '/retry', {
      batch_id: attempt.current.batch,
      preview_digest: digest,
    });
    attempt.current = null;
    setOpen(false);
    if (onCreated) onCreated(result.batch_id);
    else void navigate(`/p/${pid}/batches?batch=${result.batch_id}`);
    await queryClient.invalidateQueries({ queryKey: ['project', pid] });
  }
  return (
    <>
      <Button
        quiet
        disabled={!canWrite()}
        onClick={() => {
          action.setError(null);
          setOpen(true);
        }}
      >
        仅重跑失败项
      </Button>
      <Modal
        open={open}
        onOpenChange={(next) => {
          if (!action.busy) setOpen(next);
        }}
        title={`重跑任务 #${id} 的失败图片`}
        description="新建单阶段任务，沿用原任务的冻结配置；原结果记录保持不变。"
      >
        {preview.data && (
          <>
            <p>
              {preview.data.executor === 'wd' ? 'WD 标签' : 'LLM 视觉描述'} · {preview.data.image_count}{' '}
              张失败图片
            </p>
            <p className={ui.mono}>{preview.data.model}</p>
            <p>
              仅本阶段明确失败的图片会重新执行，忽略旧缓存；成功、跳过和未执行图片不包含在内。原组合批次必须已结束。
            </p>
            {preview.data.executor === 'llm_vision' && (
              <p className={ui.notice}>将向原配置的模型服务发送图片和证据，可能再次产生费用。</p>
            )}
          </>
        )}
        <ErrorBox
          message={
            action.error
              ? errorText(action.error)
              : preview.error
                ? errorText(preview.error)
                : preview.data?.reason
                  ? errorText(new AppError(409, preview.data.reason))
                  : undefined
          }
        />
        <div className={ui.actions}>
          <Button disabled={action.busy} onClick={() => setOpen(false)}>
            返回检查
          </Button>
          <Button disabled={action.busy || preview.isFetching} onClick={() => void preview.refetch()}>
            刷新重试预览
          </Button>
          <Button
            primary
            disabled={
              !canWrite() ||
              action.busy ||
              preview.isFetching ||
              !preview.data?.available ||
              Boolean(preview.error)
            }
            onClick={() => void action.run(submit)}
          >
            确认重跑失败项
          </Button>
        </div>
      </Modal>
    </>
  );
}
