import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { request } from '../api/client';
import { AppError, errorText } from '../api/errors';
import { useDraftGuard } from '../hooks/drafts';
import { queryClient } from '../state/queryClient';
import { canWrite, useSession } from '../state/session';
import { Button, ErrorBox, Field, ui } from '../ui';

export interface ProductDefaultsValue {
  version: string;
  cost_policy: 'deny' | 'ask' | 'allow';
  llm_limits: { concurrency: number; timeout_s: number };
  wd_enabled: boolean;
  mcp: { discovery_mode: 'default' | 'configured' };
}

export default function ProductDefaults() {
  const connection = useSession((s) => s.connection);
  const query = useQuery({
    queryKey: ['product-config'],
    queryFn: ({ signal }) => request<ProductDefaultsValue>('GET', '/api/config', undefined, { signal }),
    enabled: connection !== 'reauth',
  });
  return (
    <section className={ui.section} aria-label="产品默认设置">
      <h2>产品默认设置</h2>
      <p>
        所有项目和命令行共享。新来源复制费用策略；已有来源保持原值。WD
        仅控制新任务表单的默认勾选，不会安装模型或开始执行。
      </p>
      <p>LLM 限制在每个新请求受理时固定，已受理请求保持原限制。修改不会触发模型调用。</p>
      <ErrorBox message={query.error ? errorText(query.error) : undefined} />
      {query.data?.llm_limits && <DefaultsForm value={query.data} blocked={!!query.error} />}
      {!query.data && <Button onClick={() => void query.refetch()}>重新读取产品设置</Button>}
    </section>
  );
}

