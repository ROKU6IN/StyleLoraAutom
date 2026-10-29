import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { AppError, errorText } from '../api/errors';
import type { Accepted, BackendRegistry, Operation } from '../api/types';
import { useAction } from '../hooks/useAction';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, EmptyState, ErrorBox, ui } from '../ui';
import SettingsLease from './SettingsLease';
import LauncherInspection from './LauncherInspection';

export default function Launcher() {
  const action = useAction(),
    cancel = useAction(),
    connection = useSession((s) => s.connection);
  const registry = useQuery({
    queryKey: ['backends'],
    queryFn: ({ signal }) => request<BackendRegistry>('GET', '/api/backends', undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  const id = registry.data?.probe_operation_id;
  const operation = useQuery({
    queryKey: ['operations', 'backend', id],
    queryFn: ({ signal }) => request<Operation>('GET', `/api/operations/${id}`, undefined, { signal }),
    enabled: !!id && connection !== 'reauth',
    refetchInterval: (query) =>
      ['pending', 'running'].includes(query.state.data?.status ?? 'pending') ? 500 : false,
  });
  const running = !!id && (!operation.data || ['pending', 'running'].includes(operation.data.status));
  const backend = registry.data?.items.find((item) => item.backend_id === 'nai_launcher');
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>BACKEND / LAUNCHER</div>
          <h1>Launcher / MCP</h1>
          <p>检查本机 Launcher 的连接和版本兼容性。检查结束后会关闭本次诊断会话。</p>
        </div>
        <Button
          disabled={!canWrite() || action.busy || running}
          onClick={() =>
            void action.run(async () => {
              await request<Accepted>('POST', '/api/backends/nai_launcher/probe', {});
              await queryClient.invalidateQueries({ queryKey: ['backends'] });
            })
          }
        >
          {running ? '正在检查…' : '检查连接'}
        </Button>
      </div>
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : cancel.error
              ? errorText(cancel.error)
              : registry.error
                ? errorText(registry.error)
                : operation.error
                  ? errorText(operation.error)
                  : undefined
        }
      />
      {running && (
        <section className={ui.section} aria-label="连接检查任务">
          <div className={ui.between}>
            <p role="status">正在读取发现文件、完成握手并核对工具结构…</p>
            <Button
              quiet
              disabled={!operation.data || cancel.busy || !canWrite()}
              onClick={() =>
                void cancel.run(async () => {
                  await request(
                    'POST',
                    `/api/operations/${id}/cancel`,
                    {},
                    { version: operation.data!.version },
                  );
                  await queryClient.invalidateQueries({ queryKey: ['operations', 'backend'] });
                })
              }
            >
              取消检查
            </Button>
          </div>
        </section>
      )}
      {operation.data?.status === 'canceled' && (
        <p role="status" className={ui.notice}>
          连接检查已取消，保留上次检查结果。
        </p>
      )}
      {operation.data?.error && operation.data.status !== 'canceled' && (
        <ErrorBox message={errorText(AppError.from(503, operation.data.error))} />
      )}
      <SettingsLease />
      <LauncherInspection registry={registry.data} />
      {!backend ? (
        <EmptyState title="正在读取连接状态" description="请稍候…" />
      ) : (
        <>
          <section className={ui.section} aria-label="连接诊断">
            <div className={ui.between}>
              <h2>
                {backend.status === 'not_checked'
                  ? '尚未检查'
                  : backend.reachable
                    ? '连接检查通过'
                    : '暂时无法连接'}
              </h2>
              <span className={ui.tag}>
                {backend.transport_verified ? '协议兼容性已验证' : '兼容性未验证'}
              </span>
            </div>
            <p>{errorText(new AppError(503, backend.reason))}</p>
            <p className={ui.caption}>
              最近检查：{backend.checked_at ? new Date(backend.checked_at).toLocaleString() : '尚无记录'}
              。这里显示上次检查结果，Launcher 状态变化后请重新检查。
            </p>
            <div className={ui.formGrid}>
              <div>
                <h3>Launcher 版本</h3>
                <p>{backend.app_version ?? '—'}</p>
              </div>
              <div>
                <h3>MCP 服务版本</h3>
                <p>{backend.server_version ?? '—'}</p>
              </div>
              <div>
                <h3>协议版本</h3>
                <p>{backend.protocol_version ?? '—'}</p>
              </div>
              <div>
                <h3>发现的工具</h3>
                <p>{backend.tool_count} 个</p>
              </div>
            </div>
            {backend.schema_hash && (
              <details>
                <summary>查看兼容性诊断</summary>
                <p>
                  核心工具：{backend.core_tools.length} / 8；缺少 {backend.missing_core_tools.length} 个。
                </p>
                {backend.missing_core_tools.map((name) => (
                  <p key={name}>
                    <code>{name}</code>
                  </p>
                ))}
                <label className={ui.stack}>
                  核心工具结构摘要
                  <input readOnly value={backend.schema_hash} />
                </label>
                <label className={ui.stack}>
                  完整工具结构摘要
                  <input readOnly value={backend.full_schema_hash ?? ''} />
                </label>
              </details>
            )}
            {backend.warnings.map((warning) => (
              <p className={ui.notice} key={warning}>
                {errorText(new AppError(503, warning))}
              </p>
            ))}
          </section>
          <section className={ui.section}>
            <h2>生成尚未启用</h2>
            <p>
              当前版本提供连接诊断和 v4.3.1 设置与状态读取。图片生成、Launcher
              词库搜索和反推图像将在对应适配器验证完成后启用。
            </p>
            <p className={ui.muted}>
              启动 Launcher 的 MCP 服务后点击“检查连接”。凭据从本机运行环境读取，无需在此输入。
            </p>
          </section>
        </>
      )}
    </div>
  );
}
