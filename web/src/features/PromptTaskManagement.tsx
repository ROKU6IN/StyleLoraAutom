import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { errorCodeText, errorText } from '../api/errors';
import { canWrite, useSession } from '../state/session';
import { queryClient } from '../state/queryClient';
import { Button, ErrorBox, Field, HoldButton, Modal, ui } from '../ui';

const P = '/api/prompt-production';
type StoredTask = {
  task_id: string;
  revision: number;
  status: string;
  ready: boolean;
  requested: number | null;
  storage_bytes: number;
  can_cleanup: boolean;
  cleanup_reason: string | null;
};
type Storage = {
  items: StoredTask[];
  total: number;
  storage_bytes: number;
  receipt_count: number;
  receipt_bytes: number;
  cleanup_limit: number;
};
type CleanupPlan = {
  tasks: { task_id: string; expected_revision: number }[];
  key: string;
  bytes: number;
  ready: number;
};
const statuses: Record<string, string> = {
  pending: '等待执行',
  running: '运行中',
  paused: '已暂停',
  done: '已完成',
  failed: '失败',
  canceled: '已取消',
  cleanup_pending: '清理待完成',
  unavailable: '文件不可用',
};
export const fileSize = (value = 0) =>
  value < 1024
    ? `${value} B`
    : value < 1024 * 1024
      ? `${(value / 1024).toFixed(1)} KiB`
      : `${(value / (1024 * 1024)).toFixed(1)} MiB`;