function DefaultsForm({ value, blocked }: { value: ProductDefaultsValue; blocked: boolean }) {
  const [base, setBase] = useState(value),
    [cost, setCost] = useState(value.cost_policy);
  const [concurrency, setConcurrency] = useState(String(value.llm_limits.concurrency));
  const [timeout, setTimeout] = useState(String(value.llm_limits.timeout_s));
  const [wd, setWd] = useState(value.wd_enabled),
    [discovery, setDiscovery] = useState(value.mcp.discovery_mode);
  const [ack, setAck] = useState(false),
    [busy, setBusy] = useState(false),
    [unknown, setUnknown] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const inFlight = useRef(false),
    { register } = useDraftGuard();
  useSession((s) => s.connection);
  const dirty =
    cost !== base.cost_policy ||
    concurrency !== String(base.llm_limits.concurrency) ||
    timeout !== String(base.llm_limits.timeout_s) ||
    wd !== base.wd_enabled ||
    discovery !== base.mcp.discovery_mode;
  const valid =
    /^\d+$/.test(concurrency) &&
    Number(concurrency) >= 1 &&
    Number(concurrency) <= 8 &&
    /^\d+$/.test(timeout) &&
    Number(timeout) >= 5 &&
    Number(timeout) <= 600;
  const adopt = useCallback((next: ProductDefaultsValue) => {
    setBase(next);
    setCost(next.cost_policy);
    setConcurrency(String(next.llm_limits.concurrency));
    setTimeout(String(next.llm_limits.timeout_s));
    setWd(next.wd_enabled);
    setDiscovery(next.mcp.discovery_mode);
    setAck(false);
  }, []);
  useEffect(() => {
    if (!dirty && !busy && !unknown && value.version !== base.version) adopt(value);
  }, [value, base.version, dirty, busy, unknown, adopt]);
  const save = useCallback(async () => {
    if (inFlight.current || blocked || unknown || !valid || !canWrite() || (cost === 'allow' && !ack))
      return false;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await request<ProductDefaultsValue>(
        'PATCH',
        '/api/config',
        {
          cost_policy: cost,
          confirm_allow: cost === 'allow',
          wd_enabled: wd,
          llm_limits: { concurrency: Number(concurrency), timeout_s: Number(timeout) },
          mcp: { discovery_mode: discovery },
        },
        { version: base.version },
      );
      adopt(result);
      queryClient.setQueryData(['product-config'], result);
      void queryClient.invalidateQueries({ queryKey: ['generation', 'capabilities'] });
      void queryClient.invalidateQueries({ queryKey: ['llm', 'batch-capabilities'] });
      useSession.getState().notify('产品默认设置已保存');
      return true;
    } catch (e) {
      setError(e);
      if (e instanceof AppError && e.outcome === 'unknown') setUnknown(true);
      return false;
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }, [blocked, unknown, valid, cost, ack, wd, concurrency, timeout, discovery, base.version, adopt]);
  useEffect(() => {
    register({ dirty, label: '产品默认设置', save, discard: () => adopt(base) });
    return () => register(null);
  }, [register, dirty, save, adopt, base]);
  async function reload() {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      const next = await request<ProductDefaultsValue>('GET', '/api/config');
      adopt(next);
      queryClient.setQueryData(['product-config'], next);
      setUnknown(false);
    } catch (e) {
      setError(e);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }
  return (
    <>
      <ErrorBox message={error ? errorText(error) : undefined} />
      {unknown && <p role="status">保存结果未知。请重新读取已保存值核对后再修改。</p>}
      {dirty && base.version !== value.version && (
        <p role="status">设置已更新，当前草稿保留；请重新读取并核对。</p>
      )}
      <fieldset disabled={busy || unknown}>
        <legend>新任务默认值</legend>
        <div className={ui.formGrid}>
          <Field id="product-cost" label="新来源费用策略">
            <select
              id="product-cost"
              value={cost}
              onChange={(e) => {
                setCost(e.target.value as typeof cost);
                setAck(false);
              }}
            >
              <option value="deny">禁止非零或未知费用</option>
              <option value="ask">询问</option>
              <option value="allow">允许</option>
            </select>
          </Field>
          <Field id="product-concurrency" label="LLM 并发上限（1–8）">
            <input
              id="product-concurrency"
              type="number"
              min="1"
              max="8"
              value={concurrency}
              onChange={(e) => setConcurrency(e.target.value)}
            />
          </Field>
          <Field id="product-timeout" label="LLM 请求时限（5–600 秒）">
            <input
              id="product-timeout"
              type="number"
              min="5"
              max="600"
              value={timeout}
              onChange={(e) => setTimeout(e.target.value)}
            />
          </Field>
          <Field id="product-mcp" label="Launcher 发现位置">
            <select
              id="product-mcp"
              value={discovery}
              onChange={(e) => setDiscovery(e.target.value as typeof discovery)}
            >
              <option value="default">系统默认位置</option>
              <option value="configured">启动环境配置的位置</option>
            </select>
          </Field>
        </div>
        <label className={ui.row}>
          <input type="checkbox" checked={wd} onChange={(e) => setWd(e.target.checked)} />
          新建组合标注任务时默认勾选 WD
        </label>
        {cost === 'allow' && (
          <label className={ui.row}>
            <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
            我确认新来源默认允许非零或未知费用；创建来源和启动生成仍须另行操作
          </label>
        )}
        {discovery === 'configured' && (
          <p>
            需要在启动环境中设置 STYLELORA_LAUNCHER_APPDATA；缺失时检查会明确失败。NovelAI 直连不使用此设置。
          </p>
        )}
      </fieldset>
      {!valid && <p role="alert">并发和时限必须为所示范围内的整数。</p>}
      <div className={ui.actions}>
        <Button
          primary
          disabled={
            !dirty || !valid || busy || blocked || unknown || !canWrite() || (cost === 'allow' && !ack)
          }
          onClick={() => void save()}
        >
          保存产品设置
        </Button>
        <Button disabled={busy || !canWrite()} onClick={() => void reload()}>
          重新读取产品设置（放弃草稿）
        </Button>
      </div>
    </>
  );
}
