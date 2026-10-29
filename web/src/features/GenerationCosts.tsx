import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { projectPath } from '../api/endpoints';
import { errorText } from '../api/errors';
import { queryClient, projectKey } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, Field, HoldButton, Modal, ui } from '../ui';

export type CostState = {
  source_id: number;
  source_version: string;
  status: string;
  cost_policy: string;
  cycle_id: string | null;
  acknowledged: boolean;
  confirmation_required: boolean;
  can_confirm: boolean;
  reason: string | null;
  pending: { item_id: number; estimated_anlas: number | null } | null;
};
export type CostSummary = {
  estimated_anlas: number | null;
  observed_estimated_anlas: number | null;
  unknown_estimated_anlas: number | null;
  rejected_estimated_anlas: number | null;
  estimate_complete: boolean;
  counts: Record<string, number>;
};
export default function GenerationCosts({
  pid,
  sourceId,
  backend = 'nai_launcher',
  disabled = false,
  onChanged,
}: {
  pid: string;
  sourceId: number;
  backend?: 'nai_launcher' | 'nai_direct';
  disabled?: boolean;
  onChanged: () => Promise<void>;
}) {
  const connection = useSession((s) => s.connection);
  const [policy, setPolicy] = useState('');
  const [confirmation, setConfirmation] = useState<{ kind: 'allow' | 'cycle'; state: CostState } | null>(
    null,
  );
  const [checked, setChecked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const identity = useRef({ signature: '', key: '' });
  const path = `${projectPath(pid)}/sources/${sourceId}`;
  const state = useQuery({
    queryKey: projectKey(pid, 'generation-cost-state', sourceId),
    queryFn: ({ signal }) => request<CostState>('GET', path + '/cost-state', undefined, { signal }),
    refetchInterval: connection === 'reauth' ? false : 3000,
  });
  const summary = useQuery({
    queryKey: projectKey(pid, 'generation-cost-summary', sourceId),
    queryFn: ({ signal }) =>
      request<CostSummary>(
        'GET',
        `/api/backends/${backend}/account-status?project_id=${pid}&source_id=${sourceId}`,
        undefined,
        { signal },
      ),
    refetchInterval: connection === 'reauth' ? false : 5000,
  });
  const value = state.data;
  const target = policy || value?.cost_policy || 'ask';
  const blocked = disabled || busy || !canWrite() || !value || !!state.error;
  const stale =
    !!confirmation &&
    (confirmation.state.source_version !== value?.source_version ||
      confirmation.state.cycle_id !== value?.cycle_id ||
      (confirmation.kind === 'cycle' && !value?.can_confirm));
  async function change(kind: 'policy' | 'cycle', snapshot: CostState) {
    if (blocked) return;
    setBusy(true);
    setError(null);
    setNotice('');
    const method = kind === 'policy' ? 'PATCH' : 'POST';
    const endpoint = path + (kind === 'cycle' ? '/confirm-cost' : '');
    const body =
      kind === 'policy'
        ? { cost_policy: target, confirm_allow: target === 'allow' }
        : { cycle_id: snapshot.cycle_id };
    const signature = JSON.stringify([endpoint, method, body, snapshot.source_version]);
    if (identity.current.signature !== signature) identity.current = { signature, key: crypto.randomUUID() };
    try {
      await request(method, endpoint, body, { version: snapshot.source_version, key: identity.current.key });
      setConfirmation(null);
      setChecked(false);
      setPolicy('');
      await queryClient.invalidateQueries({ queryKey: ['project', pid] });
      await onChanged();
      setNotice(
        kind === 'policy' ? '费用策略已保存，原有周期同意已失效。' : '费用确认已处理，请核对当前来源状态。',
      );
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  function open(kind: 'allow' | 'cycle') {
    if (!value) return;
    setConfirmation({ kind, state: value });
    setChecked(false);
    setError(null);
  }
  return (
    <section className={ui.section} aria-label="生成费用与估算">
      <h2>生成费用与估算</h2>
      <p>这里只展示本地估算，不能代表实际扣费。余额、订阅和体力未知；估算为 0 也不代表免费。</p>
      <ErrorBox
        message={
          error
            ? errorText(error)
            : state.error
              ? errorText(state.error)
              : summary.error
                ? errorText(summary.error)
                : undefined
        }
      />
      {notice && <p role="status">{notice}</p>}
      {value && !state.error && (
        <>
          <Field id={`cost-policy-${sourceId}`} label="运行费用策略">
            <select
              id={`cost-policy-${sourceId}`}
              value={target}
              disabled={blocked}
              onChange={(e) => {
                setPolicy(e.target.value);
                setConfirmation(null);
              }}
            >
              <option value="deny">禁止：非零估算时暂停</option>
              <option value="ask">询问：每个运行周期确认</option>
              <option value="allow">允许：直接提交非零估算</option>
            </select>
          </Field>
          <Button
            disabled={blocked || target === value.cost_policy}
            onClick={() => (target === 'allow' ? open('allow') : void change('policy', value))}
          >
            保存费用策略
          </Button>
          <p className={ui.caption}>
            策略独立于锁定的生成参数，修改后从下一次提交检查起生效；已经派发的请求不会撤回。
          </p>
          {disabled && <p className={ui.notice}>请先保存或处理生成源草稿，再修改费用策略。</p>}
          {value.acknowledged && <p>当前运行周期已获得费用同意；暂停、改策略或重启后失效。</p>}
          {value.pending && (
            <p>
              {value.pending.estimated_anlas === null
                ? '单张费用未知。确认表示允许本周期可能产生的 Anlas 费用，不是费用上限。'
                : `最近暂停时单张估算：${value.pending.estimated_anlas} Anlas。继续时会重新准备并估价。`}
            </p>
          )}
          {value.cost_policy === 'deny' && <p>禁止策略不提供费用同意操作。</p>}
          {value.confirmation_required && (
            <Button disabled={blocked || !value.can_confirm} onClick={() => open('cycle')}>
              确认本周期费用并继续
            </Button>
          )}
          {value.reason && (
            <p className={ui.notice}>
              {value.reason === 'profile:restore_pending'
                ? '设置恢复尚未结清，暂不能继续。'
                : '真实生成适配尚未开放，费用确认暂不能启动生成。'}
            </p>
          )}
        </>
      )}
      {summary.data && !summary.error && (
        <>
          <p>
            累计本地估算：
            {summary.data.estimated_anlas === null
              ? '不完整，不能给出总额'
              : `${summary.data.estimated_anlas} Anlas`}
          </p>
          <p>
            结果未知：{summary.data.counts.unknown} 次，估算{' '}
            {summary.data.unknown_estimated_anlas ?? '无法表示'} Anlas；明确拒绝：
            {summary.data.counts.rejected} 次，排除 {summary.data.rejected_estimated_anlas ?? '无法表示'}{' '}
            Anlas。
          </p>
          {!summary.data.estimate_complete && (
            <p className={ui.notice}>
              部分请求没有报价，或记录缺失、损坏、超出扫描范围。已读取估算{' '}
              {summary.data.observed_estimated_anlas ?? '无法表示'} Anlas，仅为部分记录。
            </p>
          )}
        </>
      )}
      {confirmation && (
        <Modal
          open
          description="请核对授权范围；确认不会绕过后端的版本、周期或生成适配检查。"
          title={confirmation.kind === 'allow' ? '允许非零费用' : '确认本周期费用'}
          onOpenChange={(open) => {
            if (!open && !busy) setConfirmation(null);
          }}
        >
          <p>
            {confirmation.kind === 'allow'
              ? '允许后将直接提交可能产生费用的请求，包括费用未知的直连请求，不再逐周期询问。'
              : '同意本次运行周期内可能产生的费用，包括无法报价的请求。不是只同意当前这一张，也不是费用上限。暂停或停止后同意失效。'}
          </p>
          <ErrorBox message={error ? errorText(error) : undefined} />
          <Button disabled={busy} onClick={() => setConfirmation(null)}>
            暂不确认
          </Button>
          {stale && <p role="alert">来源或费用周期已变化，请关闭后重新核对。</p>}
          <label className={ui.row}>
            <input
              type="checkbox"
              checked={checked}
              disabled={busy || stale}
              onChange={(e) => setChecked(e.target.checked)}
            />
            我理解费用范围并同意
          </label>
          <HoldButton
            disabled={blocked || stale || !checked}
            onConfirm={() =>
              void change(confirmation.kind === 'allow' ? 'policy' : 'cycle', confirmation.state)
            }
          >
            按住确认费用操作
          </HoldButton>
        </Modal>
      )}
    </section>
  );
}
