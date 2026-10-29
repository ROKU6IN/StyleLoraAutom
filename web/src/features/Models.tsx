import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { AppError, errorText } from '../api/errors';
import type { Accepted, Installation, ModelEntry, Page, TaggerCapabilities } from '../api/types';
import { useAction } from '../hooks/useAction';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, EmptyState, ErrorBox, TaskProgress, ui } from '../ui';

const states: Record<string, string> = {
  missing: '未安装',
  unverified: '待重新校验',
  ready: '文件已校验',
  invalid: '文件不可用',
  installing: '安装进行中',
};
const phases: Record<string, string> = {
  queued: '等待安装',
  downloading: '下载文件',
  verifying_files: '核对文件摘要',
  verifying_csv: '校验标签表',
  verifying_tensors: '校验模型结构',
  publishing: '完成安装',
  ready: '校验完成',
};
export function modelBytes(bytes: number) {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024 / 1024).toFixed(2)} MiB`;
}

function ModelCard({ model, enabled }: { model: ModelEntry; enabled: boolean }) {
  const action = useAction(),
    cancel = useAction();
  const connection = useSession((s) => s.connection);
  const id = model.installation_id;
  const operation = useQuery({
    queryKey: ['tagger-installation', id],
    queryFn: ({ signal }) =>
      request<Installation>('GET', `/api/tagger/installations/${id}`, undefined, { signal }),
    enabled: !!id && connection !== 'reauth',
    refetchOnMount: 'always',
    refetchInterval: (q) =>
      ['pending', 'running'].includes(q.state.data?.status ?? 'pending') ? 500 : false,
  });
  const running = model.state === 'installing';
  const task = operation.data;
  return (
    <section className={ui.section} aria-label={`模型 ${model.model_id}`}>
      <div className={ui.between}>
        <h2>{model.model_id}</h2>
        <span className={ui.badge}>{states[model.state]}</span>
      </div>
      <p>
        {model.repo_id} · {modelBytes(model.total_bytes)}
      </p>
      <p className={ui.caption}>
        固定版本：<code>{model.revision}</code>
      </p>
      <details>
        <summary>文件与校验信息</summary>
        {model.provenance && (
          <p>
            权重使用发布者 SHA-256；标签表先核验官方固定版本的 Git 文件摘要，再使用审核固定的 SHA-256。
            许可证：{model.provenance.license} · 审核日期：{model.provenance.reviewed_at}
          </p>
        )}
        <p className={ui.caption}>
          清单：<code>{model.manifest_id}</code>
        </p>
        {model.files.map((file) => (
          <p className={ui.caption} key={file.name}>
            {file.name} · {modelBytes(file.size)}
            <br />
            <code>{file.sha256}</code>
          </p>
        ))}
        {model.verified_at && <p>本次会话校验时间：{model.verified_at}</p>}
      </details>
      <ErrorBox
        message={
          action.error
            ? errorText(action.error)
            : cancel.error
              ? errorText(cancel.error)
              : operation.error
                ? errorText(operation.error)
                : task?.error && task.status !== 'canceled'
                  ? errorText(AppError.from(503, task.error))
                  : model.reason
                    ? reasonText(model.reason)
                    : undefined
        }
      />
      {task && running && (
        <TaskProgress
          done={task.completed}
          total={task.total}
          label={`${phases[task.phase] ?? '正在处理'}（字节）`}
        />
      )}
      {task?.status === 'canceled' && <p role="status">安装已取消，可以重新开始。</p>}
      {task?.status === 'done' && model.state === 'ready' && (
        <p role="status">模型和标签表已校验，可在项目的“WD 标注任务”中开始标注。</p>
      )}
      <div className={ui.row}>
        <Button
          disabled={!enabled || !canWrite() || action.busy || running}
          onClick={() =>
            void action.run(async () => {
              await request<Accepted>('POST', '/api/tagger/models/install', {
                manifest_id: model.manifest_id,
              });
              await queryClient.invalidateQueries({ queryKey: ['tagger-models'] });
            })
          }
        >
          {model.state === 'missing' ? '下载并安装' : '重新校验文件'}
        </Button>
        {running && id && (
          <Button
            quiet
            disabled={!canWrite() || cancel.busy}
            onClick={() =>
              void cancel.run(async () => {
                const fresh = await request<Installation>('GET', `/api/tagger/installations/${id}`);
                await request('DELETE', `/api/tagger/installations/${id}`, undefined, {
                  version: fresh.version,
                });
                await queryClient.invalidateQueries({ queryKey: ['tagger-installation', id] });
                await queryClient.invalidateQueries({ queryKey: ['tagger-models'] });
              })
            }
          >
            取消安装
          </Button>
        )}
      </div>
    </section>
  );
}

export function reasonText(code: string) {
  const reasons: Record<string, string> = {
    'tagger:manifest_unconfigured': '尚无已审核的模型清单。需要先核验发布者的固定版本、文件摘要和模型结构。',
    'tagger:cuda_not_verified': '已检测到 NVIDIA 显存，CUDA 模型会话尚未验证。',
    'tagger:cuda_unavailable': '未检测到可用的 NVIDIA 显存信息。',
  };
  return reasons[code] ?? errorText(new AppError(503, code));
}

export default function Models() {
  const connection = useSession((s) => s.connection);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const cursor = cursors[cursors.length - 1];
  const caps = useQuery({
    queryKey: ['tagger-capabilities'],
    queryFn: ({ signal }) =>
      request<TaggerCapabilities>('GET', '/api/tagger/capabilities', undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 30000,
  });
  const models = useQuery({
    queryKey: ['tagger-models', cursor],
    queryFn: ({ signal }) =>
      request<Page<ModelEntry>>(
        'GET',
        `/api/tagger/models?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
        undefined,
        { signal },
      ),
    refetchInterval: connection === 'reauth' ? false : 1500,
  });
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>MODELS / WD</div>
          <h1>WD 模型管理</h1>
          <p>手动安装固定版本，校验模型和标签表后保存在本机。</p>
        </div>
      </div>
      <ErrorBox
        message={caps.error ? errorText(caps.error) : models.error ? errorText(models.error) : undefined}
      />
      <section className={ui.section} aria-label="运行环境">
        <h2>运行环境</h2>
        {caps.data && (
          <>
            <p>
              ONNX Runtime：{caps.data.runtime.available ? `可用 · ${caps.data.runtime.version}` : '不可用'}
            </p>
            {caps.data.runtime.reason && <p className={ui.notice}>{reasonText(caps.data.runtime.reason)}</p>}
            <p>CPU 批大小 1 · GPU 批大小上限 {caps.data.max_batch_size ?? 4}</p>
            <p className={ui.muted}>
              {caps.data.cuda_profiles?.length
                ? `匹配本机的已验收 CUDA 模型档案：${caps.data.cuda_profiles.length} 个。自动策略在加载模型时验证会话，并按空闲显存调整批次。`
                : '当前没有匹配的 CUDA 模型档案，自动策略最多每次一图。'}
            </p>
            {!caps.data.cuda_profiles?.length && (
              <p className={ui.muted}>{reasonText(caps.data.device.reason)}</p>
            )}
            {caps.data.device.total_bytes !== null && (
              <p>
                显存可用 {modelBytes(caps.data.device.free_bytes ?? 0)} /{' '}
                {modelBytes(caps.data.device.total_bytes)}（仅设备信息）
              </p>
            )}
          </>
        )}
        <p className={ui.notice}>安装模型不会自动运行标注。请在项目的“WD 标注任务”中选择范围与阈值后开始。</p>
      </section>
      {models.data?.items.length === 0 && (
        <EmptyState title="尚无可安装的可信模型" description={reasonText('tagger:manifest_unconfigured')} />
      )}
      {models.data?.items.map((model) => (
        <ModelCard key={model.manifest_id} model={model} enabled={caps.data?.runtime.available ?? false} />
      ))}
      {(cursors.length > 1 || models.data?.has_more) && (
        <div className={ui.row}>
          <Button quiet disabled={cursors.length === 1} onClick={() => setCursors((v) => v.slice(0, -1))}>
            上一页
          </Button>
          <span>第 {cursors.length} 页</span>
          <Button
            quiet
            disabled={!models.data?.has_more || models.isFetching}
            onClick={() => setCursors((v) => [...v, models.data!.next_cursor])}
          >
            下一页
          </Button>
        </div>
      )}
    </div>
  );
}