export default function PromptTaskManagement({ onCleaned }: { onCleaned: (ids: string[]) => void }) {
  const connection = useSession((s) => s.connection);
  const storage = useQuery({
    queryKey: ['prompt', 'storage'],
    queryFn: ({ signal }) => request<Storage>('GET', P + '/task-storage', undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  const [selected, setSelected] = useState<string[]>([]),
    [page, setPage] = useState(0),
    [plan, setPlan] = useState<CleanupPlan | null>(null),
    [acknowledged, setAcknowledged] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null),
    [notice, setNotice] = useState('');
  const rows = storage.data?.items ?? [];
  const limit = storage.data?.cleanup_limit ?? 200;
  const chosen = rows.filter((row) => selected.includes(row.task_id));
  const offset = Math.min(page, Math.max(0, Math.ceil(rows.length / 50) - 1));
  const open = () => {
    if (!chosen.length || chosen.some((row) => !row.can_cleanup)) return;
    setPlan({
      tasks: chosen.map((row) => ({ task_id: row.task_id, expected_revision: row.revision })),
      key: crypto.randomUUID(),
      bytes: chosen.reduce((n, row) => n + row.storage_bytes, 0),
      ready: chosen.filter((row) => row.ready).length,
    });
    setAcknowledged(false);
    setError(null);
    setNotice('');
  };
  const clean = async () => {
    if (!plan || !acknowledged || busy || !canWrite()) return;
    setBusy(true);
    setError(null);
    try {
      const result = await request<{ task_ids: string[]; count: number }>(
        'POST',
        P + '/task-cleanup',
        { tasks: plan.tasks },
        { key: plan.key },
      );
      onCleaned(result.task_ids);
      setSelected([]);
      setPlan(null);
      for (const id of result.task_ids) {
        queryClient.removeQueries({ queryKey: ['prompt', 'task', id] });
        queryClient.removeQueries({ queryKey: ['prompt', 'outcomes', id] });
      }
      setNotice(`已清理 ${result.count} 个任务，保留最小去重记录。`);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
      await queryClient.invalidateQueries({ queryKey: ['prompt'] });
    }
  };
  return (
    <section className={`${ui.section} ${ui.stack}`} aria-label="提示词任务存储与清理">
      <h2>任务存储与清理</h2>
      <p>
        任务快照与结果 {fileSize(storage.data?.storage_bytes)} · {storage.data?.total ?? 0} 个任务；去重记录{' '}
        {fileSize(storage.data?.receipt_bytes)} · {storage.data?.receipt_count ?? 0} 条。
      </p>
      <p className={ui.muted}>
        不会自动过期删除。请先导出需要保留的结果；运行中或仍有请求返回的任务暂不能清理。
      </p>
      <ErrorBox
        message={storage.error ? errorText(storage.error) : !plan && error ? errorText(error) : undefined}
      />
      {notice && <p role="status">{notice}</p>}
      <div className={ui.actions}>
        <Button
          disabled={busy || !!plan || !canWrite()}
          onClick={() =>
            setSelected(
              rows
                .filter((row) => row.can_cleanup)
                .slice(0, limit)
                .map((row) => row.task_id),
            )
          }
        >
          选择前 {limit} 个可清理任务
        </Button>
        <Button disabled={busy || !!plan} onClick={() => setSelected([])}>
          清空清理选择
        </Button>
        <Button
          danger
          disabled={busy || !!plan || !chosen.length || chosen.some((row) => !row.can_cleanup) || !canWrite()}
          onClick={open}
        >
          清理所选任务（{chosen.length}）
        </Button>
      </div>
      {!!rows.length && (
        <div className={ui.tableWrap}>
          <table className={ui.table}>
            <caption>产品任务文件，不包含项目来源和词典</caption>
            <thead>
              <tr>
                <th>选择</th>
                <th>任务</th>
                <th>状态</th>
                <th>占用</th>
                <th>清理限制</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(offset * 50, offset * 50 + 50).map((row) => (
                <tr key={row.task_id}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`清理任务 ${row.task_id.slice(0, 8)}`}
                      checked={selected.includes(row.task_id)}
                      disabled={
                        busy ||
                        !!plan ||
                        !row.can_cleanup ||
                        (selected.length >= limit && !selected.includes(row.task_id))
                      }
                      onChange={(e) =>
                        setSelected((values) =>
                          e.target.checked
                            ? [...values, row.task_id]
                            : values.filter((id) => id !== row.task_id),
                        )
                      }
                    />
                  </td>
                  <td>{row.task_id.slice(0, 8)}</td>
                  <td>
                    {statuses[row.status] ?? row.status}
                    {row.ready && ' · 已就绪'}
                  </td>
                  <td>{fileSize(row.storage_bytes)}</td>
                  <td>
                    {row.cleanup_reason
                      ? errorCodeText(row.cleanup_reason)
                      : row.status === 'cleanup_pending'
                        ? '保留了删除记录，可继续清理剩余文件'
                        : '可手动清理'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rows.length > 50 && (
        <div className={ui.actions}>
          <Button disabled={offset === 0} onClick={() => setPage(offset - 1)}>
            上一页任务文件
          </Button>
          <span>
            第 {offset + 1} / {Math.ceil(rows.length / 50)} 页
          </span>
          <Button disabled={(offset + 1) * 50 >= rows.length} onClick={() => setPage(offset + 1)}>
            下一页任务文件
          </Button>
        </div>
      )}
      <Modal
        open={!!plan}
        onOpenChange={(value) => {
          if (!value && !busy) setPlan(null);
        }}
        title="确认清理提示词任务"
        description="清理会删除任务快照和结果，不能从任务列表恢复；已启动的生成源与词典不受影响。"
      >
        <p>
          本次 {plan?.tasks.length ?? 0} 个任务，快照占用 {fileSize(plan?.bytes)}，其中 {plan?.ready ?? 0}{' '}
          个已就绪。少量去重记录会保留，防止旧请求再次运行。
        </p>
        <ErrorBox message={error ? errorText(error) : undefined} />
        <label className={ui.row}>
          <input
            type="checkbox"
            checked={acknowledged}
            disabled={busy}
            onChange={(e) => setAcknowledged(e.target.checked)}
          />
          我已保存需要的结果，确认清理这些任务
        </label>
        <div className={ui.actions}>
          <Button disabled={busy} onClick={() => setPlan(null)}>
            返回任务列表
          </Button>
          <HoldButton disabled={busy || !acknowledged || !canWrite()} onConfirm={() => void clean()}>
            {error ? '继续核对并清理' : '按住清理任务'}
          </HoldButton>
        </div>
      </Modal>
    </section>
  );
}

type ExportPreview = {
  task_id: string;
  revision: number;
  preview_id: string;
  digest: string;
  format: string;
  count: number;
  requested: number;
  source_ready: boolean;
  can_export: boolean;
  bytes: number;
  lossy_fields: string[];
  requires_lossy_ack: boolean;
  excluded: Record<string, number>;
  examples: {
    index: number;
    entry: { tags: string[]; natural_language: string | null; aspect: string | null; rating: string | null };
  }[];
};
const lossNames: Record<string, string> = {
  aspect: '图幅',
  rating: '评级',
  structure: '标签与描述结构',
  task_metadata: '任务信息',
  coverage_report: '覆盖报告',
};
export function TaskResultsExport({
  task,
}: {
  task: { task_id: string; revision: number; can_export_results?: boolean };
}) {
  useSession((s) => s.connection);
  const [format, setFormat] = useState('json'),
    [preview, setPreview] = useState<ExportPreview | null>(null),
    [ack, setAck] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState<unknown>(null);
  const exporting = useRef(false);
  const valid =
    preview?.task_id === task.task_id && preview.revision === task.revision && preview.format === format
      ? preview
      : null;
  const act = async (download: boolean) => {
    if (exporting.current || !canWrite() || !task.can_export_results) return;
    exporting.current = true;
    setBusy(true);
    setError(null);
    const path = P + '/tasks/' + task.task_id;
    try {
      if (!download) {
        setPreview(null);
        setAck(false);
        setPreview(
          await request<ExportPreview>('POST', path + '/results-preview', {
            format,
            expected_revision: task.revision,
          }),
        );
      } else if (valid) {
        const value = await request<{ filename: string; media_type: string; content: string }>(
          'POST',
          path + '/results-export',
          { preview_id: valid.preview_id, digest: valid.digest, acknowledge_lossy: ack },
        );
        const url = URL.createObjectURL(
          new Blob([value.content], { type: value.media_type + ';charset=utf-8' }),
        );
        const link = document.createElement('a');
        link.href = url;
        link.download = value.filename;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    } catch (failure) {
      setError(failure);
    } finally {
      exporting.current = false;
      setBusy(false);
    }
  };
  return (
    <section className={`${ui.section} ${ui.stack}`} aria-label="合格结果导出">
      <h4>导出合格结果</h4>
      <p>
        可导出已落盘且通过检查的部分结果。文件不替代完整 seeds，也不会使不足数或覆盖未通过的任务变为就绪。
      </p>
      {!task.can_export_results && (
        <p className={ui.notice}>请先暂停或结束任务，并等待在途请求和结果保存完成。</p>
      )}
      <ErrorBox message={error ? errorText(error) : undefined} />
      <Field id="task-results-format" label="结果文件格式">
        <select
          id="task-results-format"
          value={format}
          disabled={busy}
          onChange={(e) => {
            setFormat(e.target.value);
            setPreview(null);
            setAck(false);
          }}
        >
          <option value="json">JSON（完整结构与报告）</option>
          <option value="csv">CSV（结构化条目）</option>
          <option value="txt">TXT（仅内容文本，有损）</option>
        </select>
      </Field>
      <Button disabled={busy || !task.can_export_results || !canWrite()} onClick={() => void act(false)}>
        预览合格结果导出
      </Button>
      {valid && (
        <div aria-label="结果导出预览">
          <p>
            合格结果 {valid.count} 条 · 源任务目标 {valid.requested} 条 ·{' '}
            {valid.source_ready ? '源任务已就绪' : '源任务尚未就绪'} · {fileSize(valid.bytes)}
          </p>
          {Object.entries(valid.excluded).map(([reason, total]) => (
            <p key={reason}>
              未导出 {reason}：{total} 条
            </p>
          ))}
          {!!valid.lossy_fields.length && (
            <p className={ui.warning}>
              此格式不保留：{valid.lossy_fields.map((field) => lossNames[field] ?? field).join('、')}。
            </p>
          )}
          <details>
            <summary>查看导出样例（最多 20 条）</summary>
            {valid.examples.map((row) => (
              <p key={row.index}>
                {row.index + 1}. {row.entry.tags.join(', ')}
                {row.entry.natural_language && ', ' + row.entry.natural_language}
              </p>
            ))}
          </details>
          {valid.requires_lossy_ack && (
            <label className={ui.row}>
              <input
                type="checkbox"
                disabled={busy}
                checked={ack}
                onChange={(e) => setAck(e.target.checked)}
              />
              确认接受 TXT 导出的信息损失
            </label>
          )}
          <Button
            disabled={
              busy ||
              !task.can_export_results ||
              !valid.can_export ||
              (valid.requires_lossy_ack && !ack) ||
              !canWrite()
            }
            onClick={() => void act(true)}
          >
            下载合格结果文件
          </Button>
        </div>
      )}
    </section>
  );
}
