import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { projectPath } from '../api/endpoints';
import { AppError, errorCodeText, errorText } from '../api/errors';
import { projectKey, queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, Field, HoldButton, Modal, ui } from '../ui';

export type UnknownDispositionView = {
  source_id: number;
  source_status: string;
  unknown_count: number;
  pending_count: number;
  unknown_items: { item_id: number; seq: number; attempt: number }[];
  items_truncated: boolean;
  busy: boolean;
  reason: string | null;
  can_stop: boolean;
  decision: {
    decision_id: string;
    action: 'stop_source_preserve_unknown';
    phase: 'prepared' | 'completed';
    unknown_count: number;
    pending_count: number;
    created_at: string;
  } | null;
  upstream_outcome: string;
  version: string;
};

export default function UnknownDisposition({
  pid,
  sourceId,
  disabled = false,
}: {
  pid: string;
  sourceId: number;
  disabled?: boolean;
}) {
  const connection = useSession((s) => s.connection);
  const path = `${projectPath(pid)}/sources/${sourceId}/unknown-disposition`;
  const queryKey = projectKey(pid, 'unknown-disposition', sourceId);
  const summary = useQuery({
    queryKey,
    queryFn: ({ signal }) => request<UnknownDispositionView>('GET', path, undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  const [confirmation, setConfirmation] = useState<UnknownDispositionView | null>(null);
  const [stopChecked, setStopChecked] = useState(false);
  const [chargeChecked, setChargeChecked] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const sending = useRef(false);
  const value = summary.data;
  const blocked = disabled || submitting || uncertain || !canWrite();
  const stale =
    !confirmation || !!summary.error || !value?.can_stop || value.version !== confirmation.version;

  async function stop() {
    if (sending.current || blocked || stale || !stopChecked || !chargeChecked) return;
    sending.current = true;
    setSubmitting(true);
    setError(null);
    try {
      const result = await request<UnknownDispositionView>(
        'POST',
        path,
        { confirm_stop_source: true, acknowledge_unknown_charge: true },
        { version: confirmation!.version },
      );
      queryClient.setQueryData(queryKey, result);
      setConfirmation(null);
      void queryClient.invalidateQueries({ queryKey: ['project', pid] });
    } catch (failure) {
      setError(failure);
      if (failure instanceof AppError && failure.outcome === 'unknown') {
        setUncertain(true);
        setConfirmation(null);
      }
      void summary.refetch();
    } finally {
      sending.current = false;
      setSubmitting(false);
    }
  }

  async function refresh() {
    setConfirmation(null);
    const fresh = await summary.refetch();
    if (!fresh.error) {
      setUncertain(false);
      setError(null);
    }
  }

  return (
    <section className={ui.section} aria-label="未知请求人工处置">
      <h3>未知请求人工处置</h3>
      <p>
        可明确结束此来源并保存人工决定。已提交的未知请求与费用记录仍保留，未提交条目不会再运行。此操作不能证明未生成、未扣费或已退款，也不会重发请求。
      </p>
      <ErrorBox message={summary.error ? errorText(summary.error) : error ? errorText(error) : undefined} />
      {uncertain && (
        <p role="alert">
          保存结果未知，已停止重复提交。请先点击“重新读取处置记录”，核对当前阶段后再明确确认。
        </p>
      )}
      {value && !summary.error && (
        <>
          <p>
            来源 #{sourceId}：结果待核查 {value.unknown_count} 项，尚未提交 {value.pending_count} 项。
          </p>
          {value.unknown_items.length > 0 && (
            <p className={ui.caption}>
              待核查条目：
              {value.unknown_items
                .map((item) => `#${item.item_id}（序号 ${item.seq}，尝试 ${item.attempt}）`)
                .join('、')}
              {value.items_truncated && '；仅显示前 100 项，其余可按条目编号查询。'}
            </p>
          )}
          {value.reason && <p role="status">{errorCodeText(value.reason)}</p>}
          {value.decision && (
            <div className={ui.notice} role="status">
              <p>
                {value.decision.phase === 'completed'
                  ? '已保存人工结束决定；上游结果仍未知。'
                  : '发现未完成的本地结束记录；请重新核对并明确确认以完成记录。'}
              </p>
              <p>
                记录编号：{value.decision.decision_id}；创建时间：{value.decision.created_at}
              </p>
              <p>
                决定时结果待核查 {value.decision.unknown_count} 项，尚未提交 {value.decision.pending_count}{' '}
                项。
              </p>
            </div>
          )}
          {value.decision?.phase !== 'completed' && (
            <Button
              disabled={blocked || !value.can_stop}
              onClick={() => {
                setStopChecked(false);
                setChargeChecked(false);
                setConfirmation(value);
                setError(null);
              }}
            >
              {value.decision ? '核对并完成结束记录' : '核对并结束此来源'}
            </Button>
          )}
        </>
      )}
      <Button disabled={submitting || summary.isFetching} onClick={() => void refresh()}>
        重新读取处置记录
      </Button>
      <Modal
        open={!!confirmation}
        onOpenChange={(open) => {
          if (!open && !submitting) setConfirmation(null);
        }}
        title="确认结束未知请求所在来源"
        description="只结束本地来源并保留现场；不会确认上游执行结果或发起退款。"
      >
        {confirmation && (
          <>
            <p>
              来源 #{sourceId}：{confirmation.unknown_count} 个请求结果未知；{confirmation.pending_count}{' '}
              个未提交条目将停止运行。结束后不能继续此来源。
            </p>
            {stale && <p role="alert">处置证据已变化或不可读取，请关闭后重新核对。</p>}
            <Field id={`stop-source-${sourceId}`} label="我确认结束此来源，未提交条目将不再运行">
              <input
                id={`stop-source-${sourceId}`}
                type="checkbox"
                checked={stopChecked}
                onChange={(e) => setStopChecked(e.target.checked)}
              />
            </Field>
            <Field
              id={`unknown-charge-${sourceId}`}
              label="我理解未知请求可能已生成或扣费，结束来源不会结清或退款"
            >
              <input
                id={`unknown-charge-${sourceId}`}
                type="checkbox"
                checked={chargeChecked}
                onChange={(e) => setChargeChecked(e.target.checked)}
              />
            </Field>
            <HoldButton
              disabled={blocked || stale || !stopChecked || !chargeChecked}
              onConfirm={() => void stop()}
            >
              按住结束来源并保留未知记录
            </HoldButton>
          </>
        )}
      </Modal>
    </section>
  );
}
