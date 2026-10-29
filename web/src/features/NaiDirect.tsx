import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { errorText, errorCodeText } from '../api/errors';
import { queryClient } from '../state/queryClient';
import { canWrite } from '../state/session';
import { Button, ErrorBox, Field, ui } from '../ui';

type State = {
  version: string;
  available: boolean;
  credentials: { has_secret: boolean; origin: string | null };
  account: {
    active: boolean;
    tier: number;
    subscription_anlas: number;
    purchased_anlas: number;
    checked_at: string;
  } | null;
};
type Operation = { status: string; error?: { code: string }; result?: State };

export default function NaiDirect() {
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [notice, setNotice] = useState('');
  const [statusUrl, setStatusUrl] = useState<string | null>(null);
  const state = useQuery({
    queryKey: ['nai-direct'],
    queryFn: ({ signal }) => request<State>('GET', '/api/nai-direct', undefined, { signal }),
  });
  const operation = useQuery({
    queryKey: ['nai-direct-operation', statusUrl],
    enabled: !!statusUrl,
    queryFn: ({ signal }) => request<Operation>('GET', statusUrl!, undefined, { signal }),
    refetchInterval: (q) =>
      ['done', 'failed', 'canceled', 'interrupted'].includes(q.state.data?.status ?? '') ? false : 1000,
  });
  const active =
    !!statusUrl && !['done', 'failed', 'canceled', 'interrupted'].includes(operation.data?.status ?? '');
  const shown = statusUrl
    ? operation.data?.status === 'done'
      ? operation.data.result
      : undefined
    : state.data;
  async function act(kind: 'save' | 'delete' | 'probe') {
    if (busy || active || !state.data || !canWrite()) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      if (kind === 'probe') {
        const result = await request<{ status_url: string }>(
          'POST',
          '/api/nai-direct/account/probe',
          {},
          { key: crypto.randomUUID() },
        );
        setStatusUrl(result.status_url);
      } else {
        const result = await request<State>(
          kind === 'save' ? 'PUT' : 'DELETE',
          '/api/nai-direct/token',
          kind === 'save' ? { value: token } : undefined,
          { version: state.data.version },
        );
        setToken('');
        setStatusUrl(null);
        queryClient.setQueryData(['nai-direct'], result);
        await queryClient.invalidateQueries({ queryKey: ['generation', 'capabilities'] });
        setNotice(kind === 'save' ? 'Token 已加密保存在本机。' : '已删除本机保存的 Token。');
      }
    } catch (failure) {
      setError(failure);
      await state.refetch();
    } finally {
      setBusy(false);
    }
  }
  const blocked = busy || active || !canWrite() || !state.data || !!state.error;
  return (
    <div className="page">
      <div className="pageHeading">
        <div>
          <div className={ui.eyebrow}>NOVELAI / DIRECT</div>
          <h1>NovelAI 直连</h1>
          <p>直接使用官方图片 API，无需运行 Launcher。</p>
        </div>
      </div>
      <section className={ui.section} aria-label="NovelAI 凭据">
        <p>
          在 NovelAI 账户设置中取得 Persistent API Token，保存到本机加密凭据库。Token 不写入项目或浏览器存储。
        </p>
        <p>
          当前状态：{state.data?.available ? '凭据可用' : '尚未配置可用凭据'}
          {state.data?.credentials.origin === 'environment' &&
            '（使用 STYLELORA_NAI_TOKEN 环境变量，优先于本机保存值）'}
        </p>
        <Field id="nai-token" label="Persistent API Token">
          <input
            id="nai-token"
            type="password"
            autoComplete="off"
            value={token}
            disabled={blocked}
            onChange={(e) => setToken(e.target.value)}
          />
        </Field>
        <div className={ui.actions}>
          <Button disabled={blocked || !token} onClick={() => void act('save')}>
            保存 Token
          </Button>
          <Button
            disabled={blocked || !state.data?.credentials.has_secret}
            onClick={() => void act('delete')}
          >
            删除保存的 Token
          </Button>
          <Button disabled={blocked || !state.data?.available} onClick={() => void act('probe')}>
            查询账户状态
          </Button>
        </div>
        <p className={ui.caption}>查询只读取订阅和余额，不生成图片。不会在打开页面时自动查询。</p>
        {active && <p role="status">正在查询账户…</p>}
        {notice && <p role="status">{notice}</p>}
        <ErrorBox
          message={
            error
              ? errorText(error)
              : state.error
                ? errorText(state.error)
                : operation.error
                  ? errorText(operation.error)
                  : operation.data?.error
                    ? errorCodeText(operation.data.error.code)
                    : undefined
          }
        />
        {shown?.account && (
          <p>
            订阅{shown.account.active ? '有效' : '未激活'}，等级 {shown.account.tier}；订阅 Anlas{' '}
            {shown.account.subscription_anlas}，购买 Anlas {shown.account.purchased_anlas}。查询时间{' '}
            {shown.account.checked_at}。
          </p>
        )}
      </section>
      <section className={ui.section}>
        <h2>生成范围与费用</h2>
        <p>
          支持 V4.5 Full 与 V5 Full high 档的单图文生图。请在生成源中使用 nai_direct 配置；已有 Launcher
          来源保持原配置。
        </p>
        <p>
          单张费用目前显示为未知，余额不代表免费资格。禁止策略会暂停，询问策略需确认当前运行周期；允许策略按授权提交，均不保证费用上限。
        </p>
        <p>
          请求超时或返回异常后会暂停，保留结果未知；不会自动重发。收到并保存完整图片回执后，可以在生成恢复面板完成本地入库。
        </p>
      </section>
    </div>
  );
}
