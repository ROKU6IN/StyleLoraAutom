import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import { request } from '../api/client';
import { projectPath } from '../api/endpoints';
import { AppError, errorCodeText, errorText } from '../api/errors';
import type { Accepted, Operation, Page } from '../api/types';
import { queryClient, projectKey } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, Field, HoldButton, Modal, ui } from '../ui';
import UnknownDisposition from './UnknownDisposition';

export type RecoveryItem = {
  source_id: number;
  item_id: number;
  seq: number;
  attempt: number;
  status: string;
  phase: string;
  reason: string | null;
  lease_phase: string | null;
  busy: boolean;
  can_reconcile: boolean;
  can_restore: boolean;
  version: string;
};
const phases: Record<string, string> = {
  untracked: '没有操作记录',
  settled: '操作已结清',
  blocked: '需要人工核查',
  needs_reconcile: '等待只读对账',
  needs_restore: '等待恢复设置',
};
const statuses: Record<string, string> = {
  pending: '待提交',
  sent: '已提交待结清',
  done: '已登记',
  failed: '已失败',
};
export default function GenerationRecovery({
  pid,
  sourceId,
  direct = false,
  disabled = false,
}: {
  pid: string;
  sourceId: number;
  direct?: boolean;
  disabled?: boolean;
}) {
  const connection = useSession((s) => s.connection);
  const [status, setStatus] = useState('');
  const [pages, setPages] = useState<(string | null)[]>([null]);
  const [lookup, setLookup] = useState(''),
    [itemId, setItemId] = useState<number | null>(null);
  const [confirmation, setConfirmation] = useState<RecoveryItem | null>(null),
    [checked, setChecked] = useState(false);
  const [submitting, setSubmitting] = useState(false),
    [error, setError] = useState<unknown>(null);
  const [accepted, setAccepted] = useState<Accepted | null>(null),
    [uncertain, setUncertain] = useState(false);
  const completed = useRef('');
  const path = `${projectPath(pid)}/sources/${sourceId}/recovery`;
  const cursor = pages[pages.length - 1];
  const page = useQuery({
    queryKey: projectKey(pid, 'generation-recovery', sourceId, status, cursor),
    queryFn: ({ signal }) =>
      request<Page<RecoveryItem> & { total: number }>(
        'GET',
        `${path}?limit=20${status ? '&status=' + status : ''}${cursor ? '&cursor=' + encodeURIComponent(cursor) : ''}`,
        undefined,
        { signal },
      ),
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  const selected = confirmation?.item_id ?? itemId;
  const detail = useQuery({
    queryKey: projectKey(pid, 'generation-recovery-item', sourceId, selected),
    queryFn: ({ signal }) => request<RecoveryItem>('GET', `${path}/${selected}`, undefined, { signal }),
    enabled: selected !== null,
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  const operation = useQuery({
    queryKey: ['operations', 'generation-recovery', accepted?.task.id],
    queryFn: ({ signal }) => request<Operation>('GET', accepted!.status_url, undefined, { signal }),
    enabled: !!accepted && connection !== 'reauth',
    refetchInterval: (query) =>
      ['pending', 'running'].includes(query.state.data?.status ?? 'pending') ? 750 : false,
  });
  const running = !!accepted && (!operation.data || ['pending', 'running'].includes(operation.data.status));
  useEffect(() => {
    if (
      !accepted ||
      !operation.data ||
      ['pending', 'running'].includes(operation.data.status) ||
      completed.current === accepted.task.id
    )
      return;
    completed.current = accepted.task.id;
    void queryClient.invalidateQueries({ queryKey: ['project', pid] });
  }, [accepted, operation.data, pid]);
  const blocked = disabled || submitting || running || uncertain || !canWrite();
  const stale =
    !!confirmation &&
    (!detail.data ||
      !!detail.error ||
      detail.data.version !== confirmation.version ||
      !detail.data.can_restore);
  async function act(row: RecoveryItem, action: 'reconcile' | 'restore') {
    if (blocked) return;
    setSubmitting(true);
    setError(null);
    setAccepted(null);
    try {
      const value = await request<Accepted>(
        'POST',
        path,
        {
          item_id: row.item_id,
          action,
          confirm_restore: action === 'restore',
        },
        { version: row.version },
      );
      setAccepted(value);
      setConfirmation(null);
      setChecked(false);
      await queryClient.invalidateQueries({ queryKey: ['project', pid] });
    } catch (failure) {
      setError(failure);
      if (failure instanceof AppError && failure.outcome === 'unknown') setUncertain(true);
    } finally {
      setSubmitting(false);
    }
  }
  function rows(values: RecoveryItem[]) {
    return (
      <div className={ui.tableWrap}>
        <table className={ui.table}>
          <caption>生成条目的本地恢复证据</caption>
          <thead>
            <tr>
              <th>条目 / 顺序</th>
              <th>尝试 / 状态</th>
              <th>恢复状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {values.map((row) => (
              <tr key={row.item_id}>
                <td>
                  #{row.item_id} / {row.seq}
                </td>
                <td>
                  {row.attempt} / {statuses[row.status] ?? row.status}
                </td>
                <td>
                  {phases[row.phase] ?? row.phase}
                  {row.busy && <p>后台正在处理</p>}
                  {row.reason && <p className={ui.caption}>{errorCodeText(row.reason)}</p>}
                  {row.lease_phase === 'conflict' && (
                    <p>存在外部设置修改；恢复仍会检查冲突，不会强行覆盖。</p>
                  )}
                </td>
                <td>
                  <div className={ui.actions}>
                    <Button
                      disabled={blocked || !!page.error || !row.can_reconcile}
                      onClick={() => void act(row, 'reconcile')}
                    >
                      核查条目 #{row.item_id}
                    </Button>
                    {!direct && (
                      <Button
                        disabled={blocked || !!page.error || !row.can_restore}
                        onClick={() => {
                          setConfirmation(row);
                          setChecked(false);
                        }}
                      >
                        恢复设置 #{row.item_id}
                      </Button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }
  return (
    <section className={ui.section} aria-label="生成恢复与对账">
      <h2>生成恢复与对账</h2>
      <p>
        {direct
          ? '直连核查只读取本地响应回执和图片，完成已收到图片的入库，不访问 NovelAI，也不会重新生成。没有完整回执的请求保持结果未知。'
          : '页面只读取本地证据。核查会读取后端状态，不会重新生成；设置恢复需要单独确认，并保留外部修改。'}
      </p>
      <p className={ui.caption}>没有已审核适配器或证据不完整时不能执行恢复。完成恢复后仍须显式继续来源。</p>
      <ErrorBox
        message={
          error
            ? errorText(error)
            : page.error
              ? errorText(page.error)
              : detail.error
                ? errorText(detail.error)
                : operation.error
                  ? errorText(operation.error)
                  : undefined
        }
      />
      {disabled && <p className={ui.notice}>请先保存或处理生成源草稿，再执行恢复操作。</p>}
      {running && <p role="status">恢复任务正在后台处理，关闭本面板不会中止已受理操作。</p>}
      {operation.data?.status === 'done' && <p role="status">核查或恢复任务已完成，请核对最新条目状态。</p>}
      {operation.data?.error && <ErrorBox message={errorCodeText(operation.data.error.code)} />}
      {uncertain && (
        <p role="alert">
          提交结果未知，已停止重发。请先在任务进度中核对已受理操作，再重新打开本面板读取最新证据。
        </p>
      )}
      <Link to="/tasks">查看任务进度与结果</Link>
      <div className={ui.formGrid}>
        <Field id={`recovery-filter-${sourceId}`} label="核查条目状态">
          <select
            id={`recovery-filter-${sourceId}`}
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setPages([null]);
              setItemId(null);
            }}
          >
            <option value="">全部条目</option>
            {Object.entries(statuses).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </Field>
        <Field id={`recovery-id-${sourceId}`} label="按条目编号查询">
          <input
            id={`recovery-id-${sourceId}`}
            inputMode="numeric"
            value={lookup}
            onChange={(e) => setLookup(e.target.value)}
          />
        </Field>
      </div>
      <div className={ui.actions}>
        <Button
          disabled={!/^[1-9][0-9]*$/.test(lookup) || !Number.isSafeInteger(Number(lookup))}
          onClick={() => {
            setItemId(Number(lookup));
            setError(null);
          }}
        >
          查询条目
        </Button>
        <Button
          onClick={() => {
            setItemId(null);
            setPages([null]);
            void queryClient.invalidateQueries({ queryKey: ['project', pid] });
          }}
        >
          刷新本地证据
        </Button>
      </div>
      {itemId !== null
        ? detail.data && !detail.error && rows([detail.data])
        : page.data &&
          !page.error && (
            <>
              <p>
                符合状态的条目共 {page.data.total} 个；当前第 {pages.length}{' '}
                页。没有操作记录的条目不代表后端已确认未生成。
              </p>
              {page.data.items.length ? rows(page.data.items) : <p>当前页没有条目。</p>}
              <div className={ui.actions}>
                <Button disabled={pages.length === 1} onClick={() => setPages(pages.slice(0, -1))}>
                  上一页
                </Button>
                <Button
                  disabled={!page.data.next_cursor}
                  onClick={() => setPages([...pages, page.data!.next_cursor])}
                >
                  下一页
                </Button>
              </div>
            </>
          )}
      {direct && (
        <UnknownDisposition
          key={`${pid}:${sourceId}`}
          pid={pid}
          sourceId={sourceId}
          disabled={disabled || running || submitting}
        />
      )}
      <Modal
        open={!!confirmation}
        onOpenChange={(open) => {
          if (!open) setConfirmation(null);
        }}
        title="确认恢复生成设置"
        description="只恢复当前证据所绑定的设置租约，不会启动生成或取得费用同意。"
      >
        {confirmation && (
          <>
            <p>
              条目 #{confirmation.item_id}，尝试 {confirmation.attempt}
              。恢复仅针对仍等于应用写入值的设置；发现外部修改会停止并保留现场。
            </p>
            {stale && <p role="alert">恢复证据已变化或不可读取，请关闭后重新核对。</p>}
            <Field id={`recovery-confirm-${sourceId}`} label="我已核对条目并同意恢复设置">
              <input
                id={`recovery-confirm-${sourceId}`}
                type="checkbox"
                checked={checked}
                onChange={(e) => setChecked(e.target.checked)}
              />
            </Field>
            <HoldButton
              disabled={blocked || stale || !checked}
              onConfirm={() => void act(confirmation, 'restore')}
            >
              按住恢复设置
            </HoldButton>
          </>
        )}
      </Modal>
    </section>
  );
}
