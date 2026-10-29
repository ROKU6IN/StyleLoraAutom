import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link, useSearchParams } from 'react-router';
import { request, waitOperation } from '../api/client';
import { errorText } from '../api/errors';
import type { Accepted, Job, Page } from '../api/types';
import { useAction } from '../hooks/useAction';
import { useSelection } from '../state/selection';
import { canWrite, useSession } from '../state/session';
import { projectKey, queryClient } from '../state/queryClient';
import { Button, ErrorBox, Field, ui } from '../ui';
import { JobDetails } from './Jobs';

export default function VisionJobs({ pid }: { pid: string }) {
  const action = useAction(),
    [params, setParams] = useSearchParams();
  const selected = useSelection((s) => s.selection),
    selectionPid = useSelection((s) => s.pid);
  const ids = selectionPid === pid && selected.kind === 'ids' ? [...selected.ids].sort((a, b) => a - b) : [];
  const connection = useSession((s) => s.connection);
  const [mode, setMode] = useState('uncovered'),
    [fallback, setFallback] = useState('normal');
  const [rating, setRating] = useState(false),
    [includeRejected, setIncludeRejected] = useState(false);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [reclassified, setReclassified] = useState<
    { image_id: number; status: string; reason?: string }[] | null
  >(null);
  const attempt = useRef<{ fingerprint: string; batch: string } | null>(null);
  const caps = useQuery({
    queryKey: ['llm', 'executors'],
    queryFn: ({ signal }) =>
      request<{ llm_vision: { available: boolean } }>('GET', '/api/executors/capabilities', undefined, {
        signal,
      }),
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  const cursor = cursors[cursors.length - 1];
  const list = useQuery({
    queryKey: projectKey(pid, 'vision-jobs', cursor),
    queryFn: ({ signal }) =>
      request<Page<Job>>(
        'GET',
        `/api/projects/${pid}/jobs?executor=llm_vision&limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
        undefined,
        { signal },
      ),
    refetchInterval: connection === 'reauth' ? false : 1500,
  });
  const chosen = Number(params.get('job'));
  const scope =
    mode === 'selected'
      ? { selected_ids: ids }
      : {
          filter: {
            review_statuses: includeRejected ? ['keep', 'unreviewed', 'reject'] : null,
            include_missing: true,
          },
        };
  const config = { dataset_mode_policy: 'style', use_rating_model: rating, fallback };
  const allowed =
    canWrite() && !action.busy && (mode !== 'selected' || (ids.length > 0 && ids.length <= 5000));
  async function start() {
    const body = { executors: ['llm_vision'], mode, scope, config };
    const fingerprint = JSON.stringify(body);
    if (attempt.current?.fingerprint !== fingerprint)
      attempt.current = { fingerprint, batch: crypto.randomUUID() };
    const result = await request<{ items: Job[] }>('POST', `/api/projects/${pid}/jobs/batches`, {
      ...body,
      batch_id: attempt.current.batch,
    });
    attempt.current = null;
    setParams({ job: String(result.items[0].job_id) });
    setCursors([null]);
    await queryClient.invalidateQueries({ queryKey: ['project', pid] });
  }
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>ANNOTATIONS / VISION</div>
          <h1>视觉描述任务</h1>
          <p>按图片评级选择角色，将标签与自然语言成组保存到 LLM 层。</p>
        </div>
        <div className={ui.row}>
          <Link to="/settings/llm">配置 LLM 角色</Link>
          <Link to="/settings/vision-policy">视觉关键词</Link>
        </div>
      </div>
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : caps.error
              ? errorText(caps.error)
              : list.error
                ? errorText(list.error)
                : undefined
        }
      />
      {!caps.data?.llm_vision.available && (
        <p className={ui.notice}>请先配置支持图片的普通视觉角色。NSFW 图片需要单独配置对应角色，否则跳过。</p>
      )}
      <section className={ui.section} aria-label="新建视觉任务">
        <h2>新建视觉任务</h2>
        <div className={ui.formGrid}>
          <Field id="vision-mode" label="视觉任务范围">
            <select id="vision-mode" value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="uncovered">项目范围，仅补全当前未覆盖</option>
              <option value="all">项目范围，全部重新描述</option>
              <option value="selected" disabled={!ids.length}>
                工作台选中图片（{ids.length} 张）
              </option>
            </select>
          </Field>
          <Field id="vision-fallback" label="评级无法确定时">
            <select id="vision-fallback" value={fallback} onChange={(e) => setFallback(e.target.value)}>
              <option value="normal">普通角色，标记为未验证</option>
              <option value="nsfwfallback">NSFW 角色，保留未验证状态</option>
            </select>
          </Field>
        </div>
        <label className={ui.row}>
          <input type="checkbox" checked={rating} onChange={(e) => setRating(e.target.checked)} />
          关键词未命中时调用分级模型
        </label>
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
        <p className={ui.notice}>
          点击开始会向已配置的模型服务发送压缩图片与原始/WD
          标签，可能产生费用。启用分级模型后，还会发送原始自然语言。人工层和规则层不作为模型输入。
        </p>
        <p className={ui.muted}>
          仅支持风格数据集；单次最多 5000 张，同时处理最多 2
          张。暂停会完成正在处理的图片，取消会丢弃未提交结果。全部重新描述会再次调用模型。
        </p>
        <div className={ui.row}>
          <Button
            primary
            disabled={!allowed || !caps.data?.llm_vision.available}
            onClick={() => void action.run(start)}
          >
            开始视觉描述
          </Button>
          <Button
            disabled={!allowed}
            onClick={() =>
              void action.run(async () => {
                const accepted = await request<Accepted>('POST', `/api/projects/${pid}/ratings/reclassify`, {
                  scope,
                  config,
                });
                const result = await waitOperation<{
                  items: { image_id: number; status: string; reason?: string }[];
                }>(accepted);
                setReclassified(result.items);
                await queryClient.invalidateQueries({ queryKey: ['project', pid] });
              })
            }
          >
            仅重新分级
          </Button>
        </div>
        <p className={ui.caption}>
          重新分级只清除可重算的评级，保留人工评级和拒答评级；不生成描述。启用分级模型时可能产生费用。
        </p>
        {action.busy && <p role="status">正在提交或重新分级，可在任务进度查看运行状态。</p>}
        {reclassified && (
          <p role="status">
            重新分级完成：处理 {reclassified.length} 张，失败{' '}
            {reclassified.filter((i) => i.status === 'failed').length} 张，保留{' '}
            {reclassified.filter((i) => i.status === 'preserved').length} 张。
          </p>
        )}
      </section>
      {Number.isSafeInteger(chosen) && chosen > 0 && <JobDetails key={chosen} pid={pid} id={chosen} />}
      <section className={ui.section}>
        <h2>视觉任务记录</h2>
        <div className={ui.stack}>
          {list.data?.items.map((job) => (
            <div className={ui.between} key={job.job_id}>
              <span>
                #{job.job_id} ·{' '}
                {
                  {
                    pending: '等待',
                    running: '运行中',
                    paused: '已暂停',
                    done: '已完成',
                    failed: '失败',
                    canceled: '已取消',
                  }[job.status]
                }{' '}
                · {job.done_count + job.failed_count + job.skipped_count}/{job.total}
              </span>
              <Button quiet onClick={() => setParams({ job: String(job.job_id) })}>
                查看任务 {job.job_id}
              </Button>
            </div>
          ))}
        </div>
        {list.data?.items.length === 0 && <p>尚无视觉任务。</p>}
        {(cursors.length > 1 || list.data?.has_more) && (
          <div className={ui.row}>
            <Button disabled={cursors.length === 1} onClick={() => setCursors((v) => v.slice(0, -1))}>
              上一页任务
            </Button>
            <span>第 {cursors.length} 页</span>
            <Button
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
