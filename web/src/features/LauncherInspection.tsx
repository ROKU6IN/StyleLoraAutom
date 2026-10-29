import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { AppError, errorText } from '../api/errors';
import type { Accepted, BackendRegistry, Operation } from '../api/types';
import { useAction } from '../hooks/useAction';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, ui } from '../ui';

const repository = 'https://github.com/Aaalice233/Aaalice_NAI_Launcher';
const download = repository + '/releases/download/v4.3.1/NAI_Launcher_Windows_4.3.1%2B46_';

export default function LauncherInspection({ registry }: { registry?: BackendRegistry }) {
  const action = useAction(),
    cancel = useAction();
  const connection = useSession((s) => s.connection);
  const id = registry?.inspection_operation_id;
  const operation = useQuery({
    queryKey: ['operations', 'launcher-inspect', id],
    queryFn: ({ signal }) => request<Operation>('GET', `/api/operations/${id}`, undefined, { signal }),
    enabled: !!id && connection !== 'reauth',
    refetchInterval: (query) =>
      ['pending', 'running'].includes(query.state.data?.status ?? 'pending') ? 500 : false,
  });
  const running = !!id && (!operation.data || ['pending', 'running'].includes(operation.data.status));
  const value = registry?.inspection,
    settings = value?.settings,
    activity = value?.activity;
  return (
    <section className={ui.section} aria-label="Launcher 设置与运行状态">
      <div className={ui.between}>
        <h2>设置与运行状态</h2>
        <Button
          disabled={!canWrite() || action.busy || running}
          onClick={() =>
            void action.run(async () => {
              await request<Accepted>('POST', '/api/backends/nai_launcher/inspect', {});
              await queryClient.invalidateQueries({ queryKey: ['backends'] });
            })
          }
        >
          {running ? '正在读取…' : '读取 Launcher 状态'}
        </Button>
      </div>
      <p>支持 Aaalice NAI Launcher v4.3.1。只读取当前设置和队列概况，不会修改设置或生成图片。</p>
      <p>
        在 Launcher 的“设置 → 集成 → MCP”开启服务，然后点击读取。下方结果为上次读取的快照，不会自动刷新
        Launcher。
      </p>
      <p>
        <a href={repository} target="_blank" rel="noreferrer">
          官方源码
        </a>{' '}
        ·{' '}
        <a href={repository + '/releases/tag/v4.3.1'} target="_blank" rel="noreferrer">
          v4.3.1 发布说明
        </a>{' '}
        · <a href={download + 'Portable.zip'}>Windows 便携版</a> ·{' '}
        <a href={download + 'Setup.exe'}>Windows 安装版</a>
      </p>
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : cancel.error
              ? errorText(cancel.error)
              : operation.error
                ? errorText(operation.error)
                : undefined
        }
      />
      {running && (
        <Button
          quiet
          disabled={!canWrite() || !operation.data || cancel.busy}
          onClick={() =>
            void cancel.run(async () => {
              await request('POST', `/api/operations/${id}/cancel`, {}, { version: operation.data!.version });
              await queryClient.invalidateQueries({ queryKey: ['operations', 'launcher-inspect'] });
            })
          }
        >
          取消状态读取
        </Button>
      )}
      {operation.data?.status === 'canceled' && <p role="status">状态读取已取消，保留上次结果。</p>}
      {operation.data?.error && operation.data.status !== 'canceled' && (
        <ErrorBox message={errorText(AppError.from(503, operation.data.error))} />
      )}
      {!value && <p className={ui.muted}>尚未读取 Launcher 设置。</p>}
      {value && <p className={ui.caption}>最近读取：{new Date(value.checked_at).toLocaleString()}</p>}
      {value?.reason && <ErrorBox message={errorText(new AppError(503, value.reason))} />}
      {value?.status === 'ready' && settings && activity && (
        <>
          <div className={ui.formGrid}>
            <div>
              <h3>当前模型</h3>
              <p>{settings.model}</p>
              <p>
                模式：{settings.model_mode}；effort：{settings.effort ?? '不适用'}
              </p>
            </div>
            <div>
              <h3>采样设置</h3>
              <p>
                {settings.sampler} · {settings.steps} 步 · CFG {settings.scale}
              </p>
              <p>
                {settings.width} × {settings.height}
              </p>
            </div>
            <div>
              <h3>生成状态</h3>
              <p>
                {activity.generation.status} · {activity.generation.progress}%
              </p>
            </div>
            <div>
              <h3>Launcher 队列</h3>
              <p>
                等待 {activity.queue.pending} · 已完成 {activity.queue.completed} · 失败{' '}
                {activity.queue.failed}
              </p>
              <p>
                {activity.queue.execution} · {activity.queue.session_progress}%
              </p>
            </div>
          </div>
          <details>
            <summary>模型列表与固定参数</summary>
            <p>固定参数：{settings.locked_by_effort.join('、') || '无'}</p>
            <ul>
              {settings.available_models.map((model, index) => (
                <li key={`${model.id}-${index}`}>
                  {model.id}
                  {model.efforts.length > 0 && `（${model.efforts.join(' / ')}）`}
                </li>
              ))}
            </ul>
          </details>
          <p className={ui.notice}>
            只读检查成功。生成适配仍未启用；队列概况不能用于确认本项目某次生成的结果。
          </p>
        </>
      )}
      {value?.warnings.map((warning) => (
        <p key={warning} className={ui.notice}>
          {errorText(new AppError(503, warning))}
        </p>
      ))}
    </section>
  );
}
