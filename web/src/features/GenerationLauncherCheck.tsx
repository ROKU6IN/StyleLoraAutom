import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { projectPath } from '../api/endpoints';
import { AppError, errorText } from '../api/errors';
import type { Accepted, Operation } from '../api/types';
import { useAction } from '../hooks/useAction';
import { queryClient, projectKey } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, ui } from '../ui';

type Value = string | number | boolean | null;
export interface LauncherCheck {
  source_id: number;
  source_version: string;
  operation_id: string | null;
  stale: boolean;
  result: null | {
    source_version: string;
    status: 'checked' | 'unavailable';
    reason: string | null;
    checked_at: string;
    settings_match: boolean | null;
    generation_enabled: false;
    launcher_version: string | null;
    fields: {
      key: string;
      requested: Value;
      expected: Value;
      observed: Value;
      matches: boolean;
      locked: boolean;
    }[];
    issues: string[];
    warnings: string[];
    unverified: string[];
    request_sizes: Record<string, { width: number; height: number }>;
    default_aspect: string;
  };
}
const unverified: Record<string, string> = {
  effective_parameters: '实际准备结果中的有效参数与提示词',
  inherited_references: '页面继承的参考图是否为空',
  batch_size: '页面批量设置是否确实只生成一张',
  resolution_support: '所选模型是否支持来源要求的尺寸',
  atomic_settings_update: '修改与恢复设置时的并发保护',
  durable_attempt_outcome: '具体生成任务的持久结果与恢复证据',
};
const fields: Record<string, string> = {
  model: '模型',
  effort: 'Effort',
  sampler: '采样器',
  steps: '步数',
  scale: 'CFG',
  cfg_rescale: 'CFG Rescale',
  noise_schedule: '噪声调度',
  uc_preset: '负面预设',
  quality_toggle: '质量词',
  smea: 'SMEA',
  smea_dyn: 'SMEA Dyn',
  variety_plus: 'Variety+',
  decrisp: 'Decrisp',
};
function display(value: Value) {
  return value === null
    ? 'None（无预设）'
    : typeof value === 'boolean'
      ? value
        ? '开启'
        : '关闭'
      : String(value);
}
export default function GenerationLauncherCheck({
  pid,
  sourceId,
  sourceVersion,
  disabled,
}: {
  pid: string;
  sourceId: number;
  sourceVersion: string;
  disabled: boolean;
}) {
  const action = useAction(),
    cancel = useAction();
  const connection = useSession((s) => s.connection);
  const key = projectKey(pid, 'launcher-check', sourceId);
  const path = `${projectPath(pid)}/sources/${sourceId}/launcher-check`;
  const snapshot = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => request<LauncherCheck>('GET', path, undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 2000,
  });
  const id = snapshot.data?.operation_id;
  const operation = useQuery({
    queryKey: [...key, 'operation', id],
    queryFn: ({ signal }) => request<Operation>('GET', `/api/operations/${id}`, undefined, { signal }),
    enabled: !!id && connection !== 'reauth',
    refetchInterval: (query) =>
      ['pending', 'running'].includes(query.state.data?.status ?? 'pending') ? 500 : false,
  });
  const running = !!id && (!operation.data || ['pending', 'running'].includes(operation.data.status));
  const value = snapshot.data?.result;
  const stale = !!value && (snapshot.data?.stale || value.source_version !== sourceVersion);
  return (
    <section className={ui.section} aria-label="来源 Launcher 参数预检">
      <div className={ui.between}>
        <h3>Launcher 参数预检</h3>
        <Button
          disabled={disabled || !canWrite() || action.busy || running}
          onClick={() =>
            void action.run(async () => {
              await request<Accepted>('POST', path, {}, { version: sourceVersion });
              await queryClient.invalidateQueries({ queryKey: key });
            })
          }
        >
          {running ? '正在预检…' : '读取并比较 Launcher 参数'}
        </Button>
      </div>
      <p>
        比较已保存来源与 Launcher v4.3.1 的当前设置。只读取设置与状态，不修改任何参数，也不准备或提交生成。
      </p>
      {disabled && <p className={ui.notice}>请先保存来源草稿或等待当前操作完成；未保存的修改不参与预检。</p>}
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : cancel.error
              ? errorText(cancel.error)
              : snapshot.error
                ? errorText(snapshot.error)
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
              await queryClient.invalidateQueries({ queryKey: key });
            })
          }
        >
          取消参数预检
        </Button>
      )}
      {operation.data?.status === 'canceled' && <p role="status">参数预检已取消，保留上次结果。</p>}
      {operation.data?.error && operation.data.status !== 'canceled' && (
        <ErrorBox message={errorText(AppError.from(503, operation.data.error))} />
      )}
      {!value && <p className={ui.muted}>尚未预检此来源。</p>}
      {value && (
        <>
          <p className={ui.caption}>
            最近预检：{new Date(value.checked_at).toLocaleString()}。这是当时的快照，Launcher
            页面变化后请重新预检。
          </p>
          {stale && (
            <p role="status" className={ui.notice}>
              来源版本已变化，以下历史结果不适用于当前来源，请重新预检。
            </p>
          )}
          {value.reason && <ErrorBox message={errorText(new AppError(503, value.reason))} />}
          {value.status === 'checked' && (
            <>
              <p>
                {stale
                  ? '历史参数比较'
                  : value.settings_match
                    ? '可读取的参数一致，仍需完成生成适配。'
                    : '来源参数与 Launcher 当前设置存在差异。'}
              </p>
              <div className={ui.tableWrap}>
                <table>
                  <caption>来源与 Launcher 设置逐项比较</caption>
                  <thead>
                    <tr>
                      <th>参数</th>
                      <th>来源值</th>
                      <th>Launcher 对应值</th>
                      <th>当前读取值</th>
                      <th>比较</th>
                    </tr>
                  </thead>
                  <tbody>
                    {value.fields.map((field) => (
                      <tr key={field.key}>
                        <th scope="row">{fields[field.key] ?? field.key}</th>
                        <td>{display(field.requested)}</td>
                        <td>{display(field.expected)}</td>
                        <td>
                          {field.key === 'effort' && field.observed === null
                            ? '不适用'
                            : display(field.observed)}
                        </td>
                        <td>
                          {field.matches ? '一致' : '不同'}
                          {field.locked && '（当前模型固定）'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className={ui.caption}>
                本项目的无负面预设 None 对应 Launcher 数值 3；V5 Full 的 medium
                对应独立模型变体。这里的对应值只用于比较，不会写入 Launcher。
              </p>
              {value.issues.map((code) => (
                <p className={ui.notice} key={code}>
                  {errorText(new AppError(409, code))}
                </p>
              ))}
              <p>
                来源要求尺寸：
                {Object.entries(value.request_sizes)
                  .map(([aspect, size]) => `${aspect} ${size.width} × ${size.height}`)
                  .join('；')}
                。尺寸随请求传入，不与当前页面尺寸判等。
              </p>
            </>
          )}
          <details>
            <summary>尚不能确认的生成条件</summary>
            <ul>
              {value.unverified.map((key) => (
                <li key={key}>{unverified[key] ?? key}</li>
              ))}
            </ul>
          </details>
          {value.warnings.map((code) => (
            <p className={ui.notice} key={code}>
              {errorText(new AppError(503, code))}
            </p>
          ))}
        </>
      )}
      <p className={ui.notice}>
        参数相同不代表可以生成。当前生成适配尚未启用，预检不会锁定来源、授权费用或解除恢复阻塞。
      </p>
    </section>
  );
}
